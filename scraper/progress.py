# progress.py

import json
import logging
import os

logger = logging.getLogger(__name__)

PROGRESS_FILE = "progress.json"


def load_progress() -> dict | None:
    """
    Load progress from progress.json.

    Returns:
        dict with keys: transaction_type, slug, page, output_path — if file exists
        None — if no progress file found (fresh run)
    """
    if not os.path.exists(PROGRESS_FILE):
        logger.info("No progress file found — starting fresh run.")
        return None

    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            progress = json.load(f)
        logger.info(f"Resumed from progress file: {progress}")
        return progress

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Progress file is corrupted ({e}) — starting fresh run.")
        return None


def save_progress(
    transaction_type: str,
    slug: str,
    page: int,
    output_path: str,
    property_type: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    progress_key: str | None = None,
):
    """
    Save current scraping position to progress.json.
    Called after each page is successfully scraped and saved.

    Args:
        transaction_type: "prodazhbi" or "naemi"
        slug:             region slug, e.g. "grad-shumen"
        page:             page number just completed
        output_path:      path to the CSV file being written
        property_type:    property type slug if at level-2 cascade (optional)
        price_min:        lower price bound if at level-3 cascade (optional)
        price_max:        upper price bound if at level-3 cascade (optional)
        progress_key:     string key identifying the current scrape unit,
                          used to resume mid-cascade (optional)
    """
    progress = {
        "transaction_type": transaction_type,
        "slug":             slug,
        "page":             page,
        "output_path":      output_path,
        "property_type":    property_type,
        "price_min":        price_min,
        "price_max":        price_max,
        "progress_key":     progress_key,
    }

    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        logger.debug(f"Progress saved: {progress}")

    except OSError as e:
        logger.error(f"Failed to save progress file: {e}")


def clear_progress():
    """
    Delete progress.json on successful run completion.
    Called once at the end of main() after all regions are scraped.
    """
    if os.path.exists(PROGRESS_FILE):
        try:
            os.remove(PROGRESS_FILE)
            logger.info("Progress file cleared — run completed successfully.")
        except OSError as e:
            logger.error(f"Failed to delete progress file: {e}")
    else:
        logger.info("No progress file to clear.")