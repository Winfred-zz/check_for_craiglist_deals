"""Offline CLI behavior: fixture scans, injected transient fetches.

Fixtures: offline-scan manifest (example.html 4 valid cards; second.html
no-results page) + offline-failures manifest (denied page + suspicious
layout). No live HTTP to Craigslist anywhere: fixture mode never
contacts Craigslist (load_fixture_manifest validated against CSV rows at
startup), and the only socket connections in this file are the loopback
server tests below.
"""

import json
import threading
from decimal import Decimal
from pathlib import Path

import re
import pytest
import acquire
import notify
import parse
from acquire import AcquirePermanent, AcquireTransient
import http.server
import requests
from http.server import BaseHTTPRequestHandler

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
SEARCH_EXAMPLE = "https://sfbay.craigslist.org/search/sss?query=example"
SEARCH_SECOND = "https://sfbay.craigslist.org/search/sss?query=second"


def offline_manifest(path: Path) -> dict[str, str]:
    with open(path / "manifest.json", encoding="utf-8") as handle:
        return json.load(handle)


def _response(
    url: str, status: int, body: str, headers: dict[str, str]
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.headers.update(headers)
    response._content = body.encode("utf-8")  # noqa: SLF001
    response.encoding = "utf-8"
    return response


class _FakeSession:
    """Duck-typed requests.Session: records attempts, replays outcomes."""

    def __init__(
        self,
        outcomes: list[tuple[int, str, dict[str, str]]],
        repeat_forever: bool = False,
    ) -> None:
        self.attempted: list[str] = []
        self.outcomes: list[tuple[int, str, dict[str, str]]] = list(outcomes)
        self.repeat_forever = repeat_forever

    def get(self, url: str, **kwargs: object) -> requests.Response:
        del kwargs
        self.attempted.append(url)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
        elif self.repeat_forever:
            outcome = self.replay
        else:
            raise AssertionError("unexpected extra request")
        self.replay = outcome
        status, body, headers = outcome
        return _response(url, status, body, headers)


def test_fixture_scan_yields_only_valid_cards() -> None:
    manifest = offline_manifest(FIX / "offline-scan")
    scan_dir = FIX / "offline-scan"
    html = (scan_dir / manifest[SEARCH_EXAMPLE]).read_text(encoding="utf-8")
    items, rejected = parse.parse_search_html(html)
    assert [item["id"] for item in items] == [
        "5012345678",
        "5098765432",
        "5111111111",
        "5111111110",
    ]
    assert [item["price_usd"] for item in items] == [
        Decimal("19"),
        Decimal("19.99"),
        Decimal("0"),
        Decimal("1234"),
    ]
    assert [item["link"] for item in items] == [
        "https://sfbay.craigslist.org/sys/d/example-posting/5012345678.html",
        "https://sfbay.craigslist.org/sys/d/posting/5098765432.html",
        "https://sfbay.craigslist.org/sys/d/zero-item/5111111111.html",
        "https://sfbay.craigslist.org/sys/d/relative-posting/5111111110.html",
    ]
    assert len(items) + len(rejected) == 12
    assert {card["reason"] for card in rejected} == {
        "duplicate id",
        "link not https",
        "missing title",
        "non-numeric price",
        "range price",
        "unsupported currency",
        "malformed listing url",
        "link not a craigslist host",
    }
    assert {card["position"] for card in rejected} == {2, 4, 5, 6, 7, 8, 9, 10}
    assert SEARCH_SECOND in manifest
    second_html = (scan_dir / manifest[SEARCH_SECOND]).read_text(encoding="utf-8")
    second_items, second_rejected = parse.parse_search_html(second_html)
    assert second_items == []
    assert second_rejected == []


def test_live_shape_modern_links_parse_in_full_pipeline() -> None:
    """live-modern.html: sanitized capture of a REAL 2026-10-21 Craigslist
    search page (links rewritten to FIXEDID placeholders, no real searches).
    The live page uses /view/d/<slug>/<opaque-id> links; the synthetic
    numeric-.html fixtures alone cannot certify this format, so the real
    layout is kept as a fixture and driven through layout_status + parser
    + renderer offline."""
    manifest = offline_manifest(FIX / "offline-scan")
    html = (FIX / "offline-scan" / manifest[
        "https://sfbay.craigslist.org/search/sss?query=live"
    ]).read_text(encoding="utf-8")
    assert acquire.layout_status(html) == "results"
    items, rejected = parse.parse_search_html(html)
    assert len(items) == 30
    assert rejected == []
    assert all(re.match(r"FIXEDID\d\dFIXEDID\Z", item["id"]) for item in items)
    assert all(
        notify.is_safe_listing_url(item["link"]) for item in items
    )


def test_layout_classes_are_distinct() -> None:
    denied = (FIX / "offline-failures" / "denied.html").read_text(encoding="utf-8")
    broken = (FIX / "offline-failures" / "broken.html").read_text(encoding="utf-8")
    example_html = (FIX / "offline-scan" / "example.html").read_text(encoding="utf-8")
    assert acquire.layout_status(denied) == "denied"
    assert acquire.layout_status(broken) == "suspicious"
    assert acquire.layout_status(example_html) == "results"


def test_retry_policy_separates_transient_and_permanent() -> None:
    slept: list[float] = []
    session = _FakeSession(
        [(429, "too many", {"Retry-After": "2"}), (200, "<html>ok</html>", {})]
    )
    html = acquire.fetch_search_page(
        session,
        SEARCH_EXAMPLE,
        max_retries=4,
        timeout=30.0,
        sleep=lambda seconds: slept.append(seconds),
    )
    assert html == "<html>ok</html>"
    assert session.attempted == [SEARCH_EXAMPLE] * 2
    assert slept == [2.0], "Retry-After honored on 429"

    permanent = _FakeSession([(407, "proxy denied", {})])
    try:
        acquire.fetch_search_page(permanent, "u", sleep=lambda s: slept.append(s))
    except AcquirePermanent as exc:
        assert "407" in str(exc)
    else:
        raise AssertionError("4xx must be permanent")
    assert permanent.attempted == ["u"], "single attempt only"

    exhausted = _FakeSession(
        [(503, "server", {"Retry-After": "1"})], repeat_forever=True
    )
    slept5: list[float] = []
    try:
        acquire.fetch_search_page(
            exhausted,
            "u",
            max_retries=2,
            sleep=lambda s: slept5.append(s),
        )
    except AcquireTransient as exc:
        assert "gave up after 2 retries" in str(exc)
    else:
        raise AssertionError("exhaustion must raise AcquireTransient")
    assert slept5 == [1.0, 1.0], "one bounded sleep per failed attempt"
    assert exhausted.attempted == ["u"] * 3, "max_retries + 1 total attempts"


def test_backoff_bounds_and_retry_after_honored() -> None:
    assert acquire.retry_delay_seconds(0, None) == 5.0
    assert acquire.retry_delay_seconds(1, None) == 10.0
    assert acquire.retry_delay_seconds(3, None) == 40.0
    assert acquire.retry_delay_seconds(6, None) == 300.0, "exponential cap"
    assert acquire.retry_delay_seconds(2, "120") == 120.0
    assert acquire.retry_delay_seconds(0, "0") == 1.0, "floor of 1s"
    assert acquire.retry_delay_seconds(0, "999999") == 5.0, "out-of-range ignored"
    assert acquire.retry_delay_seconds(0, "not-a-number") == 5.0
    assert acquire.retry_delay_seconds(0, "Fri, 24 Nov 1996") == 5.0


def test_cancellation_stops_before_the_request() -> None:
    session = _FakeSession([(200, "<html>ok</html>", {})])
    try:
        acquire.fetch_search_page(
            session, SEARCH_EXAMPLE, cancel_check=lambda: True
        )
    except AcquireTransient as exc:
        assert "cancelled" in str(exc)
    else:
        raise AssertionError("cancel_check must abort the fetch")
    assert session.attempted == [], "no attempt made after cancellation"


def _loopback_outcomes() -> list[tuple[int, str, dict[str, str]]]:
    return [
        (429, "nope", {"Retry-After": "1"}),
        (200, "<html>ok</html>", {}),
    ]


class _LoopbackHandler(BaseHTTPRequestHandler):
    """Answers from a queued outcome list so the retry path is exercised."""

    outcomes: list[tuple[int, str, dict[str, str]]] = []
    hits: int = 0

    def do_GET(self) -> None:
        _LoopbackHandler.hits += 1
        status, body, headers = self.outcomes.pop(0)  # type: ignore[attr-defined]
        payload = body.encode("utf-8")
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def close(self) -> None:  # stay silent: no per-request access log lines
        pass


def test_loopback_http_exercises_real_requests_path() -> None:
    """One real socket exchange, loopback only (conftest blocks the rest)."""
    _LoopbackHandler.outcomes = _loopback_outcomes()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    try:
        slept: list[float] = []
        html = acquire.fetch_search_page(
            requests.Session(),
            f"http://127.0.0.1:{port}/search",
            max_retries=4,
            timeout=5.0,
            sleep=lambda seconds: slept.append(seconds),
        )
        assert html == "<html>ok</html>"
        assert slept == [1.0], "real Retry-After header honored end-to-end"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_loopback_permanent_and_transient_exhaustion() -> None:
    """Real-socket failure classes: 404 raises AcquirePermanent after ONE
    attempt (no retry storm), 503 exhausts into AcquireTransient."""
    cases: list[tuple[list[tuple[int, str, dict[str, str]]], type[Exception]]] = [
        ([(404, "gone", {})], AcquirePermanent),
        (
            [(503, "down", {})] * 4,
            AcquireTransient,
        ),
    ]
    for outcomes, expected in cases:
        _LoopbackHandler.outcomes = list(outcomes)
        _LoopbackHandler.hits = 0
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            slept: list[float] = []
            with pytest.raises(expected) as raised:
                acquire.fetch_search_page(
                    requests.Session(),
                    f"http://127.0.0.1:{port}/search",
                    max_retries=3,
                    timeout=5.0,
                    sleep=lambda seconds: slept.append(seconds),
                )
            expected_attempts = 1 if expected is AcquirePermanent else 4
            assert _LoopbackHandler.hits == expected_attempts
            assert str(raised.value), "failure text is actionable"
            if expected is AcquireTransient:
                assert len(slept) == 3, "no sleep after the final failure"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_loopback_cancellation_stops_the_real_request() -> None:
    """cancel_check=True: fetch raises before any socket hits the server."""
    _LoopbackHandler.outcomes = [(200, "<html>ok</html>", {})]
    _LoopbackHandler.hits = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with pytest.raises(AcquireTransient, match="cancelled"):
            acquire.fetch_search_page(
                requests.Session(),
                f"http://127.0.0.1:{port}/search",
                cancel_check=lambda: True,
            )
        assert _LoopbackHandler.hits == 0, "cancellation precedes the socket"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
