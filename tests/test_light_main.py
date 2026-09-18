import config
import csv

from light_scraper import main as light_main
from light_scraper import run_state


REGION = {"region": "Пловдив", "slug": "oblast-plovdiv"}


class RecordingStore:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def append(self, *args, **kwargs):
        self.events.append(self.name)
        return 1


def test_page_data_is_durable_before_checkpoint(monkeypatch, tmp_path):
    _run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    events = []
    listing = {"source_id": "a"}

    responses = iter([[listing.copy()], []])
    monkeypatch.setattr(config, "MAX_PAGES", 2)
    monkeypatch.setattr(light_main, "fetch_page", lambda *_args, **_kwargs: "html")
    monkeypatch.setattr(light_main, "parse_listings_page", lambda *_args: next(responses))
    monkeypatch.setattr(light_main, "save_manifest", lambda *_args: events.append("manifest"))
    monkeypatch.setattr(light_main, "save_progress", lambda **_kwargs: events.append("checkpoint"))

    rows = light_main._scrape_index_pages(
        "prodazhbi",
        REGION,
        resume_page=None,
        progress_key="oblast-plovdiv",
        pass_number=1,
        output_path="rows.csv",
        seen_store=RecordingStore("seen", events),
        index_store=RecordingStore("index", events),
        manifest=manifest,
        run_dir=tmp_path / "run",
    )

    assert rows == [listing]
    assert events[:4] == ["seen", "index", "manifest", "checkpoint"]
    assert events[4:] == ["manifest"]  # valid empty page clears any old failure
    state = manifest["transactions"]["prodazhbi"]
    assert state["pages_attempted"] == 2
    assert state["pages_completed"] == 1


def test_first_index_page_reuses_preflight_html(monkeypatch, tmp_path):
    _run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    html = "preflight page one"
    parsed_html = []
    fetch_calls = []

    monkeypatch.setattr(config, "MAX_PAGES", 1)
    monkeypatch.setattr(
        light_main,
        "fetch_page",
        lambda *_args, **_kwargs: fetch_calls.append(True) or "unexpected",
    )
    monkeypatch.setattr(
        light_main,
        "parse_listings_page",
        lambda value, *_args: parsed_html.append(value) or [{"source_id": "a"}],
    )
    monkeypatch.setattr(light_main, "save_progress", lambda **_kwargs: None)

    light_main._scrape_index_pages(
        "prodazhbi", REGION, resume_page=None, progress_key="region",
        pass_number=1, output_path="rows.csv", manifest=manifest,
        run_dir=tmp_path / "run", initial_html=html,
    )

    assert parsed_html == [html]
    assert fetch_calls == []


