"""Response models. Fields the bot relies on are required, so a renamed/removed field raises ApiChanged instead of a silent None."""
import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from booksy_bot.timezones import local_slot


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Slot(_Model):
    at: dt.time = Field(alias="t")      # local wall-clock time (Europe/Warsaw)
    p: str | None = None                # meaning unknown; always "" in observed traffic


class Day(_Model):
    day: dt.date = Field(alias="date")
    slots: list[Slot]
    working_day: Any = None             # only null observed so far

    def starts(self) -> list[dt.datetime]:
        """Slot start times as timezone-aware datetimes; DST handled in one place."""
        return [local_slot(self.day, s.at) for s in self.slots]


class TimeSlotsResponse(_Model):
    days: list[Day] = Field(alias="time_slots")


class Staffer(_Model):
    id: int
    name: str
    visible: bool = True
    active: bool = True


class Variant(_Model):
    id: int                                                    # = service_variant_id for time_slots / booking
    duration: int | None = None                                # minutes
    price: float | None = None
    service_price: str | None = None                           # display text, e.g. "60,00 zł"
    prepayment: float = 0.0
    deposit: float = 0.0
    staffer_ids: list[int] = Field(default_factory=list, alias="staffer_id")

    @property
    def needs_payment(self) -> bool:
        """Prepayment or deposit required: the bot must not auto-book these (booking-flow-safety #4)."""
        return self.prepayment > 0 or self.deposit > 0


class Service(_Model):
    id: int
    name: str
    active: bool = True
    is_available_for_customer_booking: bool = True
    variants: list[Variant]


class ServiceCategory(_Model):
    name: str = ""
    services: list[Service]


class Business(_Model):
    id: int
    name: str
    staff: list[Staffer]
    service_categories: list[ServiceCategory]
    url: str | None = None                                     # slug part after booksy.com/pl-pl/

    @property
    def page_url(self) -> str:
        return f"https://booksy.com/pl-pl/{self.url or self.id}"

    def bookable_services(self) -> list[Service]:
        return [s for c in self.service_categories for s in c.services if s.active and s.is_available_for_customer_booking]


class BusinessResponse(_Model):
    business: Business
