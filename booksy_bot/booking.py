"""Booking a slot from a Telegram message: check with dry_run/, the user confirms, then book exactly once.

Safety rules (booking-flow-safety): fresh dry_run right before the real POST, stop on any payment/deposit,
one attempt per (salon, start) stored in SQLite before sending, never resend after an unknown outcome.
Booksy has no slot lock, so there is nothing to roll back.
"""
from dataclasses import dataclass
from datetime import datetime

from booksy_bot.api.client import BooksyClient
from booksy_bot.api.http import ApiError, SessionExpired
from booksy_bot.api.models import AppointmentResponse
from booksy_bot.storage import Storage
from booksy_bot.timezones import SERVICE_TZ, local_slot

SLOT_FMT = "%Y%m%d%H%M"      # local wall-clock in button commands (callback_data max 64 bytes)


def now() -> datetime:
    return datetime.now(SERVICE_TZ)


@dataclass(frozen=True)
class BookArgs:
    business_id: int
    variant_id: int
    staffer_id: int
    start: datetime          # aware, Europe/Warsaw

    @classmethod
    def parse(cls, args: list[str]) -> "BookArgs":
        local = datetime.strptime(args[3], SLOT_FMT)
        return cls(int(args[0]), int(args[1]), int(args[2]), local_slot(local.date(), local.time()))

    @classmethod
    def of(cls, business_id: int, variant_id: int, staffer_id: int, iso: str) -> "BookArgs":
        return cls(business_id, variant_id, staffer_id, datetime.fromisoformat(iso).astimezone(SERVICE_TZ))

    def encode(self) -> str:
        return f"{self.business_id} {self.variant_id} {self.staffer_id} {self.start:{SLOT_FMT}}"


async def check(client: BooksyClient, b: BookArgs) -> AppointmentResponse:
    """dry_run for exactly this slot; ValueError with a readable reason when it can no longer be booked."""
    if b.start <= now():
        raise ValueError("ten termin już minął")
    try:
        r = await client.dry_run(b.business_id, b.variant_id, b.staffer_id, b.start)
    except SessionExpired:
        raise
    except ApiError as e:
        if e.status == 400:      # e.g. matcher_conflict: "usługa nie jest dostępna w wybranym czasie"
            raise ValueError("ten termin jest już zajęty albo niedostępny") from e
        raise
    if r.appointment.booked_from != b.start.replace(tzinfo=None):
        raise ValueError(f"Booksy zwrócił inną godzinę ({r.appointment.booked_from:%d.%m %H:%M}) – nie rezerwuję")
    return r


async def book(client: BooksyClient, storage: Storage, b: BookArgs, chat_id: int | None = None) -> str:
    """Re-check and book once. Returns the message for the user; ApiError before sending propagates."""
    slot = b.start.isoformat()
    blocked = storage.start_booking(b.business_id, slot, chat_id)
    if blocked == "confirmed":
        return "✅ Ten termin jest już zarezerwowany przez bota."   # by this person or a friend: one slot, one visit
    if blocked:  # pending / unknown: an earlier attempt may have booked it
        return ("⚠️ Wcześniejsza próba rezerwacji tego terminu nie dała pewnej odpowiedzi. "
                "Sprawdź wizyty w aplikacji Booksy – nie ponawiam, żeby nie zarezerwować dwa razy.")
    sent, status, uid = False, "unknown", None   # pessimistic default for anything unexpected after sending
    try:
        r = await check(client, b)                # the slot may have gone since the user saw the message
        if r.needs_payment:
            return "💳 Ten termin wymaga przedpłaty albo kaucji – bot nie płaci. Zarezerwuj ręcznie w Booksy."
        sent = True
        try:
            res = await client.book(b.business_id, b.variant_id, b.staffer_id, b.start)
        except SessionExpired:
            status = "failed"                     # 401: the server did not act
            raise
        except ApiError as e:
            if e.status is not None and 400 <= e.status < 500:
                status = "failed"                 # refused by the server, nothing booked
                return f"❌ Booksy odrzucił rezerwację: {e}"
            # shortcut: no automatic check of "my appointments" (endpoint not mapped yet); the user checks the app.
            return ("⚠️ Brak pewnej odpowiedzi od Booksy – rezerwacja mogła się udać. Sprawdź wizyty w aplikacji Booksy. "
                    "Nie ponawiam, żeby nie zarezerwować dwa razy.")
        uid = res.appointment.appointment_uid
        if uid is None:
            return "⚠️ Booksy odpowiedział bez numeru wizyty – sprawdź w aplikacji Booksy, czy rezerwacja jest."
        status = "confirmed"
        return f"✅ Zarezerwowano {b.start:%d.%m %H:%M}" + (f", {r.appointment.total}" if r.appointment.total else "") + "."
    finally:
        storage.finish_booking(b.business_id, slot, status if sent else "failed", None if uid is None else str(uid))
