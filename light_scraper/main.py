# =============================================================================
# real_estate_scraper — Light Scraper — Main
# Purpose: Incremental scraper for imot.bg. Two-pass architecture:
#          Pass 1 — index-only scrape, compare against DB, collect changes.
#          Pass 2 — rolling detail refresh for oldest 10% of active listings.
#          Outputs raw CSV files consumed by the real_estate_cleaning pipeline.
# Run:     python -m light_scraper.main
# =============================================================================

import csv
import logging
import os
from datetime import datetime, timezone

from config import (
    OUTPUT_DIR, LOG_DIR, TRANSACTION_TYPES,
    PROPERTY_TYPES,
    PRODAZHBI_PRICE_MIN, PRODAZHBI_PRICE_MAX,
    NAEMI_PRICE_MIN, NAEMI_PRICE_MAX,
)
from regions import REGIONS
from scraper.fetcher import fetch_page
from scraper.parser import parse_listings_page, is_capped
from scraper.detail_parser import parse_detail_page
from scraper.url_builder import build_listings_url
from scraper.validator import filter_valid_listings, is_valid_listing
from light_scraper.db import get_connection, fetch_active_listings, fetch_inactive_listings, fetch_pass2_listings
from light_scraper.comparator import classify_listings, compute_missing, NEW, CHANGED, UNCHANGED, REAPPEARED, MISSING
from light_scraper.progress import load_progress, save_progress, save_progress_pass2, clear_progress

from main import (
    setup_logging, get_output_path, write_header, append_listings,
    scrape_pages, scrape_price_buckets, preflight_check,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSV columns — same schema as full scraper output
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "region", "locality", "locality_type", "area",
    "property_type", "bedrooms",
    "poster_type", "agency_name",
    "price", "area_m2", "floor",
    "construction_type", "construction_status", "year_built",
    "gas", "tec",
    "features",
    "date_posted", "date_modified",
    "has_photos", "agency_phone",
    "listing_url", "source_id", "listing_tier", "transaction_type",
    "scraped_at", "status",
]


# ---------------------------------------------------------------------------
# Pass 1 — Index-only scrape
# ---------------------------------------------------------------------------

def run_pass1(
    transaction_type: str,
    output_path: str,
    active_in_db: dict,
    inactive_in_db: set,
    resume: dict | None,
) -> dict:
    """
    Scrape all index pages for all regions without fetching detail pages.
    Classifies, enriches with detail pages, and writes to CSV per region —
    so progress is saved incrementally and a crash doesn't lose everything.

    Returns:
        {
            "all_scraped_ids": set of all source_ids seen across all regions,
            "missing":         list of { source_id, listing_id } — computed at end,
        }
    """
    all_scraped_ids: set = set()

    price_min = PRODAZHBI_PRICE_MIN if transaction_type == "prodazhbi" else NAEMI_PRICE_MIN
    price_max = PRODAZHBI_PRICE_MAX if transaction_type == "prodazhbi" else NAEMI_PRICE_MAX

    catching_up = resume is not None

    for region_entry in REGIONS:
        slug = region_entry["slug"]

        if catching_up:
            if slug != resume["slug"]:
                logger.info(f"Pass 1 — skipping completed region: {slug}")
                continue
            else:
                catching_up = False

        logger.info(f"Pass 1 — scraping index pages: {slug} | {transaction_type}")

        region_capped = preflight_check(transaction_type, slug)

        if not region_capped:
            listings = _scrape_index_pages(
                transaction_type, region_entry,
                resume_page=resume["page"] if resume and slug == resume["slug"] else None,
                progress_key=slug,
                pass_number=1,
                output_path=output_path,
            )
        else:
            listings = _scrape_index_pages_cascade(
                transaction_type, region_entry,
                price_min=price_min, price_max=price_max,
                resume=resume,
                output_path=output_path,
            )

        # Classify this region's listings
        region_results = classify_listings(listings, active_in_db, inactive_in_db)

        # Track all scraped source_ids for global MISSING computation later
        all_scraped_ids.update(l["source_id"] for l in listings if l.get("source_id"))

        # Strip internal tracking fields from CHANGED listings before writing to CSV
        for listing in region_results[CHANGED]:
            listing.pop("old_price", None)
            listing.pop("listing_id", None)

        # Enrich and write to CSV immediately — crash-safe, no data held in memory
        needs_detail = (
            region_results[NEW] +
            region_results[CHANGED] +
            region_results[REAPPEARED]
        )
        if needs_detail:
            enriched = enrich_with_detail(needs_detail)
            valid = filter_valid_listings(enriched)
            append_listings(output_path, valid)
            logger.info(
                f"Pass 1 — {slug}: saved {len(valid)} listings "
                f"({len(region_results[NEW])} new | "
                f"{len(region_results[CHANGED])} changed | "
                f"{len(region_results[REAPPEARED])} reappeared | "
                f"{len(region_results[UNCHANGED])} unchanged)"
            )
        else:
            logger.info(
                f"Pass 1 — {slug}: 0 listings to save "
                f"({len(region_results[UNCHANGED])} unchanged)"
            )

    # Compute MISSING once — after all regions have been scraped
    missing = compute_missing(all_scraped_ids, active_in_db)

    return {"all_scraped_ids": all_scraped_ids, "missing": missing}


