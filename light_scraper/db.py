# =============================================================================
# real_estate_scraper — Light Scraper — DB
# Purpose: Database connection and queries for the light scraper.
#          Provides active/inactive listing lookups for Pass 1 comparison
#          and oldest-checked listing fetch for Pass 2 rolling refresh.
# =============================================================================

import os
import logging
import psycopg2
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# Map scraper transaction type slugs to DB enum values
_TX_TO_ENUM = {
    "prodazhbi": "sale",
    "naemi":     "rental",
}


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


def fetch_pass2_listings(conn, transaction_type: str) -> list[dict]:
    """
    Fetch the oldest 10% of active listings by date_last_checked.
    Used by Pass 2 rolling detail refresh.
    Returns a list of dicts with listing_url and source_id.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT CEIL(COUNT(*) * 0.05)::int
        FROM listings
        WHERE status = 'active'
          AND transaction_type = %s
    """, (_TX_TO_ENUM[transaction_type],))
    limit = cursor.fetchone()[0]

    cursor.execute("""
        SELECT source_id, listing_url
        FROM listings
        WHERE status = 'active'
          AND transaction_type = %s
        ORDER BY date_last_checked ASC
        LIMIT %s
    """, (_TX_TO_ENUM[transaction_type], limit))
    rows = cursor.fetchall()
    cursor.close()

    logger.info(f"Fetched {len(rows):,} listings for Pass 2 rolling refresh.")

    return [{"source_id": row[0], "listing_url": row[1], "property_type": "unknown", "locality": "unknown"} for row in rows]