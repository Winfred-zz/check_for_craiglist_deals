"""Hourly Craigslist deal scanner with durable alerts and a bounded shutdown.

Approved lifecycle (design.md + handoff §4):
- Every config is validated BEFORE any scan or send; config errors exit
  2 with an actionable message. Search CSV URLs are validated to https
  Craigslist hosts (no arbitrary-URL fetches, F17). The Discord token
  value is never echoed.
- Logging is set up once, here: console INFO + rotating file DEBUG
  (logs/bot.log, 1 MiB x 5 backups - the old code's "2 MB" claim was
  wrong, F23). Uncaught failures log CRITICAL with traceback.
- One instance at a time: a kernel advisory lock (fcntl) on a stable
  lockfile inode in the data dir; a second process refuses to start
  (exit 3). A dead holder's lock is released by the kernel (no stale
  PID probing).
- Hourly scans, finish-to-start, never overlapping. Quiet hours (UTC
  02:00-12:00) suppress DELIVERY only: scans run and new events are
  stored pending; the window-open scan delivers all pending.
- At-least-once delivery: pending rows are written (atomically with the
  observation update) before the send attempt, mark sent only after a
  successful send. A crash between send and mark-sent can duplicate
  within a small window (approved); alerts are never lost, and no
  listing can be "forgotten" mid-rewrite (F09's CSV-rewrite bug is
  gone: the durable CSV is never rewritten, imports are INSERT OR
  IGNORE with a one-time backup copy).
- SIGTERM raises Shutdown inside the running code; the drain is bounded
  (~10 s worst case), releases the instance lock, and pending events
  are already durable.

Offline modes: --fixture-dir + manifest.json (search URL -> fixture html)
replaces network fetches with local HTML and a stub transport for
delivery; --dry-run prints alert identities, opens state only read-only,
and never touches Discord. With CHECK_FOR_CRAIGLIST_DEALS_DISABLE_NETWORK=1
set, a production (non-fixture) scan refuses to start, so an accidental
network call fails loudly.
"""

from collections.abc import Awaitable, Callable

from typing import cast

from config import EnvValues, SearchRow

from parse import ItemRecord

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sqlite3
import sys
from datetime import datetime, timezone

import colorlog
import discord
from discord.abc import Messageable
import requests

import acquire
import clock
import config
import deliver
import notify
import parse
import selection
import state

DEFAULT_CSV = os.path.join("configs", "craigslist_deals_to_check.csv")
DEFAULT_DATA_DIR = "data"
LEGACY_KNOWN_FILE = state.LEGACY_KNOWN_FILE
STUB_FAIL_AFTER_ENV = "CHECK_FOR_CRAIGLIST_DEALS_STUB_FAIL_AFTER"
DISABLE_NETWORK_ENV = "CHECK_FOR_CRAIGLIST_DEALS_DISABLE_NETWORK"
SCAN_INTERVAL_SECONDS = 3600
LOG_DIR = "logs"

cancel_requested = False


class Shutdown(BaseException):
    """Raised from the SIGTERM handler; unwinds into a bounded drain."""


class LoginRejected(BaseException):
    """Discord login rejected: exit 4, the token is never echoed."""


def setup_logging(log_dir: str, console_only: bool = False) -> logging.Logger:
    """Console (colorlog, INFO) + rotating file (DEBUG); console-only for dry."""
    logger = logging.getLogger("check_for_craiglist_deals")
    console = colorlog.StreamHandler()
    console.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(levelname)s%(reset)s %(message)s",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
    )
    console.setLevel(logging.INFO)
    if console_only:
        logger.setLevel(logging.INFO)
        logger.handlers = [console]
        return logger
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        raise config.ConfigError(f"cannot create log dir: {exc}") from exc
    try:
        rotating = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "bot.log"),
            maxBytes=1_000_000,
            backupCount=5,
        )
    except OSError as exc:
        raise config.ConfigError(f"cannot open log file: {exc}") from exc
    rotating.setLevel(logging.DEBUG)
    rotating.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.setLevel(logging.DEBUG)
    logger.handlers = [console, rotating]
    return logger


