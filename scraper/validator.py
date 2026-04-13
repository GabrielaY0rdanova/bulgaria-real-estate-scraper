# validator.py

# Validation module for real estate listings.
# Rejects rows missing required fields or failing sanity checks.
# Logs warnings for rejected listings.


import logging

logger = logging.getLogger(__name__)

# Required fields that must not be None or empty
REQUIRED_FIELDS = ["source_id", "listing_url", "property_type", "locality"]

def is_valid_listing(listing: dict) -> bool:
    """
    Check if a listing dict has all required fields populated and passes sanity checks.

    Args:
        listing: dictionary representing a listing

    Returns:
        True if valid, False if any required field is None/empty or sanity check fails
    """
    source_id = listing.get("source_id", "<unknown>")

    # Check required fields
    for field in REQUIRED_FIELDS:
        value = listing.get(field)
        if value is None:
            logger.warning("Listing rejected: field '%s' is None (source_id=%s)", field, source_id)
            return False
        if isinstance(value, str) and value.strip() == "":
            logger.warning("Listing rejected: field '%s' is empty (source_id=%s)", field, source_id)
            return False

    # Sanity check: area_m2 > 0
    area = listing.get("area_m2")
    if area is not None and isinstance(area, (int, float)) and area <= 0:
        logger.warning("Listing rejected: area_m2 <= 0 (source_id=%s, area_m2=%s)", source_id, area)
        return False

    # Sanity check: year_built > 0
    year = listing.get("year_built")
    if year is not None and isinstance(year, int) and year <= 0:
        logger.warning("Listing rejected: year_built <= 0 (source_id=%s, year_built=%s)", source_id, year)
        return False

    return True


def filter_valid_listings(listings: list[dict]) -> list[dict]:
    """
    Filter a list of listings, keeping only valid ones.

    Args:
        listings: list of listing dictionaries

    Returns:
        List of valid listings
    """
    return [listing for listing in listings if is_valid_listing(listing)]
