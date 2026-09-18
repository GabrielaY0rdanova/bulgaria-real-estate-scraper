from decimal import Decimal

from light_scraper.comparator import classify_listings


def test_reappeared_listing_keeps_database_listing_id():
    scraped = [{"source_id": "back", "price": "120", "listing_url": "url"}]

    result = classify_listings(
        scraped,
        active_in_db={},
        inactive_in_db={"back": {"listing_id": 42, "price": 100}},
    )

    assert result["reappeared"] == [{
        "source_id": "back",
        "price": "120",
        "listing_url": "url",
        "listing_id": 42,
        "old_price": 100,
    }]


def test_decimal_price_with_cents_is_not_a_false_change():
    result = classify_listings(
        [{"source_id": "same", "price": "44 993.69 €"}],
        active_in_db={
            "same": {"listing_id": 7, "price": Decimal("44993.69")}
        },
        inactive_in_db={},
    )

    assert result["changed"] == []
    assert [row["source_id"] for row in result["unchanged"]] == ["same"]
