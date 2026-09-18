# =============================================================================
# real_estate_scraper — Light Scraper — Main
# Purpose: Incremental scraper for imot.bg. Two-pass architecture:
#          Pass 1 — index-only scrape, compare against DB, collect changes.
#          Pass 2 — rolling detail refresh for oldest 5% of eligible listings.
#          Outputs raw CSV files consumed by the real_estate_cleaning pipeline.
# Run:     python -m light_scraper.main
# =============================================================================

import argparse
import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from config import (
    OUTPUT_DIR, TRANSACTION_TYPES,
    PROPERTY_TYPES,
    PRODAZHBI_PRICE_MIN, PRODAZHBI_PRICE_MAX,
    NAEMI_PRICE_MIN, NAEMI_PRICE_MAX,
)
from regions import REGIONS
from scraper.fetcher import fetch_page
from scraper.parser import parse_listings_page
from scraper.detail_parser import parse_detail_page
from scraper.url_builder import build_listings_url
from scraper.validator import filter_valid_listings, is_valid_listing
from light_scraper.db import get_connection, fetch_active_listings, fetch_inactive_listings, fetch_pass2_listings
from light_scraper.comparator import classify_listings, compute_missing, NEW, CHANGED, UNCHANGED, REAPPEARED, MISSING
from light_scraper.progress import load_progress, save_progress, save_progress_pass2, clear_progress
from light_scraper.run_state import (
    ActionStore,
    IndexListingStore,
    ListingRowStore,
    Pass2SelectionStore,
    SeenIdStore,
    can_compute_missing,
    clear_unit_failure,
    create_run,
    finish_run,
    finish_pass1,
    finish_transaction,
    load_manifest,
    mark_region_complete,
    mark_unit_failed,
    save_manifest,
)

