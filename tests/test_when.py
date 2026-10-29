import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from catchup import CatchupError
from catchup.when import format_age, parse_since, parse_until

# Wednesday 2026-10-28 23:30 in Prague (CET, UTC+1, after the DST switch on 10-25).
NOW = datetime(2026, 10, 28, 22, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def prague_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "Europe/Prague")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1d", datetime(2026, 10, 29, 22, 30, tzinfo=UTC)),
        ("3d", datetime(2026, 10, 31, 22, 30, tzinfo=UTC)),
        ("1w", datetime(2026, 11, 4, 22, 30, tzinfo=UTC)),
        ("mon", datetime(2026, 11, 1, 23, 0, tzinfo=UTC)),
        ("2026-11-03", datetime(2026, 11, 2, 23, 0, tzinfo=UTC)),
        # Before the DST switch Prague is UTC+2.
        ("2026-10-20", datetime(2026, 10, 19, 22, 0, tzinfo=UTC)),
    ],
)
def test_parse_until(text: str, expected: datetime) -> None:
    assert parse_until(text, NOW) == expected


def test_mon_on_a_monday_is_the_following_monday() -> None:
    monday_morning = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    assert parse_until("mon", monday_morning) == datetime(2026, 11, 8, 23, 0, tzinfo=UTC)


def test_mon_uses_the_local_day_not_the_utc_day() -> None:
    # Sunday 23:30 UTC is already Monday 00:30 in Prague.
    sunday_utc = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)
    assert parse_until("mon", sunday_utc) == datetime(2026, 11, 8, 23, 0, tzinfo=UTC)


def test_parse_since() -> None:
    assert parse_since("3d", NOW) == datetime(2026, 10, 25, 22, 30, tzinfo=UTC)
    assert parse_since("2026-10-01", NOW) == datetime(2026, 9, 30, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize("text", ["tomorrow", "1h", "d", "2026-13-01"])
def test_rejects_unknown_forms(text: str) -> None:
    with pytest.raises(CatchupError, match="cannot read"):
        parse_until(text, NOW)


def test_since_rejects_mon() -> None:
    with pytest.raises(CatchupError):
        parse_since("mon", NOW)


def test_format_age_rounds_down_and_says_now_under_a_minute() -> None:
    assert [format_age(timedelta(seconds=s)) for s in (59, 60, 3599, 3600, 86400 * 3)] == [
        "now",
        "1m",
        "59m",
        "1h",
        "3d",
    ]
