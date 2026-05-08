# =============================================================================
# real_estate_scraper — Light Scraper — Log Watcher
# Purpose: Telegram notification bot for light scraper runs.
#          Tails the latest log file and sends real-time updates.
# Run:     python -m light_scraper.log_watcher (in a separate terminal)
# =============================================================================

import os
import re
import json
import time
import glob
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LOG_DIR = "logs"
POLL_INTERVAL = 10
IDLE_TIMEOUT = 600
DETAIL_FETCH_IDLE_TIMEOUT = 7200  # 2 hours — detail phase fetches ~4000 pages at 1s each
TOTAL_REGIONS = 54
STATE_FILE = "light_watcher_state.json"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_TIMESTAMP_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
)

# "========== Light scraper run started at ..."
_RUN_STARTED_PATTERN = re.compile(
    r"Light scraper run started"
)

# "========== Light scraper run finished ..."
_RUN_FINISHED_PATTERN = re.compile(
    r"Light scraper run finished"
)

# "===== Transaction type: prodazhbi ====="
_TRANSACTION_PATTERN = re.compile(
    r"Transaction type:\s*(\S+)"
)

# "Fetched 155,063 active listings from DB."
_DB_ACTIVE_PATTERN = re.compile(
    r"Fetched ([\d,]+) active listings from DB"
)

# "Fetched 0 inactive listings from DB."
_DB_INACTIVE_PATTERN = re.compile(
    r"Fetched ([\d,]+) inactive listings from DB"
)

# "Pass 1 — scraping index pages: oblast-blagoevgrad | prodazhbi"
_REGION_STARTED_PATTERN = re.compile(
    r"Pass 1 — scraping index pages:\s*(.+?)\s*\|"
)

# "Pre-flight result for oblast-blagoevgrad/ednostaen: CAPPED"
_PREFLIGHT_RESULT_PATTERN = re.compile(
    r"Pre-flight result for (.+?):\s*(CAPPED|under cap)"
)

# "Pass 1 — page 1: https://..." — first page of a new property type being scraped
_PAGE1_PATTERN = re.compile(
    r"Pass 1 — page 1: https?://[^/]+/obiavi/(?:prodazhbi|naemi)/[^/]+/([^/?]+)"
)

# Level-1 page 1 — URL has no property type segment (region is under cap, scraped directly)
# e.g. "Pass 1 — page 1: https://www.imot.bg/obiavi/prodazhbi/grad-razgrad"
_PAGE1_LEVEL1_PATTERN = re.compile(
    r"Pass 1 — page 1: https?://[^/]+/obiavi/(?:prodazhbi|naemi)/([^/?]+)(?:/p-\d+)?$"
)

# "Pass 1 — grad-razgrad: saved 164 listings (37 new | 127 changed | 0 reappeared | 174 unchanged)"
_REGION_CLASSIFIED_PATTERN = re.compile(
    r"Pass 1 — (.+?): saved (\d+) listings \((\d+) new \| (\d+) changed \| (\d+) reappeared \| (\d+) unchanged\)"
)

# "Pass 1 — saved N listings."
_PASS1_SAVED_PATTERN = re.compile(
    r"Pass 1 — saved\s+(\d+)\s+listings"
)

# "Pass 1 — N listings no longer visible"
_PASS1_MISSING_PATTERN = re.compile(
    r"Pass 1 — (\d+)\s+listings no longer visible"
)

# "===== Pass 2 — rolling detail refresh ====="
_PASS2_STARTED_PATTERN = re.compile(
    r"Pass 2 — rolling detail refresh"
)

# "Pass 2 — refreshing N listings."
_PASS2_REFRESHING_PATTERN = re.compile(
    r"Pass 2 — refreshing\s+([\d,]+)"
)

# "Pass 2 — saved N refreshed listings."
_PASS2_SAVED_PATTERN = re.compile(
    r"Pass 2 — saved\s+(\d+)\s+refreshed listings"
)

# "Pass 2 — 100/N saved"
_PASS2_PROGRESS_PATTERN = re.compile(
    r"Pass 2 — (\d+)/([\d,]+) saved"
)

