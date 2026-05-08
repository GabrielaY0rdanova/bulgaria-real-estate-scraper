# =============================================================================
# real_estate_scraper — Light Scraper — Progress
# Purpose: Resume support for light scraper runs. Tracks current pass,
#          region, and page so interrupted runs can continue from where
#          they left off without re-scraping completed regions.
# =============================================================================

import json
import logging
import os

logger = logging.getLogger(__name__)

PROGRESS_FILE = "light_scraper_progress.json"


def load_progress() -> dict | None:
    """
    Load progress from light_scraper_progress.json.

    Returns:
        dict with keys: pass_number, slug, page, output_path — if file exists
        None — if no progress file found (fresh run)
    """
    if not os.path.exists(PROGRESS_FILE):
        logger.info("No light scraper progress file found — starting fresh run.")
        return None

    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            progress = json.load(f)
        logger.info(f"Resumed from progress file: {progress}")
        return progress

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Progress file corrupted ({e}) — starting fresh run.")
        return None


def save_progress(
    pass_number: int,
    slug: str,
    page: int,
    output_path: str,
    transaction_type: str,
    property_type: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    progress_key: str | None = None,
):
    """
    Save current scraping position to light_scraper_progress.json.
    Called after each page is successfully scraped and saved.

    Args:
        pass_number:  1 (index-only) or 2 (rolling detail refresh)
        slug:         region slug e.g. "grad-shumen"
        page:         page number just completed
        output_path:  path to the CSV file being written
        property_type: property type slug if at level-2 cascade (optional)
        price_min:    lower price bound if at level-3 cascade (optional)
        price_max:    upper price bound if at level-3 cascade (optional)
        progress_key: string key identifying the current scrape unit (optional)
    """
    progress = {
        "pass_number":    pass_number,
        "slug":           slug,
        "page":           page,
        "output_path":    output_path,
        "property_type":  property_type,
        "price_min":      price_min,
        "price_max":      price_max,
        "progress_key":   progress_key,
        "transaction_type": transaction_type,
    }

    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        logger.debug(f"Light scraper progress saved: {progress}")

    except OSError as e:
        logger.error(f"Failed to save light scraper progress file: {e}")


def save_progress_pass2(index: int, output_path: str, transaction_type: str):
    """
    Save Pass 2 progress to light_scraper_progress.json.
    Called every 100 listings during the rolling detail refresh.

    Args:
        index:       number of listings processed so far
        output_path: path to the CSV file being written
    """
    progress = {
        "pass_number":      2,
        "pass2_index":      index,
        "output_path":      output_path,
        "transaction_type": transaction_type,
    }

    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        logger.debug(f"Pass 2 progress saved: index={index}")

    except OSError as e:
        logger.error(f"Failed to save Pass 2 progress file: {e}")


def clear_progress():
    """
    Delete light_scraper_progress.json on successful run completion.
    Called once at the end of main() after all passes are complete.
    """
    if os.path.exists(PROGRESS_FILE):
        try:
            os.remove(PROGRESS_FILE)
            logger.info("Light scraper progress file cleared — run completed successfully.")
        except OSError as e:
            logger.error(f"Failed to delete light scraper progress file: {e}")
    else:
        logger.info("No light scraper progress file to clear.")