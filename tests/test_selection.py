"""Unit tests for selection.derive_events: new/low/rise/rebound/dedup.

Contract under test (design.md): every newly discovered posting id and
every strictly-new historical low produces exactly one event; rises and
rebounds never alert. derive_events mutates the ``known`` snapshot it is
given so overlapping rows cannot alert twice in one scan — these tests
assert that mutation too, because it is what keeps state and events
consistent for the caller.
"""

from decimal import Decimal

from config import SearchRow
from parse import ItemRecord
from selection import ListingRecord, derive_events


ID1 = "111111111"
ID2 = "222222222"
URL1 = "https://sfbay.craigslist.org/d/one/111111111.html"
URL2 = "https://sfbay.craigslist.org/d/two/222222222.html"
SEARCH_A = "https://sfbay.craigslist.org/search/sss?query=one"
SEARCH_B = "https://sfbay.craigslist.org/search/sss?query=two"


def _row(name: str, url: str) -> SearchRow:
    return {"friendly_name": name, "url": url}


def _item(posting_id: str, link: str, title: str, price: str) -> ItemRecord:
    return {
        "id": posting_id,
        "title": title,
        "price_usd": Decimal(price),
        "link": link,
    }


def _known(url: str, title: str, low: str, original: str) -> ListingRecord:
    return {
        "url": url,
        "title": title,
        "original_price": Decimal(original),
        "last_price": Decimal(low),
        "low_price": Decimal(low),
    }


def test_new_listing_emits_one_event() -> None:
    items = [_item(ID1, URL1, "Desk", "10.00")]
    known: dict[str, dict] = {}
    events = derive_events([_row("Example", SEARCH_A)], {SEARCH_A: items}, known)
    assert [event["event_key"] for event in events] == [f"{ID1}@new"]
    assert [event["kind"] for event in events] == ["new"]
    assert events[0]["price_usd"] == Decimal("10.00")
    assert events[0]["prev_price"] is None
    # the scan snapshot is updated so a second row cannot re-discover it
    assert known[ID1]["low_price"] == Decimal("10.00")


def test_first_run_has_no_silent_baseline() -> None:
    items = [
        _item(ID1, URL1, "Desk", "10.00"),
        _item(ID2, URL2, "Bike", "0"),
    ]
    events = derive_events([_row("Example", SEARCH_A)], {SEARCH_A: items}, {})
    assert [event["event_key"] for event in events] == [f"{ID1}@new", f"{ID2}@new"]
    assert events[1]["price_usd"] == Decimal("0")


def test_unchanged_observation_never_alerts() -> None:
    known = {ID1: _known(URL1, "Desk", "10.00", "10.00")}
    items = [_item(ID1, URL1, "Desk", "10.00")]
    events = derive_events([_row("Example", SEARCH_A)], {SEARCH_A: items}, known)
    assert events == []
    assert known[ID1]["low_price"] == Decimal("10.00")


def test_rise_updates_observation_but_not_the_low() -> None:
    known = {ID1: _known(URL1, "Desk", "10.00", "10.00")}
    items = [_item(ID1, URL1, "Desk", "12.00")]
    events = derive_events([_row("Example", SEARCH_A)], {SEARCH_A: items}, known)
    assert events == []
    assert known[ID1]["last_price"] == Decimal("12.00")
    assert known[ID1]["low_price"] == Decimal("10.00")


def test_rebound_back_to_a_seen_low_does_not_realert() -> None:
    known = {ID1: _known(URL1, "Desk", "15.00", "20.00")}
    events = derive_events(
        [_row("Example", SEARCH_A)],
        {SEARCH_A: [_item(ID1, URL1, "Desk", "15.00")]},
        known,
    )
    assert events == []
    assert known[ID1]["low_price"] == Decimal("15.00")
    # a second observation at the same price stays silent as well
    again = derive_events(
        [_row("Example", SEARCH_A)],
        {SEARCH_A: [_item(ID1, URL1, "Desk", "15.00")]},
        known,
    )
    assert again == []


