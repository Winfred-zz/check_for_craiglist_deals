"""State contract: durable pending, atomic events, validated migration.

Each case uses a scratch data dir whose cleanup runs even when the case
raises, so one failing assertion cannot leak state into the next case.
"""

import os
import shutil
import sqlite3
import tempfile

import state

from state import StateError, open_state, state_db_path, validate_state_schema

from decimal import Decimal

from parse import ItemRecord
from selection import EventRecord

FIXTURES = "tests/fixtures"
LEGACY_GOOD = f"{FIXTURES}/known_deals.csv"
LEGACY_BAD_PRICE = f"{FIXTURES}/known_deals_bad.csv"
LEGACY_BAD_URL = f"{FIXTURES}/known_deals_badurl.csv"
LEGACY_BAD_HEADERS = f"{FIXTURES}/known_deals_headers.csv"

ID_A = "5012345678"
ID_B = "5098765432"


def _event(kind: str, posting_id: str, cents: str, key: str) -> EventRecord:
    price = Decimal(cents)
    return {
        "event_key": key,
        "kind": kind,
        "posting_id": posting_id,
        "price_usd": price,
        "prev_price": price if kind == "price-drop" else None,
        "url": f"https://sfbay.craigslist.org/sys/d/x/{posting_id}.html",
        "title": "Example one",
        "friendly_name": "Example search",
        "search_url": "https://sfbay.craigslist.org/search/sss?query=example",
    }


def _item(posting_id: str, price: str, title: str) -> ItemRecord:
    return {
        "id": posting_id,
        "title": title,
        "price_usd": Decimal(price),
        "link": f"https://sfbay.craigslist.org/sys/d/x/{posting_id}.html",
    }


class _Scratch:
    """Scratch data dir; cleanup removes it even when the case raises."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="cfd-state-")

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _rows(con: sqlite3.Connection, sql: str, *args: object) -> list[tuple[str, ...]]:
    return [tuple(str(part) for part in row) for row in con.execute(sql, args)]


def _seed_valid_db(path: str) -> None:
    """Write a schema-valid DB at ``path`` without open_state's fixed name."""
    con = sqlite3.connect(path)
    try:
        with con:
            _ = con.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
            _ = con.execute("INSERT INTO schema (version) VALUES (?)", (1,))
            for table, columns in state.TABLE_COLUMNS.items():
                fields = ", ".join(f"{name} TEXT" for name in columns)
                _ = con.execute(f"CREATE TABLE {table} ({fields})")
    finally:
        con.close()


def test_open_state_creates_db_and_validates_schema() -> None:
    scratch = _Scratch()
    try:
        con, imported, conflicts = open_state(scratch.dir, None)
        try:
            assert (imported, conflicts) == (0, 0)
            assert os.path.isfile(state_db_path(scratch.dir))
            assert _rows(con, "SELECT version FROM schema") == [("1",)]
            for table, columns in state.TABLE_COLUMNS.items():
                found = [
                    str(row[1])
                    for row in con.execute(f"PRAGMA table_info({table})")
                ]
                assert found == columns
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_legacy_migration_merges_duplicate_ids() -> None:
    scratch = _Scratch()
    try:
        con, imported, conflicts = open_state(scratch.dir, LEGACY_GOOD)
        try:
            assert (imported, conflicts) == (2, 1)
            rows = {str(row[0]): row for row in con.execute(
                "SELECT posting_id, url, title, original_price, low_price FROM listings"
            )}
            assert set(rows) == {ID_A, ID_B}
            dup = rows[ID_B]
            assert Decimal(str(dup[4])) == Decimal("15.00"), "min current_price is low"
            assert dup[1] == (
                "https://sfbay.craigslist.org/sys/d/posting/5098765432.html"
            )
            assert dup[2] == "Second posting", "first valid identity wins"
            assert Decimal(str(dup[3])) == Decimal("19.99")
            backups = [
                name
                for name in os.listdir(scratch.dir)
                if name.startswith("known_deals.csv.migrated-")
                and name.endswith(".bak")
            ]
            assert len(backups) == 1, "one-time backup, source left intact"
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_legacy_migration_is_idempotent_on_restart() -> None:
    scratch = _Scratch()
    try:
        _first, first_imported, _ = open_state(scratch.dir, LEGACY_GOOD)
        _first.close()
        _second, second_imported, conflicts = open_state(scratch.dir, LEGACY_GOOD)
        _second.close()
        assert first_imported == 2
        assert second_imported == 0, "re-import never duplicates rows"
        assert conflicts == 1
    finally:
        scratch.cleanup()


def test_legacy_bad_price_raises_with_line_number() -> None:
    scratch = _Scratch()
    try:
        try:
            open_state(scratch.dir, LEGACY_BAD_PRICE)
        except StateError as exc:
            message = str(exc)
            assert "line 3" in message
            assert "current_price" in message
            return
        raise AssertionError("bad legacy price accepted")
    finally:
        scratch.cleanup()