from main import (
    setup_logging, write_header, append_listings, preflight_check,
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


def parse_args():
    parser = argparse.ArgumentParser(description="Run the incremental scraper.")
    parser.add_argument(
        "--only",
        choices=TRANSACTION_TYPES,
        help="Scrape only one transaction type and emit an empty completed package for the other.",
    )
    return parser.parse_args()


def initialize_empty_transaction(run_dir, manifest, transaction_type: str) -> None:
    """Create a valid no-op package for a transaction excluded from this run."""
    ListingRowStore(run_dir, transaction_type).export_csv(CSV_COLUMNS)
    ActionStore(run_dir, transaction_type).export_csv()

    seen_path = Path(run_dir) / f"{transaction_type}_seen_ids.csv"
    with seen_path.open("w", encoding="utf-8-sig", newline="") as file:
        csv.DictWriter(
            file,
            fieldnames=("source_id", "first_seen_at", "region_slug"),
        ).writeheader()
        file.flush()
        os.fsync(file.fileno())

    Pass2SelectionStore(run_dir, transaction_type).create([])
    state = manifest["transactions"][transaction_type]
    state["expected_regions"] = []
    state["completed_regions"] = []
    state["pass1_status"] = "complete"
    state["pass2_status"] = "complete"
    state["allow_missing_updates"] = True
    state["status"] = "complete"
    save_manifest(run_dir, manifest)


# ---------------------------------------------------------------------------
# Pass 1 — Index-only scrape
# ---------------------------------------------------------------------------

def run_pass1(
    transaction_type: str,
    output_path: str,
    active_in_db: dict,
    inactive_in_db: dict,
    resume: dict | None,
    run_dir=None,
    manifest: dict | None = None,
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
    seen_store = SeenIdStore(run_dir, transaction_type) if run_dir else None
    index_store = IndexListingStore(run_dir, transaction_type) if run_dir else None
    row_store = ListingRowStore(run_dir, transaction_type) if run_dir else None
    action_store = ActionStore(run_dir, transaction_type) if run_dir else None
    all_scraped_ids: set = set(seen_store.ids) if seen_store else set()

    price_min = PRODAZHBI_PRICE_MIN if transaction_type == "prodazhbi" else NAEMI_PRICE_MIN
    price_max = PRODAZHBI_PRICE_MAX if transaction_type == "prodazhbi" else NAEMI_PRICE_MAX

    catching_up = resume is not None

    for region_entry in REGIONS:
        slug = region_entry["slug"]

        completed_regions = (
            manifest["transactions"][transaction_type]["completed_regions"]
            if manifest is not None
            else []
        )
        if slug in completed_regions:
            logger.info(f"Pass 1 — skipping manifest-complete region: {slug}")
            if catching_up and resume and slug == resume.get("slug"):
                catching_up = False
            continue

        if catching_up:
            if slug != resume["slug"]:
                logger.info(f"Pass 1 — skipping completed region: {slug}")
                continue
            else:
                catching_up = False

        region_resume = _resume_for_region(resume, slug)

        logger.info(f"Pass 1 — scraping index pages: {slug} | {transaction_type}")

        region_capped, preflight_html = _preflight_with_html(transaction_type, slug)

        if not region_capped:
            listings = _scrape_index_pages(
                transaction_type, region_entry,
                resume_page=region_resume["page"] if region_resume else None,
                progress_key=slug,
                pass_number=1,
                output_path=output_path,
                seen_store=seen_store,
                index_store=index_store,
                manifest=manifest,
                run_dir=run_dir,
                initial_html=preflight_html,
            )
        else:
            listings = _scrape_index_pages_cascade(
                transaction_type, region_entry,
                price_min=price_min, price_max=price_max,
                resume=region_resume,
                output_path=output_path,
                seen_store=seen_store,
                index_store=index_store,
                manifest=manifest,
                run_dir=run_dir,
            )

        if manifest is not None:
            region_failed = any(
                failure["unit"] == slug
                or failure["unit"].startswith(f"{slug}:")
                or failure["unit"].startswith(f"{slug}/")
                for failure in manifest["transactions"][transaction_type]["failed_units"]
            )
            if region_failed:
                raise RuntimeError(
                    f"Pass 1 stopped after an incomplete scan unit in {slug}. "
                    "Resume the same run after the fetch problem is resolved."
                )

        if index_store:
            listings = index_store.rows_for_region(slug)

        # Classify this region's listings
        region_results = classify_listings(listings, active_in_db, inactive_in_db)

        # Track all scraped source_ids for global MISSING computation later
        all_scraped_ids.update(l["source_id"] for l in listings if l.get("source_id"))

        pending_actions = []
        for action in (NEW, CHANGED, REAPPEARED):
            for listing in region_results[action]:
                pending_actions.append({
                    "source_id": listing.get("source_id"),
                    "listing_id": listing.get("listing_id"),
                    "action": action,
                    "old_price": listing.get("old_price"),
                    "new_price": listing.get("price"),
                    "observed_at": listing.get("scraped_at") or datetime.now(timezone.utc).isoformat(),
                })

        # Strip action-only tracking fields before writing raw rows.
        for action in (CHANGED, REAPPEARED):
            for listing in region_results[action]:
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
            if row_store and action_store:
                row_store.upsert(valid)
                valid_ids = {listing["source_id"] for listing in valid}
                action_store.upsert([
                    action for action in pending_actions
                    if action["source_id"] in valid_ids
                ])
            else:
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

        if manifest is not None:
            counts = manifest["transactions"][transaction_type]["counts"]
            for action in (NEW, CHANGED, UNCHANGED, REAPPEARED):
                counts[action] += len(region_results[action])
            counts["output_rows"] = len(row_store.rows) if row_store else (
                counts["output_rows"] + (len(valid) if needs_detail else 0)
            )
            mark_region_complete(manifest, transaction_type, slug)
            save_manifest(run_dir, manifest)

    # Compute MISSING once — after all regions have been scraped
    if seen_store:
        all_scraped_ids = set(seen_store.ids)

    if manifest is not None:
        pass1_complete = finish_pass1(manifest, transaction_type)
        save_manifest(run_dir, manifest)
        missing = compute_missing(all_scraped_ids, active_in_db) if pass1_complete else []
        if pass1_complete:
            manifest["transactions"][transaction_type]["counts"][MISSING] = len(missing)
            if row_store and action_store:
                observed_at = datetime.now(timezone.utc).isoformat()
                action_store.upsert([
                    {
                        "source_id": listing["source_id"],
                        "listing_id": listing.get("listing_id"),
                        "action": MISSING,
                        "old_price": None,
                        "new_price": None,
                        "observed_at": observed_at,
                    }
                    for listing in missing
                ])
                row_store.export_csv(CSV_COLUMNS)
                action_store.export_csv()
            save_manifest(run_dir, manifest)
    else:
        missing = compute_missing(all_scraped_ids, active_in_db)

    return {"all_scraped_ids": all_scraped_ids, "missing": missing}


def _resume_for_region(resume: dict | None, region_slug: str) -> dict | None:
    """Limit a Pass 1 checkpoint to the region that created it."""
    if resume and resume.get("slug") == region_slug:
        return resume
    return None


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
    seen_store=None,
    index_store=None,
    manifest: dict | None = None,
    run_dir=None,
    initial_html: str | None = None,
) -> list[dict]:
    """
    Scrape all index pages for one URL combination.
    Returns all listings found — no detail fetches, no DB writes.
    """
    slug = region_entry["slug"]
    start = resume_page + 1 if resume_page is not None else 1
    all_listings = []
    last_page_count = 40
    ended_naturally = False

    from config import MAX_PAGES
    for page in range(start, start + MAX_PAGES):
        url = build_listings_url(
            transaction_type, slug, page,
            property_type=property_type,
            price_min=price_min,
            price_max=price_max,
        )
        logger.info(f"Pass {pass_number} — page {page}: {url}")
        scan_unit = progress_key or slug
        page_unit = f"{scan_unit}/page-{page}"
        if manifest is not None:
            manifest["transactions"][transaction_type]["pages_attempted"] += 1

        if page == 1 and start == 1 and initial_html is not None:
            html = initial_html
            initial_html = None
            logger.info(f"Pass {pass_number} — reusing pre-flight HTML for page 1.")
        else:
            html = fetch_page(
                url,
                page_type="listings",
                last_page_was_partial=(last_page_count < 40),
                reuse_connection=True,
            )

        if html is None:
            logger.warning(f"Pass {pass_number} — fetch failed for {page_unit}.")
            if manifest is not None:
                state = manifest["transactions"][transaction_type]
                state["fetch_failures"] += 1
                mark_unit_failed(manifest, transaction_type, page_unit, "fetch_failed")
                save_manifest(run_dir, manifest)
            if run_dir:
                save_progress(
                    pass_number=pass_number,
                    slug=slug,
                    page=page - 1,
                    output_path=output_path,
                    transaction_type=transaction_type,
                    property_type=property_type,
                    price_min=price_min,
                    price_max=price_max,
                    progress_key=progress_key,
                    run_id=Path(run_dir).name,
                    run_dir=str(run_dir),
                )
            break

        listings = parse_listings_page(html, region_entry, transaction_type)

        if not listings:
            logger.info(f"Pass {pass_number} — empty page {page} for {slug}.")
            if manifest is not None:
                clear_unit_failure(manifest, transaction_type, page_unit)
                save_manifest(run_dir, manifest)
            ended_naturally = True
            break

        last_page_count = len(listings)
        all_listings.extend(listings)

        # Durable page order: IDs and index rows must reach disk before the
        # checkpoint advances. A failed write raises and leaves this page to be
        # retried on the next run.
        if seen_store:
            seen_store.append(listings, slug)
        if index_store:
            index_store.append(listings, slug, page_unit)
        if manifest is not None:
            clear_unit_failure(manifest, transaction_type, page_unit)
            manifest["transactions"][transaction_type]["pages_completed"] += 1
            save_manifest(run_dir, manifest)

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
            run_id=Path(run_dir).name,
            run_dir=str(run_dir),
        )

    if not ended_naturally and manifest is not None:
        state = manifest["transactions"][transaction_type]
        already_failed = any(item["unit"] == page_unit for item in state["failed_units"])
        if not already_failed and state["pages_completed"] > 0:
            unit = progress_key or slug
            logger.warning(f"Pass {pass_number} — maximum page limit reached for {unit}.")
            mark_unit_failed(manifest, transaction_type, unit, "max_pages_reached")
            save_manifest(run_dir, manifest)

    return all_listings


