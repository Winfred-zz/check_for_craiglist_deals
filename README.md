# check_for_craiglist_deals

Scans craigslist search URLs, learns known listings in a persistent SQLite
state DB, and posts a Discord alert the first time a posting appears or
drops below its known low price. Alerts fire only in the open window
(quiet hours are `[02:00, 12:00)` UTC); during quiet hours new alerts are
stored as pending and drained when the window opens.

See `design.md` for the full design contract, exit-code table, and
adopted decisions.

## CLI

    python check_for_craiglist_deals.py [--csv PATH] [--data-dir DIR]
        [--log-dir DIR] [--env-file PATH] [--fixture-dir DIR]
        [--once] [--dry-run] [--at ISO8601]

| Flag | Meaning |
| --- | --- |
| `--csv` | Search-list CSV (default `craigslist_deals_to_check.csv`, CWD-relative). Headers: `friendly_name,url`. |
| `--data-dir` | State directory: `state.sqlite`, instance lock `bot.lock`, legacy `known_deals.csv` join (default `data`). |
| `--log-dir` | Rotating `bot.log` + console stderr (default `logs`). |
| `--env-file` | `.env` with `DISCORD_TOKEN` + `CHANNEL_ID` (never in argv; values never logged). |
| `--fixture-dir` | Offline scan: read saved HTML instead of fetching; requires `<fixture-dir>/manifest.json` mapping each CSV search URL to an HTML file. Network/Discord code paths are never used. |
| `--once` | Single scan pass then exit. |
| `--dry-run` | Console-only logging; state DB opened **read-only**; never acquires the instance lock. |
| `--at` | Override the clock for deterministic offline runs; aware ISO timestamps only (naive refused). |

Exit codes: `0` clean; `1` unexpected runtime error (or `--once` scan
failures); `2` refused config/state (bad CSV, `.env`, corrupt DB,
unwritable mount dirs); `3` another instance holds the lock (or startup
drain failed); `4` Discord login rejected.

## State

- `data/state.sqlite` — tables `listings`, `pending`, `sent`, `schema`;
  a new listing/price-drop event is applied atomically (listing + pending
  event in one transaction; send marks `sent` and deletes `pending` in
  one transaction). Crash-safe: at-least-once delivery.
- Legacy `known_deals.csv` (headers `title,original_price,current_price,url`)
  in the data dir migrates once into `listings`; the original file is
  renamed `known_deals.csv.migrated-<UTC-stamp>.bak` (never written in
  place). Duplicate-id rows merge to the min low-price; conflicts are
  reported, never silently discarded.
- A partial or corrupt DB is refused (exit 2); history is never reset.

## Tests (offline)

    python -m pytest tests           # hermetic; conftest blocks non-loopback connects
    ruff check .                     # clean per pyproject.toml (no per-file ignores)

Fixture scans need `manifest.json`; delivery inside fixture mode uses the
stub transport — set `CHECK_FOR_CRAIGLIST_DEALS_STUB_FAIL_AFTER=N` to
simulate the first N send attempts failing (pending stays durable;
next window-open run redelivers).

## Docker

The image runs as uid 1000 (`cfd`); state/log dirs default to
`/svc/data`, `/svc/logs`. Mount writable host dirs or named volumes there
(unwritable mounts are refused with exit 2). `docker stop` (SIGTERM)
drains pending alerts, then exits 0; pending events stay durable.
Named volumes are created with uid-1000 ownership by the image; plain
bind mounts must already be writable by uid 1000.

    docker build -t cfd .
    docker volume create cfdstate && docker volume create cfdlogs
    docker run --rm -v cfdstate:/svc/data -v cfdlogs:/svc/logs \
      --env-file .env cfd
