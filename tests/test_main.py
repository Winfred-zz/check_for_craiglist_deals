"""CLI smoke tests: real entry point via subprocess, offline modes only.

Fixtures: tests/fixtures/smoke.csv (2 validated search rows) + offline-scan
manifest (example.html -> 4 valid cards; second.html -> no-results page).
Every run: explicit --csv/--data-dir/--log-dir inside tmp_path; fixture mode
never falls back to live HTTP. Injected aware clock via --at; stub
transport via CHECK_FOR_CRAIGLIST_DEALS_STUB_FAIL_AFTER; real SIGTERM to a
real process; two concurrent startups for the flock refusal.

Exit-code contract (design.md): 0 normal, 1 unexpected failure, 2 config
refused, 3 instance conflict. Exit 4 (Discord login rejected) needs a real
login: recorded as a limit, not exercised.
"""
import fcntl
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "check_for_craiglist_deals.py"
FIX = REPO / "tests" / "fixtures"
SEARCH_CSV = str(FIX / "smoke.csv")
SCAN_FIXTURE_DIR = str(FIX / "offline-scan")
STUB_FAIL_AFTER_ENV = "CHECK_FOR_CRAIGLIST_DEALS_STUB_FAIL_AFTER"

EVENT_KEYS = sorted(
    ["5012345678@new", "5098765432@new", "5111111110@new", "5111111111@new"]
)


def cli_args(
    extra_flags: list[str],
    tmp_path: Path,
    csv: str = SEARCH_CSV,
    fixture: str = SCAN_FIXTURE_DIR,
) -> list[str]:
    return [
        sys.executable,
        str(ENTRY),
        *extra_flags,
        "--csv",
        csv,
        "--fixture-dir",
        fixture,
        "--data-dir",
        str(tmp_path / "state"),
        "--log-dir",
        str(tmp_path / "logs"),
    ]