def _scrape_index_price_buckets(
    transaction_type: str,
    region_entry: dict,
    property_type: str,
    price_min: int,
    price_max: int,
    output_path: str,
    seen_store=None,
    index_store=None,
    manifest: dict | None = None,
    run_dir=None,
    resume_key: str | None = None,
    resume_page: int | None = None,
) -> list[dict]:
    """
    Recursively binary-split the price range until each bucket is under cap.
    Pre-flights each bucket before scraping — mirrors scrape_price_buckets()
    from the full scraper but returns listings instead of writing to CSV.
    """
    mid = (price_min + price_max) // 2
    resume_range = _price_range_from_progress_key(resume_key)

    if price_min >= price_max or mid == price_min:
        logger.warning(f"Price range [{price_min}-{price_max}] cannot be split further. Scraping as-is.")
        key = f"{region_entry['slug']}:{property_type}:{price_min}-{price_max}"
        return _scrape_index_pages(
            transaction_type, region_entry,
            resume_page=resume_page if resume_key == key else None,
            progress_key=key,
            pass_number=1,
            output_path=output_path,
            property_type=property_type,
            price_min=price_min,
            price_max=price_max,
            seen_store=seen_store,
            index_store=index_store,
            manifest=manifest,
            run_dir=run_dir,
        )

    all_listings = []
    for (lo, hi) in [(price_min, mid), (mid + 1, price_max)]:
        key = f"{region_entry['slug']}:{property_type}:{lo}-{hi}"
        if resume_range and hi < resume_range[0]:
            logger.info(f"Pass 1 — skipping completed price bucket: {key}")
            continue

        contains_resume_range = bool(
            resume_range
            and lo <= resume_range[0]
            and resume_range[1] <= hi
        )
        bucket_resume_page = resume_page if resume_key == key else None

        bucket_capped, preflight_html = _preflight_with_html(
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
                seen_store=seen_store,
                index_store=index_store,
                manifest=manifest,
                run_dir=run_dir,
                resume_key=resume_key if contains_resume_range else None,
                resume_page=resume_page if contains_resume_range else None,
            ))
        else:
            all_listings.extend(_scrape_index_pages(
                transaction_type, region_entry,
                resume_page=bucket_resume_page,
                progress_key=key,
                pass_number=1,
                output_path=output_path,
                property_type=property_type,
                price_min=lo,
                price_max=hi,
                seen_store=seen_store,
                index_store=index_store,
                manifest=manifest,
                run_dir=run_dir,
                initial_html=preflight_html,
            ))
    return all_listings


