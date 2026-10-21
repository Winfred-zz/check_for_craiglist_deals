# check_for_craiglist_deals — modernization design

Status: implementation in progress against `handoff/0001.md`, 2026-10-19/20. The
behavioral contract below was adopted as the recommended set (owner absent; the
resuming session was told to adopt design.md recommendations and record them —
see "Adopted decisions" at the end of this section). Detailed evidence,
implementation order, and acceptance checklists: `handoff/0001.md` (local, Git-ignored),
with `handoff/0002.md` as the verification record. Read `AGENTS.md` and the latest
handoff before continuing. README now describes the modernized application.

## Purpose and boundaries

Bring the existing Craigslist-to-Discord monitor to the eBay project's engineering
standard: deterministic decisions, durable notifications, bounded failures, clean
startup/shutdown, reproducible installation, meaningful offline tests, and a usable
container. Retain Craigslist's **new-listing and price-drop** purpose. Do not copy
eBay's one-alert-per-item/no-price-drop policy or its HTML parser.

This is a small single-process application, not a shared scraping framework.
No commands, plugin system, distributed workers, anti-bot bypass, new marketplace,
price-ceiling CSV column, or official-API migration is required. Preserve the
existing project/script spelling (`craiglist`) and CSV spelling (`craigslist`).

## Current behavior and hazards

The script reads `friendly_name,url` search rows and a global known-deals CSV.
It scans only at host-local hours 00–01 and 13–23, emits all discoveries and
qualifying decreases, and records them before delivery. The opening description
claiming only one event per hour is not enforced. Known `current_price` decreases
but never increases: price-drop detection currently means a new historical low,
not any decrease from the immediately preceding observation.

Assessment reproduced premature persistence, loss of a newly appended row when a
later price-drop rewrite occurs, missing-price inheritance, and cent truncation.
These are correctness defects, not reasons to preserve the CSV implementation.

## Proposed behavioral contract

These are Craigslist-specific recommendations. **Adopted 2026-10-20 as the
contract below** (owner absent — the resumed session was instructed not to ask
questions and to record adopted defaults; each row is the shipped behavior):

| Area | Recommended contract | Relationship to legacy behavior |
|---|---|---|
| Events | Alert for each newly discovered valid listing and each new historical-low price. Emit all eligible events per scan. | Preserves executable behavior over contradictory comments. |
| Price increases | Record last observed price separately; compare decreases with the historical low. A rebound then return to an already-reported price does not alert. | Preserves existing low-watermark behavior. |
| Identity | Global deduplication by numeric posting ID extracted from a validated Craigslist listing URL; retain canonical URL for display. | Replaces fragile exact-URL equality; different IDs/reposts remain new. |
| Overlapping searches | First CSV row discovering an event supplies its search context; emit that event once. | Makes within-scan behavior deterministic. |
| First run | Existing visible listings are new and eligible; no implicit silent baseline. | Preserves discovery behavior. |
| Money | Exact USD cents, nonnegative. Reject missing, malformed, range, and unsupported-currency values rather than guessing; accept an explicitly displayed zero. Search URL defines filters. | Fixes stale/unbound values and integer truncation. No eBay ceiling or shipping rule. |
| Quiet hours | UTC [02:00,12:00); continue scanning, store events, deliver outside the window. Pending events delivered as discovered, without revalidation. | Deliberate proposal: legacy skips scans and excludes the whole noon hour. |
| Pending decreases | Distinct new lows are distinct durable events, ordered by discovery; do not silently coalesce/drop them. | Makes downtime/quiet backlog behavior explicit. |
| Delivery | At-least-once, durable outbox. Only successful sends acknowledge events; failures remain pending. | Fixes lost-alert window; a crash after send but before acknowledgement can duplicate. |
| Cadence | One scan, then a cancellable one-hour wait; no overlap or catch-up burst. Requests sequential, with documented pacing and bounded per-search budget. | Explicit finish-to-start scheduling replaces incidental task/sleep timing. |
| Failures | Bad config/state stops startup; per-search transient or recognizable page failure is logged as failed, not empty, and later searches continue. Persistence failure is fatal. | Removes silent failure and unsafe continuation. |
| Runtime | Python 3.12 baseline, one instance per state directory, cooperative SIGTERM shutdown within ten seconds. | New explicit support/operational guarantees. |

