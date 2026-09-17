"""Durable files and completeness state for one monthly light-scraper run."""

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from regions import REGIONS


SCHEMA_VERSION = 1
TRANSACTIONS = ("prodazhbi", "naemi")
SEEN_COLUMNS = ("source_id", "first_seen_at", "region_slug")
ACTION_COLUMNS = (
    "source_id",
    "listing_id",
    "action",
    "old_price",
    "new_price",
    "observed_at",
)
PRIMARY_ACTIONS = {"new", "changed", "reappeared", "missing"}


def generate_run_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_run(runs_root: str | Path, run_id: str | None = None) -> tuple[Path, dict]:
    run_id = run_id or generate_run_id()
    run_dir = Path(runs_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    expected_regions = [entry["slug"] for entry in REGIONS]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "status": "running",
        "expected_regions": len(expected_regions),
        "transactions": {
            transaction: _new_transaction_state(expected_regions)
            for transaction in TRANSACTIONS
        },
        "errors": [],
    }
    save_manifest(run_dir, manifest)
    return run_dir, manifest


def _new_transaction_state(expected_regions: list[str]) -> dict:
    return {
        "status": "pending",
        "pass1_status": "pending",
        "pass2_status": "pending",
        "expected_regions": expected_regions.copy(),
        "completed_regions": [],
        "failed_units": [],
        "pages_attempted": 0,
        "pages_completed": 0,
        "fetch_failures": 0,
        "rejected_rows": 0,
        "counts": {
            "new": 0,
            "changed": 0,
            "unchanged": 0,
            "reappeared": 0,
            "missing": 0,
            "output_rows": 0,
        },
        "pass2": {
            "selected": 0,
            "completed": 0,
            "saved": 0,
            "rejected": 0,
        },
        "allow_missing_updates": False,
    }


def manifest_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "manifest.json"