def _price_range_from_progress_key(progress_key: str | None) -> tuple[int, int] | None:
    if not progress_key:
        return None
    try:
        range_part = progress_key.rsplit(":", 1)[1]
        lower, upper = range_part.split("-", 1)
        return int(lower), int(upper)
    except (IndexError, ValueError):
        logger.warning(f"Invalid light-scraper price progress key: {progress_key}")
        return None


def _scrape_index_pages_cascade(
    transaction_type: str,
    region_entry: dict,
    price_min: int,
    price_max: int,
    resume: dict | None,
    output_path: str,
    seen_store=None,
    index_store=None,
    manifest: dict | None = None,
    run_dir=None,
) -> list[dict]:
    """
    Run the full property-type + price cascade for a capped region,
    collecting index-page listings only (no detail fetches).
    """
    slug = region_entry["slug"]
    all_listings = []

    prop_cap_map = {}
    prop_html_map = {}
    for prop_type in PROPERTY_TYPES:
        if (
            resume
            and prop_type == resume.get("property_type")
            and resume.get("progress_key")
        ):
            prop_cap_map[prop_type] = True
            prop_html_map[prop_type] = None
        else:
            prop_cap_map[prop_type], prop_html_map[prop_type] = _preflight_with_html(
                transaction_type,
                slug,
                property_type=prop_type,
            )

    resume_property_type = resume.get("property_type") if resume else None
    resume_price_key = resume.get("progress_key") if resume else None
    resume_page = resume.get("page") if resume else None
    catching_up_property = resume_property_type is not None

    for prop_type, prop_capped in prop_cap_map.items():
        if catching_up_property:
            if prop_type != resume_property_type:
                logger.info(f"Pass 1 — skipping completed property type: {prop_type}")
                continue
            catching_up_property = False

        is_resume_property = prop_type == resume_property_type
        if prop_capped:
            listings = _scrape_index_price_buckets(
                transaction_type, region_entry,
                property_type=prop_type,
                price_min=price_min,
                price_max=price_max,
                output_path=output_path,
                seen_store=seen_store,
                index_store=index_store,
                manifest=manifest,
                run_dir=run_dir,
                resume_key=resume_price_key if is_resume_property else None,
                resume_page=resume_page if is_resume_property else None,
            )
            all_listings.extend(listings)
        else:
            listings = _scrape_index_pages(
                transaction_type, region_entry,
                resume_page=resume_page if is_resume_property and not resume_price_key else None,
                progress_key=f"{slug}:{prop_type}",
                pass_number=1,
                output_path=output_path,
                property_type=prop_type,
                seen_store=seen_store,
                index_store=index_store,
                manifest=manifest,
                run_dir=run_dir,
                initial_html=prop_html_map[prop_type],
            )
            all_listings.extend(listings)

    return all_listings


