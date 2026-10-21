"""Alert-message rendering: pure functions, no Discord/network concerns.

Message text stays close to the legacy wording but with validated
Craigslist URLs (listing links must pass parse.py's https/host/ID
validation; search links must pass config.py's search-URL policy),
markdown-escaping of user-controlled names/titles (so titles containing
brackets/parens cannot break the message or the link, and "@" sequences
cannot rely on the sender's allowed_mentions), and exact cent display
(the legacy int() truncation is gone).
"""

from urllib.parse import urlparse

from parse import CL_HOST_RE
from selection import EventRecord

MARKDOWN_SPECIALS = str.maketrans(
    {
        "\\": "\\\\",
        "`": "\\`",
        "*": "\\*",
        "_": "\\_",
        "{": "\\{",
        "}": "\\}",
        "|": "\\|",
        "[": "\\[",
        "<": "\\<",
        "#": "\\#",
        "+": "\\+",
        "-": "\\-",
        "!": "\\!",
        ")": "\\)",
        "(": "\\(",
        ">": "\\>",
        "]": "\\]",
        "~": "\\~",
    }
)
MAX_MESSAGE_CHARS = 4000
TRIM_MARKER = ".."


def is_safe_listing_url(url: str) -> bool:
    """True for a validated https Craigslist listing URL (ends .html)."""
    if not url.endswith(".html"):
        return False
    match = CL_HOST_RE.match(urlparse(url).netloc)
    return bool(match and urlparse(url).scheme == "https")


def is_safe_search_url(url: str) -> bool:
    """True for URLs config.py would accept as search rows."""
    parsed = urlparse(url)
    return bool(
        parsed.scheme == "https"
        and CL_HOST_RE.match(parsed.netloc)
        and parsed.path.startswith(("/search", "/d/"))
    )


def _trim(text: str, budget: int) -> str:
    """Trim user text so the message fits; never touches URL segments."""
    if len(text) <= budget:
        return text
    if budget <= len(TRIM_MARKER):
        return TRIM_MARKER[:budget]
    return text[: budget - len(TRIM_MARKER)] + TRIM_MARKER


def render_alert_message(event: EventRecord) -> str:
    """Return the exact one-line message for an event.

    Raises ValueError on events carrying unsafe URLs/names — a rendered
    message is only ever emitted for validated records.
    """
    if not is_safe_listing_url(event["url"]):
        raise ValueError(f"unsafe listing url: {event['url']!r}")
    if not is_safe_search_url(event["search_url"]):
        raise ValueError(f"unsafe search url: {event['search_url']!r}")
    if event["kind"] == "new":
        overhead = (
            len("Found a new craigslist deal for : ")
            + len(event["url"])
            + len(f" [all deals]({event['search_url']})")
        )
        if overhead >= MAX_MESSAGE_CHARS:
            raise ValueError(
                f"message template exceeds the {MAX_MESSAGE_CHARS}-char"
                + f" Discord limit (urls too long): {overhead}"
            )
        name = _trim(
            event["friendly_name"].translate(MARKDOWN_SPECIALS),
            MAX_MESSAGE_CHARS - overhead,
        )
        return (
            f"Found a new craigslist deal for {name}: {event['url']} "
            + f"[all deals]({event['search_url']})"
        )
    if event["kind"] == "price-drop":
        prev_price = event["prev_price"]
        if prev_price is None:
            raise ValueError(f"price-drop event has no previous price: {event}")
        new_text = f"${event['price_usd']:,.2f}"
        prev_text = f"${prev_price:,.2f}"
        overhead = (
            len("Price drop for ")
            + len(f" to {new_text} from {prev_text} ")
            + len(f"[deal]({event['url']}) [all deals]({event['search_url']})")
        )
        if overhead >= MAX_MESSAGE_CHARS:
            raise ValueError(
                f"message template exceeds the {MAX_MESSAGE_CHARS}-char"
                + f" Discord limit (urls too long): {overhead}"
            )
        title = _trim(
            event["title"].translate(MARKDOWN_SPECIALS),
            MAX_MESSAGE_CHARS - overhead,
        )
        return (
            f"Price drop for {title} to {new_text} from {prev_text} "
            + f"[deal]({event['url']}) [all deals]({event['search_url']})"
        )
    raise ValueError(f"unsupported event kind: {event['kind']!r}")