No active deployment status, production dataset, live selector validity, or permission
to contact Craigslist/Discord has been established. Preserve any discovered local
state; do not assume the eBay owner's stopped-bot statement applies here.

## Architecture

Keep the existing script as the entry point. Introduce only modules with a real
boundary; names below are proposed, not files that already exist.

| Component | Responsibility |
|---|---|
| `check_for_craiglist_deals.py` | CLI/main, logging setup, signal handling, Discord lifecycle, sequential scan orchestration. Import has no side effects. |
| `config.py` | Search CSV, environment, optional env file, validated paths and HTTPS Craigslist search URLs. |
| `acquire.py` | HTTP session, status classification, bounded transient retries and Retry-After within a total deadline; cancellation. |
| `parse.py` | Pure HTML-to-listings parser, per-card validation, exact amounts, posting IDs/canonical URLs, explicit page outcomes. |
| `selection.py` | Deterministic new-listing/new-low events from validated listings and prior state. No network or filesystem. |
| `state.py` | SQLite schema, atomic observation/event writes, pending/sent transitions, migration, single-instance enforcement. |
| `notify.py` | Safe rendering and Discord delivery boundary; pending drain stops or preserves failures according to documented policy. |
| `clock.py` | Injected aware clock and quiet-window predicate, only if enough logic warrants its own module. |

Do not extract a generic helper layer. Remove obsolete helper/command boilerplate
once all callers have migrated. Configure standard-library rotating logging once
in main: INFO console, DEBUG file, explicit actual retention and no token logging.
Single-instance operation does not require multiprocess logging infrastructure.

## State and delivery invariants

Use SQLite under configurable `data/`; keep user input under `configs/` and logs
under `logs/`. Default paths resolve from a documented application root, not cwd.
An explicit data root governs writable paths for containers and smoke runs.

- A listing stores ID, canonical URL, title, original price, latest observation,
  historical low, and observation time. Pending events store kind, price transition,
  search context, creation time, and the complete information needed after restart.
- Updating a listing's historical low and creating the corresponding outbox event
  happens in one transaction. A new low must never become suppressed without a
  durable pending event. Duplicate observations do not create duplicate events.
- Acknowledgement occurs after successful delivery only. No transaction remains
  open across a network send. Document the unavoidable duplicate crash window.
- In-memory and durable views agree after each search. New listings cannot disappear
  when a different listing changes price. Do not rewrite a whole CSV per item.
- Version the schema. Refuse corrupt, unreadable, or unsupported existing state;
  missing state is a separate first-run case. Never reset history to make startup work.
- One process owns each state root; use an established OS lock strategy supported by
  the documented platform. Verify concurrent startup and automatic release after death.

### Legacy migration

Back up `configs/known_deals.csv` before migration, preserving it locally. Parse the
exact legacy headers `title,original_price,current_price,url`; import valid rows
as already-known listings without issuing discovery alerts. `current_price` is the
legacy low-watermark; the true latest observed price cannot be recovered. Existing
missed notifications and truncated cents cannot be reconstructed.

For duplicate posting IDs with differing URLs/rows, use the minimum valid recorded
current price as the low-watermark, retain the first valid original-price/title
record deterministically, and report conflicts. Invalid rows must produce actionable
errors with row numbers; do not silently discard history. Commit migration atomically,
record its completion, and make rerunning it idempotent. Keep backup until verified.

## HTTP and parser contract

Only supported HTTPS Craigslist hosts/search paths are accepted; validate redirect
and listing targets too. Respect access restrictions; recognize known deny/challenge
pages as failures rather than bypassing them. Fixtures establish parser behavior,
not proof of today's live markup. Keep TLS verification on.

Separate successful empty results, malformed individual cards, and page failure.
A malformed card must not prevent later valid siblings; reset all fields per card.
Never reuse a previous card's amount, accept arbitrary hosts, or strip every
non-digit and call the result USD. Handle expected static-result markup first;
unexpected markup must be visible as unsupported rather than quietly "no deals".
Set concrete connect/read limits, attempt count, overall request budget and pacing
during implementation; verify cancellation across both requests and retry waits.

## Planned operator and offline interfaces

Proposed flags (not implemented): `--once`, `--dry-run`, `--csv`, `--data-dir`,
`--log-dir`, `--env-file`, `--fixture-dir`, and timezone-aware `--at`.
Dry-run uses the production config/parser/selection/rendering path but performs
zero sends, migrations, state writes, locks, or directory creation. Existing state
may be read without mutation; absent state is simulated in memory. Fixture mode
must replace acquisition, never secretly fall back to live HTTP. Offline non-dry
integration runs may write isolated temporary state and use a controlled delivery
sink that can succeed/fail; that sink is never a live-path fallback.

