import csv

import main


REGION = {"country": "bulgaria", "region": "Пловдив", "slug": "oblast-plovdiv"}


def _listing(source_id="listing-1"):
    return {
        "source_id": source_id,
        "listing_url": f"https://example.test/{source_id}",
        "property_type": "2-СТАЕН",
        "locality": "Пловдив",
    }


def test_csv_header_and_rows_are_written_in_declared_order(tmp_path):
    output = tmp_path / "listings.csv"
    main.write_header(output)
    main.append_listings(output, [_listing()])

    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    assert rows[0]["source_id"] == "listing-1"
    assert rows[0]["property_type"] == "2-СТАЕН"
    assert list(rows[0]) == main.CSV_COLUMNS


def test_scrape_page_enriches_saves_then_records_checkpoint(monkeypatch, tmp_path):
    output = tmp_path / "listings.csv"
    events = []
    listing = _listing()

    monkeypatch.setattr(main, "MAX_PAGES", 1)
    monkeypatch.setattr(main, "build_listings_url", lambda *_args, **_kwargs: "https://example.test/page-1")
    monkeypatch.setattr(main, "fetch_page", lambda url, **_kwargs: "detail" if "listing-1" in url else "listings")
    monkeypatch.setattr(main, "parse_listings_page", lambda *_args: [listing])
    monkeypatch.setattr(main, "parse_detail_page", lambda _html: {"year_built": 2020})
    monkeypatch.setattr(main, "filter_valid_listings", lambda listings: listings)
    monkeypatch.setattr(main, "append_listings", lambda path, rows: events.append(("saved", path, rows.copy())))
    monkeypatch.setattr(main, "save_progress", lambda *args, **kwargs: events.append(("checkpoint", args, kwargs)))

    hit_cap = main.scrape_pages(
        "prodazhbi",
        REGION,
        str(output),
        property_type="dvustaen",
        resume_page=3,
        progress_key="bucket-key",
    )

    assert hit_cap is False
    assert listing["year_built"] == 2020
    assert [event[0] for event in events] == ["saved", "checkpoint"]
    assert events[1][1][2] == 4
    assert events[1][2]["progress_key"] == "bucket-key"