def _scrape_index_pages(
    transaction_type: str,
    region_entry: dict,
    resume_page: int | None,
    progress_key: str,
    pass_number: int,
    output_path: str,
    property_type: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
) -> list[dict]:
    """
    Scrape all index pages for one URL combination.
    Returns all listings found — no detail fetches, no DB writes.
    """
    slug = region_entry["slug"]
    start = resume_page + 1 if resume_page is not None else 1
    all_listings = []
    last_page_count = 40

    from config import MAX_PAGES, START_PAGE
    for page in range(start, start + MAX_PAGES):
        url = build_listings_url(
            transaction_type, slug, page,
            property_type=property_type,
            price_min=price_min,
            price_max=price_max,
        )
        logger.info(f"Pass {pass_number} — page {page}: {url}")

        html = fetch_page(url, page_type="listings", last_page_was_partial=(last_page_count < 40))

        if html is None:
            logger.info(f"Pass {pass_number} — no more pages for {slug}.")
            break

        listings = parse_listings_page(html, region_entry, transaction_type)

        if not listings:
            logger.info(f"Pass {pass_number} — empty page {page} for {slug}.")
            break

        last_page_count = len(listings)
        all_listings.extend(listings)

        save_progress(
            pass_number=pass_number,
            slug=slug,
            page=page,
            output_path=output_path,
            transaction_type=transaction_type,
            property_type=property_type,
            price_min=price_min,
            price_max=price_max,
            progress_key=progress_key,
        )

    return all_listings


def _scrape_index_price_buckets(
    transaction_type: str,
    region_entry: dict,
    property_type: str,
    price_min: int,
    price_max: int,
    output_path: str,
) -> list[dict]:
    """
    Recursively binary-split the price range until each bucket is under cap.
    Pre-flights each bucket before scraping — mirrors scrape_price_buckets()
    from the full scraper but returns listings instead of writing to CSV.
    """
    mid = (price_min + price_max) // 2

    if price_min >= price_max or mid == price_min:
        logger.warning(f"Price range [{price_min}-{price_max}] cannot be split further. Scraping as-is.")
        return _scrape_index_pages(
            transaction_type, region_entry,
            resume_page=None,
            progress_key=f"{region_entry['slug']}:{property_type}:{price_min}-{price_max}",
            pass_number=1,
            output_path=output_path,
            property_type=property_type,
            price_min=price_min,
            price_max=price_max,
        )

    all_listings = []
    for (lo, hi) in [(price_min, mid), (mid + 1, price_max)]:
        bucket_capped = preflight_check(
            transaction_type, region_entry["slug"],
            property_type=property_type,
            price_min=lo, price_max=hi,
        )
        if bucket_capped:
            logger.info(f"Bucket [{lo}-{hi}] pre-flight shows cap — splitting further.")
            all_listings.extend(_scrape_index_price_buckets(
                transaction_type, region_entry,
                property_type=property_type,
                price_min=lo, price_max=hi,
                output_path=output_path,
            ))
        else:
            all_listings.extend(_scrape_index_pages(
                transaction_type, region_entry,
                resume_page=None,
                progress_key=f"{region_entry['slug']}:{property_type}:{lo}-{hi}",
                pass_number=1,
                output_path=output_path,
                property_type=property_type,
                price_min=lo,
                price_max=hi,
            ))
    return all_listings


