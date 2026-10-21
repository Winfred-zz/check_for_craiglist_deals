"""Alert-message rendering rules: safe URLs, escaped names, exact cents."""

from decimal import Decimal

import pytest

from notify import MAX_MESSAGE_CHARS, TRIM_MARKER, render_alert_message
from notify import is_safe_listing_url, is_safe_search_url

LISTING = "https://sfbay.craigslist.org/sys/d/example-posting/5012345678.html"
SEARCH = "https://sfbay.craigslist.org/search/sss?query=example"


def event(kind: str, **overrides: object) -> dict:
    base = {
        "event_key": "5012345678@new",
        "kind": kind,
        "posting_id": "5012345678",
        "price_usd": Decimal("19"),
        "prev_price": None,
        "url": LISTING,
        "title": "Example one",
        "friendly_name": "Example search",
        "search_url": SEARCH,
    }
    base.update(overrides)
    return base


def test_new_event_renders_exact_legacy_shaped_line() -> None:
    message = render_alert_message(event("new"))
    assert message == (
        f"Found a new craigslist deal for Example search: {LISTING} "
        f"[all deals]({SEARCH})"
    )


def test_price_drop_line_carries_exact_cents_both_ways() -> None:
    dropped = event(
        "price-drop",
        price_usd=Decimal("19.98"),
        prev_price=Decimal("19.99"),
        event_key="5098765432@1998",
        title="Second posting",
    )
    message = render_alert_message(dropped)
    assert message == (
        "Price drop for Second posting to $19.98 from $19.99 "
        f"[deal]({LISTING}) [all deals]({SEARCH})"
    )


def test_price_display_keeps_thousands_and_zero() -> None:
    dropped = event(
        "price-drop",
        price_usd=Decimal("0"),
        prev_price=Decimal("1234"),
        title="Bulk item",
    )
    message = render_alert_message(dropped)
    assert " to $0.00 from $1,234.00 " in message


def test_names_and_titles_are_markdown_escaped() -> None:
    sneaky_name = "A *b* [c](d) `e`"
    sneaky_title = "x #y <z> {w} |u~ v-p (q) !r +s _t"
    new_message = render_alert_message(event("new", friendly_name=sneaky_name))
    assert "A \\*b\\* \\[c\\]\\(d\\) \\`e\\`" in new_message
    drop_message = render_alert_message(
        event(
            "price-drop",
            title=sneaky_title,
            price_usd=Decimal("1"),
            prev_price=Decimal("2"),
        )
    )
    assert "x \\#y \\<z\\> \\{w\\} \\|u\\~ v\\-p \\(q\\) \\!r \\+s \\_t" in drop_message
    assert "% off" in render_alert_message(event("new", friendly_name="10% off"))


def test_unsafe_urls_are_refused_before_rendering() -> None:
    with pytest.raises(ValueError) as raised:
        render_alert_message(event("new", url="http://x.com/y.html"))
    assert "unsafe listing url" in str(raised.value)
    with pytest.raises(ValueError) as raised:
        render_alert_message(event("new", search_url="https://evil.example/x"))
    assert "unsafe search url" in str(raised.value)


def test_price_drop_without_previous_price_is_refused() -> None:
    missing = event("price-drop", prev_price=None)
    with pytest.raises(ValueError) as raised:
        render_alert_message(missing)
    assert "no previous price" in str(raised.value)


def test_unknown_kind_is_refused_not_silently_dropped() -> None:
    with pytest.raises(ValueError) as raised:
        render_alert_message(event("bogus-kind"))
    assert "unsupported event kind" in str(raised.value)


def test_over_long_names_are_trimmed_to_the_limit() -> None:
    huge = event("new", friendly_name="n" * 4500)
    message = render_alert_message(huge)
    assert len(message) == MAX_MESSAGE_CHARS
    assert message.startswith("Found a new craigslist deal for n")
    assert message[len("Found a new craigslist deal for "):].startswith(
        "n" * (MAX_MESSAGE_CHARS - len("Found a new craigslist deal for ")
               - len(f": {LISTING} [all deals]({SEARCH})") - len(TRIM_MARKER))
        + TRIM_MARKER
    )


def test_listing_url_safety_policy() -> None:
    assert is_safe_listing_url(LISTING)
    assert not is_safe_listing_url("http://sfbay.craigslist.org/d/x/5.html")
    assert not is_safe_listing_url("https://evil.example.com/d/x/5012345678.html")
    assert not is_safe_listing_url("https://sfbay.craigslist.org/d/x/no-id")


def test_search_url_safety_policy() -> None:
    assert is_safe_search_url(SEARCH)
    assert not is_safe_search_url("http://sfbay.craigslist.org/search/sss")
    assert not is_safe_search_url("https://craigslist.org.evil/search/sss")
    assert not is_safe_search_url("https://sfbay.craigslist.org/profile/sss")