def test_legacy_bad_url_raises_with_line_number() -> None:
    scratch = _Scratch()
    try:
        try:
            open_state(scratch.dir, LEGACY_BAD_URL)
        except StateError as exc:
            message = str(exc)
            assert "line 3" in message
            assert "listing url" in message
            return
        raise AssertionError("bad legacy url accepted")
    finally:
        scratch.cleanup()


def test_legacy_wrong_headers_are_refused() -> None:
    scratch = _Scratch()
    try:
        try:
            open_state(scratch.dir, LEGACY_BAD_HEADERS)
        except StateError as exc:
            assert "headers" in str(exc)
            return
        raise AssertionError("wrong legacy headers accepted")
    finally:
        scratch.cleanup()


def test_legacy_negative_amount_is_refused() -> None:
    scratch = _Scratch()
    path = os.path.join(scratch.dir, "negative.csv")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("title,original_price,current_price,url\n")
        handle.write(
            "Neg,19,-1,"
            + f"https://sfbay.craigslist.org/sys/d/x/{ID_A}.html\n"
        )
    try:
        try:
            open_state(scratch.dir, path)
        except StateError as exc:
            message = str(exc)
            assert "line 2" in message
            assert "non-negative" in message
            return
        raise AssertionError("negative amount accepted")
    finally:
        scratch.cleanup()


def test_pending_events_returns_pending_only_in_order() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, None)
        try:
            _ = con.execute(
                (
                    "INSERT INTO pending (event_key, message, created_utc)"
                    " VALUES (?, ?, ?)"
                ),
                ("200@new", "m", "20261019T130000Z"),
            )
            _ = con.execute(
                (
                    "INSERT INTO pending (event_key, message, created_utc)"
                    " VALUES (?, ?, ?)"
                ),
                ("100@new", "m", "20261019T120000Z"),
            )
            _ = con.execute(
                "INSERT OR IGNORE INTO sent (event_key, recorded_utc) VALUES (?, ?)",
                ("900@new", "20261019T120000Z"),
            )
            keys = [event["event_key"] for event in state.pending_events(con)]
            assert keys == ["100@new", "200@new"], "oldest first; sent rows excluded"
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_apply_event_new_writes_listing_and_pending_together() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, None)
        try:
            event = _event("new", ID_A, "1900", f"{ID_A}@new")
            state.apply_event(con, event, "rendered message")
            rows = _rows(con, "SELECT posting_id, url, title, low_price FROM listings")
            assert rows == [(ID_A, event["url"], "Example one", "1900")]
            assert _rows(con, "SELECT event_key, message FROM pending") == [
                (f"{ID_A}@new", "rendered message")
            ]
            assert _rows(con, "SELECT event_key FROM sent") == []
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_apply_event_price_drop_lowers_watermark() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, LEGACY_GOOD)
        try:
            event = _event("price-drop", ID_B, "1200", f"{ID_B}@1200")
            state.apply_event(con, event, "drop message")
            assert _rows(
                con,
                "SELECT low_price FROM listings",
            ) == [("19",), ("1200",)]
            assert [e["event_key"] for e in state.pending_events(con)] == [
                f"{ID_B}@1200"
            ]
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_apply_event_without_listing_row_rolls_back() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, None)
        try:
            event = _event("price-drop", ID_A, "1200", f"{ID_A}@1200")
            try:
                state.apply_event(con, event, "drop message")
            except StateError as exc:
                assert "no listing row" in str(exc)
            else:
                raise AssertionError("orphan price-drop accepted")
            assert _rows(con, "SELECT posting_id FROM listings") == []
            assert _rows(con, "SELECT event_key FROM pending") == []
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_duplicate_pending_event_is_refused() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, None)
        try:
            event = _event("new", ID_A, "1900", f"{ID_A}@new")
            state.apply_event(con, event, "first")
            try:
                state.apply_event(con, event, "second")
            except StateError as exc:
                assert "event persistence failed" in str(exc)
            else:
                raise AssertionError("duplicate pending accepted")
            assert _rows(con, "SELECT event_key, message FROM pending") == [
                (f"{ID_A}@new", "first")
            ]
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_record_observations_updates_history_without_alerts() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, LEGACY_GOOD)
        try:
            items_by_url = {
                "https://sfbay.craigslist.org/search/sss?query=example": [
                    _item(ID_B, "40.00", "Renamed")
                ]
            }
            state.record_observations(con, items_by_url)
            rows = {str(row[0]): row for row in con.execute(
                "SELECT posting_id, low_price, last_price, title FROM listings"
            )}
            assert Decimal(str(rows[ID_B][1])) == Decimal("15.00"), "low stays a low"
            assert Decimal(str(rows[ID_B][2])) == Decimal("40.00")
            assert rows[ID_B][3] == "Renamed"
            assert _rows(con, "SELECT event_key FROM pending") == [], (
                "no silent re-alert"
            )
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_record_observations_never_invents_a_listing_row() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, None)
        try:
            state.record_observations(con, {"u": [_item(ID_A, "1", "t")]})
            assert _rows(con, "SELECT posting_id FROM listings") == []
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_mark_sent_moves_pending_to_sent() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, None)
        try:
            state.apply_event(con, _event("new", ID_A, "1900", f"{ID_A}@new"), "m")
            state.mark_sent(con, f"{ID_A}@new")
            assert _rows(con, "SELECT event_key FROM pending") == []
            assert _rows(con, "SELECT event_key FROM sent") == [(f"{ID_A}@new",)]
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_failed_send_leaves_pending_without_sent() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, None)
        try:
            state.apply_event(con, _event("new", ID_A, "1900", f"{ID_A}@new"), "m")
            assert _rows(con, "SELECT event_key FROM pending") == [
                (f"{ID_A}@new",)
            ]
            assert _rows(con, "SELECT event_key FROM sent") == [], (
                "at-least-once: a failed send must not mark sent"
            )
        finally:
            con.close()
    finally:
        scratch.cleanup()


