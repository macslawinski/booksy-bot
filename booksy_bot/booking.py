"""Booking a slot from a Telegram message: check with dry_run/, the user confirms, then book exactly once.

Safety rules (booking-flow-safety): fresh dry_run right before the real POST, stop on any payment/deposit,
one attempt per (salon, start) stored in SQLite before sending, never resend after an unknown outcome.
Booksy has no slot lock, so there is nothing to roll back.
"""
from dataclasses import dataclass
from datetime import datetime

from booksy_bot.api.client import BooksyClient
from booksy_bot.api.http import ApiError, SessionExpired
from booksy_bot.api.models import AppointmentDetails, AppointmentResponse
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
            uid = await visit_at(client, b)               # unknown outcome: Booksy's own list says whether it happened
            if uid is None:
                return ("⚠️ Brak pewnej odpowiedzi od Booksy – rezerwacja mogła się udać. Sprawdź wizyty w aplikacji Booksy. "
                        "Nie ponawiam, żeby nie zarezerwować dwa razy.")
        else:
            uid = res.appointment.appointment_uid or await visit_at(client, b)
            if uid is None:
                return "⚠️ Booksy odpowiedział bez numeru wizyty – sprawdź w aplikacji Booksy, czy rezerwacja jest."
        status = "confirmed"
        return (f"✅ Zarezerwowano {b.start:%d.%m %H:%M}" + (f", {r.appointment.total}" if r.appointment.total else "")
                + ". Odwołać możesz w /visits.")
    finally:
        storage.finish_booking(b.business_id, slot, status if sent else "failed", None if uid is None else str(uid))


async def visit_at(client: BooksyClient, b: BookArgs) -> str | None:
    """uid of my active visit at exactly this salon and start, from Booksy's list; None if absent or the list fails."""
    try:
        visits = (await client.my_bookings()).bookings
    except ApiError:
        return None
    start = b.start.replace(tzinfo=None)              # the list has naive local wall-clock times
    return next((str(v.appointment_uid) for v in visits
                 if v.active and v.business.id == b.business_id and v.booked_from == start), None)


async def visit_months(client: BooksyClient, business_id: int) -> set[str]:
    """'YYYY-MM' of my active visits in this salon, also those booked by hand in the app.
    A list up to 5 min old is fine: it only changes when someone books by hand, and the bot's own bookings reset it."""
    visits = (await client.my_bookings(max_age=300)).bookings
    return {f"{v.booked_from:%Y-%m}" for v in visits if v.active and v.business.id == business_id}


async def cancellable(client: BooksyClient, uid: str) -> AppointmentDetails:
    """Fresh state of one of my visits (the token's account: Booksy refuses other people's); ValueError if not cancellable."""
    a = (await client.appointment(uid)).appointment
    if a.status != "A":
        raise ValueError("ta wizyta nie jest już aktywna (może została odwołana)")
    if not a.actions.cancel:
        raise ValueError("Booksy nie pozwala już odwołać tej wizyty (minął termin salonu) – zadzwoń do salonu")
    return a


async def cancel(client: BooksyClient, storage: Storage, chat_id: int, uid: str) -> str:
    """Cancel once, then confirm with a fresh GET: the POST's own answer is not trusted (unknown outcome is possible)."""
    a = await cancellable(client, uid)                    # fresh _version; a double click finds it already cancelled
    try:
        await client.cancel(uid, a.version)
    except SessionExpired:
        raise
    except ApiError as e:
        if e.status is not None and 400 <= e.status < 500:
            return f"❌ Booksy odrzucił odwołanie: {e}"
        # unknown outcome or unexpected body: the GET below says what happened
    if (await client.appointment(uid)).appointment.status == "A":
        return "⚠️ Booksy nie potwierdził odwołania – sprawdź wizytę w aplikacji Booksy."
    storage.cancelled(chat_id, uid)                         # the bot's own record, if it booked this visit
    return f"✅ Odwołano wizytę {a.booked_from:%d.%m %H:%M} – {a.business.name}."
