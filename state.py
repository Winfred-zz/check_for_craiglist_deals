"""Durable alert state: listings history, pending and sent events (SQLite).

Contract (design.md): at-least-once delivery. A scan's observation
updates AND its pending event insert happen in ONE SQLite transaction,
so a new low can never become suppressed without a durable pending
event (the legacy CSV-rewrite bug that erased new rows is gone). A
listing is inserted the moment its discovery event is created, so a
failed send or crash cannot re-discover it as a duplicate alert either.
sent rows are written only after a successful send.

Legacy `configs/known_deals.csv` rows are IMPORTED (INSERT OR IGNORE,
idempotent) on open; the file is never modified by opening state, and
its content is copied once to data/known_deals.csv.migrated-<stamp>.bak
so a new data dir can re-import while the source stays intact.

A corrupt, partial, or unsupported existing DB is refused loudly
(StateError), never silently reset: losing history would duplicate
alerts. Persistence failures raise StateError so callers stop rather
than "continue as if deduplication is durable".

Single-instance enforcement: a kernel advisory lock (fcntl.flock) on a
stable lockfile inode held for the process lifetime. A live holder
blocks startup; a dead holder's lock is released by the kernel — no
stale-PID probing or unlink races.
"""

import fcntl
import os
import csv
import sqlite3
import shutil
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TypedDict, cast

from parse import ItemRecord, parse_listing_link
from selection import EventRecord, ListingRecord

STATE_DB = "state.sqlite"
LOCK_FILE = "bot.lock"
LEGACY_KNOWN_FILE = "known_deals.csv"
LEGACY_HEADERS = ("title", "original_price", "current_price", "url")
SCHEMA_VERSION = 1

TABLE_COLUMNS: dict[str, list[str]] = {
    "listings": [
        "posting_id",
        "url",
        "title",
        "original_price",
        "last_price",
        "low_price",
        "first_seen_utc",
        "updated_utc",
    ],
    "pending": ["event_key", "message", "created_utc"],
    "sent": ["event_key", "recorded_utc"],
}


class PendingEvent(TypedDict):
    """One row of the pending table, decoded for callers."""
    event_key: str
    message: str
    created_utc: str


class StateError(Exception):
    """Persistence failure that must stop 'continue as if durable'."""


def _dec(text: str) -> Decimal:
    return Decimal(text)


def _enc(dec_value: Decimal) -> str:
    return str(dec_value)