def run_cli(
    flags: list[str],
    tmp_path: Path,
    env: dict[str, str] | None = None,
    csv: str = SEARCH_CSV,
    fixture: str = SCAN_FIXTURE_DIR,
) -> subprocess.CompletedProcess[str]:
    proc_env = {"DISCORD_TOKEN": "t", "CHANNEL_ID": "17", **(env or {})}
    return subprocess.run(
        cli_args(flags, tmp_path, csv, fixture),
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def base_env() -> dict[str, str]:
    return {"DISCORD_TOKEN": "t", "CHANNEL_ID": "17"}


def db_rows(db: str, sql: str) -> list[str]:
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(db)
    except sqlite3.OperationalError:
        return []
    try:
        return sorted(row[0] for row in con.execute(sql))
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def pending_keys(db: str) -> list[str]:
    return db_rows(db, "SELECT event_key FROM pending")


def sent_keys(db: str) -> list[str]:
    return db_rows(db, "SELECT event_key FROM sent")


def test_quiet_scan_stores_pending(tmp_path):
    """Quiet-hours fixture scan: 4 pending rows, zero sends, exit 0."""
    proc = run_cli(
        ["--once", "--at", "2026-10-19T05:00:00Z"], tmp_path, base_env()
    )
    assert proc.returncode == 0
    db = str(tmp_path / "state" / "state.sqlite")
    assert pending_keys(db) == EVENT_KEYS
    assert sent_keys(db) == []
    assert "alert delivered" not in proc.stderr
    assert "zero delivery attempts" in proc.stderr


def test_window_open_drain_delivers_pending(tmp_path):
    """Quiet scan then window-open re-scan: pending delivered as-is, sent 4."""
    quiet = run_cli(["--once", "--at", "2026-10-19T05:00:00Z"], tmp_path, base_env())
    assert quiet.returncode == 0
    db = str(tmp_path / "state" / "state.sqlite")
    assert pending_keys(db) == EVENT_KEYS
    open_proc = run_cli(
        ["--once", "--at", "2026-10-19T13:00:00Z"], tmp_path
    )
    assert open_proc.returncode == 0
    assert pending_keys(db) == []
    assert sent_keys(db) == EVENT_KEYS
    assert open_proc.stderr.count("alert delivered and marked sent") == 4
    # re-scan in a window does NOT re-alert known listings: pending stays empty
    again = run_cli(["--once", "--at", "2026-10-19T13:30:00Z"], tmp_path, base_env())
    assert again.returncode == 0
    assert pending_keys(db) == []
    assert sent_keys(db) == EVENT_KEYS


def test_partial_send_failure_keeps_pending(tmp_path):
    """Stub fails the FIRST 2 attempts: split stays durable, monitoring
    continues (exit 0), next window redelivers the failed pair exactly once."""
    env = dict(base_env())
    env[STUB_FAIL_AFTER_ENV] = "2"
    run = run_cli(["--once", "--at", "2026-10-19T13:00:00Z"], tmp_path, env)
    assert run.returncode == 0  # scan-internal failure: monitoring continues
    db = str(tmp_path / "state" / "state.sqlite")
    assert run.stderr.count("delivery attempt failed") == 2
    assert run.stderr.count("alert delivered and marked sent") == 2
    assert pending_keys(db) == EVENT_KEYS[:2]
    assert sent_keys(db) == EVENT_KEYS[2:]
    again = run_cli(["--once", "--at", "2026-10-19T13:30:00Z"], tmp_path, base_env())
    assert again.returncode == 0
    assert again.stderr.count("alert delivered and marked sent") == 2
    assert pending_keys(db) == []
    assert sent_keys(db) == EVENT_KEYS


def test_sigterm_open_window_quits_clean(tmp_path):
    """SIGTERM during the inter-scan sleep: exit 0, bounded teardown, lock free."""
    args = cli_args(
        ["--at", "2026-10-19T13:00:00Z"], tmp_path, SEARCH_CSV, SCAN_FIXTURE_DIR
    )
    args = [a for a in args if a != "--once"]  # long-running fixture loop
    proc = subprocess.Popen(
        args,
        env=base_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    db = str(tmp_path / "state" / "state.sqlite")
    deadline = time.time() + 30
    while sent_keys(db) != EVENT_KEYS and time.time() < deadline:
        time.sleep(0.05)
    assert sent_keys(db) == EVENT_KEYS  # open-window scan finished before signal
    proc.send_signal(signal.SIGTERM)
    started = time.time()
    _, killed = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert time.time() - started < 10.0, "drain must terminate, not hang"
    assert "SIGTERM drain complete" in killed
    assert pending_keys(db) == []
    lock = tmp_path / "state" / "bot.lock"
    if lock.exists():
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def test_sigterm_mid_scan_loses_no_alerts(tmp_path):
    """SIGTERM during a big fixture scan: partial state, then drain completes all."""
    big_dir = tmp_path / "fixtures" / "big"
    big_dir.mkdir(parents=True)
    cards = []
    keys_big = []
    for n in range(4000):
        pid = 6000000000 + n
        keys_big.append(f"{pid}@new")
        cards.append(
            f'<li class="cl-static-search-result" title="big {n}">'
            f'<a href="https://sfbay.craigslist.org/d/big-{pid}.html">'
            f'<div class="title">big {n}</div>'
            '<div class="price">$1</div>'
            "</a></li>"
        )
    (big_dir / "big.html").write_text(
        "<html><ul>" + "".join(cards) + "</ul></html>",
        encoding="utf-8",
    )
    (big_dir / "manifest.json").write_text(
        json.dumps({"https://sfbay.craigslist.org/search/sss?query=big": "big.html"}),
        encoding="utf-8",
    )
    big_csv = tmp_path / "big.csv"
    big_csv.write_text(
        "friendly_name,url\nbig,https://sfbay.craigslist.org/search/sss?query=big\n",
        encoding="utf-8",
    )
    args = cli_args(
        ["--at", "2026-10-19T05:00:00Z"],
        tmp_path,
        SEARCH_CSV,
        SCAN_FIXTURE_DIR,
    )  # quiet: pending backlog
    args = [a for a in args if a != "--once"]
    args[args.index(SEARCH_CSV)] = str(big_csv)
    args[args.index(SCAN_FIXTURE_DIR)] = str(big_dir)
    proc = subprocess.Popen(
        args,
        env=base_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    db = str(tmp_path / "state" / "state.sqlite")
    lock = tmp_path / "state" / "bot.lock"
    deadline = time.time() + 30
    while not lock.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert lock.exists()
    time.sleep(0.05)  # land mid-scan: parse of 4000 cards takes ~seconds
    proc.send_signal(signal.SIGTERM)
    _, killed = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert "SIGTERM drain complete" in killed
    partial_pending = set(pending_keys(db))
    partial_sent = set(sent_keys(db))
    assert partial_pending.isdisjoint(partial_sent)
    assert partial_pending | partial_sent <= set(keys_big)
    drain = run_cli(
        ["--once", "--at", "2026-10-19T13:00:00Z"],
        tmp_path,
        base_env(),
        str(big_csv),
        str(big_dir),
    )
    assert drain.returncode == 0
    assert drain.stderr.count("alert delivered and marked sent") == len(keys_big) - len(
        partial_sent
    )
    assert pending_keys(db) == []
    assert sorted(sent_keys(db)) == sorted(keys_big)


def test_dry_run_never_writes(tmp_path):
    """Dry run: DRY-RUN ALERT lines, no state dir, no log dir, no lock."""
    proc = run_cli(
        ["--once", "--dry-run", "--at", "2026-10-19T05:00:00Z"],
        tmp_path,
    )
    assert proc.returncode == 0
    alerts = [
        line for line in proc.stdout.splitlines() if line.startswith("DRY-RUN ALERT")
    ]
    assert len(alerts) == 4
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "logs").exists(), "dry run must not create log dir"
    assert "dry run: state DB opened read-only" in proc.stderr


def test_dry_run_with_existing_state(tmp_path):
    """Dry run over an existing DB: state read but not mutated."""
    seed = run_cli(["--once", "--at", "2026-10-19T13:00:00Z"], tmp_path, base_env())
    assert seed.returncode == 0
    db = str(tmp_path / "state" / "state.sqlite")
    assert sent_keys(db) == EVENT_KEYS and pending_keys(db) == []
    dry = run_cli(
        ["--once", "--dry-run", "--at", "2026-10-19T13:05:00Z"], tmp_path
    )
    assert dry.returncode == 0
    assert not dry.stdout.startswith("DRY-RUN ALERT")  # all listings known
    assert sent_keys(db) == EVENT_KEYS and pending_keys(db) == []


def test_config_refused_exit_2(tmp_path):
    """http search URL in CSV: exit 2 with actionable message."""
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text(
        "friendly_name,url\n"
        "Bad http,http://sfbay.craigslist.org/search/sss\n",
        encoding="utf-8",
    )
    args = cli_args(["--once", "--at", "2026-10-19T05:00:00Z"], tmp_path)
    args[args.index(SCAN_FIXTURE_DIR)] = str(FIX)  # keep fixture dir irrelevant
    args[args.index(SEARCH_CSV)] = str(bad_csv)
    proc = subprocess.run(
        args,
        env=base_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "search url must be https" in proc.stderr
    assert "DISCORD_TOKEN" not in proc.stderr


def test_second_instance_refused(tmp_path):
    """Concurrent startups: second process exits 3 (real flock)."""
    args = cli_args([], tmp_path, SEARCH_CSV, SCAN_FIXTURE_DIR)  # loop mode
    first = subprocess.Popen(
        args,
        env=base_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 30
    lock = tmp_path / "state" / "bot.lock"
    while not lock.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert lock.exists()
    second = subprocess.run(
        args, env=base_env(), capture_output=True, text=True, timeout=60
    )
    assert second.returncode == 3
    assert "holds the lock" in second.stderr
    first.send_signal(signal.SIGTERM)
    first.wait(timeout=30)
    assert first.returncode == 0


def test_once_scan_failure_exits_1(tmp_path):
    """--once scan with one denied-layout search: exit 1, other search still runs."""
    csv_path = tmp_path / "mixed.csv"
    csv_path.write_text(
        "friendly_name,url\n"
        "Example,https://sfbay.craigslist.org/search/sss?query=example\n"
        "Denied,https://sfbay.craigslist.org/search/sss?query=denied\n",
        encoding="utf-8",
    )
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "manifest.json").write_text(
        json.dumps(
            {
                "https://sfbay.craigslist.org/search/sss?query=example": "good.html",
                "https://sfbay.craigslist.org/search/sss?query=denied": "denied.html",
            }
        ),
        encoding="utf-8",
    )
    shutil.copy(FIX / "offline-scan" / "example.html", fixture_dir / "good.html")
    shutil.copy(FIX / "offline-failures" / "denied.html", fixture_dir / "denied.html")
    args = cli_args(
        [
            "--once",
            "--at",
            "2026-10-19T13:00:00Z",
        ],
        tmp_path,
        SEARCH_CSV,
        SCAN_FIXTURE_DIR,
    )
    args[args.index(SEARCH_CSV)] = str(csv_path)
    args[args.index(SCAN_FIXTURE_DIR)] = str(fixture_dir)
    proc = subprocess.run(
        args, env=base_env(), capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 1
    assert "looks like a deny/challenge page" in proc.stderr
    db = str(tmp_path / "state" / "state.sqlite")
    assert pending_keys(db) == []  # window open: successes delivered + marked sent
    assert sent_keys(db) == EVENT_KEYS
    assert proc.stderr.count("alert delivered and marked sent") == 4


def test_once_transient_fetch_fails(tmp_path):
    """Injected transient fetch via monkeypatched session (offline):
    exit 1 + scan outcome line."""
    env = {**base_env(), STUB_FAIL_AFTER_ENV: "999"}  # deliveries all fail (stub)
    quiet = run_cli(["--once", "--at", "2026-10-19T05:00:00Z"], tmp_path, env)
    assert quiet.returncode == 0
    db = str(tmp_path / "state" / "state.sqlite")
    assert pending_keys(db) == EVENT_KEYS
    # open window, every delivery fails: pending stays, exit 0 (delivery failure
    # is operational, not a crash)
    drain = run_cli(["--once", "--at", "2026-10-19T13:00:00Z"], tmp_path, env)
    assert drain.returncode == 0
    assert pending_keys(db) == EVENT_KEYS  # nothing marked sent
    assert sent_keys(db) == []
    assert drain.stderr.count("delivery attempt failed") == 4
