# main.py

import csv
import logging
import os
from datetime import datetime, timezone

from config import OUTPUT_DIR, LOG_DIR, MAX_PAGES, START_PAGE, TRANSACTION_TYPES, PROPERTY_TYPES, PRODAZHBI_PRICE_MIN, PRODAZHBI_PRICE_MAX, NAEMI_PRICE_MIN, NAEMI_PRICE_MAX
from regions import REGIONS
from scraper.fetcher import fetch_page
from scraper.parser import parse_listings_page, is_capped
from scraper.detail_parser import parse_detail_page
from scraper.url_builder import build_listings_url
from scraper.validator import filter_valid_listings
from scraper.progress import load_progress, save_progress, clear_progress


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# No handlers are attached until setup_logging() runs in main().
# Do not call logger at module level.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging():
    """Configure root logger with file and console handlers."""

    os.makedirs(LOG_DIR, exist_ok=True)

    log_filename = datetime.now(timezone.utc).strftime("%Y_%m_%d_%H%M%S") + ".log"
    log_path = os.path.join(LOG_DIR, log_filename)

    log_format = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ]
    )

    logging.info(f"Logging initialised. Log file: {log_path}")


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
# Listings are written incrementally — one page at a time — rather than buffered
# in memory. If the scraper is interrupted, everything written so far is preserved.
# The header is written once before the region loop; subsequent pages append rows.

# Full ordered column list — must match the keys produced by the parsers.
# bedrooms is always None at this stage (not available on the listings or detail
# page) and is populated downstream in real_estate_cleaning.
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


