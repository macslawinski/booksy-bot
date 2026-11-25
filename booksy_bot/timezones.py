"""Service-local time <-> UTC in one place. Never add/subtract fixed hours (luxmed-bot's `minusHours(2)` broke every winter).

Requires the `tzdata` package on Windows/slim images (zoneinfo has no system tz database there).
"""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

SERVICE_TZ = ZoneInfo("Europe/Warsaw")


class NonexistentLocalTime(ValueError):
    """Wall-clock time skipped by the spring DST jump (e.g. 02:30 on the last Sunday of March)."""


def local_slot(day: date, hhmm: str | time) -> datetime:
    """Aware datetime for a slot the service reports as local wall-clock time.

    Ambiguous autumn times (02:00-02:59 occurs twice) resolve to the first occurrence (fold=0, summer time);
    nonexistent spring times raise, because booking a time that does not exist is a bug upstream, not something to guess.
    """
    t = hhmm if isinstance(hhmm, time) else time.fromisoformat(hhmm)
    dt = datetime.combine(day, t, tzinfo=SERVICE_TZ)
    roundtrip = dt.astimezone(timezone.utc).astimezone(SERVICE_TZ)
    if roundtrip.replace(tzinfo=None) != dt.replace(tzinfo=None):
        raise NonexistentLocalTime(f"{day} {t} does not exist in {SERVICE_TZ.key}")
    return dt


def to_utc(day: date, hhmm: str | time) -> datetime:
    return local_slot(day, hhmm).astimezone(timezone.utc)


def service_wall_clock(dt: datetime) -> str:
    """Format an aware datetime the way the service expects local time ('YYYY-MM-DD HH:MM')."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime: attach a timezone before formatting")
    return dt.astimezone(SERVICE_TZ).strftime("%Y-%m-%d %H:%M")
