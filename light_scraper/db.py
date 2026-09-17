# =============================================================================
# real_estate_scraper — Light Scraper — DB
# Purpose: Database connection and queries for the light scraper.
#          Provides active/inactive listing lookups for Pass 1 comparison
#          and oldest-checked listing fetch for Pass 2 rolling refresh.
# =============================================================================

import os
import logging
from datetime import date, datetime
import psycopg2
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# Map scraper transaction type slugs to DB enum values
_TX_TO_ENUM = {
    "prodazhbi": "sale",
    "naemi":     "rental",
}

_LOCALITY_TYPE_TO_RAW = {
    "city": "град",
    "village": "село",
    "resort_complex": "к.к.",
    "area": "м-т",
    "highway": "магистрала",
    "reservoir": "яз.",
    "main_road": "главен",
    "mountain_hut": "хижа",
    "railway_station": "Гара",
    "villa_zone": "вилна зона",
}

_CONSTRUCTION_STATUS_TO_RAW = {
    "completed": "Въведен в експлоатация",
    "under_construction": "Ще бъде въведен в експлоатация",
    "not_completed": "Не е въведен в експлоатация",
}

_TEC_TO_RAW = {
    "district_heating": "ДА",
    "no_district_heating": "НЕ",
    "local_heating": "Лок.отопл.",
    "being_installed": "Прокарва се",
}

_POSTER_TYPE_TO_RAW = {
    "agency": "агенция",
    "owner": "собственик",
}

_MONTHS_BG = {
    1: "януари", 2: "февруари", 3: "март", 4: "април",
    5: "май", 6: "юни", 7: "юли", 8: "август",
    9: "септември", 10: "октомври", 11: "ноември", 12: "декември",
}