Use actionable exit statuses: 0 successful run, 1 unexpected runtime failure,
2 refused configuration/state, 3 instance conflict, 4 rejected Discord login.
A failed search in `--once` must produce a nonzero scan outcome even while allowing
other searches to finish; long-running mode logs it and proceeds to the next cycle.

Docker should contain application code and canonical dependencies, use a supported
Python base and non-root account, and mount writable state/logs independently from
read-only configs. `.dockerignore` must exclude secrets, state, logs, handoffs,
agent instructions, environments, and development artifacts. Do not copy eBay's
Dockerfile blindly; validate this project's paths, volume ownership, and entry point.

## Verification strategy

Use pytest and Ruff; no coverage quota or placeholder tests. Guard tests against
external HTTP/Discord calls. Loopback servers are permitted for controlled acquisition
integration. Use sanitized representative HTML and deterministic aware clocks.

1. Parser and decision tests: malformed cards, missing amounts, currency/ranges,
   cents, IDs/URLs, overlap, new item, new low, rise/rebound, restart deduplication.
2. State/delivery integration: new item plus drop in one scan, atomic rollback,
   migration/duplicates/corruption, successful and failed sends, partial batch failure,
   quiet backlog and restart. Assert durable contents, not only mock calls.
3. Actual CLI smokes: fixtures and temporary directories, foreign cwd, read-only
   dry-run, invalid configuration, controlled send failure/restart, concurrent startup,
   and real process SIGTERM. Record output and exit status.
4. Fresh environment install, dependency consistency, full offline suite, lint,
   container build/non-root execution and persistence across recreation.
5. CI runs the useful suite and real offline CLI smoke with no secrets, read-only
   repository permissions, timeout, and reviewed action versions. A workflow file
   does not prove merge protection; report actual repository-setting limits.

Live markup and Discord delivery require separately authorized checks. Record any
unexercised surface explicitly rather than copying eBay's verification claims.

## Adopted decisions (implementation-time, verified by tests unless noted)

- Legacy known-deals CSV source: `<data-dir>/known_deals.csv` (migration only
  when the run opens state writable). Migrated file renamed to
  `known_deals.csv.migrated-<UTC-stamp>.bak`; second run imports nothing.
- Dry runs: console-only logging (no `logs/` dir, no `bot.log`); state DB
  opened via read-only `file:` URI; sqlite3 refuses writes on that handle.
- SIGTERM handler is registered before lock/state acquisition so shutdown is
  always handled; Shutdown unwinds scan+delivery loops into a bounded drain,
  then exit 0. Tests observe the lock file before signalling (startup race
  window is interpreter imports, not our lifecycle).
- Event identity: posting id (9-12 digits from `/d/.../<id>.html`) + kind
  (`new`/`price-drop`); event_key `{id}@new` / `{id}@{cents}`. Sent rows are
  keyed the same, so restarts never re-deliver.
- At-least-once durable outbox: event applied to listings+pending atomically
  BEFORE send; mark sent only after successful send. Failure/restart redelivers.
- Quiet hours suppress delivery only, never scans (UTC, [02:00,12:00) —
  converted from host tz inside `clock.is_quiet`).
- Exit codes exercised offline: 0 clean; 1 unexpected runtime + `--once` scan
  with failed searches; 2 refused config/state (incl. corrupt DB, unsafe data
  dir, non-loopback `socket.connect` guard hit, `..._DISABLE_NETWORK=1` live
  refusal, fixture manifest gaps); 3 instance-lock refusal; 4 login rejected
  (UNEXERCISED: needs live Discord credentials — see Verification).
- Fixture mode is the only network-free production path: fixture-dir manifest
  json maps search URL → saved HTML; every CSV row needs an entry (else exit
  2 "fixture manifest missing ..."), keeping fixture runs honest.
- Test env `..._STUB_FAIL_AFTER=N` makes the stub transport raise after N
  successful deliveries (delivery-failure/pending-redelivery regressions).
- CLI defaults stay relative to the repo/app dir via module constants; README uses
  absolute volume mounts. Legacy known_deals.csv is read from the DATA dir
