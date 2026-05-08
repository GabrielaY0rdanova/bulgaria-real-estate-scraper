# =============================================================================
# real_estate_scraper — Light Scraper — Comparator
# Purpose: Compare freshly scraped index-page data against the existing DB.
#          Classifies each scraped listing into one of five actions and
#          returns structured result sets for the main loop to act on.
# =============================================================================

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action constants
# ---------------------------------------------------------------------------

NEW         = "new"           # source_id not seen before → insert
CHANGED     = "changed"       # known source_id, price changed → update + price_history
UNCHANGED   = "unchanged"     # known source_id, same price → skip
REAPPEARED  = "reappeared"    # was inactive, now visible again → reactivate
MISSING     = "missing"       # was active, no longer visible → mark inactive


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_listings(
    scraped: list[dict],
    active_in_db: dict,
    inactive_in_db: set,
) -> dict:
    """
    Classify each scraped listing against the existing DB state.

    Args:
        scraped:       list of dicts from parse_listings_page() —
                       each dict must have at least source_id and price
        active_in_db:  dict from db.fetch_active_listings() —
                       { source_id: { listing_id, price } }
        inactive_in_db: set of source_ids from db.fetch_inactive_listings()

    Returns:
        dict with five keys — each mapping to a list of relevant dicts:
        {
            "new":        [ scraped listing dicts ],
            "changed":    [ { scraped listing dict + listing_id + old_price } ],
            "unchanged":  [ scraped listing dicts ],
            "reappeared": [ scraped listing dicts ],
            "missing":    [ { source_id, listing_id } ],
        }
    """
    results = {
        NEW:        [],
        CHANGED:    [],
        UNCHANGED:  [],
        REAPPEARED: [],
    }

    scraped_ids = set()

    for listing in scraped:
        source_id = listing.get("source_id")
        if not source_id:
            continue

        scraped_ids.add(source_id)
        scraped_price = _parse_price(listing.get("price"))

        if source_id in active_in_db:
            db_entry = active_in_db[source_id]
            db_price = db_entry["price"]

            if _prices_differ(scraped_price, db_price):
                results[CHANGED].append({
                    **listing,
                    "listing_id": db_entry["listing_id"],
                    "old_price":  db_price,
                })
            else:
                results[UNCHANGED].append(listing)

        elif source_id in inactive_in_db:
            results[REAPPEARED].append(listing)

        else:
            results[NEW].append(listing)

    # MISSING is intentionally not computed here.
    # It must be computed once after ALL regions are scraped,
    # by comparing the full accumulated scraped_ids against active_in_db.
    # Computing it per-region would falsely flag every other region's listings as missing.
    results[MISSING] = []

    # Log summary
    logger.info(
        f"Classification: {len(results[NEW])} new | "
        f"{len(results[CHANGED])} changed | "
        f"{len(results[UNCHANGED])} unchanged | "
        f"{len(results[REAPPEARED])} reappeared"
    )

    return results


def compute_missing(all_scraped_ids: set, active_in_db: dict) -> list[dict]:
    """
    Compute listings that were active in the DB but not seen in any region
    during this scrape run. Called once after all regions are scraped.

    Args:
        all_scraped_ids: union of source_ids seen across all regions
        active_in_db:    dict from db.fetch_active_listings()

    Returns:
        list of { source_id, listing_id } dicts
    """
    missing = [
        {"source_id": sid, "listing_id": data["listing_id"]}
        for sid, data in active_in_db.items()
        if sid not in all_scraped_ids
    ]
    logger.info(f"Missing (not seen in any region): {len(missing):,}")
    return missing


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _parse_price(raw_price) -> float | None:
    """
    Parse a raw price string from the scraper into a float.
    Returns None for price-on-request or unparseable values.
    Example: "89 990 €" → 89990.0
    """
    if raw_price is None:
        return None
    if isinstance(raw_price, (int, float)):
        return float(raw_price)
    # Strip currency symbol, spaces, and non-numeric characters
    cleaned = raw_price.replace("€", "").replace(" ", "").replace("\xa0", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _prices_differ(price_a: float | None, price_b: float | None) -> bool:
    """
    Compare two prices. Two None values are considered equal (both on request).
    A float and None are considered different.
    """
    if price_a is None and price_b is None:
        return False
    if price_a is None or price_b is None:
        return True
    return price_a != price_b