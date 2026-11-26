import asyncio
import copy
import json
from datetime import datetime

import httpx
import pytest

import booksy_bot.booking as bookingmod
import booksy_bot.bot as botmod
from booksy_bot.bot import Bot
from booksy_bot.storage import Storage
from booksy_bot.timezones import SERVICE_TZ
from conftest import make_client
from test_booking import BOOK_PATH, DRY
from test_bot import BUSINESS, OWNER, SLOTS, FakeTg

FRIEND, STRANGER = 222, 333
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=SERVICE_TZ)
TOKEN = "friend-token-" + "x" * 20


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(bookingmod, "now", lambda: NOW)
    monkeypatch.setattr(botmod, "today", lambda: NOW.date())


def api(name, log, slots=SLOTS):
    """Fake Booksy for one person: records (who, path); books whatever is asked, then that slot is gone for everyone."""
    def handler(req):
        log.append((name, req.url.path, req.headers.get("X-Access-Token")))
        if req.url.path.endswith("/time_slots"):
            data = copy.deepcopy(slots)
            for d in data["time_slots"]:
                d["slots"] = [s for s in d["slots"] if f"{d['date']} {s['t']}" not in BOOKED]
            return httpx.Response(200, json=data)
        if "/businesses/100001/" in req.url.path:
            return httpx.Response(200, json=BUSINESS)
        body = json.loads(req.content)
        at = body["subbookings"][0]["booked_from"]
        resp = copy.deepcopy(DRY)
        resp["appointment"]["booked_from"] = at.replace(" ", "T")
        if req.url.path == BOOK_PATH:
            BOOKED.add(at)
            resp["appointment"]["appointment_uid"] = len(BOOKED)
        return httpx.Response(201, json=resp)
    return make_client(handler)


BOOKED: set[str] = set()


@pytest.fixture
def setup():
    BOOKED.clear()
    log = []
    storage = Storage(":memory:")
    tg = FakeTg()
    bot = Bot(tg, api("owner", log), storage, OWNER, friend_client=lambda chat_id: api(f"friend{chat_id}", log))
    bot.password = "test-pass"                                       # BOT_PASSWORD
    return bot, storage, tg, log


def run(bot, text, chat_id=OWNER, name=""):
    return asyncio.run(bot.handle(chat_id, text, name))


def join(bot, chat_id=FRIEND):
    assert "Podaj hasło" in run(bot, "/start", chat_id, "Ania")
    return run(bot, "test-pass", chat_id, "Ania")


def test_join_with_password_strangers_are_ignored(setup):
    bot, storage, tg, _ = setup
    assert run(bot, "/list", STRANGER) is None                      # no /start: silence
    assert run(bot, "test-pass", STRANGER) is None                  # password without /start: silence
    assert "Podaj hasło" in run(bot, "/start", FRIEND, "Ania")
    assert storage.user(FRIEND) is None                             # not in yet
    assert "Zostało prób: 2" in run(bot, "wrong", FRIEND)
    welcome = run(bot, "test-pass", FRIEND, "Ania")
    assert "Twoje konto Booksy" in welcome and "/login" in welcome
    assert any("Dołączył(a) Ania" in text for chat, text in tg.sent if chat == OWNER)
    assert "Ania" in run(bot, "/users") and "bez tokenu" in run(bot, "/users")
    assert run(bot, "/users", FRIEND).startswith("Nie rozumiem")     # owner-only commands


def test_no_password_configured_nobody_joins(setup):
    bot, storage, _, _ = setup
    bot.password = ""
    assert run(bot, "/start", FRIEND) is None and run(bot, "", FRIEND) is None and storage.user(FRIEND) is None


def test_friend_uses_own_token_and_account(setup):
    bot, storage, _, log = setup
    join(bot)
    assert "Wyślij" in run(bot, "/token short", FRIEND)
    assert "Token zapisany" in run(bot, f"/token {TOKEN}", FRIEND)
    assert storage.user(FRIEND)[1] == TOKEN
    run(bot, "/find 100001 300001 -1 20261127-20261128 0 1440", FRIEND)
    assert {who for who, _, _ in log} == {f"friend{FRIEND}"}         # nothing went through the owner's account
    log.clear()
    run(bot, "/find 100001 300001 -1 20261127-20261128 0 1440")
    assert {who for who, _, _ in log} == {"owner"}


def test_friend_limits(setup):
    bot, storage, _, _ = setup
    join(bot)
    q = "100001 300001 -1 20261127-20261128 0 1440"
    storage.add_monitor(FRIEND, 100001, "S", "u", 300001, "X", -1, 4, date_from="2026-12-01", date_to="2026-12-31", autobook=True)
    assert "limit 1 autorezerwacji" in run(bot, f"/autoon {q}", FRIEND)
    for _ in range(2):
        storage.add_monitor(FRIEND, 100001, "S", "u", 300001, "X", -1, 4)
    assert "limit 3 powiadomień" in run(bot, f"/watch {q}", FRIEND)