def test_true_new_low_alerts_once_with_both_prices() -> None:
    known = {ID1: _known(URL1, "Desk", "20.00", "20.00")}
    events = derive_events(
        [_row("Example", SEARCH_A)],
        {SEARCH_A: [_item(ID1, URL1, "Desk", "15.00")]},
        known,
    )
    assert [event["event_key"] for event in events] == [f"{ID1}@1500"]
    assert [event["kind"] for event in events] == ["price-drop"]
    assert events[0]["price_usd"] == Decimal("15.00")
    assert events[0]["prev_price"] == Decimal("20.00")
    assert known[ID1]["low_price"] == Decimal("15.00")


def test_cent_level_drop_is_a_distinct_event() -> None:
    known = {ID1: _known(URL1, "Desk", "19.99", "19.99")}
    events = derive_events(
        [_row("Example", SEARCH_A)],
        {SEARCH_A: [_item(ID1, URL1, "Desk", "19.98")]},
        known,
    )
    assert [event["event_key"] for event in events] == [f"{ID1}@1998"]
    assert events[0]["prev_price"] == Decimal("19.99")
    assert known[ID1]["low_price"] == Decimal("19.98")


def test_new_low_toward_zero_alerts() -> None:
    known = {ID1: _known(URL1, "Desk", "0.05", "5.00")}
    events = derive_events(
        [_row("Example", SEARCH_A)], {SEARCH_A: [_item(ID1, URL1, "Desk", "0")]}, known
    )
    assert [event["event_key"] for event in events] == [f"{ID1}@0"]
    assert events[0]["price_usd"] == Decimal("0")


def test_two_new_lows_in_one_scan_are_two_events() -> None:
    known = {
        ID1: _known(URL1, "Desk", "20.00", "20.00"),
        ID2: _known(URL2, "Bike", "30.00", "30.00"),
    }
    items = [
        _item(ID1, URL1, "Desk", "18.00"),
        _item(ID2, URL2, "Bike", "25.00"),
    ]
    events = derive_events([_row("Example", SEARCH_A)], {SEARCH_A: items}, known)
    assert [event["event_key"] for event in events] == [f"{ID1}@1800", f"{ID2}@2500"]
    assert known[ID1]["low_price"] == Decimal("18.00")
    assert known[ID2]["low_price"] == Decimal("25.00")


def test_empty_scan_yields_no_events() -> None:
    events = derive_events([_row("Example", SEARCH_A)], {SEARCH_A: []}, {})
    assert events == []


def test_overlapping_rows_alert_once_with_first_context() -> None:
    row_a = _row("First", SEARCH_A)
    row_b = _row("Second", SEARCH_B)
    items_by_url = {
        SEARCH_A: [_item(ID1, URL1, "Desk", "10.00")],
        SEARCH_B: [_item(ID1, URL1, "Desk copy", "10.00")],
    }
    events = derive_events([row_a, row_b], items_by_url, {})
    assert [event["event_key"] for event in events] == [f"{ID1}@new"]
    assert events[0]["friendly_name"] == "First"
    assert events[0]["search_url"] == SEARCH_A
    assert events[0]["title"] == "Desk"


def test_same_id_under_different_urls_is_one_event() -> None:
    variant = URL1 + "?context=share"
    items_by_url = {
        SEARCH_A: [_item(ID1, URL1, "Desk", "10.00")],
        SEARCH_B: [_item(ID1, variant, "Desk", "10.00")],
    }
    events = derive_events(
        [_row("First", SEARCH_A), _row("Second", SEARCH_B)], items_by_url, {}
    )
    assert [event["event_key"] for event in events] == [f"{ID1}@new"]


def test_duplicate_id_in_one_page_is_one_event() -> None:
    items = [
        _item(ID1, URL1, "Desk", "10.00"),
        _item(ID1, URL1, "Desk copy", "9.00"),
    ]
    events = derive_events([_row("Example", SEARCH_A)], {SEARCH_A: items}, {})
    assert [event["event_key"] for event in events] == [f"{ID1}@new"]
    assert events[0]["price_usd"] == Decimal("10.00")