def save_manifest(run_dir: str | Path, manifest: dict) -> None:
    path = manifest_path(run_dir)
    temp_path = path.with_suffix(".json.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(manifest, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_manifest(run_dir: str | Path) -> dict:
    with manifest_path(run_dir).open(encoding="utf-8") as file:
        manifest = json.load(file)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported or invalid run manifest")
    if manifest.get("status") not in {"running", "partial", "failed", "complete"}:
        raise ValueError("Invalid run status")
    if not isinstance(manifest.get("transactions"), dict):
        raise ValueError("Manifest transactions are missing")
    return manifest


def mark_region_complete(manifest: dict, transaction_type: str, region_slug: str) -> None:
    state = _transaction_state(manifest, transaction_type)
    if region_slug not in state["expected_regions"]:
        raise ValueError(f"Unexpected region: {region_slug}")
    if region_slug not in state["completed_regions"]:
        state["completed_regions"].append(region_slug)
    state["status"] = "running"
    state["pass1_status"] = "running"


def mark_unit_failed(manifest: dict, transaction_type: str, unit: str, reason: str) -> None:
    state = _transaction_state(manifest, transaction_type)
    failure = {"unit": unit, "reason": reason}
    if failure not in state["failed_units"]:
        state["failed_units"].append(failure)
    state["status"] = "partial"
    state["pass1_status"] = "partial"
    state["allow_missing_updates"] = False
    manifest["status"] = "partial"


def clear_unit_failure(manifest: dict, transaction_type: str, unit: str) -> None:
    """Remove a fetch failure after the same unit succeeds on resume."""
    state = _transaction_state(manifest, transaction_type)
    state["failed_units"] = [
        failure for failure in state["failed_units"]
        if failure.get("unit") != unit
    ]
    if not state["failed_units"]:
        state["status"] = "running"
        state["pass1_status"] = "running"
    if not any(
        transaction["failed_units"]
        for transaction in manifest["transactions"].values()
    ):
        manifest["status"] = "running"


def finish_pass1(manifest: dict, transaction_type: str) -> bool:
    state = _transaction_state(manifest, transaction_type)
    expected = set(state["expected_regions"])
    completed = set(state["completed_regions"])
    complete = completed == expected and not state["failed_units"]
    state["pass1_status"] = "complete" if complete else "partial"
    state["status"] = "running" if complete else "partial"
    state["allow_missing_updates"] = complete
    if not complete:
        manifest["status"] = "partial"
    return complete


def can_compute_missing(manifest: dict, transaction_type: str) -> bool:
    state = _transaction_state(manifest, transaction_type)
    return bool(
        state["pass1_status"] == "complete"
        and state["allow_missing_updates"]
        and not state["failed_units"]
        and set(state["completed_regions"]) == set(state["expected_regions"])
    )


def finish_transaction(manifest: dict, transaction_type: str) -> None:
    state = _transaction_state(manifest, transaction_type)
    if state["pass1_status"] != "complete" or state["pass2_status"] != "complete":
        raise ValueError(f"Cannot complete unfinished transaction: {transaction_type}")
    state["status"] = "complete"


def finish_run(manifest: dict) -> None:
    if not all(
        state["status"] == "complete"
        for state in manifest["transactions"].values()
    ):
        raise ValueError("Cannot complete a run with unfinished transactions")
    manifest["status"] = "complete"
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()


def _transaction_state(manifest: dict, transaction_type: str) -> dict:
    if transaction_type not in TRANSACTIONS:
        raise ValueError(f"Unsupported transaction type: {transaction_type}")
    try:
        return manifest["transactions"][transaction_type]
    except KeyError as error:
        raise ValueError(f"Transaction state missing: {transaction_type}") from error


class SeenIdStore:
    """Append-only, deduplicated seen-ID file that can be restored on resume."""

    def __init__(self, run_dir: str | Path, transaction_type: str):
        if transaction_type not in TRANSACTIONS:
            raise ValueError(f"Unsupported transaction type: {transaction_type}")
        self.path = Path(run_dir) / f"{transaction_type}_seen_ids.csv"
        self.ids = self._load_existing_ids()

    def _load_existing_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        with self.path.open(encoding="utf-8-sig", newline="") as file:
            return {
                row["source_id"].strip()
                for row in csv.DictReader(file)
                if row.get("source_id") and row["source_id"].strip()
            }

    def append(self, listings: list[dict], region_slug: str, observed_at: str | None = None) -> int:
        timestamp = observed_at or datetime.now(timezone.utc).isoformat()
        new_ids = []
        page_ids = set()
        for listing in listings:
            source_id = str(listing.get("source_id") or "").strip()
            if not source_id or source_id in self.ids or source_id in page_ids:
                continue
            page_ids.add(source_id)
            new_ids.append(source_id)

        if not new_ids:
            return 0

        write_header = not self.path.exists()
        with self.path.open("a", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=SEEN_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerows(
                {
                    "source_id": source_id,
                    "first_seen_at": timestamp,
                    "region_slug": region_slug,
                }
                for source_id in new_ids
            )
            file.flush()
            os.fsync(file.fileno())

        self.ids.update(new_ids)
        return len(new_ids)


class IndexListingStore:
    """Durable, deduplicated Pass 1 index rows stored as JSON Lines."""

    def __init__(self, run_dir: str | Path, transaction_type: str):
        if transaction_type not in TRANSACTIONS:
            raise ValueError(f"Unsupported transaction type: {transaction_type}")
        self.path = Path(run_dir) / f"{transaction_type}_pass1_index.jsonl"
        self.listings = self._load_existing_rows()

    def _load_existing_rows(self) -> dict[str, dict]:
        rows = {}
        if not self.path.exists():
            return rows
        with self.path.open(encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"Invalid staging JSON on line {line_number}: {self.path}"
                    ) from error
                source_id = str(record.get("source_id") or "").strip()
                listing = record.get("listing")
                if not source_id or not isinstance(listing, dict):
                    raise ValueError(
                        f"Invalid staging record on line {line_number}: {self.path}"
                    )
                rows[source_id] = record
        return rows

    @property
    def ids(self) -> set[str]:
        return set(self.listings)

    def append(
        self,
        listings: list[dict],
        region_slug: str,
        scan_unit: str,
        observed_at: str | None = None,
    ) -> int:
        timestamp = observed_at or datetime.now(timezone.utc).isoformat()
        new_records = []
        page_ids = set()

        for listing in listings:
            source_id = str(listing.get("source_id") or "").strip()
            if not source_id or source_id in self.listings or source_id in page_ids:
                continue
            page_ids.add(source_id)
            new_records.append({
                "source_id": source_id,
                "region_slug": region_slug,
                "scan_unit": scan_unit,
                "observed_at": timestamp,
                "listing": listing,
            })

        if not new_records:
            return 0

        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            for record in new_records:
                file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        self.listings.update({record["source_id"]: record for record in new_records})
        return len(new_records)

    def listing_rows(self) -> list[dict]:
        return [record["listing"] for record in self.listings.values()]

    def rows_for_region(self, region_slug: str) -> list[dict]:
        return [
            record["listing"]
            for record in self.listings.values()
            if record["region_slug"] == region_slug
        ]


class ListingRowStore:
    """Last-write-wins staging for full raw listing rows."""

    def __init__(self, run_dir: str | Path, transaction_type: str):
        _validate_transaction(transaction_type)
        self.transaction_type = transaction_type
        self.path = Path(run_dir) / f"{transaction_type}_rows_staging.jsonl"
        self.rows = _load_jsonl_records(self.path, "listing")

    def upsert(self, listings: list[dict]) -> int:
        records = []
        for listing in listings:
            source_id = str(listing.get("source_id") or "").strip()
            if not source_id:
                continue
            records.append({"source_id": source_id, "listing": listing})
        _append_jsonl(self.path, records)
        self.rows.update({record["source_id"]: record for record in records})
        return len(records)

    def export_csv(self, columns: list[str] | tuple[str, ...]) -> Path:
        output_path = self.path.parent / f"{self.transaction_type}_rows.csv"
        temp_path = output_path.with_suffix(".csv.tmp")
        try:
            with temp_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for source_id in sorted(self.rows):
                    writer.writerow(self.rows[source_id]["listing"])
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, output_path)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise
        return output_path


class ActionStore:
    """Durable action metadata with one effective action per source ID."""

    def __init__(self, run_dir: str | Path, transaction_type: str):
        _validate_transaction(transaction_type)
        self.transaction_type = transaction_type
        self.path = Path(run_dir) / f"{transaction_type}_actions_staging.jsonl"
        self.actions = self._load_actions()

    def _load_actions(self) -> dict[str, dict]:
        actions = {}
        if not self.path.exists():
            return actions
        with self.path.open(encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"Invalid action staging JSON on line {line_number}") from error
                _validate_action(record)
                actions[record["source_id"]] = _merge_action(actions.get(record["source_id"]), record)
        return actions

    def upsert(self, records: list[dict]) -> int:
        normalised = []
        for record in records:
            item = {column: record.get(column) for column in ACTION_COLUMNS}
            item["source_id"] = str(item.get("source_id") or "").strip()
            _validate_action(item)
            normalised.append(item)
        _append_jsonl(self.path, normalised)
        for record in normalised:
            source_id = record["source_id"]
            self.actions[source_id] = _merge_action(self.actions.get(source_id), record)
        return len(normalised)

    def export_csv(self) -> Path:
        output_path = self.path.parent / f"{self.transaction_type}_actions.csv"
        temp_path = output_path.with_suffix(".csv.tmp")
        try:
            with temp_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=ACTION_COLUMNS)
                writer.writeheader()
                for source_id in sorted(self.actions):
                    writer.writerow(self.actions[source_id])
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, output_path)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise
        return output_path


