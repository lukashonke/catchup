"""Parsing of the `--until` and `--since` values and the agent interval, and showing ages."""

import re
from datetime import UTC, date, datetime, time, timedelta

from catchup import CatchupError

_RELATIVE = re.compile(r"(\d+)([dw])")
_HOURS = re.compile(r"(\d+)h")
_INTERVAL = re.compile(r"(\d+)([mh])")


def parse_until(text: str, now: datetime) -> datetime:
    """Return the future moment that `Nh`, `Nd`, `Nw`, `mon`, `tomorrow` or a date names, in UTC."""
    today = now.astimezone().date()
    if text == "mon":
        # On a Monday, "mon" means the following Monday, not today.
        return _local_start_of(today + timedelta(days=7 - today.weekday()))
    if text == "tomorrow":
        return _local_start_of(today + timedelta(days=1))
    if match := _HOURS.fullmatch(text):
        return now.astimezone(UTC) + timedelta(hours=int(match[1]))
    return _parse(text, now, sign=1, forms="3h, 1d, 3d, 1w, mon, tomorrow or YYYY-MM-DD")


def parse_since(text: str, now: datetime) -> datetime:
    """Return the past moment that `Nd`, `Nw` or `YYYY-MM-DD` names, in UTC."""
    return _parse(text, now, sign=-1, forms="1d, 3d, 1w or YYYY-MM-DD")


def parse_every(text: str) -> timedelta:
    """Return the interval that `Nm` or `Nh` names, at least one minute."""
    if (match := _INTERVAL.fullmatch(text)) and int(match[1]) > 0:
        return timedelta(minutes=int(match[1]) * (60 if match[2] == "h" else 1))
    raise CatchupError(f"cannot read {text!r} as an interval; use 30m, 1h or 4h")


def _parse(text: str, now: datetime, sign: int, forms: str) -> datetime:
    if match := _RELATIVE.fullmatch(text):
        days = int(match[1]) * (7 if match[2] == "w" else 1)
        return now.astimezone(UTC) + sign * timedelta(days=days)
    try:
        return _local_start_of(date.fromisoformat(text))
    except ValueError:
        raise CatchupError(f"cannot read {text!r} as a time; use {forms}") from None


def _local_start_of(day: date) -> datetime:
    # A naive datetime passed to astimezone() is read as local time, with DST applied.
    return datetime.combine(day, time()).astimezone().astimezone(UTC)


def format_age(elapsed: timedelta) -> str:
    """Return a short age such as `now`, `5m`, `3h` or `2d`, rounded down."""
    minutes = max(0, int(elapsed.total_seconds() // 60))
    if minutes == 0:
        return "now"
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"
