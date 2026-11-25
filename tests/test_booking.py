import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

import booksy_bot.booking as bookingmod
from booksy_bot.booking import BookArgs, book, check
from booksy_bot.storage import Storage
from booksy_bot.timezones import SERVICE_TZ, NonexistentLocalTime
from conftest import make_client

DRY = json.loads((Path(__file__).parent / "fixtures/dry_run.json").read_text(encoding="utf-8"))  # real response, trimmed
DRY_PATH = "/core/v2/customer_api/me/appointments/business/100001/dry_run/"
BOOK_PATH = "/core/v2/customer_api/me/appointments/business/100001/"
SLOT = BookArgs.parse(["100001", "300001", "200001", "202611271400"])
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=SERVICE_TZ)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(bookingmod, "now", lambda: NOW)


def booksy(dry=DRY, on_book=None, slots=None, business=None):
    """dry_run echoes the requested time like the real API; on_book: Response or exception for the real POST.
    slots/business: also serve time_slots and the salon (for bot-level tests); those are not recorded in `calls`."""
    calls = []

    def handler(req):
        if slots is not None and req.url.path.endswith("/time_slots"):
            return httpx.Response(200, json=slots)
        if business is not None and "/businesses/100001/" in req.url.path:
            return httpx.Response(200, json=business)
        if dry is None and req.url.path == DRY_PATH:
            return httpx.Response(400, json={"errors": [{"code": "matcher_conflict"}]})
        body = json.loads(req.content)
        calls.append((req.url.path, body))
        resp = copy.deepcopy(dry)
        resp["appointment"]["booked_from"] = body["subbookings"][0]["booked_from"].replace(" ", "T")
        if req.url.path == DRY_PATH:
            return httpx.Response(201, json=resp)
        if req.url.path == BOOK_PATH:
            if isinstance(on_book, Exception):
                raise on_book
            if on_book is not None:
                return on_book
            resp["appointment"]["appointment_uid"] = 987654
            return httpx.Response(201, json=resp)
        return httpx.Response(404)
    return make_client(handler), calls


def paths(calls):
    return [p for p, _ in calls]


def status(storage, slot=SLOT):
    return storage.db.execute("select status from booking_attempt where slot = ?", (slot.start.isoformat(),)).fetchone()[0]


def test_dry_run_request_and_real_response():
    client, calls = booksy()
    r = asyncio.run(check(client, SLOT))
    assert not r.needs_payment and r.appointment.total == "330,00\xa0zł"
    assert calls == [(DRY_PATH, {
        "subbookings": [{"staffer_id": 200001, "booked_from": "2026-11-27 14:00",
                         "service_variant": {"mode": "variant", "id": 300001}, "combo_children": []}],
        "is_cancellation_fee_preauth_flow": False, "pre_auth_type": "control", "compatibilities": {"prepayment": True}})]


@pytest.mark.parametrize("field,value", [("prepayment_total", "50.00"), ("cancellation_fee_total", "20.00")])
def test_payment_stops_booking(field, value):
    dry = copy.deepcopy(DRY)
    dry["appointment_payment"][field] = value
    client, calls = booksy(dry)
    storage = Storage(":memory:")
    assert "przedpłaty" in asyncio.run(book(client, storage, SLOT))
    assert BOOK_PATH not in paths(calls) and status(storage) == "failed"


def test_missing_payment_data_counts_as_payment():
    dry = copy.deepcopy(DRY)
    del dry["appointment_payment"]
    client, _ = booksy(dry)
    assert asyncio.run(check(client, SLOT)).needs_payment


def test_books_once_and_double_click_does_not_book_again():
    client, calls = booksy()
    storage = Storage(":memory:")
    assert "Zarezerwowano 27.11 14:00, 330,00\xa0zł" in asyncio.run(book(client, storage, SLOT))
    assert paths(calls) == [DRY_PATH, BOOK_PATH] and status(storage) == "confirmed"
    assert "już zarezerwowany" in asyncio.run(book(client, storage, SLOT))
    assert paths(calls) == [DRY_PATH, BOOK_PATH]