# "Pass 2 — complete. N listings refreshed."
_PASS2_COMPLETE_PATTERN = re.compile(
    r"Pass 2 — complete\.\s+([\d,]+) listings refreshed"
)

# "Pass 2 — WARNING: high rejection rate 95.0% (95/100 rejected in first 100)"
_PASS2_REJECTION_WARNING_PATTERN = re.compile(
    r"Pass 2 — WARNING: high rejection rate ([\d.]+)%"
)

# "Pass 2 — summary: 2121 processed | 1800 saved | 321 rejected | top field: locality"
_PASS2_SUMMARY_PATTERN = re.compile(
    r"Pass 2 — summary: ([\d,]+) processed \| ([\d,]+) saved \| ([\d,]+) rejected \| top field: (\w+)"
)

# "Fetching detail pages for N listings."
_DETAIL_FETCH_PATTERN = re.compile(
    r"Fetching detail pages for\s+([\d,]+)"
)

# Error patterns
_ALL_ATTEMPTS_FAILED_PATTERN = re.compile(
    r"All \d+ attempts failed for URL:\s*(\S+)"
)
_PREFLIGHT_FETCH_FAILED_PATTERN = re.compile(
    r"Pre-flight fetch failed for (.+?)\s*—"
)
_CONNECTION_ERROR_PATTERN = re.compile(
    r"Connection error"
)
_FETCH_NONE_PATTERN = re.compile(
    r"Fetch returned None on page"
)
_CAP_LIKELY_HIT_PATTERN = re.compile(
    r"Cap likely hit for"
)
_SOFT_BLOCK_PATTERN = re.compile(
    r"Soft-block suspected"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_log_timestamp(line: str) -> float | None:
    match = _TIMESTAMP_PATTERN.match(line)
    if match:
        try:
            dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            return dt.timestamp()
        except ValueError:
            return None
    return None


def extract_time_str(line: str) -> str:
    match = _TIMESTAMP_PATTERN.match(line)
    if match:
        return match.group(1).split(" ")[1]
    return datetime.now().strftime("%H:%M:%S")


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    else:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}h {m}m {s}s"


