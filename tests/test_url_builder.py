"""Unit tests for scraper.url_builder."""

import pytest

from scraper.url_builder import build_listings_url


@pytest.mark.parametrize("page", [None, 1])
def test_first_page_has_no_page_suffix(page):
    url = build_listings_url(
        "prodazhbi",
        "oblast-blagoevgrad",
        page=page,
    )

    assert url == "https://www.imot.bg/obiavi/prodazhbi/oblast-blagoevgrad"


def test_later_page_has_page_suffix():
    url = build_listings_url(
        "naemi",
        "grad-sofiya",
        page=5,
    )

    assert url == "https://www.imot.bg/obiavi/naemi/grad-sofiya/p-5"


def test_property_type_is_added_to_path():
    url = build_listings_url(
        "prodazhbi",
        "grad-sofiya",
        property_type="dvustaen",
    )

    assert url == "https://www.imot.bg/obiavi/prodazhbi/grad-sofiya/dvustaen"


def test_price_range_is_added_as_query_string():
    url = build_listings_url(
        "prodazhbi",
        "grad-sofiya",
        price_min=0,
        price_max=100_000,
    )

    assert url == (
        "https://www.imot.bg/obiavi/prodazhbi/grad-sofiya"
        "?price_min=0&price_max=100000"
    )


def test_full_cascade_url_combines_path_page_and_price_range():
    url = build_listings_url(
        "prodazhbi",
        "oblast-varna",
        page=3,
        property_type="tristaen",
        price_min=120_000,
        price_max=240_000,
    )

    assert url == (
        "https://www.imot.bg/obiavi/prodazhbi/oblast-varna/tristaen/p-3"
        "?price_min=120000&price_max=240000"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"transaction_type": "unknown", "slug": "oblast-varna"},
        {"transaction_type": "prodazhbi", "slug": ""},
        {"transaction_type": "prodazhbi", "slug": "oblast-varna", "page": 0},
        {"transaction_type": "prodazhbi", "slug": "oblast-varna", "price_min": 10},
        {"transaction_type": "prodazhbi", "slug": "oblast-varna", "price_min": 20, "price_max": 10},
        {"transaction_type": "prodazhbi", "slug": "oblast-varna", "price_min": -1, "price_max": 10},
    ],
)
def test_invalid_url_parameters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        build_listings_url(**kwargs)