def test_restart_after_crash_replays_pending_exactly_once() -> None:
    scratch = _Scratch()
    try:
        con, _imported, _conflicts = open_state(scratch.dir, None)
        state.apply_event(con, _event("new", ID_A, "1900", f"{ID_A}@new"), "m")
        con.close()
        reopened, imported, conflicts = open_state(scratch.dir, None)
        try:
            assert (imported, conflicts) == (0, 0)
            assert [e["event_key"] for e in state.pending_events(reopened)] == [
                f"{ID_A}@new"
            ]
            assert [
                str(row[0])
                for row in reopened.execute("SELECT posting_id FROM listings")
            ] == [ID_A], "discovery row is durable, so no re-discovery alert"
        finally:
            reopened.close()
    finally:
        scratch.cleanup()


def test_schema_validation_refuses_newer_version() -> None:
    scratch = _Scratch()
    path = state_db_path(scratch.dir)
    con = sqlite3.connect(path)
    try:
        with con:
            _ = con.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
            _ = con.execute("INSERT INTO schema (version) VALUES (?)", (99,))
        try:
            validate_state_schema(con)
        except StateError as exc:
            assert "newer than supported" in str(exc)
        else:
            raise AssertionError("newer schema accepted")
    finally:
        con.close()
        scratch.cleanup()


def test_schema_validation_refuses_partial_tables() -> None:
    scratch = _Scratch()
    path = state_db_path(scratch.dir)
    con = sqlite3.connect(path)
    try:
        with con:
            _ = con.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
            _ = con.execute("INSERT INTO schema (version) VALUES (?)", (1,))
            _ = con.execute("CREATE TABLE listings (posting_id TEXT)")
        try:
            validate_state_schema(con)
        except StateError as exc:
            assert "listings" in str(exc)
        else:
            raise AssertionError("partial schema accepted")
    finally:
        con.close()
        scratch.cleanup()


def test_open_state_never_resets_rejected_db() -> None:
    scratch = _Scratch()
    path = state_db_path(scratch.dir)
    con = sqlite3.connect(path)
    with con:
        _ = con.execute("CREATE TABLE schema (version INTEGER PRIMARY KEY)")
        _ = con.execute("INSERT INTO schema (version) VALUES (?)", (0,))
    con.close()
    try:
        try:
            open_state(scratch.dir, None)
        except StateError as exc:
            assert "older than supported" in str(exc)
        else:
            raise AssertionError("unsupported db opened")
        assert sqlite3.connect(path).execute(
            "SELECT version FROM schema"
        ).fetchall() == [
            (0,)
        ], "rejected state must stay on disk untouched"
    finally:
        scratch.cleanup()


def test_instance_lock_blocks_second_holder() -> None:
    scratch = _Scratch()
    try:
        fd = state.acquire_instance_lock(scratch.dir)
        assert fd is not None
        try:
            assert state.acquire_instance_lock(scratch.dir) is None
        finally:
            state.release_instance_lock(fd)
        again = state.acquire_instance_lock(scratch.dir)
        assert again is not None, "kernel lock is releasable"
        state.release_instance_lock(again)
    finally:
        scratch.cleanup()


def test_readonly_uri_encodes_special_paths_and_refuses_writes() -> None:
    scratch = _Scratch()
    try:
        nested = os.path.join(scratch.dir, "sub dir")
        os.makedirs(nested)
        weird = os.path.join(nested, "st#ate%✓.sqlite")
        _seed_valid_db(weird)
        uri = state.readonly_uri(weird)
        assert uri.endswith("?mode=ro")
        body = uri[len("file://") : -len("?mode=ro")]
        assert "#" not in body and "?" not in body
        assert "%23" in body and "%E2%9C%93" in body
        ro = sqlite3.connect(uri)
        try:
            validate_state_schema(ro)
            try:
                _ = ro.execute("INSERT INTO sent (event_key) VALUES (?)", ("x",))
            except sqlite3.OperationalError as exc:
                assert "read-only" in str(exc) or "readonly" in str(exc)
            else:
                raise AssertionError("writable despite mode=ro")
        finally:
            ro.close()
    finally:
        scratch.cleanup()