def _utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def acquire_instance_lock(state_dir: str) -> int | None:
    """Take the single-instance kernel lock. Returns the lock file fd."""
    lock_path = os.path.join(state_dir, LOCK_FILE)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise StateError(f"cannot create instance lock in {state_dir}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        _ = os.ftruncate(fd, 0)
        _ = os.write(fd, f"pid {os.getpid()} (diagnostic only)\n".encode())
    except OSError as exc:
        os.close(fd)
        raise StateError(f"cannot write instance lock: {exc}") from exc
    return fd


def release_instance_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def state_db_path(state_dir: str) -> str:
    return os.path.join(state_dir, STATE_DB)


def readonly_uri(db_path: str) -> str:
    """Read-only SQLite URI for an existing DB path.

    Path.as_uri() percent-encodes '#', '?', '%', spaces, and Unicode so
    the mode=ro query can never be swallowed by a fragment and the URI
    can never silently point at (or create) a different file.
    """
    absolute = Path(os.path.abspath(db_path)).as_uri()
    return f"{absolute}?mode=ro"


def validate_state_schema(con: sqlite3.Connection) -> None:
    """Shared non-mutating validator used by both openers."""
    try:
        rows = cast(
            "list[tuple[int, ...]]",
            con.execute("SELECT version FROM schema").fetchall(),
        )
    except sqlite3.Error as exc:
        raise StateError(
            f"state database partial or missing tables: schema ({exc})"
        ) from exc
    versions = [row[0] for row in rows]
    if not versions:
        raise StateError(
            "state database has no schema version row (corrupt or partial)"
        )
    if len(versions) > 1:
        raise StateError(
            f"state database has {len(versions)} schema version rows (corrupt)"
        )
    version = versions[0]
    if version > SCHEMA_VERSION:
        raise StateError(
            f"state database schema version {version} is newer than supported"
            + f" (max {SCHEMA_VERSION}); refusing to reuse state"
        )
    if version < SCHEMA_VERSION:
        raise StateError(
            f"state database schema version {version} is older than supported"
            + f" (min {SCHEMA_VERSION}); no migration available"
        )
    for table, columns in TABLE_COLUMNS.items():
        info = cast(
            "list[tuple[object, ...]]",
            con.execute(f"PRAGMA table_info({table})").fetchall(),
        )
        found = [str(row[1]) for row in info]
        if found != columns:
            raise StateError(
                f"state database partial or missing tables: {table}"
                + f" (has {found}, need {columns})"
            )


def _merge_legacy_rows(legacy_path: str) -> tuple[dict[str, dict[str, str]], int]:
    """Parse + merge the legacy CSV into one entry per posting ID.

    Returns (merged entries, conflict count). Duplicate posting IDs keep
    the MINIMUM current_price as the low watermark and the first valid
    title/original_price/url in file order; conflicts are counted so the
    caller can log them. Invalid rows raise StateError with row numbers.
    """
    try:
        with open(legacy_path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            headers = tuple(reader.fieldnames or [])
            if headers != LEGACY_HEADERS:
                raise StateError(
                    "legacy known-deals CSV must have exactly the headers "
                    + f"{LEGACY_HEADERS} (has {headers})"
                )
            merged: dict[str, dict[str, str]] = {}
            conflicts = 0
            for line_no, row in enumerate(reader, start=2):
                amounts: dict[str, str] = {}
                for column in ("current_price", "original_price"):
                    raw_price = (row[column] or "").strip()
                    try:
                        price = Decimal(raw_price)
                    except InvalidOperation:
                        raise StateError(
                            f"legacy known-deals CSV line {line_no}: bad {column}"
                            + f" {raw_price!r}"
                        ) from None
                    if not price.is_finite() or price < 0:
                        raise StateError(
                            f"legacy known-deals CSV line {line_no}: {column} must"
                            + " be a finite non-negative amount"
                            + f" (got {raw_price!r})"
                        )
                    amounts[column] = _enc(price)
                posting_id, canonical, _reason = parse_listing_link(row["url"])
                if posting_id is None:
                    raise StateError(
                        f"legacy known-deals CSV line {line_no}: listing url"
                        + f" {row['url']!r} is not a validated craigslist posting"
                    )
                entry: dict[str, str] = {
                    "posting_id": cast("str", posting_id),
                    "url": cast("str", canonical),
                    "title": (row["title"] or "").strip(),
                    "original_price": amounts["original_price"],
                    "low_price": amounts["current_price"],
                    "last_price": amounts["current_price"],
                }
                existing = merged.get(posting_id)
                if existing is None:
                    merged[posting_id] = entry
                    continue
                conflicts += 1
                if _dec(entry["low_price"]) < _dec(existing["low_price"]):
                    merged[posting_id] = {**existing, "low_price": entry["low_price"]}
            return merged, conflicts
    except (OSError, UnicodeError) as exc:
        raise StateError(f"legacy known-deals CSV unreadable: {exc}") from exc


def _listings_count(con: sqlite3.Connection) -> int:
    """Rows in listings; used to report newly inserted legacy imports."""
    row = con.execute("SELECT count() FROM listings").fetchone()
    return cast("int", row[0])


def open_state(
    state_dir: str,
    legacy_known_path: str | None,
) -> tuple[sqlite3.Connection, int, int]:
    """Open (creating as needed) the state DB; import the legacy CSV.

    Returns (connection, newly inserted posting count, legacy conflict count).
    Import is INSERT OR IGNORE so re-opening is idempotent and never
    destroys the import source. Backup copy happens only once (existence
    of any known_deals.csv.migrated-*.bak suppresses it). Raises StateError
    on unreadable/corrupt/partial/unsupported state. Must run while the
    instance lock is held.
    """
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError as exc:
        raise StateError(f"cannot create data directory: {exc}") from exc
    if not (os.access(state_dir, os.W_OK) and os.access(state_dir, os.X_OK)):
        raise StateError(f"directory exists but is not writable: {state_dir}")
    db_path = state_db_path(state_dir)
    fresh = not os.path.isfile(db_path)
    try:
        con = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        raise StateError(f"state database unreadable: {exc}") from exc
    if fresh:
        try:
            with con:
                _ = con.execute(
                    "CREATE TABLE schema (version INTEGER PRIMARY KEY)"
                )
                _ = con.execute(
                    "INSERT INTO schema (version) VALUES (?)", (SCHEMA_VERSION,)
                )
                _ = con.execute(
                    "CREATE TABLE listings (posting_id TEXT PRIMARY KEY,"
                    + " url TEXT, title TEXT, original_price TEXT, last_price TEXT,"
                    + " low_price TEXT, first_seen_utc TEXT, updated_utc TEXT)"
                )
                _ = con.execute(
                    "CREATE TABLE pending (event_key TEXT PRIMARY KEY,"
                    + " message TEXT, created_utc TEXT)"
                )
                _ = con.execute(
                    "CREATE TABLE sent (event_key TEXT PRIMARY KEY,"
                    + " recorded_utc TEXT)"
                )
        except sqlite3.Error as exc:
            con.close()
            raise StateError(
                "state database initialization failed (an existing file in the"
                + f" way, a read-only filesystem, or corruption): {exc}"
            ) from exc
    try:
        validate_state_schema(con)
    except StateError:
        con.close()
        raise
    imported = 0
    conflicts = 0
    if legacy_known_path and os.path.isfile(legacy_known_path):
        merged, conflicts = _merge_legacy_rows(legacy_known_path)
        already_backed_up = any(
            name.startswith(f"{LEGACY_KNOWN_FILE}.migrated-")
            and name.endswith(".bak")
            for name in os.listdir(state_dir)
        )
        if not already_backed_up:
            backup = os.path.join(
                state_dir, f"{LEGACY_KNOWN_FILE}.migrated-{_utc_stamp()}.bak"
            )
            try:
                shutil.copyfile(legacy_known_path, backup)
            except OSError as exc:
                con.close()
                raise StateError(
                    f"legacy known-deals backup failed: {exc}"
                ) from exc
        try:
            first = _utc_stamp()
            before = _listings_count(con)
            with con:
                for entry in merged.values():
                    _ = con.execute(
                        "INSERT OR IGNORE INTO listings (posting_id, url, title,"
                        + " original_price, last_price, low_price,"
                        + " first_seen_utc, updated_utc)"
                        + " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            entry["posting_id"],
                            entry["url"],
                            entry["title"],
                            entry["original_price"],
                            entry["last_price"],
                            entry["low_price"],
                            first,
                            first,
                        ),
                    )
            imported = _listings_count(con) - before
        except sqlite3.Error as exc:
            con.close()
            raise StateError(f"legacy import transaction failed: {exc}") from exc
    return con, imported, conflicts


def listings_snapshot(con: sqlite3.Connection) -> dict[str, ListingRecord]:
    rows = cast(
        "list[tuple[object, ...]]",
        con.execute(
            "SELECT posting_id, url, title, original_price, last_price,"
            + " low_price FROM listings"
        ).fetchall(),
    )
    return {
        str(row[0]): {
            "url": cast("str", row[1]),
            "title": cast("str", row[2]),
            "original_price": _dec(cast("str", row[3])),
            "last_price": _dec(cast("str", row[4])),
            "low_price": _dec(cast("str", row[5])),
        }
        for row in rows
    }


def pending_events(con: sqlite3.Connection) -> list[PendingEvent]:
    rows = cast(
        "list[tuple[str, ...]]",
        con.execute(
            "SELECT event_key, message, created_utc FROM pending"
            + " ORDER BY created_utc, event_key"
        ).fetchall(),
    )
    return [
        {
            "event_key": row[0],
            "message": row[1],
            "created_utc": row[2],
        }
        for row in rows
    ]


def apply_event(con: sqlite3.Connection, event: EventRecord, message: str) -> None:
    """Atomically record an event's observation update + pending row.

    For discovery: insert the listing row (marks it seen forever) and the
    pending event in ONE transaction. For price-drop: lower the
    watermark and enqueue in ONE transaction; a missing listing row is a
    logic error raised as StateError (selection only drops known ids).
    """
    try:
        with con:
            if event["kind"] == "new":
                _ = con.execute(
                    "INSERT OR IGNORE INTO listings (posting_id, url, title,"
                    + " original_price, last_price, low_price, first_seen_utc,"
                    + " updated_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event["posting_id"],
                        event["url"],
                        event["title"],
                        _enc(event["price_usd"]),
                        _enc(event["price_usd"]),
                        _enc(event["price_usd"]),
                        _utc_stamp(),
                        _utc_stamp(),
                    ),
                )
            else:
                cur = con.execute(
                    "UPDATE listings SET low_price = ? WHERE posting_id = ?",
                    (_enc(event["price_usd"]), event["posting_id"]),
                )
                if cur.rowcount == 0:
                    raise StateError(
                        f"price-drop event for posting {event['posting_id']}"
                        " has no listing row"
                    )
            _ = con.execute(
                "INSERT INTO pending (event_key, message, created_utc)"
                + " VALUES (?, ?, ?)",
                (event["event_key"], message, _utc_stamp()),
            )
    except sqlite3.Error as exc:
        raise StateError(f"event persistence failed: {exc}") from exc


