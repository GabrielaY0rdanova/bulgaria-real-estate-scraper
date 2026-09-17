import csv
from types import SimpleNamespace

import main
from scraper import fetcher, progress


def _large(html):
    return html + (" " * fetcher.MIN_PAGE_SIZE)


def test_one_listing_flows_from_html_to_csv_and_checkpoint(monkeypatch, tmp_path):
    listing_page = _large("""
    <div class="SearchInfoLine">1 обява</div>
    <div id="ida1a123" class="item BEST">
      <a class="title" href="https://www.imot.bg/obiava-test">
        Продава 2-СТАЕН<location>гр. Пловдив, Център</location>
      </a>
      <div class="price"><div>120 000 €<br>234 699.60 лв.</div></div>
      <div class="info">70 кв.м, 3-ти ет. от 6, тел.: 0888 111 222</div>
      <div class="seller"><div class="name"><a>Пример Имоти</a></div></div>
      <a class="photos"><strong>и 5 снимки</strong></a>
    </div>
    """)
    empty_page = _large('<div class="SearchInfoLine">0 обяви</div>')
    detail_page = _large("""
    <div class="adPrice"><div class="info"><div>Публикувана в 10:00 на 17 септември, 2026 год.</div></div></div>
    <div class="adParams">
      <div>Газ:<strong>НЕ</strong></div>
      <div>ТEЦ:<strong>ДА</strong></div>
      <div>Строителство:<strong>Тухла, </strong>Въведен в експлоатация <strong>2020 г.</strong></div>
    </div>
    """)

    def fake_get(url, headers, timeout):
        if "obiava-test" in url:
            html = detail_page
        elif "/p-2" in url:
            html = empty_page
        else:
            html = listing_page
        return SimpleNamespace(status_code=200, text=html, encoding=None)

    output = tmp_path / "prodazhbi.csv"
    progress_file = tmp_path / "progress.json"
    main.write_header(output)
    monkeypatch.setattr(main, "MAX_PAGES", 2)
    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(progress_file))

    hit_cap = main.scrape_pages(
        "prodazhbi",
        {"region": "Пловдив", "slug": "oblast-plovdiv"},
        str(output),
    )

    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    assert hit_cap is False
    assert len(rows) == 1
    assert rows[0]["source_id"] == "1a123"
    assert rows[0]["locality"] == "Пловдив"
    assert rows[0]["area"] == "Център"
    assert rows[0]["year_built"] == "2020"
    assert rows[0]["agency_phone"] == "0888111222"
    assert progress.load_progress()["page"] == 1