def _scrape_index_pages_cascade(
    transaction_type: str,
    region_entry: dict,
    price_min: int,
    price_max: int,
    resume: dict | None,
    output_path: str,
) -> list[dict]:
    """
    Run the full property-type + price cascade for a capped region,
    collecting index-page listings only (no detail fetches).
    """
    slug = region_entry["slug"]
    all_listings = []

    prop_cap_map = {}
    for prop_type in PROPERTY_TYPES:
        prop_cap_map[prop_type] = preflight_check(transaction_type, slug, property_type=prop_type)

    for prop_type, prop_capped in prop_cap_map.items():
        if prop_capped:
            listings = _scrape_index_price_buckets(
                transaction_type, region_entry,
                property_type=prop_type,
                price_min=price_min,
                price_max=price_max,
                output_path=output_path,
            )
            all_listings.extend(listings)
        else:
            listings = _scrape_index_pages(
                transaction_type, region_entry,
                resume_page=None,
                progress_key=f"{slug}:{prop_type}",
                pass_number=1,
                output_path=output_path,
                property_type=prop_type,
            )
            all_listings.extend(listings)

    return all_listings


# ---------------------------------------------------------------------------
# Detail fetch — shared by Pass 1 (new/changed/reappeared) and Pass 2
# ---------------------------------------------------------------------------

