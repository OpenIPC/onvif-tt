"""Unit tests for the LOCAL-DEVICE-UTC-DATETIME helpers.

No DUT required — these exercise the pure parts of the check, which is where
both of its subtle cases live: a second the ONVIF schema allows but Python's
``datetime`` does not, and the difference between evidence and its absence.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

from onvif_tt.cases.base import _as_datetime


def _dt(year=2026, month=9, day=2, hour=11, minute=30, second=15):
    """A stand-in for the zeep object a tt:DateTime deserialises into."""
    return SimpleNamespace(
        Date=SimpleNamespace(Year=year, Month=month, Day=day),
        Time=SimpleNamespace(Hour=hour, Minute=minute, Second=second),
    )


def test_ordinary_time_round_trips():
    got = _as_datetime(_dt())
    assert got == datetime.datetime(
        2026, 9, 2, 11, 30, 15, tzinfo=datetime.timezone.utc)


def test_seconds_are_utc_aware():
    assert _as_datetime(_dt()).tzinfo is datetime.timezone.utc


def test_leap_second_is_not_read_as_a_missing_field():
    """onvif.xsd documents tt:Time/Second as "Range is 0 to 61".

    ``datetime`` stops at 59, so building one through the constructor turns a
    conformant device's timestamp into ``None`` — which the test can only
    report as a missing UTCDateTime, failing a mandatory case during a leap
    second with the wrong reason. 60 rolls into the following minute, which is
    within the check's thirty-second tolerance.
    """
    for second, expected_minute, expected_second in ((60, 31, 0), (61, 31, 1)):
        got = _as_datetime(_dt(minute=30, second=second))
        assert got is not None, f"Second={second} was rejected"
        assert (got.minute, got.second) == (expected_minute, expected_second)


def test_leap_second_at_midnight_rolls_the_day():
    got = _as_datetime(_dt(day=2, hour=23, minute=59, second=60))
    assert got == datetime.datetime(
        2026, 9, 3, 0, 0, 0, tzinfo=datetime.timezone.utc)


def test_a_genuinely_impossible_field_is_still_none():
    """The leniency is for seconds alone; everything else still means
    "this is not a datetime"."""
    assert _as_datetime(_dt(month=13)) is None
    assert _as_datetime(_dt(hour=24)) is None
    assert _as_datetime(_dt(minute=60)) is None
    assert _as_datetime(_dt(second="not a number")) is None


def test_absent_or_partial_field_is_none():
    assert _as_datetime(None) is None
    assert _as_datetime(SimpleNamespace(Date=None, Time=None)) is None
    assert _as_datetime(
        SimpleNamespace(Date=_dt().Date, Time=None)) is None