def test_failed_fetch_marks_run_partial_and_saves_safe_resume_point(monkeypatch, tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    checkpoint_calls = []

    monkeypatch.setattr(config, "MAX_PAGES", 1)
    monkeypatch.setattr(light_main, "fetch_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(light_main, "save_progress", lambda **kwargs: checkpoint_calls.append(kwargs))

    rows = light_main._scrape_index_pages(
        "prodazhbi",
        REGION,
        resume_page=None,
        progress_key="oblast-plovdiv",
        pass_number=1,
        output_path="rows.csv",
        manifest=manifest,
        run_dir=run_dir,
    )

    assert rows == []
    assert len(checkpoint_calls) == 1
    assert checkpoint_calls[0]["page"] == 0
    assert manifest["status"] == "partial"
    assert manifest["transactions"]["prodazhbi"]["fetch_failures"] == 1
    assert run_state.can_compute_missing(manifest, "prodazhbi") is False


def test_successful_retry_clears_matching_fetch_failure(monkeypatch, tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    run_state.mark_unit_failed(
        manifest, "prodazhbi", "oblast-plovdiv/page-1", "fetch_failed"
    )
    responses = iter([[{"source_id": "a"}], []])

    monkeypatch.setattr(config, "MAX_PAGES", 2)
    monkeypatch.setattr(light_main, "fetch_page", lambda *_args, **_kwargs: "html")
    monkeypatch.setattr(light_main, "parse_listings_page", lambda *_args: next(responses))
    monkeypatch.setattr(light_main, "save_progress", lambda **_kwargs: None)

    light_main._scrape_index_pages(
        "prodazhbi", REGION, resume_page=0, progress_key="oblast-plovdiv",
        pass_number=1, output_path="rows.csv", manifest=manifest,
        run_dir=run_dir,
    )

    assert manifest["transactions"]["prodazhbi"]["failed_units"] == []


def test_empty_retry_page_also_clears_matching_fetch_failure(monkeypatch, tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    run_state.mark_unit_failed(
        manifest, "prodazhbi", "oblast-plovdiv/page-1", "fetch_failed"
    )

    monkeypatch.setattr(config, "MAX_PAGES", 1)
    monkeypatch.setattr(light_main, "fetch_page", lambda *_args, **_kwargs: "html")
    monkeypatch.setattr(light_main, "parse_listings_page", lambda *_args: [])
    monkeypatch.setattr(light_main, "save_progress", lambda **_kwargs: None)

    light_main._scrape_index_pages(
        "prodazhbi", REGION, resume_page=0, progress_key="oblast-plovdiv",
        pass_number=1, output_path="rows.csv", manifest=manifest,
        run_dir=run_dir,
    )

    assert manifest["transactions"]["prodazhbi"]["failed_units"] == []


def test_complete_pass1_records_missing_actions(monkeypatch, tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    manifest["transactions"]["prodazhbi"]["expected_regions"] = [REGION["slug"]]
    run_state.mark_region_complete(manifest, "prodazhbi", REGION["slug"])

    monkeypatch.setattr(light_main, "REGIONS", [REGION])

    result = light_main.run_pass1(
        "prodazhbi", str(run_dir / "prodazhbi_rows.csv"),
        active_in_db={"gone": {"listing_id": 77, "price": 100}},
        inactive_in_db={}, resume=None, run_dir=run_dir, manifest=manifest,
    )

    with (run_dir / "prodazhbi_actions.csv").open(encoding="utf-8-sig", newline="") as file:
        actions = list(csv.DictReader(file))

    assert result["missing"] == [{"source_id": "gone", "listing_id": 77}]
    assert len(actions) == 1
    assert actions[0]["source_id"] == "gone"
    assert actions[0]["action"] == "missing"


def test_failed_pass2_detail_is_not_saved_as_refreshed(monkeypatch, tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    selected = [{
        "source_id": "failed-detail",
        "listing_url": "https://example.test/failed",
        "property_type": "КЪЩА",
        "locality": "София",
    }]

    monkeypatch.setattr(
        light_main, "fetch_pass2_listings",
        lambda *_args, **_kwargs: [row.copy() for row in selected],
    )
    monkeypatch.setattr(
        light_main, "enrich_with_detail",
        lambda rows: [{**row, "_detail_fetch_ok": False} for row in rows],
    )
    monkeypatch.setattr(light_main, "save_progress_pass2", lambda *_args, **_kwargs: None)

    light_main.run_pass2(
        object(), str(run_dir / "prodazhbi_rows.csv"), "prodazhbi",
        run_dir=run_dir, manifest=manifest,
    )

    assert run_state.ListingRowStore(run_dir, "prodazhbi").rows == {}
    assert run_state.ActionStore(run_dir, "prodazhbi").actions == {}
    assert manifest["transactions"]["prodazhbi"]["pass2"]["rejected"] == 1


def test_resumed_pass2_repairs_row_without_matching_action(monkeypatch, tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    selected = [{
        "source_id": "interrupted-row",
        "listing_id": 17,
        "listing_url": "https://example.test/interrupted",
        "property_type": "КЪЩА",
        "locality": "София",
        "price": 150000,
        "scraped_at": "2026-09-17T08:00:00+00:00",
    }]
    run_state.Pass2SelectionStore(run_dir, "prodazhbi").create(selected)
    run_state.ListingRowStore(run_dir, "prodazhbi").upsert(selected)

    monkeypatch.setattr(light_main, "save_progress_pass2", lambda *_args, **_kwargs: None)

    light_main.run_pass2(
        object(), str(run_dir / "prodazhbi_rows.csv"), "prodazhbi",
        resume_index=1, run_dir=run_dir, manifest=manifest,
    )

    actions = run_state.ActionStore(run_dir, "prodazhbi").actions
    assert actions["interrupted-row"]["action"] == "refreshed"
    assert actions["interrupted-row"]["listing_id"] == 17
    assert actions["interrupted-row"]["old_price"] == 150000
    assert actions["interrupted-row"]["new_price"] == 150000


def test_nested_price_bucket_keeps_resume_page(monkeypatch):
    page_calls = []

    monkeypatch.setattr(
        light_main,
        "preflight_check",
        lambda _tx, _slug, **kwargs: (kwargs["price_min"], kwargs["price_max"]) == (0, 50),
    )
    monkeypatch.setattr(
        light_main,
        "_scrape_index_pages",
        lambda *args, **kwargs: page_calls.append(kwargs) or [],
    )

    light_main._scrape_index_price_buckets(
        "prodazhbi",
        REGION,
        "dvustaen",
        0,
        100,
        "rows.csv",
        resume_key="oblast-plovdiv:dvustaen:0-25",
        resume_page=7,
    )

    resumed = [call for call in page_calls if call["price_min"] == 0 and call["price_max"] == 25]
    assert len(resumed) == 1
    assert resumed[0]["resume_page"] == 7


def test_cascade_skips_completed_property_types_and_resumes_target(monkeypatch):
    page_calls = []
    preflight_types = []

    monkeypatch.setattr(light_main, "PROPERTY_TYPES", ["ednostaen", "dvustaen", "tristaen"])
    monkeypatch.setattr(
        light_main,
        "preflight_check",
        lambda _tx, _slug, **kwargs: preflight_types.append(kwargs.get("property_type")) or False,
    )
    monkeypatch.setattr(
        light_main,
        "_scrape_index_pages",
        lambda *args, **kwargs: page_calls.append(kwargs) or [],
    )

    light_main._scrape_index_pages_cascade(
        "prodazhbi",
        REGION,
        0,
        100,
        resume={
            "property_type": "dvustaen",
            "progress_key": None,
            "page": 5,
        },
        output_path="rows.csv",
    )

    assert preflight_types == ["ednostaen", "dvustaen", "tristaen"]
    assert [call["property_type"] for call in page_calls] == ["dvustaen", "tristaen"]
    assert page_calls[0]["resume_page"] == 5
    assert page_calls[1]["resume_page"] is None


def test_cascade_passes_resume_into_capped_property(monkeypatch):
    bucket_calls = []
    resume_key = "oblast-plovdiv:dvustaen:0-25"

    monkeypatch.setattr(light_main, "PROPERTY_TYPES", ["ednostaen", "dvustaen", "tristaen"])
    monkeypatch.setattr(light_main, "preflight_check", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(light_main, "_scrape_index_pages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        light_main,
        "_scrape_index_price_buckets",
        lambda *args, **kwargs: bucket_calls.append(kwargs) or [],
    )

    light_main._scrape_index_pages_cascade(
        "prodazhbi",
        REGION,
        0,
        100,
        resume={
            "property_type": "dvustaen",
            "progress_key": resume_key,
            "page": 6,
        },
        output_path="rows.csv",
    )

    assert len(bucket_calls) == 1
    assert bucket_calls[0]["property_type"] == "dvustaen"
    assert bucket_calls[0]["resume_key"] == resume_key
    assert bucket_calls[0]["resume_page"] == 6


def test_pass1_resume_is_not_reused_for_later_regions():
    resume = {
        "slug": "grad-burgas",
        "property_type": "garazh-parkomyasto",
        "page": 2,
    }

    assert light_main._resume_for_region(resume, "grad-burgas") == resume
    assert light_main._resume_for_region(resume, "grad-sofiya") is None


def test_pass1_output_is_deduplicated_and_completed_region_is_not_repeated(monkeypatch, tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    manifest["expected_regions"] = 1
    for state in manifest["transactions"].values():
        state["expected_regions"] = [REGION["slug"]]
    listing = {
        "source_id": "a",
        "listing_url": "https://example.test/a",
        "property_type": "2-СТАЕН",
        "locality": "Пловдив",
        "price": "100 €",
        "scraped_at": "2026-09-17T08:00:00+00:00",
    }
    responses = iter([[listing.copy()], []])

    monkeypatch.setattr(light_main, "REGIONS", [REGION])
    monkeypatch.setattr(config, "MAX_PAGES", 2)
    monkeypatch.setattr(light_main, "preflight_check", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(light_main, "fetch_page", lambda *_args, **_kwargs: "html")
    monkeypatch.setattr(light_main, "parse_listings_page", lambda *_args: next(responses))
    monkeypatch.setattr(light_main, "save_progress", lambda **_kwargs: None)
    monkeypatch.setattr(light_main, "enrich_with_detail", lambda rows: rows)
    monkeypatch.setattr(light_main, "filter_valid_listings", lambda rows: rows)

    first = light_main.run_pass1(
        "prodazhbi",
        str(run_dir / "prodazhbi_rows.csv"),
        active_in_db={},
        inactive_in_db={},
        resume=None,
        run_dir=run_dir,
        manifest=manifest,
    )
    second = light_main.run_pass1(
        "prodazhbi",
        str(run_dir / "prodazhbi_rows.csv"),
        active_in_db={},
        inactive_in_db={},
        resume={"slug": REGION["slug"], "page": 1},
        run_dir=run_dir,
        manifest=manifest,
    )

    with (run_dir / "prodazhbi_rows.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    with (run_dir / "prodazhbi_actions.csv").open(encoding="utf-8-sig", newline="") as file:
        actions = list(csv.DictReader(file))

    assert first["all_scraped_ids"] == {"a"}
    assert second["all_scraped_ids"] == {"a"}
    assert len(rows) == 1
    assert len(actions) == 1
    assert actions[0]["action"] == "new"


def test_pass2_selection_is_fixed_and_excludes_pass1_rows(monkeypatch, tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    pass1_row = {
        "source_id": "pass1-id",
        "listing_url": "https://example.test/pass1",
        "property_type": "2-СТАЕН",
        "locality": "Пловдив",
    }
    run_state.ListingRowStore(run_dir, "prodazhbi").upsert([pass1_row])

    selected = [{
        "source_id": "pass2-id",
        "listing_id": 10,
        "listing_url": "https://example.test/pass2",
        "property_type": "КЪЩА",
        "locality": "София",
        "price": 200000,
        "scraped_at": "2026-09-17T08:00:00+00:00",
    }]
    selections = []

    def fetch_selection(
        _conn,
        _transaction_type,
        exclude_ids=None,
        eligible_ids=None,
    ):
        selections.append((exclude_ids, eligible_ids))
        return [row.copy() for row in selected]

    monkeypatch.setattr(light_main, "fetch_pass2_listings", fetch_selection)
    monkeypatch.setattr(light_main, "enrich_with_detail", lambda rows: rows)
    monkeypatch.setattr(light_main, "is_valid_listing", lambda _row: True)
    monkeypatch.setattr(light_main, "save_progress_pass2", lambda *_args, **_kwargs: None)

    light_main.run_pass2(
        object(), str(run_dir / "prodazhbi_rows.csv"), "prodazhbi",
        run_dir=run_dir, manifest=manifest,
    )

    assert selections == [({"pass1-id"}, set())]
    assert run_state.Pass2SelectionStore(run_dir, "prodazhbi").load() == selected

    def fail_if_queried(*_args, **_kwargs):
        raise AssertionError("A resumed Pass 2 must reuse its fixed selection")

    monkeypatch.setattr(light_main, "fetch_pass2_listings", fail_if_queried)
    light_main.run_pass2(
        object(), str(run_dir / "prodazhbi_rows.csv"), "prodazhbi",
        resume_index=1, run_dir=run_dir, manifest=manifest,
    )

    with (run_dir / "prodazhbi_rows.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    with (run_dir / "prodazhbi_actions.csv").open(encoding="utf-8-sig", newline="") as file:
        actions = list(csv.DictReader(file))

    assert {row["source_id"] for row in rows} == {"pass1-id", "pass2-id"}
    assert len([row for row in actions if row["source_id"] == "pass2-id"]) == 1
    assert manifest["transactions"]["prodazhbi"]["pass2"] == {
        "selected": 1,
        "completed": 1,
        "saved": 1,
        "rejected": 0,
    }
