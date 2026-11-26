import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

import booksy_bot.booking as bookingmod
from booksy_bot.bot import Bot
from booksy_bot.storage import Storage
from booksy_bot.timezones import SERVICE_TZ
from conftest import make_client
from test_bot import OWNER, FakeTg

APPT = json.loads((Path(__file__).parent / "fixtures/appointment.json").read_text(encoding="utf-8"))  # real GET, trimmed
UID = "700001"
SLOT = "2026-12-11T11:00:00+01:00"
GET_PATH = f"/core/v2/customer_api/me/appointments/{UID}/"
CANCEL_PATH = GET_PATH + "action/"


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(bookingmod, "now", lambda: datetime(2026, 11, 26, 12, 0, tzinfo=SERVICE_TZ))


def booksy(on_cancel=None, cancels=True, can_cancel=True):
    """Fake Booksy: GET returns the fixture with the current status; the cancel action switches it off."""
    state = {"status": "A"}
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "GET" and req.url.path == GET_PATH:
            data = copy.deepcopy(APPT)
            data["appointment"]["status"] = state["status"]
            data["appointment"]["actions"]["cancel"] = can_cancel and state["status"] == "A"
            return httpx.Response(200, json=data)
        if req.url.path == CANCEL_PATH:
            if cancels:
                state["status"] = "C"
            if isinstance(on_cancel, Exception):
                raise on_cancel
            return on_cancel or httpx.Response(200, json={"appointment": {}})
        return httpx.Response(404)
    return make_client(handler), calls


def setup(**kw):
    storage = Storage(":memory:")
    storage.start_booking(100001, SLOT, OWNER)
    storage.finish_booking(100001, SLOT, "confirmed", UID)
    client, calls = booksy(**kw)
    return Bot(FakeTg(), client, storage, OWNER), storage, calls


def run(bot, text, chat_id=OWNER):
    return asyncio.run(bot.handle(chat_id, text))


def cancel_posts(calls):
    return [c for c in calls if c[1] == CANCEL_PATH]


def test_visits_cancel_confirm_then_cancelled_once():
    bot, storage, calls = setup()
    text, buttons = run(bot, "/visits")
    assert "11.12, 11:00" in text and buttons[0][0]["callback_data"] == f"/cancel {UID}"
    text, buttons = run(bot, f"/cancel {UID}")
    assert "Odwołać wizytę <b>11.12 11:00</b> – Salon, 179,00" in text and cancel_posts(calls) == []   # nothing sent yet
    assert "✅ Odwołano wizytę 11.12 11:00" in run(bot, f"/cancelok {UID}")
    assert cancel_posts(calls) == [("POST", CANCEL_PATH, {"action": "cancel", "_version": 1795690523485, "no_thumbs": "true"})]
    assert storage.bookings(OWNER) == [] and storage.booked_months(100001, OWNER) == set()
    assert storage.start_booking(100001, SLOT, OWNER) is None                 # the slot can be booked again
    assert "nie rezerwował" in run(bot, f"/cancelok {UID}")                   # double click: no second cancel
    assert len(cancel_posts(calls)) == 1


def test_someone_elses_visit_is_not_touched():
    bot, _, calls = setup()
    assert "nie rezerwował" in run(bot, "/cancel 999")
    assert calls == []


def test_past_salon_deadline_no_cancel_sent():
    bot, storage, calls = setup(can_cancel=False)
    assert "minął termin" in run(bot, f"/cancelok {UID}")
    assert cancel_posts(calls) == [] and storage.bookings(OWNER) == [(SLOT, UID)]


def test_refused_cancel_keeps_the_visit():
    bot, storage, calls = setup(on_cancel=httpx.Response(400, json={"errors": [{"code": "x"}]}), cancels=False)
    assert "odrzucił odwołanie" in run(bot, f"/cancelok {UID}")
    assert storage.bookings(OWNER) == [(SLOT, UID)]


@pytest.mark.parametrize("cancels,expected", [(True, "✅ Odwołano"), (False, "nie potwierdził")])
def test_unknown_outcome_is_checked_not_resent(cancels, expected):
    bot, storage, calls = setup(on_cancel=httpx.ReadTimeout("slow"), cancels=cancels)
    assert expected in run(bot, f"/cancelok {UID}")
    assert len(cancel_posts(calls)) == 1
    assert (storage.bookings(OWNER) == []) is cancels
