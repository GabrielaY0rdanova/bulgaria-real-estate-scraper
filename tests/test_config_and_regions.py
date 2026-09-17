from config import (
    NAEMI_PRICE_MAX,
    NAEMI_PRICE_MIN,
    PROPERTY_TYPES,
    PRODAZHBI_PRICE_MAX,
    PRODAZHBI_PRICE_MIN,
    TRANSACTION_TYPES,
)
from regions import REGIONS


def test_regions_have_unique_non_empty_slugs_and_names():
    slugs = [entry["slug"] for entry in REGIONS]

    assert len(REGIONS) == 54
    assert len(slugs) == len(set(slugs))
    assert all(entry["region"].strip() for entry in REGIONS)
    assert all(slug.startswith(("oblast-", "grad-")) for slug in slugs)


def test_each_administrative_region_has_matching_city_entry():
    oblast_names = {entry["region"] for entry in REGIONS if entry["slug"].startswith("oblast-")}
    city_names = {entry["region"] for entry in REGIONS if entry["slug"].startswith("grad-")}

    assert oblast_names == city_names
    assert len(oblast_names) == 27


def test_configured_transaction_and_property_types_are_unique():
    assert TRANSACTION_TYPES == ["prodazhbi", "naemi"]
    assert len(PROPERTY_TYPES) == len(set(PROPERTY_TYPES))
    assert all(value and value == value.strip() for value in PROPERTY_TYPES)


def test_price_ranges_are_non_negative_and_ordered():
    assert 0 <= PRODAZHBI_PRICE_MIN < PRODAZHBI_PRICE_MAX
    assert 0 <= NAEMI_PRICE_MIN < NAEMI_PRICE_MAX