def test_failed_page_is_not_saved_or_checkpointed(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MAX_PAGES", 1)
    monkeypatch.setattr(main, "build_listings_url", lambda *_args, **_kwargs: "https://example.test/page")
    monkeypatch.setattr(main, "fetch_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "append_listings", lambda *_args: (_ for _ in ()).throw(AssertionError("must not save")))
    monkeypatch.setattr(main, "save_progress", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not checkpoint")))

    assert main.scrape_pages("prodazhbi", REGION, str(tmp_path / "out.csv")) is False


def test_full_last_page_reports_possible_cap(monkeypatch, tmp_path):
    listings = [_listing(str(index)) for index in range(40)]

    monkeypatch.setattr(main, "MAX_PAGES", 1)
    monkeypatch.setattr(main, "build_listings_url", lambda *_args, **_kwargs: "https://example.test/page")
    monkeypatch.setattr(main, "fetch_page", lambda *_args, **_kwargs: "html")
    monkeypatch.setattr(main, "parse_listings_page", lambda *_args: listings)
    monkeypatch.setattr(main, "parse_detail_page", lambda _html: {})
    monkeypatch.setattr(main, "filter_valid_listings", lambda rows: rows)
    monkeypatch.setattr(main, "append_listings", lambda *_args: None)
    monkeypatch.setattr(main, "save_progress", lambda *_args, **_kwargs: None)

    assert main.scrape_pages("prodazhbi", REGION, str(tmp_path / "out.csv")) is True


def test_empty_results_end_without_checkpoint(monkeypatch, tmp_path):
    checkpoint_calls = []

    monkeypatch.setattr(main, "MAX_PAGES", 1)
    monkeypatch.setattr(main, "build_listings_url", lambda *_args, **_kwargs: "https://example.test/page")
    monkeypatch.setattr(main, "fetch_page", lambda *_args, **_kwargs: "html")
    monkeypatch.setattr(main, "parse_listings_page", lambda *_args: [])
    monkeypatch.setattr(main, "save_progress", lambda *_args, **_kwargs: checkpoint_calls.append(True))

    assert main.scrape_pages("prodazhbi", REGION, str(tmp_path / "out.csv")) is False
    assert checkpoint_calls == []


def test_scrape_page_logs_structured_page_and_unit_summaries(monkeypatch, tmp_path, caplog):
    listing = _listing()

    monkeypatch.setattr(main, "MAX_PAGES", 1)
    monkeypatch.setattr(main, "build_listings_url", lambda *_args, **_kwargs: "https://example.test/page")
    monkeypatch.setattr(main, "fetch_page", lambda url, **_kwargs: None if "listing-1" in url else "html")
    monkeypatch.setattr(main, "parse_listings_page", lambda *_args: [listing])
    monkeypatch.setattr(main, "filter_valid_listings", lambda rows: rows)
    monkeypatch.setattr(main, "append_listings", lambda *_args: None)
    monkeypatch.setattr(main, "save_progress", lambda *_args, **_kwargs: None)

    with caplog.at_level("INFO"):
        main.scrape_pages("prodazhbi", REGION, str(tmp_path / "out.csv"))

    assert "PAGE_SUMMARY" in caplog.text
    assert "found=1 saved=1 rejected=0 detail_failed=1" in caplog.text
    assert "SCRAPE_UNIT_SUMMARY" in caplog.text
    assert "stop_reason=max_pages_reached" in caplog.text


def test_main_resumes_current_region_then_starts_following_region_fresh(monkeypatch, tmp_path):
    regions = [
        {"region": "A", "slug": "region-a"},
        {"region": "B", "slug": "region-b"},
        {"region": "C", "slug": "region-c"},
    ]
    output = tmp_path / "existing.csv"
    output.write_text("header\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "TRANSACTION_TYPES", ["prodazhbi"])
    monkeypatch.setattr(main, "REGIONS", regions)
    monkeypatch.setattr(main, "load_progress", lambda: {
        "transaction_type": "prodazhbi",
        "slug": "region-b",
        "page": 3,
        "output_path": str(output),
        "property_type": None,
        "progress_key": "region-b",
    })
    monkeypatch.setattr(main, "scrape_region", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(main, "clear_progress", lambda: None)

    main.main()

    assert [call[0][1]["slug"] for call in calls] == ["region-b", "region-c"]
    assert calls[0][1]["resume_page"] == 3
    assert calls[1][1]["resume_page"] is None


def test_nested_price_bucket_keeps_resume_page(monkeypatch, tmp_path):
    scrape_calls = []

    def fake_preflight(_transaction_type, _slug, property_type=None, price_min=None, price_max=None):
        return (price_min, price_max) == (0, 50)

    def fake_scrape_pages(*args, **kwargs):
        scrape_calls.append(kwargs)
        return False

    monkeypatch.setattr(main, "preflight_check", fake_preflight)
    monkeypatch.setattr(main, "scrape_pages", fake_scrape_pages)

    main.scrape_price_buckets(
        "prodazhbi",
        REGION,
        str(tmp_path / "out.csv"),
        property_type="dvustaen",
        price_min=0,
        price_max=100,
        resume_key="oblast-plovdiv:dvustaen:0-25",
        resume_page=7,
    )

    resumed = [call for call in scrape_calls if call["price_min"] == 0 and call["price_max"] == 25]
    assert len(resumed) == 1
    assert resumed[0]["resume_page"] == 7


def test_region_resume_skips_completed_property_types(monkeypatch, tmp_path):
    page_calls = []
    preflight_calls = []

    monkeypatch.setattr(main, "PROPERTY_TYPES", ["ednostaen", "dvustaen", "tristaen"])
    monkeypatch.setattr(
        main,
        "preflight_check",
        lambda *args, **kwargs: preflight_calls.append(kwargs.get("property_type")) or False,
    )
    monkeypatch.setattr(main, "scrape_pages", lambda *args, **kwargs: page_calls.append(kwargs) or False)

    main.scrape_region(
        "prodazhbi",
        REGION,
        str(tmp_path / "out.csv"),
        resume_page=4,
        resume_property_type="dvustaen",
        resume_price_key=None,
    )

    assert preflight_calls == ["ednostaen", "dvustaen", "tristaen"]
    assert [call["property_type"] for call in page_calls] == ["dvustaen", "tristaen"]
    assert page_calls[0]["resume_page"] == 4
    assert page_calls[1]["resume_page"] is None


def test_region_passes_page_to_resumed_price_bucket(monkeypatch, tmp_path):
    bucket_calls = []
    preflight_calls = []
    resume_key = "oblast-plovdiv:dvustaen:0-2500000"

    monkeypatch.setattr(main, "PROPERTY_TYPES", ["ednostaen", "dvustaen", "tristaen"])
    monkeypatch.setattr(
        main,
        "preflight_check",
        lambda *args, **kwargs: preflight_calls.append(kwargs.get("property_type")) or False,
    )
    monkeypatch.setattr(main, "scrape_pages", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        main,
        "scrape_price_buckets",
        lambda *args, **kwargs: bucket_calls.append(kwargs),
    )

    main.scrape_region(
        "prodazhbi",
        REGION,
        str(tmp_path / "out.csv"),
        resume_page=6,
        resume_property_type="dvustaen",
        resume_price_key=resume_key,
    )

    assert "dvustaen" not in preflight_calls
    assert len(bucket_calls) == 1
    assert bucket_calls[0]["property_type"] == "dvustaen"
    assert bucket_calls[0]["resume_key"] == resume_key
    assert bucket_calls[0]["resume_page"] == 6
