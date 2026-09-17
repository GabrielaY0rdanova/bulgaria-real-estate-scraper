from datetime import datetime

from bs4 import BeautifulSoup

from scraper.parser import (
    _extract_area,
    _extract_construction_type,
    _extract_floor,
    _extract_phone,
    _extract_year,
    is_capped,
    parse_listing,
    parse_listings_page,
)


REGION = {"region": "Пловдив", "slug": "oblast-plovdiv"}


def _listing_tag(html):
    return BeautifulSoup(html, "html.parser").find("div", id=lambda value: value and value.startswith("ida"))


def test_is_capped_detects_1000_plus_only_in_search_info():
    assert is_capped('<div class="SearchInfoLine">1 - 40 от общо 1000+ обяви</div>') is True
    assert is_capped('<div class="SearchInfoLine">1 - 40 от общо 843 обяви</div>') is False
    assert is_capped('<div>1000+ обяви</div>') is False


def test_parse_complete_agency_listing():
    html = """
    <div id="ida1a177330175223978" class="item BEST">
      <a class="title" href="//www.imot.bg/obiava-123">
        Продава 2-СТАЕН<location>гр. Пловдив, Център</location>
      </a>
      <div class="price"><div>89 990 €<br>176 005.14 лв.</div></div>
      <div class="info">75 кв.м, 2-ри ет. от 6, тел.: 089 766 9075</div>
      <div class="seller"><div class="name"><a>Пример Имоти</a></div></div>
      <a class="photos"><strong>и 8 снимки</strong></a>
    </div>
    """

    result = parse_listing(_listing_tag(html), REGION, "prodazhbi")

    assert result["source_id"] == "1a177330175223978"
    assert result["listing_tier"] == "BEST"
    assert result["listing_url"] == "https://www.imot.bg/obiava-123"
    assert result["property_type"] == "2-СТАЕН"
    assert result["region"] == "Пловдив"
    assert result["locality"] == "Пловдив"
    assert result["locality_type"] == "град"
    assert result["area"] == "Център"
    assert result["price"] == "89 990 €"
    assert result["area_m2"] == 75.0
    assert result["floor"] == "2 от 6"
    assert result["poster_type"] == "агенция"
    assert result["agency_name"] == "Пример Имоти"
    assert result["agency_phone"] == "0897669075"
    assert result["has_photos"] is True
    assert result["transaction_type"] == "prodazhbi"
    assert result["status"] == "active"
    assert datetime.fromisoformat(result["scraped_at"]).tzinfo is not None


def test_private_listing_does_not_store_phone():
    html = """
    <div id="ida-private" class="item">
      <a class="title" href="https://www.imot.bg/obiava-private">
        Дава под наем 1-СТАЕН<location>град София, област София</location>
      </a>
      <div class="info">40 кв.м, тел.: 0888 111 222</div>
      <a class="photos"></a>
    </div>
    """

    result = parse_listing(_listing_tag(html), {"region": "София", "slug": "sofiya"}, "naemi")

    assert result["property_type"] == "1-СТАЕН"
    assert result["locality"] == "София"
    assert result["area"] is None
    assert result["poster_type"] == "собственик"
    assert result["agency_name"] is None
    assert result["agency_phone"] is None
    assert result["has_photos"] is False


def test_second_settlement_replaces_the_first_locality():
    html = """
    <div id="ida-village" class="item TOP">
      <a class="title">Продава КЪЩА<location>гр. Пловдив, с. Марково</location></a>
    </div>
    """

    result = parse_listing(_listing_tag(html), REGION, "prodazhbi")

    assert result["locality_type"] == "село"
    assert result["locality"] == "Марково"
    assert result["area"] is None


def test_parse_listings_page_returns_each_listing():
    html = """
    <div id="idaone" class="item"><a class="title">Продава ГАРАЖ<location>гр. Пловдив</location></a></div>
    <div id="unrelated"></div>
    <div id="idatwo" class="item"><a class="title">Продава МАГАЗИН<location>гр. Пловдив</location></a></div>
    """

    results = parse_listings_page(html, REGION, "prodazhbi")

    assert [item["source_id"] for item in results] == ["one", "two"]
    assert [item["property_type"] for item in results] == ["ГАРАЖ", "МАГАЗИН"]


def test_parse_listings_page_returns_empty_list_when_no_listings_exist():
    assert parse_listings_page("<html></html>", REGION, "prodazhbi") == []


def test_field_helpers_handle_present_and_missing_values():
    assert _extract_area("123.5 кв.м") == 123.5
    assert _extract_area("без посочена площ") is None
    assert _extract_floor("4-ти ет. от 11") == "4 от 11"
    assert _extract_floor("партер") is None
    assert _extract_year("въведен в експлоатация 2020 г.") == 2020
    assert _extract_year("годината не е посочена") is None
    assert _extract_construction_type("тип ЕПК") == "Епк"
    assert _extract_construction_type("типът не е посочен") is None
    assert _extract_phone("тел.: 0883 66 84 66") == "0883668466"
    assert _extract_phone("без телефон") is None
