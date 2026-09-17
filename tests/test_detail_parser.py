from scraper.detail_parser import parse_detail_page


EXPECTED_KEYS = {
    "construction_type", "construction_status", "year_built", "gas",
    "tec", "features", "date_posted", "date_modified",
}


def test_parse_complete_detail_page():
    html = """
    <div class="adPrice"><div class="info">
      <div>Публикувана в 14:54 на 23 март, 2026 год.</div>
    </div></div>
    <div class="adParams">
      <div>Газ:<strong>ДА</strong></div>
      <div>ТEЦ:<strong>НЕ</strong></div>
      <div>Строителство:<strong>Тухла, </strong>Въведен в експлоатация <strong>2020 г.</strong></div>
    </div>
    <div class="carExtri"><div class="items">
      <div>Тухла</div><div>Асансьор</div><div>Обзаведен</div>
    </div></div>
    """

    assert parse_detail_page(html) == {
        "construction_type": "Тухла",
        "construction_status": "Въведен в експлоатация",
        "year_built": 2020,
        "gas": "ДА",
        "tec": "НЕ",
        "features": "Асансьор, Обзаведен",
        "date_posted": "Публикувана в 14:54 на 23 март, 2026 год.",
        "date_modified": None,
    }


def test_modified_date_is_stored_separately():
    html = """
    <div class="adPrice"><div class="info">
      <div>Коригирана в 10:10 на 24 март, 2026 год.</div>
    </div></div>
    """
    result = parse_detail_page(html)

    assert result["date_posted"] is None
    assert result["date_modified"] == "Коригирана в 10:10 на 24 март, 2026 год."


def test_empty_page_returns_all_fields_with_none_values():
    result = parse_detail_page("<html><body></body></html>")

    assert set(result) == EXPECTED_KEYS
    assert all(value is None for value in result.values())


def test_empty_year_and_only_construction_feature_are_handled():
    html = """
    <div class="adParams">
      <div>Строителство:<strong>Панел, </strong><strong></strong></div>
    </div>
    <div class="carExtri"><div class="items">
      <div>Панел</div><div>   </div>
    </div></div>
    """
    result = parse_detail_page(html)

    assert result["construction_type"] == "Панел"
    assert result["construction_status"] is None
    assert result["year_built"] is None
    assert result["features"] is None
