import json
from datetime import datetime, timezone

import pytest

from light_scraper import run_state


def test_generate_run_id_is_utc_and_stable():
    value = datetime(2026, 9, 17, 8, 30, 45, tzinfo=timezone.utc)
    assert run_state.generate_run_id(value) == "20260917T083045Z"


def test_create_run_writes_manifest_for_both_transactions(tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="20260917T083045Z")

    assert run_dir.name == "20260917T083045Z"
    assert manifest["status"] == "running"
    assert manifest["expected_regions"] == 54
    assert set(manifest["transactions"]) == {"prodazhbi", "naemi"}
    assert run_state.load_manifest(run_dir) == manifest


def test_existing_run_directory_is_not_overwritten(tmp_path):
    run_state.create_run(tmp_path, run_id="same-run")

    with pytest.raises(FileExistsError):
        run_state.create_run(tmp_path, run_id="same-run")


def test_seen_ids_are_persisted_deduplicated_and_restored(tmp_path):
    run_dir, _manifest = run_state.create_run(tmp_path, run_id="run")
    store = run_state.SeenIdStore(run_dir, "prodazhbi")

    written = store.append(
        [{"source_id": "a"}, {"source_id": "b"}, {"source_id": "a"}, {"source_id": None}],
        "oblast-plovdiv",
        observed_at="2026-09-17T08:00:00+00:00",
    )
    second_write = store.append(
        [{"source_id": "b"}, {"source_id": "c"}],
        "grad-plovdiv",
        observed_at="2026-09-17T09:00:00+00:00",
    )
    restored = run_state.SeenIdStore(run_dir, "prodazhbi")

    assert written == 2
    assert second_write == 1
    assert restored.ids == {"a", "b", "c"}


def test_missing_is_allowed_only_after_every_region_completes(tmp_path):
    _run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    expected = manifest["transactions"]["prodazhbi"]["expected_regions"]

    for slug in expected[:-1]:
        run_state.mark_region_complete(manifest, "prodazhbi", slug)

    assert run_state.finish_pass1(manifest, "prodazhbi") is False
    assert run_state.can_compute_missing(manifest, "prodazhbi") is False

    run_state.mark_region_complete(manifest, "prodazhbi", expected[-1])

    assert run_state.finish_pass1(manifest, "prodazhbi") is True
    assert run_state.can_compute_missing(manifest, "prodazhbi") is True


def test_failure_disables_missing_even_when_all_regions_are_marked_complete(tmp_path):
    _run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    for slug in manifest["transactions"]["naemi"]["expected_regions"]:
        run_state.mark_region_complete(manifest, "naemi", slug)
    run_state.mark_unit_failed(manifest, "naemi", "grad-varna/page-3", "fetch_failed")

    assert run_state.finish_pass1(manifest, "naemi") is False
    assert run_state.can_compute_missing(manifest, "naemi") is False
    assert manifest["status"] == "partial"


def test_completed_transactions_can_finish_the_run(tmp_path):
    _run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    for transaction_type in run_state.TRANSACTIONS:
        state = manifest["transactions"][transaction_type]
        state["pass1_status"] = "complete"
        state["pass2_status"] = "complete"
        run_state.finish_transaction(manifest, transaction_type)

    run_state.finish_run(manifest)

    assert manifest["status"] == "complete"
    assert manifest["finished_at"] is not None


def test_run_cannot_finish_with_an_incomplete_transaction(tmp_path):
    _run_dir, manifest = run_state.create_run(tmp_path, run_id="run")

    with pytest.raises(ValueError, match="unfinished transactions"):
        run_state.finish_run(manifest)


def test_manifest_save_is_atomic_and_preserves_old_file_on_failure(monkeypatch, tmp_path):
    run_dir, manifest = run_state.create_run(tmp_path, run_id="run")
    original = run_state.manifest_path(run_dir).read_text(encoding="utf-8")
    manifest["status"] = "partial"
    monkeypatch.setattr(run_state.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("failed")))

    with pytest.raises(OSError, match="failed"):
        run_state.save_manifest(run_dir, manifest)

    assert run_state.manifest_path(run_dir).read_text(encoding="utf-8") == original
    assert not (run_dir / "manifest.json.tmp").exists()