def parse_at(value: str) -> datetime:
    """--at: ISO timestamp with UTC offset or trailing Z; deterministic clock."""
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise config.ConfigError(
                f"--at must be ISO UTC (e.g. 2026-10-19T13:00:00Z): {value!r}"
            ) from None
        if moment.tzinfo is None:
            raise config.ConfigError(
                "--at must carry a UTC offset or Z (got naive)"
            )
    else:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)

def open_state_readonly(data_dir: str) -> sqlite3.Connection | None:
    """Dry runs open the DB read-only; absent DB means empty state."""
    db_path = state.state_db_path(data_dir)
    if not os.path.isfile(db_path):
        return None
    try:
        con = sqlite3.connect(state.readonly_uri(db_path))
    except sqlite3.Error as exc:
        raise state.StateError(f"state database unreadable: {exc}") from exc
    try:
        state.validate_state_schema(con)
    except state.StateError:
        con.close()
        raise
    return con


def load_fixture_manifest(
    fixture_dir: str,
    rows: list[SearchRow],
) -> dict[str, str]:
    """Validate manifest.json (search URL -> fixture html) against CSV rows."""
    manifest_path = os.path.join(fixture_dir, "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise config.ConfigError(f"fixture manifest unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise config.ConfigError("fixture manifest must be a JSON object")
    manifest = {str(key): str(val) for key, val in raw.items()}
    missing = [row["url"] for row in rows if row["url"] not in manifest]
    if missing:
        raise config.ConfigError(
            "fixture manifest is missing entries for search URLs: "
            + ", ".join(missing)
        )
    return manifest


async def scan_pass(
    rows: list[SearchRow],
    con: sqlite3.Connection | None,
    fixture_dir: str | None,
    manifest: dict[str, str],
    session: requests.Session | None,
    client: discord.Client | None,
    sender: Callable[[str], Awaitable[None]] | None,
    channel_id: int | None,
    dry: bool,
    now: datetime,
    logger: logging.Logger,
) -> int:
    """One scan: acquire (fixture or live), parse, select, persist, deliver.

    Returns the number of searches that failed (transient/permanent fetch
    or denied/suspicious page): monitoring continues past them, but the
    caller must surface a nonzero outcome for a failed --once scan.
    """
    items_by_search: dict[str, list[ItemRecord]] = {}
    failed_searches = 0
    for row in rows:
        search_url = row["url"]
        try:
            if fixture_dir is not None:
                with open(
                    os.path.join(fixture_dir, manifest[search_url]),
                    encoding="utf-8",
                ) as handle:
                    html = handle.read()
            else:
                assert session is not None  # live scan: async_main provides one
                html = acquire.fetch_search_page(
                    session,
                    search_url,
                    cancel_check=lambda: cancel_requested,
                )
        except acquire.AcquireTransient as exc:
            failed_searches += 1
            logger.error(
                "transient scan failure for %r (monitoring continues): %s",
                search_url,
                exc,
            )
            continue
        except acquire.AcquirePermanent as exc:
            failed_searches += 1
            logger.error(
                "permanent scan failure for %r (monitoring continues): %s",
                search_url,
                exc,
            )
            continue
        layout = acquire.layout_status(html)
        if layout == "denied":
            failed_searches += 1
            logger.warning(
                "search page for %r looks like a deny/challenge page; skipped",
                search_url,
            )
            continue
        if layout == "suspicious":
            failed_searches += 1
            logger.warning(
                "search page for %r has neither cards nor known markers;"
                " not treated as a clean empty scan",
                search_url,
            )
            continue
        items, rejected = parse.parse_search_html(html)
        for card in rejected:
            logger.info(
                "rejected card for %r: %s at position %d",
                search_url,
                card["reason"],
                card["position"],
            )
        items_by_search[search_url] = items
    known = state.listings_snapshot(con) if con is not None else {}
    events = selection.derive_events(rows, items_by_search, known)
    for event in events:
        try:
            message = notify.render_alert_message(event)
        except ValueError as exc:
            logger.error("alert render refused (id %s): %s", event["posting_id"], exc)
            continue
        if dry:
            print(
                f"DRY-RUN ALERT: event={event['event_key']}"
                f" kind={event['kind']} id={event['posting_id']}"
                f" price_usd={event['price_usd']}"
                f" query={event['search_url']}",
                flush=True,
            )
            continue
        assert con is not None  # non-dry scan: main provides a writable DB
        state.apply_event(con, event, message)
    if dry:
        logger.info(
            "dry run: state DB opened read-only; no writes, no sends (%d events)"
            % len(events)
        )
        return failed_searches
    assert con is not None  # non-dry: open_state guarantees writable state
    state.record_observations(con, items_by_search)
    quiet = clock.is_quiet(now)
    if quiet:
        logger.info(
            "quiet hours (UTC 02:00-12:00): %d new pending stored,"
            " zero delivery attempts",
            len(events),
        )
        return failed_searches
    if client is not None:
        assert channel_id is not None
        try:
            channel = await client.fetch_channel(channel_id)
        except (discord.errors.Forbidden, discord.errors.NotFound):
            channel = None
        if channel is None:
            logger.error(
                "channel %d not fetchable: permission state error;"
                " pending events stay pending",
                channel_id,
            )
            return failed_searches
        if not hasattr(channel, "send"):
            logger.error(
                "channel %d is not messageable (e.g. voice); events stay"
                " pending",
                channel_id,
            )
            return failed_searches
        sender = deliver.make_discord_deliverer(cast("Messageable", channel))
    assert sender is not None
    for pending in state.pending_events(con):
        try:
            await sender(pending["message"])
        except (deliver.DeliverFailed, discord.errors.HTTPException) as exc:
            logger.error(
                "delivery attempt failed for %s; event stays pending: %s",
                pending["event_key"],
                exc,
            )
            continue
        state.mark_sent(con, pending["event_key"])
        logger.info("alert delivered and marked sent: %s", pending["event_key"])
    return failed_searches


async def async_main(
    dry: bool,
    fixture_dir: str | None,
    once: bool,
    rows: list[SearchRow],
    con: sqlite3.Connection | None,
    env: EnvValues | None,
    manifest: dict[str, str] | None,
    start_at: datetime | None,
    logger: logging.Logger,
) -> int:
    """Login once (production path), then loop scans hourly, finish-to-start."""
    client: discord.Client | None = None
    session: requests.Session | None = None
    sender: Callable[[str], Awaitable[None]] | None = None
    channel_id: int | None = None
    if dry:
        sender = None
        if fixture_dir is None:
            session = requests.Session()  # dry-live scans still fetch pages
    elif fixture_dir:
        raw = os.environ.get(STUB_FAIL_AFTER_ENV)
        stub = deliver.StubTransport(fail_after=int(raw) if raw else None)
        sender = stub.deliver
    else:
        assert env is not None  # live run: main loaded env before login
        client = discord.Client(intents=discord.Intents.default())
        try:
            await client.login(env["discord_token"])
            await client.connect(reconnect=False)
            await client.wait_until_ready()
        except (discord.LoginFailure, discord.errors.HTTPException) as exc:
            raise LoginRejected(str(exc)) from exc
        channel_id = env["channel_id"]
        logger.info(
            "Discord login complete (banner); monitoring channel %d",
            channel_id,
        )
        session = requests.Session()
    total_failures = 0
    first = True
    manifest_map: dict[str, str] = manifest or {}
    while True:
        if cancel_requested:
            logger.warning("cancellation requested; leaving scan loop")
            break
        now = start_at if first and start_at else clock.utc_now()
        first = False
        total_failures += await scan_pass(
            rows,
            con,
            fixture_dir,
            manifest_map,
            session,
            client,
            sender,
            channel_id,
            dry,
            now,
            logger,
        )
        if once:
            break
        finish = clock.utc_now()
        delay = SCAN_INTERVAL_SECONDS - (clock.utc_now() - finish).total_seconds()
        while delay > 0 and not cancel_requested:
            await asyncio.sleep(min(delay, 60.0))
            delay = SCAN_INTERVAL_SECONDS - (clock.utc_now() - finish).total_seconds()
    if client is not None:
        await client.close()
    return total_failures


def sigterm_handler(signum: int, frame: object) -> None:
    """Cancel the running scan; Shutdown unwinds through blocking calls."""
    global cancel_requested
    del signum, frame
    cancel_requested = True
    raise Shutdown()


def main(argv: list[str] | None = None) -> int:
    """Validate config, guard single instance, run the async lifecycle."""
    parser = argparse.ArgumentParser(
        description="Scan Craigslist searches hourly and alert one Discord channel."
    )
    _ = parser.add_argument(
        "--once",
        action="store_true",
        help="run a single scan and exit (for cron/manual runs)",
    )
    _ = parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print alert identities; never send and never write state",
    )
    _ = parser.add_argument(
        "--fixture-dir",
        metavar="DIR",
        help="offline fixture dir with manifest.json (never contacts Craigslist)",
    )
    _ = parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="state dir holding state.sqlite and the instance lock",
    )
    _ = parser.add_argument(
        "--log-dir",
        default=LOG_DIR,
        help="log dir for the rotating bot.log",
    )
    _ = parser.add_argument(
        "--at",
        metavar="ISO-UTC",
        help="deterministic scan clock (e.g. 2026-10-19T13:00:00Z)",
    )
    _ = parser.add_argument(
        "--env-file",
        help="optional .env file; process environment values win",
    )
    _ = parser.add_argument(
        "--csv", default=DEFAULT_CSV, help="Craigslist search CSV path"
    )
    args = parser.parse_args(argv)

    dry_run = cast("bool", args.dry_run)
    once = cast("bool", args.once)
    csv_path = cast("str", args.csv)
    data_dir = cast("str", args.data_dir)
    log_dir = cast("str", args.log_dir)
    env_file = cast("str | None", args.env_file)
    at_raw = cast("str | None", args.at)
    fixture_dir = cast("str | None", args.fixture_dir)

    try:
        logger = setup_logging(log_dir, console_only=dry_run)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    env: EnvValues | None = None
    start_at: datetime | None = None
    manifest: dict[str, str] | None = None
    con: sqlite3.Connection | None = None
    lock_fd: int | None = None
    _ = signal.signal(signal.SIGTERM, sigterm_handler)
    exit_code = 0
    failures = 0
    try:
        rows = config.load_search_rows(csv_path)
        if at_raw:
            start_at = parse_at(at_raw)
        if fixture_dir:
            manifest = load_fixture_manifest(fixture_dir, rows)
        if not dry_run and not fixture_dir:
            if os.environ.get(DISABLE_NETWORK_ENV) == "1":
                raise config.ConfigError(
                    "network disabled for this run; production scans need live"
                    " Craigslist access (use --fixture-dir offline)"
                )
            env = config.load_env(env_file)
        if dry_run:
            con = open_state_readonly(data_dir)
        else:
            _ = config.ensure_writable_dir(data_dir)
            con, imported, conflicts = state.open_state(
                data_dir,
                os.path.join(data_dir, LEGACY_KNOWN_FILE),
            )
            if imported:
                logger.info(
                    "legacy known-deals CSV imported into state DB:"
                    " %d postings merged (%d duplicate conflicts)",
                    imported,
                    conflicts,
                )
        if not dry_run:
            lock_fd = state.acquire_instance_lock(data_dir)
            if lock_fd is None:
                logger.critical(
                    "another instance holds the lock in %s: refusing to start a"
                    " second scanner",
                    data_dir,
                )
                print(
                    f"config error: another instance holds the lock in {data_dir}",
                    file=sys.stderr,
                )
                return 3
        failures = asyncio.run(
            async_main(
                dry_run,
                fixture_dir,
                once,
                rows,
                con,
                env,
                manifest,
                start_at,
                logger,
            )
        )
    except Shutdown:
        logger.info("SIGTERM drain complete; pending events stay durable")
    except deliver.DeliverFailed as exc:
        logger.critical(
            "startup drain delivery failed: %s (pending events stay durable,"
            " next window retries)",
            exc,
        )
        exit_code = 3
    except (config.ConfigError, state.StateError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        logger.critical("startup validation failed: %s", exc)
        return 2
    except LoginRejected as exc:
        logger.critical("Discord login rejected: %s", exc)
        exit_code = 4
    except KeyboardInterrupt:
        logger.warning("interrupted by SIGINT")
        exit_code = 130
    except Exception:  # last-resort: log, never swallow silently
        logger.critical("uncaught failure:", exc_info=True)
        exit_code = 1
    finally:
        if con is not None:
            con.close()
        if lock_fd is not None:
            state.release_instance_lock(lock_fd)
    if once and failures > 0 and exit_code == 0:
        print(
            f"scan outcome: {failures} search(es) failed this run",
            file=sys.stderr,
        )
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
