"""Quiet-window evaluation on an injected, timezone-aware clock.

Approved contract (design.md): alert delivery is suppressed from 02:00
inclusive until 12:00 exclusive in the configured timezone (UTC here);
noon allows delivery. Scanning still runs during quiet hours — found
events are stored pending and delivered when the window opens. Naive
datetimes are rejected: the contract is explicit about the zone, and the
legacy host-local `datetime.now().hour` guesswork is gone.
"""

from datetime import datetime, timezone

QUIET_START_HOUR = 2
QUIET_END_HOUR = 12


def is_quiet(now: datetime) -> bool:
    """True when alert delivery must be suppressed at ``now``.

    The window is defined in UTC, so any aware input is converted first:
    the host clock, its timezone and DST transitions can never move the
    window (the legacy host-local ``.hour`` comparison is gone).
    """
    if now.tzinfo is None:
        raise ValueError("quiet-window clock must be timezone-aware (e.g. UTC)")
    hour = now.astimezone(timezone.utc).hour
    return QUIET_START_HOUR <= hour < QUIET_END_HOUR


def utc_now() -> datetime:
    """Explicit UTC clock; the approved timezone for this project."""
    return datetime.now(tz=timezone.utc)
