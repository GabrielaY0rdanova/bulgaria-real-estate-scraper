import pytest

from scraper.validator import filter_valid_listings, is_valid_listing


def _valid_listing():
    return {
        "source_id": "1a123",
        "listing_url": "https://www.imot.bg/obiava-123",
        "property_type": "2-СТАЕН",
        "locality": "Пловдив",
        "area_m2": 70.0,
        "year_built": 2020,
    }


def test_valid_listing_is_accepted():
    assert is_valid_listing(_valid_listing()) is True


@pytest.mark.parametrize("field", ["source_id", "listing_url", "property_type", "locality"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_required_field_is_rejected(field, value):
    listing = _valid_listing()
    listing[field] = value

    assert is_valid_listing(listing) is False


@pytest.mark.parametrize("field,value", [("area_m2", 0), ("area_m2", -5), ("year_built", 0), ("year_built", -1)])
def test_non_positive_numeric_values_are_rejected(field, value):
    listing = _valid_listing()
    listing[field] = value

    assert is_valid_listing(listing) is False


def test_filter_keeps_only_valid_rows_without_mutating_input():
    valid = _valid_listing()
    invalid = {**_valid_listing(), "source_id": None}
    rows = [valid, invalid]

    assert filter_valid_listings(rows) == [valid]
    assert rows == [valid, invalid]