class Pass2SelectionStore:
    """Immutable ordered Pass 2 selection reused by every resumed session."""

    def __init__(self, run_dir: str | Path, transaction_type: str):
        _validate_transaction(transaction_type)
        self.path = Path(run_dir) / f"{transaction_type}_pass2_selection.json"

    def exists(self) -> bool:
        return self.path.exists()

    def create(self, listings: list[dict]) -> list[dict]:
        if self.path.exists():
            raise FileExistsError(f"Pass 2 selection already exists: {self.path}")
        seen = set()
        selection = []
        for listing in listings:
            source_id = str(listing.get("source_id") or "").strip()
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            selection.append(listing)

        temp_path = self.path.with_suffix(".json.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(selection, file, ensure_ascii=False, indent=2, default=str)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self.path)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise
        return selection

    def load(self) -> list[dict]:
        with self.path.open(encoding="utf-8") as file:
            selection = json.load(file)
        if not isinstance(selection, list):
            raise ValueError("Pass 2 selection must be a JSON array")
        source_ids = set()
        for listing in selection:
            if not isinstance(listing, dict):
                raise ValueError("Invalid Pass 2 selection row")
            source_id = str(listing.get("source_id") or "").strip()
            if not source_id or source_id in source_ids:
                raise ValueError("Pass 2 selection contains missing or duplicate source IDs")
            source_ids.add(source_id)
        return selection


def _validate_transaction(transaction_type: str) -> None:
    if transaction_type not in TRANSACTIONS:
        raise ValueError(f"Unsupported transaction type: {transaction_type}")


def _load_jsonl_records(path: Path, payload_key: str) -> dict[str, dict]:
    records = {}
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid staging JSON on line {line_number}: {path}") from error
            source_id = str(record.get("source_id") or "").strip()
            if not source_id or not isinstance(record.get(payload_key), dict):
                raise ValueError(f"Invalid staging record on line {line_number}: {path}")
            records[source_id] = record
    return records


def _append_jsonl(path: Path, records: list[dict]) -> None:
    if not records:
        return
    with path.open("a", encoding="utf-8", newline="\n") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str))
            file.write("\n")
        file.flush()
        os.fsync(file.fileno())


def _validate_action(record: dict) -> None:
    if not str(record.get("source_id") or "").strip():
        raise ValueError("Action source_id is required")
    if record.get("action") not in PRIMARY_ACTIONS | {"refreshed"}:
        raise ValueError(f"Unsupported action: {record.get('action')}")


def _merge_action(existing: dict | None, new: dict) -> dict:
    if existing is None:
        return new
    if existing["action"] in PRIMARY_ACTIONS and new["action"] == "refreshed":
        return existing
    if existing["action"] == "refreshed" and new["action"] in PRIMARY_ACTIONS:
        return new
    return new