def get_output_path(transaction_type: str) -> str:
    """Build the output CSV path for a transaction type, keyed to today's UTC date.

    Example: data/prodazhbi_24_03_2026.csv
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%d_%m_%Y")
    filename = f"{transaction_type}_{date_str}.csv"
    return os.path.join(OUTPUT_DIR, filename)


def write_header(filepath: str):
    """Write the CSV header row. Called once per transaction type, before the region loop.

    Uses utf-8-sig (UTF-8 with BOM) so Excel opens Cyrillic content correctly.
    """
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()


def append_listings(filepath: str, listings: list[dict]):
    """Append a batch of listings to an existing CSV file."""
    if not listings:
        return
    with open(filepath, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writerows(listings)


# ---------------------------------------------------------------------------
# Core scraping logic
# ---------------------------------------------------------------------------

def scrape_pages(
    transaction_type: str,
    region_entry: dict,
    output_path: str,
    property_type: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    resume_page: int | None = None,
    progress_key: str | None = None,
) -> bool:
    """
    Scrape all pages for one URL combination (region + optional property_type +
    optional price range). Appends valid listings to the CSV after each page.

    Returns:
        True  — cap was hit (last page had listings, suggesting more exist)
        False — natural end (empty page reached, all listings captured)

    Args:
        transaction_type: "prodazhbi" or "naemi"
        region_entry:     dict with keys: country, region, slug
        output_path:      path to the CSV file for this transaction type
        property_type:    property type slug for level-2 cascade (optional)
        price_min:        lower price bound for level-3 cascade (optional)
        price_max:        upper price bound for level-3 cascade (optional)
        resume_page:      page to resume from (None = start from START_PAGE)
        progress_key:     string key saved to progress.json to identify this
                          scrape unit for resume purposes
    """
    slug = region_entry["slug"]
    start = resume_page + 1 if resume_page is not None else START_PAGE
    last_page_had_listings = False
    last_page_count = 40  # conservative default; updated after each page

    label = slug
    if property_type:
        label += f"/{property_type}"
    if price_min is not None:
        label += f" [{price_min}-{price_max}]"

    logger.info(f"--- Scraping: {label} | {transaction_type} | from page {start} ---")

    for page in range(start, start + MAX_PAGES):

        url = build_listings_url(
            transaction_type, slug, page,
            property_type=property_type,
            price_min=price_min,
            price_max=price_max,
        )
        logger.info(f"Page {page}: {url}")

        html = fetch_page(url, page_type="listings", last_page_was_partial=(last_page_count < 40))

        if html is None:
            logger.warning(f"Fetch returned None on page {page} for {label}. Stopping.")
            break

        listings = parse_listings_page(html, region_entry, transaction_type)

        if not listings:
            logger.info(f"No listings on page {page} for {label}. End of results.")
            last_page_had_listings = False
            break

        logger.info(f"Found {len(listings)} listings on page {page}.")
        last_page_had_listings = True
        last_page_count = len(listings)

        # --- Detail page enrichment ---
        for listing in listings:
            detail_url = listing.get("listing_url")
            if not detail_url:
                logger.warning(f"No listing_url for source_id={listing.get('source_id')}. Skipping detail fetch.")
                continue
            detail_html = fetch_page(detail_url, page_type="detail")
            if detail_html is None:
                logger.warning(f"Detail fetch failed for {detail_url}. Detail fields will be None.")
                continue
            listing.update(parse_detail_page(detail_html))

        # --- Validate and save ---
        valid_listings = filter_valid_listings(listings)
        rejected = len(listings) - len(valid_listings)
        if rejected:
            logger.warning(f"{rejected} listing(s) rejected by validator on page {page}.")

        append_listings(output_path, valid_listings)
        logger.info(f"Saved {len(valid_listings)} valid listings from page {page}.")

        # Save progress — include progress_key to identify cascade position on resume
        save_progress(transaction_type, slug, page, output_path,
                      property_type=property_type,
                      price_min=price_min, price_max=price_max,
                      progress_key=progress_key)

    # Cap detection: only consider capped if the last page was FULL (40 listings).
    # If the last page had fewer than 40 listings, it ended naturally — even if
    # the next fetch returned None. This prevents the binary search from splitting
    # tiny price ranges like 62487-62489 that clearly can't have 1000+ listings.
    hit_cap = last_page_had_listings and last_page_count == 40
    if hit_cap:
        logger.warning(f"Cap likely hit for {label} — will split further.")
    return hit_cap


def scrape_price_buckets(
    transaction_type: str,
    region_entry: dict,
    output_path: str,
    property_type: str,
    price_min: int,
    price_max: int,
    resume_key: str | None = None,
    resume_page: int | None = None,
):
    """
    Level-3 cascade: binary-search the price range until each bucket is under cap.
    Pre-flights each range before scraping — avoids scraping thousands of pages
    just to discover a range is still capped.
    Recursively splits [price_min, price_max] in half if cap is hit.
    """
    mid = (price_min + price_max) // 2

    # Avoid infinite recursion on degenerate ranges (e.g. min == max)
    if price_min >= price_max or mid == price_min:
        logger.warning(f"Price range [{price_min}-{price_max}] cannot be split further. Scraping as-is.")
        scrape_pages(
            transaction_type, region_entry, output_path,
            property_type=property_type,
            price_min=price_min, price_max=price_max,
        )
        return

    for (lo, hi) in [(price_min, mid), (mid + 1, price_max)]:
        key = f"{region_entry['slug']}:{property_type}:{lo}-{hi}"

        # Resume: skip buckets already completed in a previous run
        if resume_key and key < resume_key:
            logger.info(f"Skipping completed price bucket: {key}")
            continue

        r_page = resume_page if (resume_key and key == resume_key) else None

        # --- Pre-flight check before scraping this bucket ---
        # Avoids scraping up to MAX_PAGES pages just to discover the range is
        # still capped. If the "1000+" indicator is present, split immediately.
        bucket_capped = preflight_check(
            transaction_type, region_entry["slug"],
            property_type=property_type,
            price_min=lo, price_max=hi,
        )

        if bucket_capped:
            logger.info(f"Bucket [{lo}-{hi}] pre-flight shows cap — splitting immediately.")
            scrape_price_buckets(
                transaction_type, region_entry, output_path,
                property_type=property_type,
                price_min=lo, price_max=hi,
            )
            continue

        hit_cap = scrape_pages(
            transaction_type, region_entry, output_path,
            property_type=property_type,
            price_min=lo, price_max=hi,
            resume_page=r_page,
            progress_key=key,
        )

        if hit_cap:
            # Slow-path fallback: pre-flight missed it (e.g. is_capped parsing
            # failure), split anyway rather than silently truncating.
            logger.warning(f"Bucket [{lo}-{hi}] hit cap after scraping — splitting as fallback.")
            scrape_price_buckets(
                transaction_type, region_entry, output_path,
                property_type=property_type,
                price_min=lo, price_max=hi,
            )


def preflight_check(
    transaction_type: str,
    slug: str,
    property_type: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
) -> bool:
    """
    Fetch page 1 of a URL combination and check whether it shows "1000+".
    Used before any real scraping to decide which cascade level to start at,
    and before each price bucket to avoid scraping capped ranges.

    Returns:
        True  — "1000+" detected, cap is hit
        False — under cap, or fetch failed (safe to attempt normal scrape)
    """
    url = build_listings_url(transaction_type, slug, page=1,
                             property_type=property_type,
                             price_min=price_min, price_max=price_max)

    label = f"{slug}/{property_type}" if property_type else slug
    if price_min is not None:
        label += f" [{price_min}-{price_max}]"
    logger.info(f"Pre-flight check: {url}")

    html = fetch_page(url, page_type="listings")
    if html is None:
        logger.warning(f"Pre-flight fetch failed for {label} — assuming no cap.")
        return False

    capped = is_capped(html)
    logger.info(f"Pre-flight result for {label}: {'CAPPED' if capped else 'under cap'}")
    return capped


def scrape_region(
    transaction_type: str,
    region_entry: dict,
    output_path: str,
    resume_page: int | None = None,
    resume_property_type: str | None = None,
    resume_price_key: str | None = None,
):
    """
    Cascade scraper for one region with pre-flight cap detection.

    Before scraping any listings, fetches page 1 of the region (and each
    property type if needed) to check the "1000+" indicator. This determines
    which cascade level to start at — avoiding duplicate scraping entirely.

      Level 1 — region not capped: scrape slug alone
      Level 2 — region capped: pre-flight each property type, then scrape
      Level 3 — property type capped: go straight to price binary search

    Args:
        transaction_type:     "prodazhbi" or "naemi"
        region_entry:         dict with keys: country, region, slug
        output_path:          path to the CSV file for this transaction type
        resume_page:          page to resume from within the current scrape unit
        resume_property_type: property type to resume from (level-2 resume)
        resume_price_key:     price bucket key to resume from (level-3 resume)
    """
    slug = region_entry["slug"]
    region_name = region_entry["region"]
    logger.info(f"===== Region: {region_name} ({slug}) | {transaction_type} =====")

    price_min = PRODAZHBI_PRICE_MIN if transaction_type == "prodazhbi" else NAEMI_PRICE_MIN
    price_max = PRODAZHBI_PRICE_MAX if transaction_type == "prodazhbi" else NAEMI_PRICE_MAX

    # --- Pre-flight: check region level cap ---
    # Skip if resuming mid-property-type or mid-price (cap already known)
    if resume_property_type is None and resume_price_key is None:
        region_capped = preflight_check(transaction_type, slug)

        if not region_capped:
            # Level 1: region is under cap — scrape normally
            scrape_pages(
                transaction_type, region_entry, output_path,
                resume_page=resume_page,
                progress_key=slug,
            )
            return

        logger.info(f"Region {slug} capped — pre-flight checking each property type.")

    # --- Pre-flight loop: check ALL property types before scraping any ---
    # Build a map of {prop_type: is_capped} so we know upfront which ones
    # need price splitting and which can be scraped normally.
    #
    # IMPORTANT: pre-flight checks are cheap (one HTTP request each) and must
    # always run for every property type — even on a resumed run. The resume
    # pointer (resume_property_type) only tells us which property type to
    # START SCRAPING from, not which pre-flights to skip. Skipping pre-flights
    # based on the resume pointer caused types before the resume point to be
    # silently omitted from scraping in prior versions of this code.
    prop_cap_map = {}

    for prop_type in PROPERTY_TYPES:
        # If resuming mid-price-split on this specific property type, we already
        # know it was capped (that's why a price split was started). Skip its
        # pre-flight and mark it capped directly.
        if resume_property_type and prop_type == resume_property_type and resume_price_key is not None:
            prop_cap_map[prop_type] = True
            continue

        prop_cap_map[prop_type] = preflight_check(transaction_type, slug, property_type=prop_type)

    logger.info(f"Pre-flight complete. Capped: {[p for p, c in prop_cap_map.items() if c]}")

    # Scrape each property type at the appropriate cascade level.
    # On a resumed run, skip types that precede the resume point in the list.
    catching_up_prop = resume_property_type is not None

    for prop_type, prop_capped in prop_cap_map.items():

        if catching_up_prop:
            if prop_type != resume_property_type:
                logger.info(f"Skipping completed property type (scraping): {prop_type}")
                continue
            else:
                catching_up_prop = False

        # Carry resume state forward only for the property type we're resuming on.
        if resume_property_type and prop_type == resume_property_type:
            r_page = resume_page if resume_price_key is None else None
            r_price_key = resume_price_key
        else:
            r_page = None
            r_price_key = None

        prop_key = f"{slug}:{prop_type}"

        if prop_capped:
            # Level 3: go straight to price binary search
            logger.info(f"{slug}/{prop_type} capped — going straight to price split.")
            scrape_price_buckets(
                transaction_type, region_entry, output_path,
                property_type=prop_type,
                price_min=price_min,
                price_max=price_max,
                resume_key=r_price_key,
                resume_page=r_page if r_price_key else None,
            )
        else:
            # Level 2: under cap — scrape normally
            scrape_pages(
                transaction_type, region_entry, output_path,
                property_type=prop_type,
                resume_page=r_page,
                progress_key=prop_key,
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    run_start = datetime.now(timezone.utc)
    logger.info(f"========== Scraping run started at {run_start.isoformat()} ==========")

    # Load progress from previous run if it exists
    progress = load_progress()

    for transaction_type in TRANSACTION_TYPES:

        # Skip transaction types we've already completed
        if progress and transaction_type != progress["transaction_type"]:
            logger.info(f"Skipping completed transaction type: {transaction_type}")
            continue

        logger.info(f"===== Transaction type: {transaction_type} =====")

        # On a resumed run, reuse the original output file regardless of today's date.
        # If the date changed overnight, get_output_path() would generate a new filename
        # and split the dataset across two files.
        output_path = get_output_path(transaction_type)
        if progress and progress.get("output_path") and os.path.exists(progress["output_path"]):
            output_path = progress["output_path"]
            logger.info(f"Resuming existing output file: {output_path}")

        # Only write header if this is a fresh file — resumed runs append to existing CSV
        if not os.path.exists(output_path):
            write_header(output_path)
        logger.info(f"Output file: {output_path}")

        total_regions = len(REGIONS)

        # catching_up = True means we're still skipping ahead to the resume point
        catching_up = progress is not None

        for i, region_entry in enumerate(REGIONS, start=1):

            if catching_up:
                if region_entry["slug"] != progress["slug"]:
                    logger.info(f"Skipping completed region: {region_entry['slug']}")
                    continue
                else:
                    # Found our resume region — stop skipping from here on
                    catching_up = False
                    resume_page = progress["page"]
            else:
                resume_page = None

            logger.info(f"Region {i}/{total_regions}: {region_entry['region']} ({region_entry['slug']})")

            # Resume state is region-scoped: only pass property_type and price_key
            # to the region we're actually resuming. All other regions start fresh.
            resume_property_type = None
            resume_price_key = None

            if progress and region_entry["slug"] == progress["slug"]:
                resume_property_type = progress.get("property_type")
                resume_price_key = progress.get("progress_key")

            scrape_region(
                transaction_type,
                region_entry,
                output_path,
                resume_page=resume_page,
                resume_property_type=resume_property_type,
                resume_price_key=resume_price_key,
            )

        logger.info(f"===== Finished transaction type: {transaction_type} =====")

        # Clear in-memory progress so the next transaction type starts fresh.
        progress = None

    run_end = datetime.now(timezone.utc)
    elapsed = run_end - run_start
    logger.info(f"========== Scraping run finished. Total time: {elapsed} ==========")

    # Delete progress file — run completed successfully
    clear_progress()


if __name__ == "__main__":
    main()
