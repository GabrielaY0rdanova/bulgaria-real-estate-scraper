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