def _preflight_with_html(*args, **kwargs) -> tuple[bool, str | None]:
    """Run a preflight and retain page 1 for the subsequent index scan."""
    result = preflight_check(
        *args,
        **kwargs,
        return_html=True,
        reuse_connection=True,
    )
    # Keep compatibility with simple test doubles and older callers that
    # return only a boolean.
    if isinstance(result, tuple):
        return result
    return bool(result), None


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
            listing["_detail_fetch_ok"] = False
            return listing
        detail_html = fetch_page(detail_url, page_type="detail", reuse_connection=True)
        if detail_html is None:
            logger.warning(f"Detail fetch failed for {detail_url}.")
            listing["_detail_fetch_ok"] = False
            return listing
        listing.update(parse_detail_page(detail_html))
        listing["_detail_fetch_ok"] = True
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


def _repair_pass2_action_gaps(row_store, action_store, listings_to_refresh) -> int:
    """Restore refresh actions written incompletely before an interruption.

    A process can stop after the durable listing row append but before the
    matching action append. The Pass 2 checkpoint then still points past that
    listing on a later resume. Recreate only actions that belong to the fixed
    Pass 2 selection. Pass 1 rows are deliberately left untouched because
    their new/changed classification cannot be inferred here.
    """
    selected_ids = {
        str(listing.get("source_id") or "").strip()
        for listing in listings_to_refresh
    }
    missing_ids = sorted(
        selected_ids.intersection(row_store.rows).difference(action_store.actions)
    )
    if not missing_ids:
        return 0

    recovered = []
    for source_id in missing_ids:
        listing = row_store.rows[source_id]["listing"]
        recovered.append({
            "source_id": source_id,
            "listing_id": listing.get("listing_id"),
            "action": "refreshed",
            "old_price": listing.get("price"),
            "new_price": listing.get("price"),
            "observed_at": (
                listing.get("scraped_at")
                or datetime.now(timezone.utc).isoformat()
            ),
        })

    action_store.upsert(recovered)
    logger.warning(
        "Pass 2 — restored %s missing refresh actions from durable rows.",
        len(recovered),
    )
    return len(recovered)


