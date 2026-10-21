"""Deterministic event derivation from validated listings + prior state.

Approved contract (design.md, adopted 2026-10-19): alert every newly
discovered listing (by global posting-ID dedup) and every new historical
low price. Emit ALL eligible events per scan (the legacy comment's "one
message per hour" was aspirational and never enforced). A price rise
updates observation but never alerts; a rebound back to a previously
seen low does not re-alert (historical-low semantics preserved).
Overlapping search rows resolve by CSV order: the first row that
discovers an event supplies its friendly name/search context.
"""

from decimal import Decimal

from config import SearchRow
from parse import ItemRecord
from typing import TypedDict


class ListingRecord(TypedDict):
    """Prior durable state for one posting (prices as exact Decimals)."""
    url: str
    title: str
    original_price: Decimal
    last_price: Decimal
    low_price: Decimal


class EventRecord(TypedDict):
    """One derived alert event, exactly what notify/state consume."""
    event_key: str
    kind: str
    posting_id: str
    price_usd: Decimal
    prev_price: Decimal | None
    url: str
    title: str
    friendly_name: str
    search_url: str


def _cents_key(price: Decimal) -> str:
    return str(int(price.scaleb(2)))


def derive_events(
    search_rows: list[SearchRow],
    items_by_url: dict[str, list[ItemRecord]],
    known: dict[str, ListingRecord],
) -> list[EventRecord]:
    """Derive the events of one scan, deterministically.

    search_rows: validated CSV rows in file order.
    items_by_url: parse.py outputs keyed by the CSV search url.
    known: durable per-posting state (listings table snapshot). THIS
    DICT IS MUTATED to reflect each emitted event, so a posting covered
    by two overlapping search rows can alert at most once per scan and
    a new low within the same scan lowers the watermark once. Callers
    persist the SAME information atomically through state.py; this
    function performs no network or filesystem work.

    Event keys: "{posting_id}@new" for discovery, "{posting_id}@{cents}"
    for each new historical low — durable dedup identity for pending/sent
    bookkeeping.

    Within one scan each posting id is evaluated once, in CSV row order:
    a repeated card (duplicate on one page, or the same posting reached
    from overlapping rows) updates the snapshot's last observation but
    cannot emit a second event. Across scans, distinct new lows remain
    distinct events.
    """
    events: list[EventRecord] = []
    observed: set[str] = set()
    for row in search_rows:
        url = row["url"]
        for item in items_by_url.get(url, []):
            if item["id"] in observed:
                entry = known.get(item["id"])
                if entry is not None:
                    entry["last_price"] = item["price_usd"]
                continue
            observed.add(item["id"])
            entry = known.get(item["id"])
            if entry is None:
                known[item["id"]] = {
                    "url": item["link"],
                    "title": item["title"],
                    "original_price": item["price_usd"],
                    "last_price": item["price_usd"],
                    "low_price": item["price_usd"],
                }
                events.append(
                    {
                        "event_key": f"{item['id']}@new",
                        "kind": "new",
                        "posting_id": item["id"],
                        "price_usd": item["price_usd"],
                        "prev_price": None,
                        "url": item["link"],
                        "title": item["title"],
                        "friendly_name": row["friendly_name"],
                        "search_url": url,
                    }
                )
                continue
            if item["price_usd"] < entry["low_price"]:
                events.append(
                    {
                        "event_key": f"{item['id']}@{_cents_key(item['price_usd'])}",
                        "kind": "price-drop",
                        "posting_id": item["id"],
                        "price_usd": item["price_usd"],
                        "prev_price": entry["low_price"],
                        "url": item["link"],
                        "title": item["title"],
                        "friendly_name": row["friendly_name"],
                        "search_url": url,
                    }
                )
                entry["low_price"] = item["price_usd"]
            # Rises and equal prices update observation only (record_
            # observations does that); they never alert, and a rebound
            # to a previously seen low cannot re-alert because the
            # watermark only lowers.
            entry["last_price"] = item["price_usd"]
    return events
