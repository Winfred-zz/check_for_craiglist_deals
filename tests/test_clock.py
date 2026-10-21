import pytest
from datetime import datetime, timezone

from clock import is_quiet


def test_quiet_window_boundaries():
    for hour, minute, expected in (
        (1, 59, False),
        (2, 0, True),
        (11, 59, True),
        (12, 0, False),
        (13, 0, False),
    ):
        result = is_quiet(
            datetime(2026, 10, 19, hour, minute, tzinfo=timezone.utc)
        )
        assert result is expected


def test_naive_clock_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        is_quiet(datetime(2026, 10, 19, 5, 0))