def _date_to_raw(value, label: str):
    """Convert a DB date back to the scraper format accepted by cleaning."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    if not isinstance(value, datetime):
        return value
    return (
        f"{label} в {value:%H:%M} на {value.day} "
        f"{_MONTHS_BG[value.month]}, {value.year} год."
    )


def get_connection():
    """Return a psycopg2 connection using PG* environment variables."""
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=os.getenv("PGPORT"),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def fetch_active_listings(conn, transaction_type: str) -> dict:
    """
    Fetch all active listings from the DB for a given transaction type.
    Returns a dict keyed by source_id:
        { source_id: { "listing_id": int, "price": float or None } }
    Used by Pass 1 comparator to detect new, changed, and missing listings.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT source_id, listing_id, price
        FROM listings
        WHERE status = 'active'
          AND transaction_type = %s
    """, (_TX_TO_ENUM[transaction_type],))
    rows = cursor.fetchall()
    cursor.close()

    logger.info(f"Fetched {len(rows):,} active listings from DB.")

    return {
        row[0]: {"listing_id": row[1], "price": row[2]}
        for row in rows
    }


def fetch_inactive_listings(conn, transaction_type: str) -> set:
    """
    Fetch all inactive source_ids from the DB for a given transaction type.
    Used by Pass 1 comparator to detect reappeared listings.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT source_id
        FROM listings
        WHERE status = 'inactive'
          AND transaction_type = %s
    """, (_TX_TO_ENUM[transaction_type],))
    rows = cursor.fetchall()
    cursor.close()

    logger.info(f"Fetched {len(rows):,} inactive listings from DB.")

    return {row[0] for row in rows}


def fetch_pass2_listings(
    conn,
    transaction_type: str,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    """
    Fetch the oldest 5% of eligible active listings by date_last_checked.
    Used by Pass 2 rolling detail refresh.
    Returns complete raw-format rows so a detail refresh does not replace
    existing index-page fields with placeholders or null values.
    """
    cursor = conn.cursor()
    excluded = sorted(exclude_ids or set())
    cursor.execute("""
        SELECT CEIL(COUNT(*) * 0.05)::int
        FROM listings
        WHERE status = 'active'
          AND transaction_type = %s
          AND NOT (source_id = ANY(%s::text[]))
    """, (_TX_TO_ENUM[transaction_type], excluded))
    limit = cursor.fetchone()[0]

    cursor.execute("""
        SELECT
            l.source_id,
            l.listing_id,
            l.listing_url,
            l.listing_tier,
            l.price,
            l.price_on_request,
            l.date_posted,
            l.date_modified,
            l.has_photos,
            l.scraped_at,
            pt.name_bg AS property_type,
            p.bedrooms,
            p.area_m2,
            p.floor,
            p.total_floors,
            ct.name_bg AS construction_type,
            p.construction_status::text,
            p.year_built,
            p.gas,
            p.tec::text,
            c.contact_type::text,
            c.name AS agency_name,
            c.phone AS agency_phone,
            COALESCE(
                CASE WHEN g.level = 'region' THEN g.name_bg END,
                CASE WHEN gp.level = 'region' THEN gp.name_bg END,
                CASE WHEN gpp.level = 'region' THEN gpp.name_bg END
            ) AS region,
            COALESCE(
                CASE WHEN g.level = 'locality' THEN g.name_bg END,
                CASE WHEN gp.level = 'locality' THEN gp.name_bg END,
                CASE WHEN gpp.level = 'locality' THEN gpp.name_bg END
            ) AS locality,
            COALESCE(
                CASE WHEN g.level = 'locality' THEN g.locality_type END,
                CASE WHEN gp.level = 'locality' THEN gp.locality_type END,
                CASE WHEN gpp.level = 'locality' THEN gpp.locality_type END
            ) AS locality_type,
            COALESCE(
                CASE WHEN g.level = 'area' THEN g.name_bg END,
                CASE WHEN gp.level = 'area' THEN gp.name_bg END,
                CASE WHEN gpp.level = 'area' THEN gpp.name_bg END
            ) AS area,
            COALESCE(string_agg(DISTINCT f.name_bg, ', ' ORDER BY f.name_bg), '') AS features
        FROM listings l
        JOIN properties p ON p.property_id = l.property_id
        JOIN property_types pt ON pt.property_type_id = p.property_type_id
        JOIN contacts c ON c.contact_id = l.contact_id
        JOIN geographies g ON g.geo_id = p.geo_id
        LEFT JOIN geographies gp ON gp.geo_id = g.parent_id
        LEFT JOIN geographies gpp ON gpp.geo_id = gp.parent_id
        LEFT JOIN construction_types ct
          ON ct.construction_type_id = p.construction_type_id
        LEFT JOIN property_features pf ON pf.property_id = p.property_id
        LEFT JOIN features f ON f.feature_id = pf.feature_id
        WHERE l.status = 'active'
          AND l.transaction_type = %s
          AND NOT (l.source_id = ANY(%s::text[]))
        GROUP BY
            l.source_id, l.listing_id, l.listing_url, l.listing_tier, l.price,
            l.price_on_request, l.date_posted, l.date_modified, l.has_photos,
            l.scraped_at, l.date_last_checked, pt.name_bg, p.bedrooms,
            p.area_m2, p.floor, p.total_floors,
            ct.name_bg, p.construction_status, p.year_built, p.gas, p.tec,
            c.contact_type, c.name, c.phone,
            g.level, g.name_bg, g.locality_type,
            gp.level, gp.name_bg, gp.locality_type,
            gpp.level, gpp.name_bg, gpp.locality_type
        ORDER BY l.date_last_checked ASC, l.source_id ASC
        LIMIT %s
    """, (_TX_TO_ENUM[transaction_type], excluded, limit))
    rows = cursor.fetchall()
    cursor.close()

    logger.info(f"Fetched {len(rows):,} listings for Pass 2 rolling refresh.")

    result = []
    for row in rows:
        result.append({
            "source_id": row[0],
            "listing_id": row[1],
            "listing_url": row[2],
            "listing_tier": row[3],
            "price": "Цена при запитване" if row[5] else row[4],
            "date_posted": _date_to_raw(row[6], "Публикувана"),
            "date_modified": _date_to_raw(row[7], "Коригирана"),
            "has_photos": row[8],
            "scraped_at": row[9],
            "property_type": row[10],
            "bedrooms": row[11],
            "area_m2": row[12],
            "floor": (
                f"{row[13]} от {row[14]}" if row[13] is not None and row[14] is not None
                else row[13]
            ),
            "construction_type": row[15],
            "construction_status": _CONSTRUCTION_STATUS_TO_RAW.get(row[16], row[16]),
            "year_built": row[17],
            "gas": None if row[18] is None else ("ДА" if row[18] else "НЕ"),
            "tec": _TEC_TO_RAW.get(row[19], row[19]),
            "poster_type": _POSTER_TYPE_TO_RAW.get(row[20], row[20]),
            "agency_name": row[21],
            "agency_phone": row[22],
            "region": row[23],
            "locality": row[24],
            "locality_type": _LOCALITY_TYPE_TO_RAW.get(row[25], row[25]),
            "area": row[26],
            "features": row[27] or None,
            "transaction_type": transaction_type,
            "status": "active",
        })
    return result
