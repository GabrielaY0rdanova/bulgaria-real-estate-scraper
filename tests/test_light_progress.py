import json

import pytest

from light_scraper import progress


def test_pass1_progress_round_trip_includes_run_identity(monkeypatch, tmp_path):
    path = tmp_path / "progress.json"
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(path))

    progress.save_progress(
        pass_number=1,
        slug="oblast-plovdiv",
        page=4,
        output_path="rows.csv",
        transaction_type="prodazhbi",
        run_id="20260917T080000Z",
        run_dir="data/runs/20260917T080000Z",
        progress_key="oblast-plovdiv",
    )

    loaded = progress.load_progress()
    assert loaded["run_id"] == "20260917T080000Z"
    assert loaded["run_dir"] == "data/runs/20260917T080000Z"
    assert loaded["page"] == 4


def test_pass2_progress_round_trip(monkeypatch, tmp_path):
    path = tmp_path / "progress.json"
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(path))

    progress.save_progress_pass2(
        200,
        "rows.csv",
        "naemi",
        "run-id",
        "data/runs/run-id",
    )

    assert progress.load_progress()["pass2_index"] == 200


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"pass_number": 1, "slug": "x", "page": 1, "output_path": "x", "transaction_type": "prodazhbi"},
        {"pass_number": 3, "output_path": "x", "transaction_type": "prodazhbi", "run_id": "r", "run_dir": "d"},
        [],
    ],
)
def test_unsafe_or_legacy_progress_is_rejected(monkeypatch, tmp_path, value):
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(path))

    assert progress.load_progress() is None


def test_failed_replace_preserves_previous_progress(monkeypatch, tmp_path):
    path = tmp_path / "progress.json"
    original = {"old": True}
    path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(progress, "PROGRESS_FILE", str(path))
    monkeypatch.setattr(progress.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("failed")))

    with pytest.raises(OSError, match="failed"):
        progress.save_progress(
            1, "oblast-plovdiv", 1, "rows.csv", "prodazhbi",
            "run-id", "data/runs/run-id",
        )

    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert not (tmp_path / "progress.json.tmp").exists()
