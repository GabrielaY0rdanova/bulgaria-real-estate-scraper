# detail_parser.py

import logging

from bs4 import BeautifulSoup, NavigableString

from scraper.parser import (
    CONSTRUCTION_TYPES,
    _extract_year,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_detail_page(html):
    """
    Parse a detail page.
    Returns a dictionary with detail-page fields.

    Fields: construction_type, construction_status, year_built,
            gas, tec, features, date_posted, date_modified

    Args:
        html: raw HTML string from fetch_page()
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Initialise all detail-page fields to None ---
    # Each block below will overwrite the relevant fields if found.
    date_posted = None
    date_modified = None

    construction_type = None
    construction_status = None
    year_built = None
    gas = None
    tec = None

    features = None

    # -----------------------------------------------------------------------
    # Date block — inside <div class="adPrice"><div class="info">
    # The text looks like one of:
    #   "Публикувана в 14:54 на 23 март, 2026 год."
    #   "Коригирана в 10:10 на 23 март, 2026 год."
    # Only one is present at a time. Store the full raw string — cleaning
    # into a proper date format is handled downstream in real_estate_cleaning.
    # -----------------------------------------------------------------------
    date_div = soup.select_one("div.adPrice div.info > div")
    if date_div:
        text = date_div.get_text(strip=True)
        if "Публикувана" in text:
            date_posted = text
        elif "Коригирана" in text:
            date_modified = text

    # -----------------------------------------------------------------------
    # adParams block — <div class="adParams">
    # Each param is a direct child <div> with:
    #   - a text label as div.contents[0]  e.g. "Газ:", "ТEЦ:", "Строителство:"
    #   - a <strong> tag holding the value
    #
    # NOTE: "ТEЦ:" contains a Latin E — the site mixes scripts.
    #       The string must match exactly what the HTML contains.
    #
    # Газ: / ТEЦ: → grab <strong> text directly
    # Строителство: → two <strong> tags + plain text sibling between them:
    #   <strong>Тухла, </strong>Въведен в експлоатация <strong>2020 г.</strong>
    #   [0] → construction_type   (strip trailing comma)
    #   [0].next_sibling → construction_status  (plain text between the two tags)
    #   [1] → year_built via _extract_year()   (empty <strong> → None)
    # -----------------------------------------------------------------------
    params_div = soup.select_one("div.adParams")
    if params_div:
        for div in params_div.find_all("div"):

            # Guard: some divs may have no text content at all
            if not div.contents:
                continue

            label = div.contents[0].strip()

            if label == "Газ:":
                strong = div.find("strong")
                if strong:
                    gas = strong.get_text(strip=True)

            elif label == "ТEЦ:":   # Latin E — must match the site's actual character
                strong = div.find("strong")
                if strong:
                    tec = strong.get_text(strip=True)

            elif label == "Строителство:":
                strong_tags = div.find_all("strong")

                if strong_tags:
                    # First <strong> → construction type, strip trailing comma/space
                    construction_type = strong_tags[0].get_text(strip=True).rstrip(", ")

                    # Plain text node immediately after first <strong> → status phrase
                    # e.g. "Въведен в експлоатация " or "Ще бъде въведен в експлоатация "
                    # Must check isinstance(NavigableString) to avoid capturing raw HTML
                    # of sibling tags like <strong></strong> or <strong>2007 г.</strong>
                    raw_sibling = strong_tags[0].next_sibling
                    if raw_sibling and isinstance(raw_sibling, NavigableString):
                        construction_status = raw_sibling.strip() or None

                if len(strong_tags) > 1:
                    # Second <strong> → year, or empty tag when year is unknown
                    year_text = strong_tags[1].get_text(strip=True)
                    year_built = _extract_year(year_text)  # returns None if empty

    # -----------------------------------------------------------------------
    # Features block — <div class="carExtri"><div class="items">
    # Each feature is a plain <div> inside .items, e.g. <div>Асансьор</div>
    # construction_type also appears here (e.g. "Тухла", "Панел") — skip it.
    # We already captured construction_type from adParams; the duplicate here
    # is ignored to avoid confusion.
    # Returns a comma-separated string, or None if no features found.
    # -----------------------------------------------------------------------
    items_div = soup.select_one("div.carExtri div.items")
    if items_div:
        # Build a lowercase set for case-insensitive exclusion of construction types
        construction_types_lower = {ct.lower() for ct in CONSTRUCTION_TYPES}

        feature_list = []
        for item_div in items_div.find_all("div", recursive=False):
            feature_text = item_div.get_text(strip=True)
            if not feature_text:
                continue
            # Skip if this is a construction type value (e.g. "Тухла", "Панел")
            if feature_text.lower() in construction_types_lower:
                continue
            feature_list.append(feature_text)

        features = ", ".join(feature_list) if feature_list else None

    # -----------------------------------------------------------------------
    # Return all detail-page fields as a flat dict.
    # The caller merges these into the listing dict from parse_listing().
    # -----------------------------------------------------------------------
    return {
        "construction_type":   construction_type,
        "construction_status": construction_status,
        "year_built":          year_built,
        "gas":                 gas,
        "tec":                 tec,
        "features":            features,
        "date_posted":         date_posted,
        "date_modified":       date_modified,
    }
