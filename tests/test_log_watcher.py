import json
import os

import pytest

from monitoring import log_watcher


def test_log_patterns_match_current_scraper_messages():
    transaction = "2026-09-16 10:00:01 [INFO] main — ===== Transaction type: prodazhbi ====="
    region_number = "2026-09-16 10:00:02 [INFO] main — Region 2/54: Бургас (oblast-burgas)"
    region = "2026-09-16 10:00:03 [INFO] main — ===== Region: Бургас (oblast-burgas) | prodazhbi ====="
    scraping = "2026-09-16 10:00:04 [INFO] main — --- Scraping: oblast-burgas/dvustaen [0-500000] | prodazhbi | from page 4 ---"
    saved = "2026-09-16 10:00:05 [INFO] main — Saved 38 valid listings from page 4."

    assert log_watcher._TRANSACTION_PATTERN.search(transaction).group(1) == "prodazhbi"
    assert log_watcher._REGION_NUM_PATTERN.search(region_number).groups()[:2] == ("2", "54")
    assert log_watcher._REGION_PATTERN.search(region).groups() == ("Бургас", "oblast-burgas")
    assert log_watcher._SCRAPING_START_PATTERN.search(scraping).group(1) == "dvustaen"
    assert log_watcher._SAVED_LISTINGS_PATTERN.search(saved).groups() == ("38", "4")


def test_timestamp_and_duration_helpers():
    line = "2026-09-16 10:11:12 [INFO] main — message"

    assert log_watcher.parse_log_timestamp(line) is not None
    assert log_watcher.extract_time_str(line) == "10:11:12"
    assert log_watcher.parse_log_timestamp("invalid") is None
    assert log_watcher.fmt_duration(42) == "42s"
    assert log_watcher.fmt_duration(125) == "2m 5s"
    assert log_watcher.fmt_duration(3723) == "1h 2m 3s"


def test_expected_operational_warnings_are_suppressed():
    assert log_watcher.format_error_bullet("Fetch returned None on page 3 for region. Stopping.") is None
    assert log_watcher.format_error_bullet("Cap likely hit for region — will split further.") is None
    assert log_watcher.format_error_bullet("Soft-block suspected: expected listings content") is None
    assert log_watcher.format_error_bullet("Bucket [0-10] hit cap — splitting as fallback.") is None


def test_real_fetch_failures_are_compacted_for_notifications():
    failed = (
        "All 2 attempts failed for URL: "
        "https://www.imot.bg/obiavi/prodazhbi/oblast-burgas?price_min=10&price_max=20"
    )
    preflight = "Pre-flight fetch failed for oblast-burgas/dvustaen [10-20] — assuming no cap."

    assert log_watcher.format_error_bullet(failed) == "Soft-block — oblast-burgas [10–20]"
    assert log_watcher.format_error_bullet(preflight) == "Pre-flight failed — oblast-burgas/dvustaen [10-20]"
    assert log_watcher.format_error_bullet("Connection error (attempt 1/2)") == "Connection error"


def test_finalise_region_sends_summary_and_resets_region_state(monkeypatch):
    messages = []
    state = log_watcher.default_state()
    state.update({
        "region_name": "Бургас",
        "region_start_ts": 100.0,
        "region_listings": 81,
        "region_errors": ["Connection error", "Connection error"],
    })
    monkeypatch.setattr(log_watcher, "send_telegram", messages.append)

    log_watcher.finalise_region(state, now_ts=200.0, time_str="10:15:00")

    assert len(messages) == 2
    assert "1 fetch failure(s)" in messages[0]
    assert "81 listings / 3 pages" in messages[1]
    assert state["total_listings"] == 81
    assert state["region_listings"] == 0
    assert state["region_errors"] == []


def test_default_state_returns_independent_mutable_lists():
    first = log_watcher.default_state()
    second = log_watcher.default_state()

    first["region_errors"].append("error")

    assert second["region_errors"] == []


def test_watcher_region_order_comes_from_scraper_configuration():
    assert log_watcher.TOTAL_REGIONS == 54
    assert len(log_watcher.REGION_ORDER) == log_watcher.TOTAL_REGIONS
    assert log_watcher.REGION_ORDER[0] == "oblast-blagoevgrad"
    assert log_watcher.REGION_ORDER[-1] == "grad-yambol"


