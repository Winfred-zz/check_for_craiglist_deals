"""HTTP acquisition for one Craigslist search URL, with bounded retries.

Separates acquisition from parsing so fixtures exercise the real parser
without network access. TLS verification stays at requests' default
(enabled) and anti-bot protections are not bypassed. Retry policy: only
transient conditions are retried — connection/timeout errors, 429 (honor
a valid Retry-After within the overall budget), and 5xx. Permanent 4xx is
NOT retried as if transient. Retry count and total time are bounded:
exhaustion raises AcquireTransient, distinct from "no matches".
Everything sleep- and network-related is injectable so tests run fast
offline or against a loopback server only.
"""

import time
import requests
from collections.abc import Callable

from parse import CARD_SELECTOR  # shares the card-selector convention

TRANSIENT_STATUSES = (429, 500, 502, 503, 504)


class AcquireTransient(Exception):
    """Retryable failures exhausted the bounded policy (recoverable)."""


class AcquirePermanent(Exception):
    """Non-retryable HTTP failure; caller must treat the scan as failed."""


def retry_delay_seconds(attempt: int, retry_after_header: str | None) -> float:
    """Backoff for attempt (0-based); honors Retry-After when sensible.

    Retry-After is clamped to [0, 300]s; invalid/HTTP-date forms fall back
    to exponential backoff (5s * 2^attempt, capped 300s).
    """
    if retry_after_header:
        try:
            seconds = float(int(retry_after_header))
        except ValueError:
            seconds = None
        if seconds is not None and 0 <= seconds <= 300:
            return max(seconds, 1.0)
    return min(5.0 * float(1 << attempt), 300.0)


def fetch_search_page(
    session: requests.Session,
    url: str,
    *,
    max_retries: int = 4,
    timeout: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    cancel_check: Callable[[], bool] = lambda: False,
) -> str:
    """Fetch one Craigslist search page; return response HTML text.

    Unlike eBay (constructed params), the search CSV carries full
    validated Craigslist search URLs (config.py enforces the https +
    craigslist-host + search-path policy), so this fetches them directly.
    Raises AcquireTransient/AcquirePermanent; returns text only for HTTP
    200 success (status checked before parsing).
    """
    for attempt in range(max_retries + 1):
        if cancel_check():
            raise AcquireTransient("scan cancelled before/at fetch attempt")
        try:
            response = session.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "Mozilla/5.0 (Craigslist monitoring; hourly scan)"
                    )
                },
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise AcquireTransient(
                    f"transient failure after {max_retries} retries: {exc}"
                ) from exc
            sleep(retry_delay_seconds(attempt, None))
            continue
        status = response.status_code
        if status == 200:
            return response.text
        is_permanent = 400 <= status < 500 and status not in TRANSIENT_STATUSES
        if status in (403, 404) or is_permanent:
            raise AcquirePermanent(f"permanent HTTP {status} for url {url!r}")
        # 429 + 5xx: bounded transient retry; honor Retry-After on 429/503.
        if attempt >= max_retries:
            raise AcquireTransient(
                f"gave up after {max_retries} retries (last status {status})"
            )
        retry_after = None
        if status in (429, 503):
            retry_after = response.headers.get("Retry-After")
        sleep(retry_delay_seconds(attempt, retry_after))
    raise AcquireTransient("unreachable loop exit")


def layout_status(text: str) -> str:
    """Classify a 200 page for the parser/caller.

    Returns "results" when search-result cards exist, "no-results" for a
    clean zero-result page, "denied" for recognizable challenge/deny
    pages, and "suspicious" when a 200 page has neither cards nor any
    known marker — the caller logs suspicious pages and never treats them
    as a clean empty scan. Only recognizable deny markers are detected;
    no claim that every possible layout failure is caught.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text, "html.parser")
    if soup.select(CARD_SELECTOR):
        return "results"
    body_text = soup.get_text(" ", strip=True)
    lowered = body_text.lower()
    if any(
        marker in lowered
        for marker in ("access denied", "captcha", "robot check", "blocked")
    ):
        return "denied"
    no_results_markers = (
        "your search did not match anything",
        "no results",
        "0 results",
    )
    if any(marker in lowered for marker in no_results_markers):
        return "no-results"
    return "suspicious"
