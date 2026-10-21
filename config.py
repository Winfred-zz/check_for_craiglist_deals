"""Configuration loading and validation.

All startup validation lives here: no filesystem reads, signal
registration, logging setup, or environment parsing happen at import
time anywhere in the project. Paths resolve from an explicit project
root, overridable for deployment and offline smoke runs. Errors are
ConfigError with actionable, non-secret messages.
"""

from __future__ import annotations

import csv
import os
import re
import urllib.parse
from typing import TypedDict

CHANNELID_RE = re.compile(r"^[0-9]{1,25}$")
SEARCH_HEADERS = ("friendly_name", "url")
CL_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)?craigslist\.org$", re.IGNORECASE)


class SearchRow(TypedDict):
    """One validated CSV search row."""
    friendly_name: str
    url: str


class EnvValues(TypedDict):
    """Validated secrets/env: token value never printed, channel is numeric."""
    discord_token: str
    channel_id: int


class ConfigError(Exception):
    """Actionable startup configuration failure; carries no secrets."""


def _validate_search_url(url: str) -> None:
    """Accept only https Craigslist search URLs.

    Host must be craigslist.org or a <city>.craigslist.org subdomain. Path
    must be a search path: "/search..." (modern) or "/d/..." (legacy search
    form). This is what keeps F17's arbitrary-URL fetch out of the app: a
    row can never point the scanner at an arbitrary external host.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        raise ConfigError(f"search url is unparseable: {url}") from None
    if parsed.scheme != "https":
        raise ConfigError(f"search url must be https: {url}")
    if not parsed.netloc or not CL_HOST_RE.match(parsed.netloc):
        raise ConfigError(
            f"search url host must be craigslist.org or a city subdomain: {url}"
        )
    if not parsed.path.startswith(("/search", "/d/")):
        raise ConfigError(
            f"search url path must be a Craigslist search path (/search...): {url}"
        )


def load_env(env_file: str | None) -> EnvValues:
    """Return validated {discord_token, channel_id}.

    Values come from the process environment (preferred) or an optional
    explicit .env file. DISCORD_TOKEN presence is checked, never echoed.
    """
    values: dict[str, str] = {}
    if env_file:
        from dotenv import dotenv_values

        values = {key: val for key, val in dotenv_values(env_file).items() if val}
    token = os.environ.get("DISCORD_TOKEN") or values.get("DISCORD_TOKEN")
    channel = os.environ.get("CHANNELID") or values.get("CHANNELID")
    if not token:
        raise ConfigError(
            "DISCORD_TOKEN is missing. Set it in the environment or in an .env file "
            + "(template: examples/.env.example). The token value is never printed."
        )
    if channel is None:
        raise ConfigError(
            "CHANNELID is missing. Set it in the environment or .env file "
            + "(numeric Discord channel ID; see examples/.env.example)."
        )
    if not CHANNELID_RE.match(channel):
        raise ConfigError(
            "CHANNELID must be a numeric Discord channel ID (up to 25 digits)."
        )
    return {"discord_token": token, "channel_id": int(channel)}


def load_search_rows(csv_path: str) -> list[SearchRow]:
    """Read and fully validate the search CSV before any scan or send.

    A malformed row aborts startup (configuration error), so a scan can
    never persist some search results and then die on a bad row.
    """
    if not os.path.isfile(csv_path):
        raise ConfigError(f"search CSV not found at {csv_path}")
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = set(SEARCH_HEADERS) - set(reader.fieldnames or [])
            if missing:
                raise ConfigError(
                    "search CSV missing required headers: "
                    + ", ".join(sorted(missing))
                    + " (template: examples/craigslist_deals_to_check.csv)"
                )
            rows: list[SearchRow] = []
            for line_no, row in enumerate(reader, start=2):
                name = (row["friendly_name"] or "").strip()
                url = (row["url"] or "").strip()
                if not name:
                    raise ConfigError(f"search CSV line {line_no}: blank friendly_name")
                if not url:
                    raise ConfigError(f"search CSV line {line_no}: blank url")
                _validate_search_url(url)
                rows.append({"friendly_name": name, "url": url})
    except UnicodeError as exc:
        raise ConfigError(f"search CSV is not valid UTF-8 text: {exc}") from exc
    return rows


def ensure_writable_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create data directory: {exc}") from exc
    if not (os.access(path, os.W_OK) and os.access(path, os.X_OK)):
        raise ConfigError(f"directory exists but is not writable: {path}")
    return path
