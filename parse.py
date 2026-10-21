"""Pure HTML parsing for one Craigslist search page (no network, no logging).

parse_search_html() is the function under test with fixtures:
- Each card is parsed defensively; one invalid card never destroys its
  valid siblings or later cards. Optional/required fields never leak
  from a previous card (each card resets everything it reads).
- Prices are validated, not stripped-and-floated: exact USD amounts only.
  Ranges ("$20 to $30"), other currencies, "free", and malformed values
  are rejected with a reason, never turned into a zero price.
  "$19.99" keeps its cents (the legacy int() conversion truncated them).
- Listing identity: the posting ID from a validated https Craigslist
  listing URL — legacy numeric `<id>.html` or the modern
  `/view/d/<slug>/<opaque-id>` form; malformed links and duplicate IDs
  are handled explicitly.
- There is no condition/exclusion concept here: Craigslist search pages
  carry title/price/link only.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import TypedDict, cast

from bs4 import BeautifulSoup

CARD_SELECTOR = "li.cl-static-search-result"
CL_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)?craigslist\.org$", re.IGNORECASE)
USD_PRICE_RE = re.compile(r"\$(?P<amount>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\Z")
OTHER_CURRENCY_RE = re.compile(r"[€£¥₹]|USD\b|\beuros?\b", re.IGNORECASE)
LISTING_ID_RE = re.compile(r"(?<![0-9])(?P<id>[0-9]{9,12})\.html\Z")
MODERN_LISTING_RE = re.compile(r"/view/d/[^/]+/(?P<id>[A-Za-z0-9_-]{9,40})\Z")


class ItemRecord(TypedDict):
    """One validated listing card."""
    id: str
    title: str
    price_usd: Decimal
    link: str


class RejectionRecord(TypedDict):
    """Why a card was rejected and which card position it sat at."""
    reason: str
    position: int


def parse_usd_price(raw: str) -> tuple[Decimal | None, str | None]:
    """Return a Decimal for an exact USD amount, or None + reason."""
    text = raw.strip()
    if re.search(r"\bto\b", text, re.IGNORECASE):  # "$20 to $30" ranges
        return None, "range price"
    if not text.startswith("$"):
        if OTHER_CURRENCY_RE.search(text):
            return None, "unsupported currency"
        return None, "non-numeric price"
    match = USD_PRICE_RE.match(text)
    if not match:
        return None, "malformed price"
    try:
        return Decimal(match.group("amount").replace(",", "")), None
    except InvalidOperation:
        return None, "malformed price"


def parse_listing_link(href: object) -> tuple[str | None, str | None, str | None]:
    """Return (posting_id, canonical_url, reason-if-invalid).

    Only https Craigslist hosts are accepted, in both listing forms: the
    legacy ".../<9-12 digits>.html" and the modern "/view/d/<slug>/<id>"
    with a 9-40 char URL-safe opaque id. The canonical URL drops query
    strings so the same posting reached through different query strings
    dedupes by ID.
    """
    if not isinstance(href, str) or not href:
        return None, None, "link missing"
    if href.startswith("//"):
        href = "https:" + href
    if not href.startswith("https://"):
        return None, None, "link not https"
    remainder = href[len("https://"):]
    host_only = remainder.split("/")[0]
    if not host_only or not CL_HOST_RE.match(host_only):
        return None, None, "link not a craigslist host"
    path = remainder[len(host_only):]
    query_stripped = path.split("?")[0]
    id_match = LISTING_ID_RE.search(query_stripped)
    if id_match is None:
        id_match = MODERN_LISTING_RE.search(query_stripped)
    if id_match is None:
        return None, None, "malformed listing url"
    posting_id = id_match.group("id")
    canonical = f"https://{host_only}{query_stripped}"
    return posting_id, canonical, None


def parse_search_html(html: str) -> tuple[list[ItemRecord], list[RejectionRecord]]:
    """Parse one search page.

    Returns (items, rejected). Each item: {id, title, price_usd, link}.
    Each rejected: {reason, position}. A malformed card is rejected alone;
    later cards continue to parse.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[ItemRecord] = []
    rejected: list[RejectionRecord] = []
    seen_ids: set[str] = set()
    for position, card in enumerate(soup.select(CARD_SELECTOR)):
        link_tag = card.find("a")
        href = link_tag.get("href") if link_tag else None
        posting_id, canonical, link_reason = parse_listing_link(href)
        title_tag = card.find("div", class_="title")
        title = title_tag.get_text(" ", strip=True) if title_tag else None
        price_tag = card.find("div", class_="price")
        price_usd, price_reason = (
            parse_usd_price(price_tag.get_text(" ", strip=True))
            if price_tag
            else (None, "missing price")
        )
        if title is None or title == "":
            rejected.append({"reason": "missing title", "position": position})
            continue
        if posting_id is None:
            rejected.append({"reason": str(link_reason), "position": position})
            continue
        if price_usd is None:
            rejected.append({"reason": str(price_reason), "position": position})
            continue
        if posting_id in seen_ids:
            rejected.append({"reason": "duplicate id", "position": position})
            continue
        seen_ids.add(posting_id)
        items.append(
            {
                "id": posting_id,
                "title": title,
                "price_usd": price_usd,
                "link": cast("str", canonical),
            }
        )
    return items, rejected