def test_invalid_manifest_is_rejected(tmp_path):
    run_dir = tmp_path / "bad"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    with pytest.raises(ValueError):
        run_state.load_manifest(run_dir)


def test_index_store_persists_and_deduplicates_across_regions(tmp_path):
    run_dir, _manifest = run_state.create_run(tmp_path, run_id="run")
    store = run_state.IndexListingStore(run_dir, "prodazhbi")
    first = {"source_id": "a", "price": "100 €", "locality": "Пловдив"}
    second = {"source_id": "b", "price": "200 €", "locality": "Бургас"}

    assert store.append([first], "oblast-plovdiv", "oblast-plovdiv/page-1") == 1
    assert store.append([first, second], "grad-plovdiv", "grad-plovdiv/page-1") == 1

    restored = run_state.IndexListingStore(run_dir, "prodazhbi")
    assert restored.ids == {"a", "b"}
    assert restored.listing_rows() == [first, second]
    assert restored.rows_for_region("oblast-plovdiv") == [first]
    assert restored.rows_for_region("grad-plovdiv") == [second]


def test_index_store_rejects_corrupt_staging_file(tmp_path):
    run_dir, _manifest = run_state.create_run(tmp_path, run_id="run")
    path = run_dir / "naemi_pass1_index.jsonl"
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid staging JSON"):
        run_state.IndexListingStore(run_dir, "naemi")


def test_listing_row_store_keeps_latest_row_and_exports_once(tmp_path):
    run_dir, _manifest = run_state.create_run(tmp_path, run_id="run")
    store = run_state.ListingRowStore(run_dir, "prodazhbi")
    store.upsert([{"source_id": "a", "price": "100 €"}])
    store.upsert([{"source_id": "a", "price": "120 €"}, {"source_id": "b", "price": "80 €"}])

    restored = run_state.ListingRowStore(run_dir, "prodazhbi")
    output = restored.export_csv(["source_id", "price"])
    rows = output.read_text(encoding="utf-8-sig").splitlines()

    assert len(restored.rows) == 2
    assert restored.rows["a"]["listing"]["price"] == "120 €"
    assert rows == ["source_id,price", "a,120 €", "b,80 €"]


def test_action_store_preserves_primary_action_over_refresh(tmp_path):
    run_dir, _manifest = run_state.create_run(tmp_path, run_id="run")
    store = run_state.ActionStore(run_dir, "prodazhbi")
    store.upsert([{
        "source_id": "a",
        "listing_id": 1,
        "action": "changed",
        "old_price": 100,
        "new_price": 120,
        "observed_at": "2026-09-17T08:00:00+00:00",
    }])
    store.upsert([{
        "source_id": "a",
        "listing_id": 1,
        "action": "refreshed",
        "observed_at": "2026-09-17T09:00:00+00:00",
    }])

    restored = run_state.ActionStore(run_dir, "prodazhbi")
    output = restored.export_csv()

    assert restored.actions["a"]["action"] == "changed"
    assert "changed" in output.read_text(encoding="utf-8-sig")


def test_action_store_rejects_unknown_action(tmp_path):
    run_dir, _manifest = run_state.create_run(tmp_path, run_id="run")
    store = run_state.ActionStore(run_dir, "naemi")

    with pytest.raises(ValueError, match="Unsupported action"):
        store.upsert([{"source_id": "a", "action": "mystery"}])


def test_pass2_selection_is_fixed_deduplicated_and_reloadable(tmp_path):
    run_dir, _manifest = run_state.create_run(tmp_path, run_id="run")
    store = run_state.Pass2SelectionStore(run_dir, "prodazhbi")
    selected = store.create([
        {"source_id": "a", "listing_url": "url-a"},
        {"source_id": "b", "listing_url": "url-b"},
        {"source_id": "a", "listing_url": "duplicate"},
    ])

    assert [row["source_id"] for row in selected] == ["a", "b"]
    assert store.load() == selected

    with pytest.raises(FileExistsError):
        store.create([])


def test_pass2_selection_rejects_corrupt_or_duplicate_rows(tmp_path):
    run_dir, _manifest = run_state.create_run(tmp_path, run_id="run")
    store = run_state.Pass2SelectionStore(run_dir, "naemi")
    store.path.write_text('[{"source_id":"a"},{"source_id":"a"}]', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        store.load()
