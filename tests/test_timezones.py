"""DST regression tests. Dates are the EU switch days; re-check them if SERVICE_TZ changes."""
from datetime import date, datetime, timezone

import pytest

from booksy_bot.timezones import NonexistentLocalTime, local_slot, service_wall_clock, to_utc

SPRING = date(2026, 3, 29)   # 02:00 -> 03:00
AUTUMN = date(2026, 10, 25)  # 03:00 -> 02:00


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.mark.parametrize("day,hhmm,expected", [
    (date(2026, 1, 15), "14:00", utc(2026, 1, 15, 13, 0)),    # winter CET +1
    (date(2026, 7, 15), "14:00", utc(2026, 7, 15, 12, 0)),    # summer CEST +2
    (SPRING, "01:30", utc(2026, 3, 29, 0, 30)),               # before the jump: +1
    (SPRING, "03:30", utc(2026, 3, 29, 1, 30)),               # after the jump: +2
    (AUTUMN, "01:30", utc(2026, 10, 24, 23, 30)),             # before: +2
    (AUTUMN, "02:30", utc(2026, 10, 25, 0, 30)),              # ambiguous -> first occurrence (+2)
    (AUTUMN, "03:30", utc(2026, 10, 25, 2, 30)),              # after: +1
])
def test_to_utc_across_dst(day, hhmm, expected):
    assert to_utc(day, hhmm) == expected


def test_fixed_offset_would_be_wrong_in_winter():
    # the luxmed-bot bug: "-2h" is only right in summer
    assert to_utc(date(2026, 1, 15), "14:00") != utc(2026, 1, 15, 12, 0)


def test_nonexistent_spring_time_raises():
    with pytest.raises(NonexistentLocalTime):
        local_slot(SPRING, "02:30")


def test_roundtrip_wall_clock():
    for day, hhmm in [(SPRING, "03:30"), (AUTUMN, "03:30"), (date(2026, 7, 1), "09:15")]:
        assert service_wall_clock(to_utc(day, hhmm)) == f"{day} {hhmm}"


def test_naive_rejected():
    with pytest.raises(ValueError):
        service_wall_clock(datetime(2026, 7, 1, 9, 15))