def enrich_with_detail(listings: list[dict], workers: int = 3) -> list[dict]:
    """
    Fetch detail pages for a list of listings and merge the results.
    Uses a thread pool to fetch multiple detail pages in parallel.
    Returns the enriched listing dicts.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_one(listing):
        detail_url = listing.get("listing_url")
        if not detail_url:
            logger.warning(f"No listing_url for source_id={listing.get('source_id')}. Skipping detail fetch.")
            return listing
        detail_html = fetch_page(detail_url, page_type="detail")
        if detail_html is None:
            logger.warning(f"Detail fetch failed for {detail_url}.")
            return listing
        listing.update(parse_detail_page(detail_html))
        return listing

    enriched = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_one, listing): listing for listing in listings}
        for future in as_completed(futures):
            enriched.append(future.result())

    return enriched


# ---------------------------------------------------------------------------
# Pass 2 — Rolling detail refresh
# ---------------------------------------------------------------------------

def run_pass2(conn, output_path: str, transaction_type: str, resume_index: int = 0):
    """
    Re-fetch detail pages for the oldest 5% of active listings
    not checked in the last 30 days.
    Catches changes that price alone cannot signal.
    Appends refreshed listings to the output CSV incrementally.
    Uses 3 parallel workers for detail fetching.
    """
    listings_to_refresh = fetch_pass2_listings(conn, transaction_type)
    total = len(listings_to_refresh)
    logger.info(f"Pass 2 — refreshing {total:,} listings.")

    if resume_index > 0:
        logger.info(f"Pass 2 — resuming from index {resume_index}.")
        listings_to_refresh = listings_to_refresh[resume_index:]

    rejected_count = 0
    rejected_fields = {}
    early_warning_sent = False

    BATCH_SIZE = 3
    all_listings = list(listings_to_refresh)
    i = resume_index

    for batch_start in range(0, len(all_listings), BATCH_SIZE):
        batch = all_listings[batch_start:batch_start + BATCH_SIZE]
        enriched_batch = enrich_with_detail(batch)

        for listing in enriched_batch:
            i += 1
            detail_url = listing.get("listing_url")
            if not listing.get("listing_url"):
                logger.warning(f"No listing_url for source_id={listing.get('source_id')}. Skipping.")
                continue
            if is_valid_listing(listing):
                append_listings(output_path, [listing])
            else:
                rejected_count += 1
                for field in ["source_id", "listing_url", "property_type", "locality"]:
                    if not listing.get(field):
                        rejected_fields[field] = rejected_fields.get(field, 0) + 1
                        break

            if i == resume_index + 100 and not early_warning_sent:
                rate = rejected_count / 100 * 100
                if rate >= 20:
                    early_warning_sent = True
                    logger.warning(
                        f"Pass 2 — WARNING: high rejection rate {rate:.1f}% "
                        f"({rejected_count}/100 rejected in first 100)"
                    )

            if i % 100 == 0:
                logger.info(f"Pass 2 — {i}/{total} saved")
                save_progress_pass2(i, output_path, transaction_type)

    top_field = max(rejected_fields, key=rejected_fields.get) if rejected_fields else "none"
    saved_count = (i - resume_index) - rejected_count
    logger.info(
        f"Pass 2 — summary: {total:,} processed | {saved_count:,} saved | "
        f"{rejected_count:,} rejected | top field: {top_field}"
    )
    logger.info(f"Pass 2 — complete. {total:,} listings refreshed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    run_start = datetime.now(timezone.utc)
    logger.info(f"========== Light scraper run started at {run_start.isoformat()} ==========")

    progress = load_progress()
    conn = get_connection()

    try:
        for transaction_type in TRANSACTION_TYPES:

            if progress and transaction_type != progress.get("transaction_type", transaction_type):
                logger.info(f"Skipping completed transaction type: {transaction_type}")
                continue

            logger.info(f"===== Transaction type: {transaction_type} =====")

            output_path = get_output_path(transaction_type)
            if progress and progress.get("output_path") and os.path.exists(progress["output_path"]):
                output_path = progress["output_path"]
                logger.info(f"Resuming existing output file: {output_path}")

            if not os.path.exists(output_path):
                write_header(output_path)
            logger.info(f"Output file: {output_path}")

            # Load DB state once per transaction type — filtered to avoid
            # cross-contamination between prodazhbi and naemi classifications
            active_in_db = fetch_active_listings(conn, transaction_type)
            inactive_in_db = fetch_inactive_listings(conn, transaction_type)

            # --- Pass 1 ---
            resuming_pass2 = (
                progress and
                progress.get("pass_number") == 2 and
                progress.get("transaction_type") == transaction_type
            )
            if resuming_pass2:
                logger.info("Resuming from Pass 2 — skipping Pass 1 entirely.")
                pass1_results = {"all_scraped_ids": set(), "missing": []}
            else:
                resume = (
                    progress
                    if progress and
                    progress.get("pass_number") == 1 and
                    progress.get("transaction_type") == transaction_type
                    else None
                )
                pass1_results = run_pass1(
                    transaction_type, output_path,
                    active_in_db, inactive_in_db,
                    resume=resume,
                )

            # Log missing listings — cleaning pipeline handles marking inactive
            logger.info(
                f"Pass 1 — {len(pass1_results['missing']):,} listings no longer visible "
                f"(will be marked inactive by cleaning pipeline)."
            )

            # --- Pass 2 ---
            resume_pass2 = (
                progress
                if progress and
                progress.get("pass_number") == 2 and
                progress.get("transaction_type") == transaction_type
                else None
            )
            resume_index = resume_pass2.get("pass2_index", 0) if resume_pass2 else 0
            logger.info(f"===== Pass 2 — rolling detail refresh =====")
            run_pass2(conn, output_path, transaction_type, resume_index=resume_index)

            # Clear progress after each transaction type completes
            # so the next transaction type always starts fresh
            progress = None

        run_end = datetime.now(timezone.utc)
        elapsed = run_end - run_start
        logger.info(f"========== Light scraper run finished. Total time: {elapsed} ==========")

        clear_progress()

    finally:
        conn.close()


if __name__ == "__main__":
    main()