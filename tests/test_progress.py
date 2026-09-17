import json

from scraper import progress


def test_save_and_load_progress_round_trip(monkeypatch, tmp_path):
    progress_file = tmp_path / "progress.json"
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(progress_file))

    progress.save_progress(
        "prodazhbi",
        "oblast-plovdiv",
        7,
        "data/prodazhbi.csv",
        property_type="dvustaen",
        price_min=100_000,
        price_max=200_000,
        progress_key="oblast-plovdiv:dvustaen:100000-200000",
    )

    assert progress.load_progress() == {
        "transaction_type": "prodazhbi",
        "slug": "oblast-plovdiv",
        "page": 7,
        "output_path": "data/prodazhbi.csv",
        "property_type": "dvustaen",
        "price_min": 100_000,
        "price_max": 200_000,
        "progress_key": "oblast-plovdiv:dvustaen:100000-200000",
    }


def test_load_progress_returns_none_for_missing_or_invalid_file(monkeypatch, tmp_path):
    progress_file = tmp_path / "progress.json"
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(progress_file))

    assert progress.load_progress() is None

    progress_file.write_text("not valid json", encoding="utf-8")
    assert progress.load_progress() is None


def test_load_progress_rejects_valid_json_with_missing_required_fields(monkeypatch, tmp_path):
    progress_file = tmp_path / "progress.json"
    progress_file.write_text(json.dumps({"page": 3}), encoding="utf-8")
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(progress_file))

    assert progress.load_progress() is None


def test_failed_atomic_replace_preserves_previous_checkpoint(monkeypatch, tmp_path):
    progress_file = tmp_path / "progress.json"
    original = {"transaction_type": "prodazhbi", "slug": "old", "page": 2, "output_path": "old.csv"}
    progress_file.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(progress_file))
    monkeypatch.setattr(progress.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))

    progress.save_progress("prodazhbi", "new", 3, "new.csv")

    assert json.loads(progress_file.read_text(encoding="utf-8")) == original
    assert (tmp_path / "progress.json.tmp").exists() is False


def test_clear_progress_removes_existing_file(monkeypatch, tmp_path):
    progress_file = tmp_path / "progress.json"
    progress_file.write_text(json.dumps({"page": 1}), encoding="utf-8")
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(progress_file))

    progress.clear_progress()

    assert progress_file.exists() is False
