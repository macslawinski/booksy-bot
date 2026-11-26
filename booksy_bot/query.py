"""What to search for: typed dates and hours, and the Query carried in button commands and stored for monitoring."""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from booksy_bot.api.client import ANY_STAFFER
from booksy_bot.cli import business_id_arg
from booksy_bot.storage import DAY_END, Monitor

MAX_DAYS = 92              # salons open bookings up to ~2 months ahead


def fmt_minutes(m: int) -> str:
    return f"{m // 60}:{m % 60:02d}"


# ---------- user input ----------

_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?")
_TIME = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?")


def parse_dates(text: str, now: date) -> tuple[date, date]:
    """'5.12' or '1.12-15.12' (optionally with year); a date without a year that already passed means next year."""
    found = _DATE.findall(text)
    if not 1 <= len(found) <= 2:
        raise ValueError("podaj datę albo zakres, np. 5.12 albo 1.12-15.12")
    try:
        out = [date(int(y) + (2000 if len(y) == 2 else 0) if y else now.year, int(m), int(d)) for d, m, y in found]
        if not any(y for _, _, y in found):                # no year typed: pick it for the range as a whole
            if out[-1] < out[0]:
                out[-1] = out[-1].replace(year=out[-1].year + 1)          # 20.12-5.1
            if out[-1] < now:
                out = [x.replace(year=x.year + 1) for x in out]           # 10.1 typed in November
    except ValueError:
        raise ValueError(f"nie ma takiej daty: {text.strip()}") from None
    start, end = out[0], out[-1]
    if end < start:
        raise ValueError("data końcowa jest przed początkową")
    if end < now:
        raise ValueError("ten zakres dat już minął")
    if (end - max(start, now)).days >= MAX_DAYS:
        raise ValueError(f"zakres najwyżej {MAX_DAYS} dni")
    return start, end


def parse_times(text: str) -> tuple[int, int]:
    """'16-20' or '16:30-19:00' -> minutes after midnight (slot start within the range, inclusive)."""
    found = _TIME.findall(text)
    if len(found) != 2:
        raise ValueError("podaj zakres godzin, np. 16-20 albo 16:30-19:00")
    (h1, m1), (h2, m2) = found
    a, b = int(h1) * 60 + int(m1 or 0), int(h2) * 60 + int(m2 or 0)
    if int(m1 or 0) > 59 or int(m2 or 0) > 59 or not 0 <= a < b <= DAY_END:
        raise ValueError("godziny 0:00–24:00, początek przed końcem")
    return a, b


# ---------- search query ----------

@dataclass(frozen=True)
class Query:
    """What to search: carried between wizard steps inside button commands, and stored for monitoring."""
    business_id: int
    variant_id: int
    staffer_id: int = ANY_STAFFER
    days: int = 14                       # rolling window from today, used when date_from is None
    date_from: date | None = None
    date_to: date | None = None
    time_from: int = 0
    time_to: int = DAY_END

    @classmethod
    def parse(cls, args: list[str]) -> "Query":
        """<salon> <variant> [staffer] [days | YYYYMMDD-YYYYMMDD] [time_from time_to] (minutes)."""
        q = {"business_id": business_id_arg(args[0]), "variant_id": int(args[1])}
        if len(args) > 2:
            q["staffer_id"] = int(args[2])
        if len(args) > 3:
            if "-" in args[3]:
                a, b = args[3].split("-")
                q["date_from"], q["date_to"] = (datetime.strptime(x, "%Y%m%d").date() for x in (a, b))
            else:
                q["days"] = int(args[3])
        if len(args) > 5:
            q["time_from"], q["time_to"] = int(args[4]), int(args[5])
        out = cls(**q)
        if not 1 <= out.days <= MAX_DAYS:
            raise ValueError(f"liczba dni 1–{MAX_DAYS}")
        if not 0 <= out.time_from < out.time_to <= DAY_END:
            raise ValueError("zły zakres godzin")
        return out

    @classmethod
    def of(cls, m: Monitor) -> "Query":
        return cls(m.business_id, m.variant_id, m.staffer_id, m.days,
                   date.fromisoformat(m.date_from) if m.date_from else None,
                   date.fromisoformat(m.date_to) if m.date_to else None, m.time_from, m.time_to)

    def dates_arg(self) -> str:
        return f"{self.date_from:%Y%m%d}-{self.date_to:%Y%m%d}" if self.date_from else str(self.days)

    def encode(self) -> str:
        return f"{self.business_id} {self.variant_id} {self.staffer_id} {self.dates_arg()} {self.time_from} {self.time_to}"

    def window(self, now: date) -> tuple[date, date]:
        if self.date_from and self.date_to:
            return max(self.date_from, now), self.date_to
        return now, now + timedelta(days=self.days - 1)

    def expired(self, now: date) -> bool:
        return self.date_to is not None and self.date_to < now

    def scope(self) -> str:
        if self.date_from and self.date_to:
            dates = f"{self.date_from:%d.%m}" + ("" if self.date_from == self.date_to else f"–{self.date_to:%d.%m}")
        else:
            dates = f"najbliższe {self.days} dni"
        if (self.time_from, self.time_to) != (0, DAY_END):
            dates += f", {fmt_minutes(self.time_from)}–{fmt_minutes(self.time_to)}"
        return dates
