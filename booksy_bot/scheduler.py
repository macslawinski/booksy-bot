"""When to check: the normal interval and the booking-opening window (FAST_WINDOW) with its faster pace."""
import os
import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta


def interval() -> tuple[float, float]:
    lo, _, hi = os.environ.get("CHECK_MINUTES", "5-10").partition("-")
    lo_m = float(lo)
    hi_m = float(hi or lo)
    if lo_m < 2:
        raise SystemExit("CHECK_MINUTES: minimum 2 minutes – more often risks a ban")
    return lo_m * 60, hi_m * 60


@dataclass(frozen=True)
class FastWindow:
    """When a salon opens bookings (e.g. days 1-3 of the month, 19:55-20:35): check autobook monitors every few seconds."""
    first_day: int
    last_day: int
    start: time
    end: time
    seconds: float

    @classmethod
    def parse(cls, text: str) -> "FastWindow":
        """'1-3 19:55-20:35 10' = days of month, local time range, seconds between checks."""
        try:
            days, hours, secs = text.split()
            d1, _, d2 = days.partition("-")
            h1, h2 = hours.split("-")
            w = cls(int(d1), int(d2 or d1), time.fromisoformat(h1.zfill(5)), time.fromisoformat(h2.zfill(5)), float(secs))
        except ValueError:
            raise SystemExit(f"FAST_WINDOW: expected e.g. '1-3 19:55-20:35 10', got {text!r}") from None
        if w.seconds < 5:
            raise SystemExit("FAST_WINDOW: minimum 5 seconds – more often risks a ban")
        if not (1 <= w.first_day <= w.last_day <= 31 and w.start < w.end):
            raise SystemExit(f"FAST_WINDOW: bad days or hours in {text!r}")
        return w

    def active(self, now: datetime) -> bool:
        return self.first_day <= now.day <= self.last_day and self.start <= now.time() < self.end

    def seconds_to_start(self, now: datetime) -> float | None:
        """Seconds until today's window opens; None if it is not a window day or it already started."""
        if not self.first_day <= now.day <= self.last_day or now.time() >= self.start:
            return None
        return (datetime.combine(now.date(), self.start, tzinfo=now.tzinfo) - now).total_seconds()

    def next_start(self, now: datetime) -> datetime | None:
        """Start of the window running now or of the next one; None if no day in the next ~2 months fits."""
        for d in range(62):
            day = now.date() + timedelta(days=d)
            if self.first_day <= day.day <= self.last_day and datetime.combine(day, self.end, tzinfo=now.tzinfo) > now:
                return datetime.combine(day, self.start, tzinfo=now.tzinfo)
        return None

    def precheck_due(self, now: datetime) -> bool:
        """The last hour before today's window: time to check tokens."""
        to_start = self.seconds_to_start(now)
        return to_start is not None and to_start <= 3600


def next_delay(now: datetime, min_s: float, max_s: float, window: FastWindow | None) -> float:
    if window and window.active(now):
        return window.seconds * random.uniform(0.7, 1.3)        # jitter: not a fixed machine rhythm
    delay = random.uniform(min_s, max_s)
    to_start = window and window.seconds_to_start(now)
    return min(delay, to_start) if to_start is not None else delay   # wake up exactly when the window opens