def record_observations(
    con: sqlite3.Connection,
    items_by_url: dict[str, list[ItemRecord]],
) -> None:
    """Update last_price/title/url/updated_utc for every parsed card.

    One sqlite transaction for the whole pass; UPDATE skips postings
    with no row yet (discovery events insert their row before this).
    """
    stamp = _utc_stamp()
    try:
        with con:
            for items in items_by_url.values():
                for item in items:
                    _ = con.execute(
                        "UPDATE listings SET last_price = ?, updated_utc = ?,"
                        + " title = ?, url = ? WHERE posting_id = ?",
                        (
                            _enc(item["price_usd"]),
                            stamp,
                            item["title"],
                            item["link"],
                            item["id"],
                        ),
                    )
    except sqlite3.Error as exc:
        raise StateError(f"observation update failed: {exc}") from exc


def mark_sent(con: sqlite3.Connection, event_key: str) -> None:
    """Record delivery success: sent row + pending removal in ONE transaction."""
    try:
        with con:
            _ = con.execute(
                "INSERT OR IGNORE INTO sent (event_key, recorded_utc) VALUES (?, ?)",
                (event_key, _utc_stamp()),
            )
            _ = con.execute("DELETE FROM pending WHERE event_key = ?", (event_key,))
    except sqlite3.Error as exc:
        raise StateError(f"mark-sent transaction failed: {exc}") from exc
