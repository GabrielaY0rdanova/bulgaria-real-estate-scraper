from datetime import datetime
from decimal import Decimal

from light_scraper import db


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.executions = []
        self._query_number = 0
        self.closed = False

    def execute(self, query, params):
        self.executions.append((query, params))
        self._query_number += 1

    def fetchone(self):
        return (1,)

    def fetchall(self):
        return [self.row]

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, row):
        self.cursor_instance = FakeCursor(row)

    def cursor(self):
        return self.cursor_instance


def test_pass2_query_excludes_pass1_ids_and_restores_raw_context():
    row = (
        "src-1", 42, "https://example.test/1", "VIP", Decimal("123000"), False,
        datetime(2026, 4, 2, 13, 36), datetime(2026, 4, 5, 15, 5), True,
        datetime(2026, 7, 1, 8, 0), "2-СТАЕН", 1, Decimal("68.50"), 3, 9,
        "Тухла", "completed", 2018, True, "district_heating", "agency",
        "Agency", "+359", "Пловдив", "Пловдив", "city", "Център",
        "Асансьор, Климатик",
    )
    conn = FakeConnection(row)

    listings = db.fetch_pass2_listings(conn, "prodazhbi", {"seen-2", "seen-1"})

    assert conn.cursor_instance.closed is True
    assert len(conn.cursor_instance.executions) == 2
    assert conn.cursor_instance.executions[0][1] == ("sale", ["seen-1", "seen-2"])
    assert conn.cursor_instance.executions[1][1] == ("sale", ["seen-1", "seen-2"], 1)
    assert "unknown" not in conn.cursor_instance.executions[1][0]
    assert listings == [{
        "source_id": "src-1",
        "listing_id": 42,
        "listing_url": "https://example.test/1",
        "listing_tier": "VIP",
        "price": Decimal("123000"),
        "date_posted": "Публикувана в 13:36 на 2 април, 2026 год.",
        "date_modified": "Коригирана в 15:05 на 5 април, 2026 год.",
        "has_photos": True,
        "scraped_at": datetime(2026, 7, 1, 8, 0),
        "property_type": "2-СТАЕН",
        "bedrooms": 1,
        "area_m2": Decimal("68.50"),
        "floor": "3 от 9",
        "construction_type": "Тухла",
        "construction_status": "Въведен в експлоатация",
        "year_built": 2018,
        "gas": "ДА",
        "tec": "ДА",
        "poster_type": "агенция",
        "agency_name": "Agency",
        "agency_phone": "+359",
        "region": "Пловдив",
        "locality": "Пловдив",
        "locality_type": "град",
        "area": "Център",
        "features": "Асансьор, Климатик",
        "transaction_type": "prodazhbi",
        "status": "active",
    }]


def test_pass2_price_on_request_and_nullable_values():
    row = (
        "src-2", 43, "https://example.test/2", None, None, True,
        None, None, False, datetime(2026, 7, 1), "КЪЩА", None, None, None, None,
        None, None, None, None, None, "owner", None, None,
        "София", "София", "city", None, "",
    )
    listing = db.fetch_pass2_listings(FakeConnection(row), "naemi")[0]

    assert listing["price"] == "Цена при запитване"
    assert listing["floor"] is None
    assert listing["gas"] is None
    assert listing["features"] is None
    assert listing["poster_type"] == "собственик"
    assert listing["transaction_type"] == "naemi"