def test_owner_has_priority_on_the_same_barber(setup):
    bot, storage, tg, log = setup
    join(bot)
    run(bot, f"/token {TOKEN}", FRIEND)
    kw = dict(date_from="2026-11-27", date_to="2026-11-28", autobook=True)
    friend = storage.add_monitor(FRIEND, 100001, "S", "u", 300001, "X", -1, 4, **kw)    # created first
    owner = storage.add_monitor(OWNER, 100001, "S", "u", 300001, "X", -1, 4, **kw)
    storage.update_slots(friend.id, set())
    storage.update_slots(owner.id, set())
    asyncio.run(bot.check_all(fast=True))
    assert [who for who, path, _ in log if path == BOOK_PATH] == ["owner", f"friend{FRIEND}"]
    assert sorted(BOOKED) == ["2026-11-27 14:00", "2026-11-27 17:00"]   # owner: earliest, friend: the next one
    owner_msgs = [t for c, t in tg.sent if c == OWNER and "Zarezerwowano" in t]
    friend_msgs = [t for c, t in tg.sent if c == FRIEND and "Zarezerwowano" in t]
    assert "27.11 14:00" in owner_msgs[0] and "27.11 17:00" in friend_msgs[0]
    assert storage.active_monitors() == []


def test_expired_friend_token_does_not_stop_the_owner(setup, monkeypatch):
    bot, storage, tg, _ = setup
    join(bot)
    bot._friend_clients[FRIEND] = make_client(lambda req: httpx.Response(401))
    storage.add_monitor(FRIEND, 100001, "S", "u", 300001, "X", -1, 4)
    storage.add_monitor(OWNER, 100001, "S", "u", 300001, "X", -1, 4)
    monkeypatch.setattr(botmod.random, "uniform", lambda a, b: 0)   # no pause between monitors
    asyncio.run(bot.check_all())
    assert any(c == FRIEND and "/login" in t for c, t in tg.sent)
    assert any(c == OWNER and "Powiadomię" in t for c, t in tg.sent)


def test_kick_removes_token_and_monitors(setup):
    bot, storage, _, _ = setup
    join(bot)
    run(bot, f"/token {TOKEN}", FRIEND)
    storage.add_monitor(FRIEND, 100001, "S", "u", 300001, "X", -1, 4)
    assert run(bot, f"/kick {FRIEND}") == "✅ Usunięto i zablokowano."
    assert storage.user(FRIEND) is None and storage.chat_monitors(FRIEND) == []
    assert run(bot, "/list", FRIEND) is None
    assert run(bot, "/start", FRIEND) is None and run(bot, "test-pass", FRIEND) is None   # cannot rejoin with the password
    assert storage.user(FRIEND) is None


def test_booked_months_are_per_person():
    s = Storage(":memory:")
    s.start_booking(100001, "2026-12-03T10:00:00+01:00", OWNER)
    s.finish_booking(100001, "2026-12-03T10:00:00+01:00", "confirmed")
    assert s.booked_months(100001, OWNER) == {"2026-12"} and s.booked_months(100001, FRIEND) == set()


def test_wrong_password_three_times_blocks_the_chat_for_good(setup):
    bot, storage, tg, _ = setup
    run(bot, "/start", STRANGER)
    run(bot, "a", STRANGER)
    run(bot, "/start", STRANGER)                                    # restarting does not reset the tries
    run(bot, "b", STRANGER)
    assert "zablokowany" in run(bot, "c", STRANGER)
    assert any(c == OWNER and "złe hasło" in t for c, t in tg.sent)
    assert run(bot, "/start", STRANGER) is None and run(bot, "test-pass", STRANGER) is None
    bot2 = Bot(FakeTg(), bot.client, storage, OWNER)                # after a restart too
    bot2.password = "test-pass"
    assert run(bot2, "/start", STRANGER) is None and storage.user(STRANGER) is None


def test_login_gives_phone_bookmarklet_for_this_bot(setup):
    bot, _, _, _ = setup
    bot.username = "Test_bot"
    reply = run(bot, "/login")
    code = reply.split("<code>")[-1].split("</code>")[0]
    assert code.startswith("javascript:") and "https://t.me/Test_bot'" in code
    assert code.isascii() and " " not in code and "&" not in code and "<" not in code   # Safari: encoded ASCII
    assert "x-access-token" in code and "Booksy bot" in reply


def test_owner_can_set_token_from_phone_and_is_not_a_friend(setup):
    bot, storage, _, _ = setup
    assert "Token zapisany" in run(bot, f"/token {TOKEN}")
    assert storage.user(OWNER)[1] == TOKEN
    assert run(bot, "/users") == "Brak znajomych."
    assert run(bot, f"/kick {OWNER}").startswith("Nie rozumiem")    # the owner cannot remove themselves