def test_slot_gone_no_booking_and_retry_allowed():
    client = make_client(lambda req: httpx.Response(400, json={"errors": [{"code": "matcher_conflict"}]}))
    storage = Storage(":memory:")
    with pytest.raises(ValueError, match="zajęty"):
        asyncio.run(book(client, storage, SLOT))
    assert status(storage) == "failed" and storage.start_booking(100001, SLOT.start.isoformat()) is None


def test_timeout_on_booking_is_not_resent_and_blocks_retries():
    client, calls = booksy(on_book=httpx.ReadTimeout("slow"))
    storage = Storage(":memory:")
    assert "mogła się udać" in asyncio.run(book(client, storage, SLOT))
    assert paths(calls).count(BOOK_PATH) == 1 and status(storage) == "unknown"
    assert "nie dała pewnej odpowiedzi" in asyncio.run(book(client, storage, SLOT))
    assert paths(calls).count(BOOK_PATH) == 1


def test_server_error_on_booking_is_unknown_not_resent():
    client, calls = booksy(on_book=httpx.Response(502))
    storage = Storage(":memory:")
    asyncio.run(book(client, storage, SLOT))
    assert paths(calls).count(BOOK_PATH) == 1 and status(storage) == "unknown"


def test_refused_booking_is_failed_and_can_be_retried():
    client, _ = booksy(on_book=httpx.Response(400, json={"errors": [{"code": "x"}]}))
    storage = Storage(":memory:")
    assert "odrzucił" in asyncio.run(book(client, storage, SLOT))
    assert status(storage) == "failed"


def test_booking_without_uid_is_unknown():
    client, _ = booksy(on_book=httpx.Response(201, json=DRY))   # appointment_uid null
    storage = Storage(":memory:")
    assert "bez numeru wizyty" in asyncio.run(book(client, storage, SLOT))
    assert status(storage) == "unknown"


def test_dry_run_with_different_time_is_refused():
    client = make_client(lambda req: httpx.Response(201, json=DRY))   # fixture says 14:00, we ask for 15:00
    with pytest.raises(ValueError, match="inną godzinę"):
        asyncio.run(check(client, BookArgs.parse(["100001", "300001", "200001", "202611271500"])))


def test_past_slot_refused():
    with pytest.raises(ValueError, match="minął"):
        asyncio.run(check(booksy()[0], BookArgs.parse(["100001", "300001", "200001", "202611251100"])))


def test_slot_round_trip_and_dst():
    assert BookArgs.of(100001, 300001, -1, "2026-11-27T14:00:00+01:00").encode() == "100001 300001 -1 202611271400"
    summer = BookArgs.parse(["1", "2", "3", "202603290330"])     # first hour of summer time
    assert summer.start.utcoffset().total_seconds() == 7200
    with pytest.raises(NonexistentLocalTime):
        BookArgs.parse(["1", "2", "3", "202603290230"])           # skipped by the March jump
    autumn = BookArgs.parse(["1", "2", "3", "202610250230"])     # happens twice: first (summer) one
    assert autumn.start.utcoffset().total_seconds() == 7200 and autumn.encode().endswith("202610250230")


def test_book_button_asks_then_books(monkeypatch):
    import booksy_bot.bot as botmod
    from test_bot import commands, labels, new_bot, run
    monkeypatch.setattr(botmod, "today", lambda: NOW.date())
    bot, _ = new_bot()
    found = run(bot, "/find 100001 300001 -1 20261127-20261128 0 1440")
    assert commands(found)[0] == "/book 100001 300001 -1 202611271400" and "Rezerwuj" in labels(found)[0]
    bot.client, calls = booksy()
    ask = run(bot, commands(found)[0])
    assert "Rezerwować" in ask[0] and "330,00\xa0zł" in ask[0] and commands(ask) == ["/confirm 100001 300001 -1 202611271400"]
    assert len(calls) == 1                                      # asking only ran dry_run, nothing booked
    assert "Zarezerwowano" in run(bot, commands(ask)[0])
    assert len(calls) == 3                                      # fresh dry_run + one booking


