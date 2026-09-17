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

REQUIRED_FIELDS = {
    "pass_number": int,
    "output_path": str,
    "transaction_type": str,
    "run_id": str,
    "run_dir": str,
}


def _is_valid_progress(progress: object) -> bool:
    if not isinstance(progress, dict):
        return False
    if not all(isinstance(progress.get(key), value_type) for key, value_type in REQUIRED_FIELDS.items()):
        return False
    if progress["pass_number"] not in (1, 2):
        return False
    if progress["transaction_type"] not in ("prodazhbi", "naemi"):
        return False
    if not all(progress[field].strip() for field in ("output_path", "run_id", "run_dir")):
        return False
    if progress["pass_number"] == 1:
        return (
            isinstance(progress.get("slug"), str)
            and bool(progress["slug"].strip())
            and isinstance(progress.get("page"), int)
            and progress["page"] >= 0
        )
    return isinstance(progress.get("pass2_index"), int) and progress["pass2_index"] >= 0


def _write_progress(progress: dict) -> None:
    temp_path = f"{PROGRESS_FILE}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, PROGRESS_FILE)
        logger.debug(f"Light scraper progress saved: {progress}")
    except OSError as e:
        logger.error(f"Failed to save light scraper progress file: {e}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            logger.warning(f"Failed to remove temporary progress file: {temp_path}")
        raise


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
        if not _is_valid_progress(progress):
            logger.warning("Progress file is missing safe resume fields — starting fresh run.")
            return None
        logger.info(f"Resumed from progress file: {progress}")
        return progress

    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Progress file corrupted ({e}) — starting fresh run.")
        return None


def save_progress(
    pass_number: int,
    slug: str,
    page: int,
    output_path: str,
    transaction_type: str,
    run_id: str,
    run_dir: str,
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
        "run_id":           run_id,
        "run_dir":          run_dir,
    }
    _write_progress(progress)


def save_progress_pass2(
    index: int,
    output_path: str,
    transaction_type: str,
    run_id: str,
    run_dir: str,
):
    """
    Save Pass 2 progress to light_scraper_progress.json.
    Called after each detail batch during the rolling refresh.

    Args:
        index:       number of listings processed so far
        output_path: path to the CSV file being written
    """
    progress = {
        "pass_number":      2,
        "pass2_index":      index,
        "output_path":      output_path,
        "transaction_type": transaction_type,
        "run_id":           run_id,
        "run_dir":          run_dir,
    }
    _write_progress(progress)


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