def run_pass2(
    conn,
    output_path: str,
    transaction_type: str,
    resume_index: int = 0,
    run_dir=None,
    manifest: dict | None = None,
):
    """
    Re-fetch detail pages for the oldest 5% of active listings
    not checked in the last 30 days.
    Catches changes that price alone cannot signal.
    Appends refreshed listings to the output CSV incrementally.
    Uses 3 parallel workers for detail fetching.
    """
    row_store = ListingRowStore(run_dir, transaction_type) if run_dir else None
    action_store = ActionStore(run_dir, transaction_type) if run_dir else None
    selection_store = Pass2SelectionStore(run_dir, transaction_type) if run_dir else None

    if selection_store and selection_store.exists():
        listings_to_refresh = selection_store.load()
        logger.info("Pass 2 — restored fixed selection from run files.")
    else:
        exclude_ids = set(row_store.rows) if row_store else set()
        eligible_ids = (
            SeenIdStore(run_dir, transaction_type).ids
            if run_dir
            else None
        )
        listings_to_refresh = fetch_pass2_listings(
            conn,
            transaction_type,
            exclude_ids=exclude_ids,
            eligible_ids=eligible_ids,
        )
        if selection_store:
            listings_to_refresh = selection_store.create(listings_to_refresh)

    if row_store and action_store:
        _repair_pass2_action_gaps(
            row_store,
            action_store,
            listings_to_refresh,
        )

    total = len(listings_to_refresh)
    logger.info(f"Pass 2 — refreshing {total:,} listings.")
    if manifest is not None:
        manifest["transactions"][transaction_type]["pass2"]["selected"] = total
        save_manifest(run_dir, manifest)

    if resume_index > 0:
        logger.info(f"Pass 2 — resuming from index {resume_index}.")
        listings_to_refresh = listings_to_refresh[resume_index:]

    rejected_count = (
        manifest["transactions"][transaction_type]["pass2"].get("rejected", 0)
        if manifest is not None else 0
    )
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
            if not listing.get("listing_url"):
                logger.warning(f"No listing_url for source_id={listing.get('source_id')}. Skipping.")
                rejected_count += 1
                continue
            if listing.get("_detail_fetch_ok") is False:
                logger.warning(
                    "Pass 2 detail refresh failed for source_id=%s. "
                    "The listing will remain due for a future refresh.",
                    listing.get("source_id"),
                )
                rejected_count += 1
                continue
            if is_valid_listing(listing):
                if row_store and action_store:
                    row_store.upsert([listing])
                    action_store.upsert([{
                        "source_id": listing.get("source_id"),
                        "listing_id": listing.get("listing_id"),
                        "action": "refreshed",
                        "old_price": listing.get("price"),
                        "new_price": listing.get("price"),
                        "observed_at": listing.get("scraped_at") or datetime.now(timezone.utc).isoformat(),
                    }])
                else:
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

        # Rows and actions are durable before the Pass 2 checkpoint advances.
        if run_dir:
            if manifest is not None:
                pass2_state = manifest["transactions"][transaction_type]["pass2"]
                pass2_state.update({
                    "selected": total,
                    "completed": i,
                    "saved": i - rejected_count,
                    "rejected": rejected_count,
                })
                save_manifest(run_dir, manifest)
            save_progress_pass2(
                i,
                output_path,
                transaction_type,
                Path(run_dir).name,
                str(run_dir),
            )

    top_field = max(rejected_fields, key=rejected_fields.get) if rejected_fields else "none"
    saved_count = i - rejected_count
    logger.info(
        f"Pass 2 — summary: {total:,} processed | {saved_count:,} saved | "
        f"{rejected_count:,} rejected | top field: {top_field}"
    )
    logger.info(f"Pass 2 — complete. {total:,} listings refreshed.")
    if row_store and action_store:
        row_store.export_csv(CSV_COLUMNS)
        action_store.export_csv()
    if manifest is not None:
        state = manifest["transactions"][transaction_type]
        state["pass2_status"] = "complete"
        state["pass2"].update({
            "selected": total,
            "completed": i,
            "saved": saved_count,
            "rejected": rejected_count,
        })
        save_manifest(run_dir, manifest)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    setup_logging()

    run_start = datetime.now(timezone.utc)
    logger.info(f"========== Light scraper run started at {run_start.isoformat()} ==========")

    progress = load_progress()
    selected_transactions = {args.only} if args.only else set(TRANSACTION_TYPES)
    if progress:
        run_dir = Path(progress["run_dir"])
        if run_dir.name != progress["run_id"]:
            raise ValueError("Progress run_id does not match its run directory")
        manifest = load_manifest(run_dir)
        if manifest["run_id"] != progress["run_id"]:
            raise ValueError("Progress and manifest refer to different runs")
        if args.only and progress.get("transaction_type") != args.only:
            raise ValueError("--only does not match the transaction in saved progress")
    else:
        run_dir, manifest = create_run(Path(OUTPUT_DIR) / "runs")
        for transaction_type in TRANSACTION_TYPES:
            if transaction_type not in selected_transactions:
                initialize_empty_transaction(run_dir, manifest, transaction_type)

    conn = get_connection()

    try:
        for transaction_type in TRANSACTION_TYPES:

            if transaction_type not in selected_transactions:
                logger.info(f"Skipping excluded transaction type: {transaction_type}")
                continue

            if manifest["transactions"][transaction_type]["status"] == "complete":
                logger.info(f"Skipping manifest-complete transaction type: {transaction_type}")
                continue

            if progress and transaction_type != progress.get("transaction_type", transaction_type):
                logger.info(f"Skipping completed transaction type: {transaction_type}")
                continue

            logger.info(f"===== Transaction type: {transaction_type} =====")

            output_path = str(run_dir / f"{transaction_type}_rows.csv")
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
                    run_dir=run_dir,
                    manifest=manifest,
                )

            if not can_compute_missing(manifest, transaction_type):
                raise RuntimeError(
                    f"Pass 1 is incomplete for {transaction_type}. "
                    "Pass 2 and missing-listing updates are blocked."
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
            logger.info("===== Pass 2 — rolling detail refresh =====")
            run_pass2(
                conn,
                output_path,
                transaction_type,
                resume_index=resume_index,
                run_dir=run_dir,
                manifest=manifest,
            )
            finish_transaction(manifest, transaction_type)
            save_manifest(run_dir, manifest)

            # Clear progress after each transaction type completes
            # so the next transaction type always starts fresh
            progress = None

        run_end = datetime.now(timezone.utc)
        elapsed = run_end - run_start
        logger.info(f"========== Light scraper run finished. Total time: {elapsed} ==========")

        finish_run(manifest)
        save_manifest(run_dir, manifest)
        clear_progress()

    finally:
        conn.close()


if __name__ == "__main__":
    main()