def send_telegram(message: str):
    if not TELEGRAM_TOKEN:
        print(f"[watcher] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        }, timeout=10)
    except Exception as e:
        print(f"[watcher] Telegram error: {e}")


def format_error_bullet(raw_line: str) -> str | None:
    if _FETCH_NONE_PATTERN.search(raw_line):
        return None
    if _CAP_LIKELY_HIT_PATTERN.search(raw_line):
        return None
    if _SOFT_BLOCK_PATTERN.search(raw_line):
        return None
    if "Bucket" in raw_line and "splitting" in raw_line:
        return None

    m = _ALL_ATTEMPTS_FAILED_PATTERN.search(raw_line)
    if m:
        url = m.group(1)
        slug_match = re.search(r"/(?:prodazhbi|naemi)/(.+?)(?:\?|/p-|$)", url)
        price_match = re.search(r"price_min=(\d+)&price_max=(\d+)", url)
        slug = slug_match.group(1) if slug_match else url
        if price_match:
            return f"Soft-block — {slug} [{price_match.group(1)}–{price_match.group(2)}]"
        return f"Soft-block — {slug}"

    m = _PREFLIGHT_FETCH_FAILED_PATTERN.search(raw_line)
    if m:
        return f"Pre-flight failed — {m.group(1).strip()}"

    if _CONNECTION_ERROR_PATTERN.search(raw_line):
        return "Connection error"

    return None


def extract_base_property_type(slug_part: str) -> str:
    """Strip price range suffix like ' [0-312500]' from a property type string."""
    return re.sub(r"\s*\[.*?\]", "", slug_part).strip()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return default_state()


def default_state() -> dict:
    return {
        "run_start_ts":         None,
        "region_counter":       0,
        "region_slug":          None,
        "region_start_ts":      None,
        "region_errors":        [],
        "transaction_type":     "prodazhbi",
        "total_new":            0,
        "total_changed":        0,
        "total_reappeared":     0,
        "total_missing":        0,
        "total_refreshed":      0,
        "pass2_started":        False,
        "log_path":             None,
        "position":             0,
        # DB baseline (populated once per transaction type)
        "db_active":            None,
        "db_inactive":          None,
        # Pre-flight accumulation for current region
        "preflight_capped":     [],
        "preflight_under":      0,
        "preflight_sent":       False,
        # Scraping type tracking for current region
        "scrape_seen_types":    [],
        "scrape_total_types":   0,
        "scrape_started_sent":  False,
        # Detail fetch phase flag (for idle watchdog)
        "in_detail_fetch":      False,
    }


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except IOError as e:
        print(f"[watcher] Could not save state: {e}")


def get_latest_log():
    files = glob.glob(os.path.join(LOG_DIR, "*.log"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


# ---------------------------------------------------------------------------
# Region finalisation
# ---------------------------------------------------------------------------

def finalise_region(state: dict, now_ts: float, time_str: str,
                    new: int, changed: int, reappeared: int, missing: int, saved: int = 0):
    slug = state["region_slug"]
    if not slug:
        return

    duration = now_ts - state["region_start_ts"] if state["region_start_ts"] else 0

    # Send error summary first if any
    errors = state.get("region_errors", [])
    if errors:
        seen = set()
        unique_errors = []
        for e in errors:
            if e not in seen:
                seen.add(e)
                unique_errors.append(e)
        bullet_lines = "\n".join(f"• {e}" for e in unique_errors)
        send_telegram(
            f"⚠️ {len(unique_errors)} fetch failure(s) in {slug}:\n"
            f"{bullet_lines}\n"
            f"🕐 {time_str}"
        )

    send_telegram(
        f"✅ {slug}\n"
        f"🆕 {new} new | 🔄 {changed} changed | "
        + (f"👻 {reappeared} reappeared | " if reappeared > 0 else "")
        + f"💾 {saved} saved\n"
        f"⏱ {fmt_duration(duration)} | 🕐 {time_str}"
    )

    # Accumulate run totals
    state["total_new"] += new
    state["total_changed"] += changed
    state["total_reappeared"] += reappeared
    state["total_missing"] += missing

    # Reset per-region state
    state["region_errors"] = []
    state["preflight_capped"] = []
    state["preflight_under"] = 0
    state["preflight_sent"] = False
    state["scrape_seen_types"] = []
    state["scrape_total_types"] = 0
    state["scrape_started_sent"] = False


# ---------------------------------------------------------------------------
# Main watch loop
# ---------------------------------------------------------------------------

def watch():
    print("[watcher] Light scraper watcher started")

    state = load_state()
    log_path = state.get("log_path")
    position = state.get("position", 0)
    last_growth = time.time()

    while True:
        latest = get_latest_log()

        if latest != log_path:
            if latest:
                print(f"[watcher] Watching: {latest}")
                log_path = latest
                if state.get("log_path") != latest:
                    position = 0
                last_growth = time.time()
            else:
                time.sleep(POLL_INTERVAL)
                continue

        try:
            with open(log_path, "rb") as f:
                f.seek(position)
                raw = f.read()
                position = f.tell()
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue

        if raw:
            last_growth = time.time()

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        new_lines = text.split("\n")

        for line in new_lines:
            line = line.strip()
            if not line:
                continue

            line_ts = parse_log_timestamp(line)
            now_ts = line_ts or time.time()
            time_str = extract_time_str(line)
            upper = line.upper()

            # ------------------------------------------------------------------
            # 1. Run started
            # ------------------------------------------------------------------
            if _RUN_STARTED_PATTERN.search(line):
                state["run_start_ts"] = now_ts
                send_telegram(
                    f"🟢 Light scraper started\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # 2. DB baseline — active listings fetched
            # ------------------------------------------------------------------
            elif _DB_ACTIVE_PATTERN.search(line):
                m = _DB_ACTIVE_PATTERN.search(line)
                state["db_active"] = m.group(1) if m else "?"
                # Wait for inactive line too before sending — handled below

            elif _DB_INACTIVE_PATTERN.search(line):
                m = _DB_INACTIVE_PATTERN.search(line)
                state["db_inactive"] = m.group(1) if m else "?"
                # Send once we have both counts
                if state.get("db_active") is not None:
                    send_telegram(
                        f"🗄 DB baseline — {state['db_active']} active | "
                        f"{state['db_inactive']} inactive\n"
                        f"🕐 {time_str}"
                    )

            # ------------------------------------------------------------------
            # 3. Transaction type
            # ------------------------------------------------------------------
            elif "===== TRANSACTION TYPE:" in upper:
                m = _TRANSACTION_PATTERN.search(line)
                tx = m.group(1) if m else "unknown"
                state["transaction_type"] = tx
                state["region_counter"] = 0
                # Reset DB baseline so it fires again for naemi
                state["db_active"] = None
                state["db_inactive"] = None
                send_telegram(
                    f"📋 Starting: {tx}\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # 4. Region started
            # ------------------------------------------------------------------
            elif _REGION_STARTED_PATTERN.search(line):
                m = _REGION_STARTED_PATTERN.search(line)
                slug = m.group(1).strip() if m else "unknown"

                state["region_counter"] += 1
                state["region_slug"] = slug
                state["region_start_ts"] = now_ts
                state["region_errors"] = []
                state["preflight_capped"] = []
                state["preflight_under"] = 0
                state["preflight_sent"] = False
                state["scrape_seen_types"] = []
                state["scrape_total_types"] = 0
                state["scrape_started_sent"] = False
                state["in_detail_fetch"] = False

                send_telegram(
                    f"📍 Region {state['region_counter']}/{TOTAL_REGIONS} — {slug}\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # 5. Pre-flight results — accumulate, send summary when scraping starts
            # ------------------------------------------------------------------
            elif _PREFLIGHT_RESULT_PATTERN.search(line):
                m = _PREFLIGHT_RESULT_PATTERN.search(line)
                if m:
                    slug_part = m.group(1).strip()
                    result = m.group(2)
                    prop_type = slug_part.split("/")[-1] if "/" in slug_part else slug_part
                    prop_type = extract_base_property_type(prop_type)
                    if result == "CAPPED":
                        if prop_type not in state["preflight_capped"]:
                            state["preflight_capped"].append(prop_type)
                    else:
                        state["preflight_under"] = state.get("preflight_under", 0) + 1

            # ------------------------------------------------------------------
            # 6. Page 1 of a property type (or level-1 region) — send pre-flight
            #    summary once, then track scraping type progress.
            #    Two patterns:
            #      _PAGE1_PATTERN      — URL has a property type segment (level 2/3)
            #      _PAGE1_LEVEL1_PATTERN — URL ends at the region slug (level 1, no split)
            # ------------------------------------------------------------------
            elif _PAGE1_PATTERN.search(line) or _PAGE1_LEVEL1_PATTERN.search(line):
                m = _PAGE1_PATTERN.search(line)
                is_level1 = m is None  # True when only the level-1 pattern matched
                if is_level1:
                    m = _PAGE1_LEVEL1_PATTERN.search(line)
                prop_type = m.group(1) if (m and not is_level1) else None
                if prop_type:
                    prop_type = extract_base_property_type(prop_type)

                # Send pre-flight summary once per region (first time we see page 1)
                if not state.get("preflight_sent"):
                    state["preflight_sent"] = True
                    capped = state.get("preflight_capped", [])
                    under = state.get("preflight_under", 0)
                    total = len(capped) + under
                    state["scrape_total_types"] = total
                    if capped:
                        capped_str = ", ".join(capped)
                        send_telegram(
                            f"🔍 Pre-flight done — {len(capped)} capped: {capped_str} | "
                            f"{under} under cap ({total} types total)\n"
                            f"🕐 {time_str}"
                        )
                    else:
                        send_telegram(
                            f"🔍 Pre-flight done — all {under} under cap "
                            f"({total} types total)\n"
                            f"🕐 {time_str}"
                        )

                # Send "Starting scrape..." once per region
                if not state.get("scrape_started_sent"):
                    state["scrape_started_sent"] = True
                    send_telegram(
                        f"⛏️ Starting scrape...\n"
                        f"🕐 {time_str}"
                    )

                # Track unique property types and send per-type notification
                if prop_type:
                    seen = state.get("scrape_seen_types", [])
                    if prop_type not in seen:
                        seen.append(prop_type)
                        state["scrape_seen_types"] = seen
                        total = max(state.get("scrape_total_types", 0), len(seen))
                        send_telegram(
                            f"⛏️ Scraping type {len(seen)}/{total} — {prop_type}\n"
                            f"🕐 {time_str}"
                        )

            # ------------------------------------------------------------------
            # 7. Region classified — finalise and send ✅
            # ------------------------------------------------------------------
            elif _REGION_CLASSIFIED_PATTERN.search(line):
                m = _REGION_CLASSIFIED_PATTERN.search(line)
                if m:
                    finalise_region(
                        state, now_ts, time_str,
                        new=int(m.group(3)),
                        changed=int(m.group(4)),
                        reappeared=int(m.group(5)),
                        missing=0,
                        saved=int(m.group(2)),
                    )

            # ------------------------------------------------------------------
            # Detail fetch starting
            # ------------------------------------------------------------------
            elif _DETAIL_FETCH_PATTERN.search(line):
                m = _DETAIL_FETCH_PATTERN.search(line)
                n = m.group(1) if m else "?"
                state["in_detail_fetch"] = True
                send_telegram(
                    f"🔍 Fetching detail pages for {n} listings\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # Pass 1 saved
            # ------------------------------------------------------------------
            elif _PASS1_SAVED_PATTERN.search(line):
                m = _PASS1_SAVED_PATTERN.search(line)
                n = m.group(1) if m else "?"
                state["in_detail_fetch"] = False
                send_telegram(
                    f"💾 Pass 1 — saved {n} listings\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # Pass 2 started
            # ------------------------------------------------------------------
            elif _PASS2_STARTED_PATTERN.search(line):
                state["pass2_started"] = True
                send_telegram(
                    f"🔄 Pass 2 — rolling detail refresh starting\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # Pass 2 refreshing N listings
            # ------------------------------------------------------------------
            elif _PASS2_REFRESHING_PATTERN.search(line):
                m = _PASS2_REFRESHING_PATTERN.search(line)
                n = m.group(1) if m else "?"
                state["pass2_total"] = n
                send_telegram(
                    f"🔄 Pass 2 started — refreshing {n} listings\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # Pass 2 progress
            # ------------------------------------------------------------------
            elif _PASS2_PROGRESS_PATTERN.search(line):
                m = _PASS2_PROGRESS_PATTERN.search(line)
                if m:
                    done = m.group(1)
                    total = m.group(2)
                    send_telegram(
                        f"🔄 Pass 2 — {done}/{total} saved\n"
                        f"🕐 {time_str}"
                    )

            # ------------------------------------------------------------------
            # Pass 2 rejection warning
            # ------------------------------------------------------------------
            elif _PASS2_REJECTION_WARNING_PATTERN.search(line):
                m = _PASS2_REJECTION_WARNING_PATTERN.search(line)
                rate = m.group(1) if m else "?"
                send_telegram(
                    f"⚠️ Pass 2 — high rejection rate {rate}% in first 100 listings — consider stopping\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # Pass 2 summary
            # ------------------------------------------------------------------
            elif _PASS2_SUMMARY_PATTERN.search(line):
                m = _PASS2_SUMMARY_PATTERN.search(line)
                if m:
                    processed, saved, rejected, top_field = m.group(1), m.group(2), m.group(3), m.group(4)
                    send_telegram(
                        f"📊 Pass 2 summary — {processed} processed | {saved} saved | {rejected} rejected\n"
                        f"Top rejected field: {top_field}\n"
                        f"🕐 {time_str}"
                    )

            # ------------------------------------------------------------------
            # Pass 2 complete
            # ------------------------------------------------------------------
            elif _PASS2_COMPLETE_PATTERN.search(line):
                m = _PASS2_COMPLETE_PATTERN.search(line)
                n = m.group(1) if m else "?"
                state["total_refreshed"] = int(n.replace(",", ""))
                send_telegram(
                    f"✅ Pass 2 complete — {n} listings refreshed\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # Pass 2 progress
            # ------------------------------------------------------------------
            elif _PASS2_PROGRESS_PATTERN.search(line):
                m = _PASS2_PROGRESS_PATTERN.search(line)
                if m:
                    done = m.group(1)
                    total = m.group(2)
                    send_telegram(
                        f"🔄 Pass 2 — {done}/{total} saved\n"
                        f"🕐 {time_str}"
                    )

            # ------------------------------------------------------------------
            # Pass 2 complete
            # ------------------------------------------------------------------
            elif _PASS2_COMPLETE_PATTERN.search(line):
                m = _PASS2_COMPLETE_PATTERN.search(line)
                n = m.group(1) if m else "?"
                state["total_refreshed"] = int(n.replace(",", ""))
                send_telegram(
                    f"✅ Pass 2 complete — {n} listings refreshed\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # Pass 2 progress
            # ------------------------------------------------------------------
            elif _PASS2_PROGRESS_PATTERN.search(line):
                m = _PASS2_PROGRESS_PATTERN.search(line)
                if m:
                    done = m.group(1)
                    total = m.group(2)
                    send_telegram(
                        f"🔄 Pass 2 — {done}/{total} saved\n"
                        f"🕐 {time_str}"
                    )

            # ------------------------------------------------------------------
            # Pass 2 complete
            # ------------------------------------------------------------------
            elif _PASS2_COMPLETE_PATTERN.search(line):
                m = _PASS2_COMPLETE_PATTERN.search(line)
                n = m.group(1) if m else "?"
                state["total_refreshed"] = int(n.replace(",", ""))
                send_telegram(
                    f"✅ Pass 2 complete — {n} listings refreshed\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # Pass 2 saved
            # ------------------------------------------------------------------
            elif _PASS2_SAVED_PATTERN.search(line):
                m = _PASS2_SAVED_PATTERN.search(line)
                n = m.group(1) if m else "?"
                state["total_refreshed"] = int(n)
                send_telegram(
                    f"✅ Pass 2 — {n} listings refreshed\n"
                    f"🕐 {time_str}"
                )

            # ------------------------------------------------------------------
            # Run finished
            # ------------------------------------------------------------------
            elif _RUN_FINISHED_PATTERN.search(line):
                run_dur = fmt_duration(now_ts - state["run_start_ts"]) if state["run_start_ts"] else "unknown"
                send_telegram(
                    f"🏁 Light scraper complete!\n"
                    f"🆕 {state['total_new']:,} new\n"
                    f"🔄 {state['total_changed']:,} changed\n"
                    f"👻 {state['total_reappeared']:,} reappeared\n"
                    f"❌ {state['total_missing']:,} no longer visible (→ inactive)\n"
                    f"🔁 {state['total_refreshed']:,} refreshed (Pass 2)\n"
                    f"⏱ {run_dur}\n"
                    f"🕐 {time_str}"
                )

                if os.path.exists(STATE_FILE):
                    os.remove(STATE_FILE)
                return

            # ------------------------------------------------------------------
            # Errors — buffer per region
            # ------------------------------------------------------------------
            elif "[ERROR]" in upper or "[WARNING]" in upper:
                bullet = format_error_bullet(line)
                if bullet:
                    state.setdefault("region_errors", []).append(bullet)

        # Save state after each batch
        save_state({
            **state,
            "log_path": log_path,
            "position": position,
        })

        # Idle watchdog — use longer timeout during detail fetch phase
        idle_limit = DETAIL_FETCH_IDLE_TIMEOUT if state.get("in_detail_fetch") else IDLE_TIMEOUT
        if time.time() - last_growth > idle_limit:
            send_telegram(
                f"⚠️ No log activity for {idle_limit // 60} min — possible crash\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}"
            )
            last_growth = time.time()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    watch()
