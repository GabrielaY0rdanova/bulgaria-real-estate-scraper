# parser.py

import re
import logging
from datetime import datetime, timezone

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Ordered longest/most-specific first to prevent substring false matches
# e.g. "епк" must come before "пк" or "пк" would match inside "епк"
CONSTRUCTION_TYPES = [
    "сглобяема конструкция",
    "епк",
    "пк",
    "гредоред",
    "панел",
    "тухла",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_capped(html: str) -> bool:
    """
    Check whether a listings page is showing the imot.bg cap ("1000+ обяви").

    imot.bg caps search results at 1000 listings per URL. When capped, the
    SearchInfoLine div contains "1000+" instead of a plain number.

    Example HTML:
      <div class="SearchInfoLine"> 1 - 40 от общо 1000+ обяви - Продава</div>

    Args:
        html: raw HTML string from fetch_page()

    Returns:
        True  — "1000+" found, cap is hit, need to split further
        False — plain number found, or div missing (no cap)
    """
    soup = BeautifulSoup(html, "html.parser")
    info_div = soup.select_one("div.SearchInfoLine")
    if info_div and "1000+" in info_div.get_text():
        return True
    return False


def parse_listings_page(html, region_entry, transaction_type):
    """
    Parse a full listings page.
    Returns a list of listing dicts (may be empty if no listings found).

    Args:
        html:             raw HTML string from fetch_page()
        region_entry:     dict from REGIONS — has keys: region, slug
        transaction_type: "prodazhbi" or "naemi"
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find all listing divs — every listing id starts with "ida"
    listing_divs = soup.find_all("div", id=lambda x: x and x.startswith("ida"))

    if not listing_divs:
        logger.warning("No listing divs found on page.")
        return []

    listings = []
    for div in listing_divs:
        listing = parse_listing(div, region_entry, transaction_type)
        if listing:
            listings.append(listing)

    logger.info(f"Parsed {len(listings)} listings from page.")
    return listings


def parse_listing(div, region_entry, transaction_type):
    """
    Parse a single listing div.
    Returns a dict with all listings-page fields, or None if parsing fails.

    Fields sourced from the detail page (construction_type, construction_status,
    year_built, gas, tec, features) are left as None here — filled in later
    by detail_parser.py.

    Args:
        div:              BeautifulSoup Tag — the listing div
        region_entry:     dict from REGIONS — has keys: region, slug
        transaction_type: "prodazhbi" or "naemi"
    """
    try:
        # --- source_id & listing_tier ---
        # id attribute looks like "ida1a177330175223978"
        # Strip the "ida" prefix to get the source_id
        raw_id = div.get("id", "")
        source_id = raw_id.removeprefix("ida") or None

        # Class list looks like ["item", "BEST", ""] or ["item", "TOP", ""]
        # The tier is the second element — strip whitespace, None if missing/empty
        classes = div.get("class", [])
        raw_tier = classes[1].strip() if len(classes) > 1 else ""
        listing_tier = raw_tier if raw_tier else None

        # --- listing_url ---
        # The title link href — imot.bg uses protocol-relative URLs (//www.imot.bg/...)
        # We add https: prefix to make it a full valid URL
        title_tag = div.select_one("a.title")
        if title_tag and title_tag.get("href"):
            href = title_tag["href"]
            listing_url = "https:" + href if href.startswith("//") else href
        else:
            listing_url = None

        # --- property_type & location ---
        # Title link text looks like: "Продава 1-СТАЕН<location>град Пловдив, Център</location>"
        # property_type → everything before the <location> tag
        # locality + neighbourhood → inside the <location> tag
        property_type = None
        locality = None
        locality_type = None
        neighbourhood = None

        if title_tag:
            location_tag = title_tag.find("location")
            if location_tag:
                location_text = location_tag.get_text(strip=True)
                location_tag.decompose()  # remove <location> from title_tag

                # property_type is whatever text remains in the title
                # Strip the transaction verb prefix: "Продава " or "Дава под наем "
                raw_property_type = title_tag.get_text(strip=True) or None
                if raw_property_type:
                    for prefix in ("Продава ", "Дава под наем "):
                        if raw_property_type.startswith(prefix):
                            raw_property_type = raw_property_type.removeprefix(prefix).strip()
                            break
                property_type = raw_property_type or None

                # Bulgarian location_text looks like "гр. Банско, област Благоевград"
                # Structure: "{locality_type_abbr}. {locality_name}, {region_text}"
                # The part after the comma is always the administrative region — NOT a neighbourhood.
                # Neighbourhood only appears for city slugs where location looks like:
                # "гр. Банско, Грамадето" — second part is a sub-area of the city.
                # For administrative region slugs: "гр. Банско, област Благоевград" — second part is the region name.
                # We skip the region text (already known from region_entry).

                if "," in location_text:
                    locality_raw, second_part = location_text.split(",", 1)
                    second_part = second_part.strip()
                    # If second part starts with "област" it's the region — not a neighbourhood
                    if second_part.lower().startswith("област"):
                        neighbourhood = None
                    else:
                        neighbourhood = second_part or None
                else:
                    locality_raw = location_text
                    neighbourhood = None

                # locality_raw looks like "гр. Банско" or "с. Марково" or "к.к. Слънчев бряг"
                # Handle known prefixes; for anything else store the raw prefix as-is
                # so real_estate_cleaning can normalise it downstream.
                locality_raw = locality_raw.strip()
                if locality_raw.startswith("град "):
                    locality_type = "град"
                    locality = locality_raw.removeprefix("град ").strip() or None
                elif locality_raw.startswith("гр. "):
                    locality_type = "град"
                    locality = locality_raw.removeprefix("гр. ").strip() or None
                elif locality_raw.startswith("село "):
                    locality_type = "село"
                    locality = locality_raw.removeprefix("село ").strip() or None
                elif locality_raw.startswith("с. "):
                    locality_type = "село"
                    locality = locality_raw.removeprefix("с. ").strip() or None
                else:
                    # Unknown prefix (e.g. "к.к.", "вилна зона") — store raw prefix
                    # so downstream cleaning can map it to a standard locality_type.
                    # e.g. "к.к. Слънчев бряг" → locality_type="к.к.", locality="Слънчев бряг"
                    parts = locality_raw.split(None, 1)
                    if len(parts) == 2:
                        locality_type = parts[0]
                        locality = parts[1] or None
                    else:
                        locality_type = None
                        locality = locality_raw or None

        # --- price ---
        # <div class="price"><div>89 990 €<br>176 005.14 лв.</div></div>
        # We want the first line only (the euro amount) — everything before <br>
        price = None
        price_div = div.select_one("div.price")
        if price_div:
            # get_text with separator so <br> becomes a newline — then take first chunk
            price_text = price_div.get_text(separator="\n", strip=True)
            first_line = price_text.split("\n")[0].strip()
            price = first_line or None

        # --- info block fields ---
        # area_m2, floor, agency_phone all come from <div class="info">
        # This div is optional — private listings sometimes omit it entirely
        area_m2 = None
        floor = None
        raw_phone = None

        info_div = div.select_one("div.info")
        if info_div:
            info_text = info_div.get_text(separator=" ", strip=True)
            area_m2 = _extract_area(info_text)
            floor = _extract_floor(info_text)
            raw_phone = _extract_phone(info_text)

        # --- poster_type & agency_name ---
        # Agency listing → <div class="seller"> contains <div class="name"><a>
        # Private listing → no seller div, or seller div with no name link
        poster_type = "собственик"
        agency_name = None

        seller_div = div.select_one("div.seller")
        if seller_div:
            name_link = seller_div.select_one("div.name a")
            if name_link:
                poster_type = "агенция"
                agency_name = name_link.get_text(strip=True) or None

        # Store phone only for agencies (GDPR — no private individual phones)
        agency_phone = raw_phone if poster_type == "агенция" else None

        # --- has_photos ---
        # <a class="photos"><strong>и 8 снимки</strong></a>
        # If <strong> exists inside the photos link → has photos
        photos_link = div.select_one("a.photos")
        has_photos = bool(photos_link and photos_link.find("strong"))

        # --- scraped_at ---
        scraped_at = datetime.now(timezone.utc).isoformat()

        # --- Assemble result dict ---
        # Fields from detail page left as None — filled in by detail_parser.py
        return {
            # Location (from region_entry)
            "region":               region_entry["region"],
            "locality":             locality,
            "locality_type":        locality_type,
            "neighbourhood":        neighbourhood,
            # Property
            "property_type":        property_type,
            "bedrooms":             None,   # not available on listings page
            # Poster
            "poster_type":          poster_type,
            "agency_name":          agency_name,
            # Price & physical attributes
            "price":                price,
            "area_m2":              area_m2,
            "floor":                floor,
            # Construction (detail page)
            "construction_type":    None,
            "construction_status":  None,
            "year_built":           None,
            # Utilities (detail page)
            "gas":                  None,
            "tec":                  None,
            # Features (detail page)
            "features":             None,
            # Listing metadata
            "date_posted":          None,   # not available on listings page
            "date_modified":        None,   # not available on listings page
            "has_photos":           has_photos,
            "agency_phone":         agency_phone,
            "listing_url":          listing_url,
            "source_id":            source_id,
            "listing_tier":         listing_tier,
            "transaction_type":     transaction_type,
            "scraped_at":           scraped_at,
            "status":               "active",
        }

    except Exception as e:
        logger.error(f"Failed to parse listing: {e}")
        return None


# ---------------------------------------------------------------------------
# Private helpers — each extracts one field from the info block
# ---------------------------------------------------------------------------

def _extract_area(info_text):
    """
    Extract area in square metres.
    Example: '59 кв.м' → 59.0, '123кв.м' → 123.0
    """
    match = re.search(r"(\d+(?:\.\d+)?)\s*кв\.м", info_text)
    if match:
        return float(match.group(1))
    return None


def _extract_floor(info_text):
    """
    Extract floor as 'current/total'.
    Example: '2-ри ет. от 6' → '2/6', '4-ти ет. от 11' → '4/11'
    """
    match = re.search(r"(\d+)-\w+\s*ет\.\s*от\s*(\d+)", info_text)
    if match:
        return f"{match.group(1)} от {match.group(2)}"
    return None


def _extract_year(info_text):
    """
    Extract year built.
    Example: 'въведен в експлоатация 2020 г.' → 2020
    Uses \d{4} to match exactly 4 digits — avoids matching area or floor numbers.
    """
    match = re.search(r"(\d{4})\s*г\.", info_text)
    if match:
        return int(match.group(1))
    return None


def _extract_construction_type(info_text):
    """
    Extract construction type by scanning for known keywords.
    CONSTRUCTION_TYPES is ordered most-specific first to avoid substring matches
    (e.g. 'епк' before 'пк').
    Example: 'Тухла' → 'Тухла', 'ПАНЕЛ' → 'Панел', nothing found → None
    """
    info_lower = info_text.lower()
    for construction_type in CONSTRUCTION_TYPES:
        if construction_type in info_lower:
            return construction_type.capitalize()
    return None


def _extract_phone(info_text):
    """
    Extract phone number as a clean digit string.
    Example: 'тел.: 0897669075' → '0897669075'
             'тел.: 089 766 9075' → '0897669075'
             'тел.: 0883 66 84 66' → '0883668466'
    """
    match = re.search(r"тел\.:\s*([\d\s]+)", info_text)
    if match:
        digits = match.group(1).strip().replace(" ", "")
        return digits if digits else None
    return None