def test_state_round_trip_merges_new_default_fields(monkeypatch, tmp_path):
    state_file = tmp_path / "watcher_state.json"
    monkeypatch.setattr(log_watcher, "STATE_FILE", str(state_file))

    log_watcher.save_state({"region_counter": 3, "custom": "kept"})
    loaded = log_watcher.load_state()

    assert loaded["region_counter"] == 3
    assert loaded["custom"] == "kept"
    assert loaded["region_errors"] == []


def test_failed_state_replace_preserves_previous_state(monkeypatch, tmp_path):
    state_file = tmp_path / "watcher_state.json"
    state_file.write_text(json.dumps({"region_counter": 2}), encoding="utf-8")
    monkeypatch.setattr(log_watcher, "STATE_FILE", str(state_file))
    monkeypatch.setattr(log_watcher.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))

    log_watcher.save_state({"region_counter": 9})

    assert json.loads(state_file.read_text(encoding="utf-8")) == {"region_counter": 2}
    assert (tmp_path / "watcher_state.json.tmp").exists() is False


def test_get_latest_log_selects_newest_file(monkeypatch, tmp_path):
    older = tmp_path / "older.log"
    newer = tmp_path / "newer.log"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    monkeypatch.setattr(log_watcher, "LOG_DIR", str(tmp_path))

    assert log_watcher.get_latest_log() == str(newer)


def test_watch_processes_complete_synthetic_log_without_telegram(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "run.log"
    log_file.write_text(
        "\n".join([
            "malformed line that should be ignored",
            "2026-09-17 10:00:00 [INFO] main — Scraping run started",
            "2026-09-17 10:00:01 [INFO] main — ===== Transaction type: prodazhbi =====",
            "2026-09-17 10:00:02 [INFO] main — Region 1/54: Бургас (oblast-burgas)",
            "2026-09-17 10:00:03 [INFO] main — ===== Region: Бургас (oblast-burgas) | prodazhbi =====",
            "2026-09-17 10:00:04 [INFO] main — Saved 38 valid listings from page 1.",
            "2026-09-17 10:01:00 [INFO] main — Scraping run finished. Total time: 0:01:00",
        ]) + "\n",
        encoding="utf-8",
    )
    messages = []
    state_file = tmp_path / "watcher_state.json"
    monkeypatch.setattr(log_watcher, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(log_watcher, "STATE_FILE", str(state_file))
    monkeypatch.setattr(log_watcher, "send_telegram", messages.append)

    log_watcher.watch()

    assert any("Scraper started" in message for message in messages)
    assert any("Region 1/54" in message for message in messages)
    assert any("38 listings / 1 pages" in message for message in messages)
    assert any("Run complete" in message for message in messages)
    assert state_file.exists() is False


def test_watcher_resume_does_not_repeat_old_notifications(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "run.log"
    log_file.write_text(
        "\n".join([
            "2026-09-17 10:00:00 [INFO] main — Scraping run started",
            "2026-09-17 10:00:01 [INFO] main — ===== Transaction type: prodazhbi =====",
            "2026-09-17 10:00:02 [INFO] main — Region 1/54: Бургас (oblast-burgas)",
            "2026-09-17 10:00:03 [INFO] main — ===== Region: Бургас (oblast-burgas) | prodazhbi =====",
            "2026-09-17 10:00:04 [INFO] main — Saved 20 valid listings from page 1.",
        ]) + "\n",
        encoding="utf-8",
    )
    messages = []
    state_file = tmp_path / "watcher_state.json"
    monkeypatch.setattr(log_watcher, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(log_watcher, "STATE_FILE", str(state_file))
    monkeypatch.setattr(log_watcher, "send_telegram", messages.append)
    monkeypatch.setattr(log_watcher.time, "sleep", lambda _seconds: (_ for _ in ()).throw(RuntimeError("stop")))

    with pytest.raises(RuntimeError, match="stop"):
        log_watcher.watch()

    first_run_count = len(messages)
    with log_file.open("a", encoding="utf-8") as file:
        file.write("2026-09-17 10:01:00 [INFO] main — Scraping run finished. Total time: 0:01:00\n")

    log_watcher.watch()

    new_messages = messages[first_run_count:]
    assert not any("Scraper started" in message for message in new_messages)
    assert not any("Starting: prodazhbi" in message for message in new_messages)
    assert any("20 listings / 1 pages" in message for message in new_messages)
    assert any("Run complete" in message for message in new_messages)
