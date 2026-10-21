"""parse.py unit tests: per-card isolation, exact USD money, posting identity.

Synthetic HTML here (fixtures/offline-* cover the page-level scans elsewhere)
so each rejection reason is exercised one card at a time.
"""

from decimal import Decimal

import parse

CL = "https://sfbay.craigslist.org/sys/d/"


def _card(href: str | None, title: str | None, price: str | None) -> str:
    inner = ""
    if title is not None:
        inner += f'<div class="title">{title}</div>'
    if price is not None:
        inner += f'<div class="price">{price}</div>'
    if href is not None:
        inner = f'<a href="{href}">{inner}</a>'
    return f'<li class="cl-static-search-result">{inner}</li>'


def _page(*cards: str) -> str:
    return "<ul>" + "".join(cards) + "</ul>"


ONE = CL + "one/5012345678.html"


def test_price_keeps_cents_and_grouping() -> None:
    assert parse.parse_usd_price("$19.99") == (Decimal("19.99"), None)
    assert parse.parse_usd_price("$1,234.50") == (Decimal("1234.50"), None)
    assert parse.parse_usd_price("$0") == (Decimal("0"), None)


def test_price_never_guesses_a_number() -> None:
    expectations = (
        ("$20 to $30", "range price"),
        ("$20-$30", "malformed price"),
        ("$1.2345", "malformed price"),
        ("$-5", "malformed price"),
        ("$1,23", "malformed price"),
        ("€15", "unsupported currency"),
        ("USD 5", "unsupported currency"),
        ("free", "non-numeric price"),
        ("", "non-numeric price"),
    )
    for raw, reason in expectations:
        price, observed = parse.parse_usd_price(raw)
        assert price is None
        assert observed == reason


def test_link_canonicalizes_query_and_relative_form() -> None:
    posted_id, canonical, reason = parse.parse_listing_link(
        CL + "posting/5098765432.html?format=mini"
    )
    assert posted_id == "5098765432"
    assert canonical == CL + "posting/5098765432.html"
    assert reason is None
    _relative_id, relative_url, relative_reason = parse.parse_listing_link(
        "//sfbay.craigslist.org/sys/d/rel/5111111110.html"
    )
    assert relative_url == CL + "rel/5111111110.html"
    assert relative_reason is None


INSECURE = "http://sfbay.craigslist.org/sys/d/x/5012345678.html"


def test_link_rejects_unusable_targets() -> None:
    cases: list[tuple[object, str]] = [
        (None, "link missing"),
        ("", "link missing"),
        (INSECURE, "link not https"),
        (
            "https://evil.example.com/d/x/5012345678.html",
            "link not a craigslist host",
        ),
        (
            "https://craigslist.org.evil/d/x/5012345678.html",
            "link not a craigslist host",
        ),
        (CL + "no-id/notanumber.html", "malformed listing url"),
        (CL + "short/12345678.html", "malformed listing url"),
        (CL + "long/1234567890123.html", "malformed listing url"),
    ]
    for href, reason in cases:
        posted_id, canonical, observed = parse.parse_listing_link(href)
        assert (posted_id, canonical) == (None, None)
        assert observed == reason


def test_id_length_bounds() -> None:
    nine = CL + "a/123456789.html"
    twelve = CL + "a/123456789012.html"
    for href, expected_id in ((nine, "123456789"), (twelve, "123456789012")):
        posted_id, canonical, reason = parse.parse_listing_link(href)
        assert reason is None
        assert posted_id == expected_id
        assert canonical == href
    too_short = CL + "a/12345678.html"
    too_long = CL + "a/1234567890123.html"
    for href in (too_short, too_long):
        posted_id, _canonical, reason = parse.parse_listing_link(href)
        assert posted_id is None
        assert reason == "malformed listing url"


def test_cards_parse_independently() -> None:
    html = _page(
        _card(ONE, "Priced", "$19"),
        _card(CL + "two/5022222222.html", "No price", None),
        _card(CL + "three/5033333333.html", None, "$5"),
        _card(None, "No anchor", "$5"),
        _card(CL + "four/5044444444.html", "Valid again", "$7.50"),
    )
    items, rejected = parse.parse_search_html(html)
    assert [(item["id"], item["price_usd"]) for item in items] == [
        ("5012345678", Decimal("19")),
        ("5044444444", Decimal("7.50")),
    ]
    assert items[0]["title"] == "Priced"
    assert items[1]["link"] == CL + "four/5044444444.html"
    observed_rejects = [(card["reason"], card["position"]) for card in rejected]
    assert observed_rejects == [
        ("missing price", 1),
        ("missing title", 2),
        ("link missing", 3),
    ]


def test_duplicate_ids_and_bad_prices_rejected_per_card() -> None:
    html = _page(
        _card(ONE, "First", "$19"),
        _card(ONE + "?x=1", "Dup", "$8"),
        _card(CL + "five/5055555555.html", "Free bike", "free"),
        _card(CL + "six/5066666666.html", "Zero item", "$0"),
    )
    items, rejected = parse.parse_search_html(html)
    assert [(item["id"], item["price_usd"]) for item in items] == [
        ("5012345678", Decimal("19")),
        ("5066666666", Decimal("0")),
    ]
    observed_rejects = [(card["reason"], card["position"]) for card in rejected]
    assert observed_rejects == [
        ("duplicate id", 1),
        ("non-numeric price", 2),
    ]


def test_card_order_survives_a_malformed_card() -> None:
    html = _page(
        _card("https://evil.example.com/d/x/5033333333.html", "Evil", "$1"),
        _card(ONE, "Good", "$2"),
    )
    items, rejected = parse.parse_search_html(html)
    assert [item["id"] for item in items] == ["5012345678"]
    assert [(card["reason"], card["position"]) for card in rejected] == [
        ("link not a craigslist host", 0),
    ]


def test_page_without_cards_is_clean_emptiness() -> None:
    items, rejected = parse.parse_search_html(
        "<html><body><p>nothing here</p></body></html>"
    )
    assert items == []
    assert rejected == []