# ---------- autobook ----------

EMPTY = {"time_slots": []}


@pytest.fixture
def auto_bot(monkeypatch):
    import booksy_bot.bot as botmod
    from test_bot import new_bot
    monkeypatch.setattr(botmod, "today", lambda: NOW.date())
    return new_bot(slots=EMPTY)


def test_no_slots_offers_watch_or_autobook_then_books_first_slot(auto_bot):
    from test_bot import BUSINESS, SLOTS, commands, run
    from booksy_bot.bot import check_monitor
    bot, storage = auto_bot
    found = run(bot, "/find 100001 300001 -1 20261127-20261128 0 1440")
    assert "Brak wolnych terminów" in found[0]
    assert commands(found) == ["/watch 100001 300001 -1 20261127-20261128 0 1440",
                               "/auto 100001 300001 -1 20261127-20261128 0 1440"]
    ask = run(bot, commands(found)[1])
    assert "bez pytania" in ask[0] and commands(ask) == ["/autoon 100001 300001 -1 20261127-20261128 0 1440"]
    assert storage.active_monitors() == []                      # explaining does not switch anything on
    assert "Zarezerwuję automatycznie" in run(bot, commands(ask)[0])
    m = storage.active_monitors()[0]
    assert m.autobook == 1

    client, calls = booksy(slots=SLOTS, business=BUSINESS)    # a slot appears
    msg = asyncio.run(check_monitor(client, storage, m, NOW.date()))
    assert "Zarezerwowano 27.11 14:00" in msg and paths(calls) == [DRY_PATH, BOOK_PATH]
    assert calls[1][1]["subbookings"][0]["booked_from"] == "2026-11-27 14:00"   # the earliest one
    assert storage.active_monitors() == []                      # done: no second booking from this monitor


def test_autobook_refused_falls_back_to_notifications(auto_bot):
    from test_bot import SLOTS, add
    from booksy_bot.bot import check_monitor
    _, storage = auto_bot
    m = add(storage, autobook=True)
    client, calls = booksy(slots=SLOTS, on_book=httpx.Response(400, json={"errors": [{"code": "consent"}]}))
    text, buttons = asyncio.run(check_monitor(client, storage, m, NOW.date()))
    assert "Autorezerwacja wyłączona" in text and "odrzucił" in text and buttons
    assert [x.autobook for x in storage.active_monitors()] == [0]   # still notifying, no more autobook
    assert paths(calls).count(BOOK_PATH) == 1


def test_autobook_slot_gone_tries_again_next_time(auto_bot):
    from test_bot import SLOTS, add
    from booksy_bot.bot import check_monitor
    _, storage = auto_bot
    m = add(storage, autobook=True)
    client, calls = booksy(dry=None, slots=SLOTS)               # dry_run: 400 matcher_conflict
    assert asyncio.run(check_monitor(client, storage, m, NOW.date())) is None
    assert BOOK_PATH not in paths(calls) and storage.active_monitors()[0].autobook == 1


def test_autobook_refused_for_paid_service_and_limited(auto_bot):
    from test_bot import BUSINESS, add, run
    bot, storage = auto_bot
    paid = copy.deepcopy(BUSINESS)
    for c in paid["business"]["service_categories"]:
        for s in c["services"]:
            for v in s["variants"]:
                v["prepayment"] = 20.0
    bot.client = booksy(slots=EMPTY, business=paid)[0]
    assert "przedpłaty" in run(bot, "/auto 100001 300001 -1 20261127-20261128 0 1440")
    bot.client = booksy(slots=EMPTY, business=BUSINESS)[0]
    for _ in range(3):
        add(storage, autobook=True)
    assert "limit 3 autorezerwacji" in run(bot, "/autoon 100001 300001 -1 20261127-20261128 0 1440")


def test_autobook_button_fits_telegram_limit():
    from booksy_bot.bot import Query
    worst = Query(99999999, 99999999, 99999999, date_from=NOW.date(), date_to=NOW.date(), time_from=1000, time_to=1440)
    assert len(f"/autoon {worst.encode()}".encode()) <= 64
