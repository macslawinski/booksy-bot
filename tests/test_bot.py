import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest

import booksy_bot.bot as botmod
from booksy_bot.bot import Bot, Query, check_monitor, fold, parse_dates, parse_times
from booksy_bot.storage import Storage
from booksy_bot.telegram import Telegram, TelegramError
from conftest import make_client

FIX = Path(__file__).parent / "fixtures"
SLOTS = json.loads((FIX / "time_slots.json").read_text(encoding="utf-8"))      # 27.11 14:00–19:00, 28.11 08:00–10:30
BUSINESS = json.loads((FIX / "business.json").read_text(encoding="utf-8"))
OWNER = 111
NOW = date(2026, 11, 25)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(botmod, "today", lambda: NOW)


def booksy(slots=SLOTS, business=BUSINESS):
    def handler(req):
        if req.url.path.endswith("/time_slots"):
            return httpx.Response(200, json=slots)
        if "/businesses/100001/" in req.url.path:
            return httpx.Response(200, json=business)
        return httpx.Response(404)
    return make_client(handler)


class FakeTg:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text))


def new_bot(slots=SLOTS, owner=OWNER, business=BUSINESS):
    storage = Storage(":memory:")
    return Bot(FakeTg(), booksy(slots, business), storage, owner), storage


def add(storage, **kw):
    return storage.add_monitor(OWNER, 100001, "Salon", "https://booksy.com/pl-pl/100001_x", 300001, "Strzyżenie", -1, 4, **kw)


def run(bot, text, chat_id=OWNER):
    return asyncio.run(bot.handle(chat_id, text))


def commands(reply):
    return [b["callback_data"] for row in reply[1] for b in row]


def labels(reply):
    return [b["text"] for row in reply[1] for b in row]


# ---------- parsing user input ----------

@pytest.mark.parametrize("text,expected", [
    ("5.12", (date(2026, 12, 5), date(2026, 12, 5))),
    ("1.12-15.12", (date(2026, 12, 1), date(2026, 12, 15))),
    ("01.12 - 15.12.2026", (date(2026, 12, 1), date(2026, 12, 15))),
    ("20.11-30.11", (date(2026, 11, 20), date(2026, 11, 30))),   # started already, still running
    ("10.1", (date(2027, 1, 10), date(2027, 1, 10))),             # passed this year -> next year
    ("20.12-5.1", (date(2026, 12, 20), date(2027, 1, 5))),        # across New Year
])
def test_parse_dates(text, expected):
    assert parse_dates(text, NOW) == expected


@pytest.mark.parametrize("text,error", [
    ("jutro", "podaj datę"), ("31.02", "nie ma takiej daty"), ("15.12.2026-1.12.2026", "przed początkową"), ("15.12-1.12", "najwyżej"),
    ("1.11.2026-5.11.2026", "minął"), ("1.12-15.3", "najwyżej"),
])
def test_parse_dates_rejects(text, error):
    with pytest.raises(ValueError, match=error):
        parse_dates(text, NOW)


@pytest.mark.parametrize("text,expected", [("16-20", (960, 1200)), ("16:30-19:00", (990, 1140)), ("7.15 - 9", (435, 540)),
                                           ("0-24", (0, 1440))])
def test_parse_times(text, expected):
    assert parse_times(text) == expected


@pytest.mark.parametrize("text", ["20-16", "16", "16:75-18", "10-25"])
def test_parse_times_rejects(text):
    with pytest.raises(ValueError):
        parse_times(text)


def test_query_survives_button_round_trip():
    q = Query(278383, 21482769, 880923, 14, date(2026, 12, 1), date(2026, 12, 15), 990, 1140)
    assert Query.parse(q.encode().split()) == q
    assert len(f"/watch {q.encode()}".encode()) <= 64        # Telegram callback_data limit
    assert q.scope() == "01.12–15.12, 16:30–19:00"


def test_fold_ignores_case_and_polish_letters():
    assert fold("meskie") in fold("Strzyżenie MĘSKIE") and fold("zl") in fold("Złota")


# ---------- storage ----------

def test_update_slots_reports_only_new_and_reappeared():
    s = Storage(":memory:")
    m = add(s)
    assert s.update_slots(m.id, {"a", "b"}) == (["a", "b"], True)
    assert s.update_slots(m.id, {"a", "b"}) == ([], False)
    assert s.update_slots(m.id, {"a"}) == ([], False)                    # b taken by someone
    assert s.update_slots(m.id, {"a", "b", "c"}) == (["b", "c"], False)  # b freed again (cancellation) + c new


def test_deactivate_only_own_monitor():
    s = Storage(":memory:")
    m = add(s)
    assert s.deactivate(999, m.id) is False and s.deactivate(OWNER, m.id) is True
    assert s.active_monitors() == []


def test_database_from_previous_versions_is_migrated(tmp_path):
    db = tmp_path / "old.sqlite3"
    con = sqlite3.connect(db)
    con.execute("create table monitor(id integer primary key, chat_id integer not null, business_id integer not null,"
                " business_name text not null, business_url text not null, variant_id integer not null,"
                " service_name text not null, staffer_id integer not null default -1, days integer not null default 14,"
                " active integer not null default 1, checked_at text, created_at text not null default current_timestamp,"
                " hour_from integer not null default 0, hour_to integer not null default 24)")
    con.execute("insert into monitor(chat_id, business_id, business_name, business_url, variant_id, service_name, hour_from)"
                " values (1, 2, 'S', 'u', 3, 'x', 17)")
    con.commit()
    con.close()
    m = Storage(str(db)).active_monitors()[0]
    assert (m.date_from, m.time_from, m.time_to) == (None, 17 * 60, 24 * 60)
    Storage(str(db))  # second start: nothing left to migrate


# ---------- monitoring ----------

def test_check_monitor_first_then_silent_then_new():
    bot, storage = new_bot()
    m = add(storage)
    assert "Powiadomię o nowych terminach" in asyncio.run(check_monitor(bot.client, storage, m, NOW))
    assert asyncio.run(check_monitor(bot.client, storage, m, NOW)) is None

    more = json.loads(json.dumps(SLOTS))
    more["time_slots"][0]["slots"].append({"t": "19:30", "p": ""})
    msg, buttons = asyncio.run(check_monitor(booksy(more), storage, m, NOW))
    assert [b["callback_data"] for row in buttons for b in row] == ["/book 100001 300001 -1 202611271930"]
    assert "Nowe terminy" in msg and "19:30" in msg and "14:00" not in msg and "booksy.com/pl-pl/100001_x" in msg


def test_monitor_ignores_slots_outside_time_range():
    bot, storage = new_bot()
    m = add(storage, time_from=17 * 60, time_to=24 * 60)
    asyncio.run(check_monitor(bot.client, storage, m, NOW))
    more = json.loads(json.dumps(SLOTS))
    more["time_slots"][1]["slots"].append({"t": "11:00", "p": ""})   # new, but in the morning
    assert asyncio.run(check_monitor(booksy(more), storage, m, NOW)) is None


def test_expired_date_range_ends_monitor():
    bot, storage = new_bot()
    add(storage, date_from="2026-11-01", date_to="2026-11-20")
    asyncio.run(bot.check_all())
    assert storage.active_monitors() == [] and "Zakres dat minął" in bot.tg.sent[0][1]


def test_check_all_alerts_once_on_expired_token():
    storage = Storage(":memory:")
    add(storage)
    tg = FakeTg()
    bot = Bot(tg, make_client(lambda req: httpx.Response(401)), storage, OWNER)
    asyncio.run(bot.check_all())
    asyncio.run(bot.check_all())
    assert len(tg.sent) == 1 and "Token Booksy wygasł" in tg.sent[0][1]


def test_expired_token_alert_says_how_long_it_lived():
    storage = Storage(":memory:")
    add(storage)
    storage.set_token(OWNER, "t", "fp")
    storage.db.execute("update user set token_set_at = datetime('now', '-3 days')")
    tg = FakeTg()
    asyncio.run(Bot(tg, make_client(lambda req: httpx.Response(401)), storage, OWNER).check_all())
    assert "działał ok. 3 dni" in tg.sent[0][1]


def test_pre_window_check_reminds_only_autobook_people_with_expired_token():
    storage = Storage(":memory:")
    add(storage, autobook=True)
    add(storage, autobook=True)                                  # same person: one request, one reminder
    tg = FakeTg()
    calls = []
    bot = Bot(tg, make_client(lambda req: calls.append(req) or httpx.Response(401)), storage, OWNER)
    asyncio.run(bot.check_all())                                 # told once already
    asyncio.run(bot.pre_window_check())
    assert len(tg.sent) == 2 and "Za niecałą godzinę" in tg.sent[1][1]
    bot, _ = new_bot()
    add(bot.storage, autobook=True)
    asyncio.run(bot.pre_window_check())
    assert bot.tg.sent == []                                     # token works: stay quiet


# ---------- conversation ----------

def test_unknown_owner_gets_chat_id_and_strangers_are_ignored():
    bot, _ = new_bot(owner=None)
    assert "TELEGRAM_CHAT_ID=555" in run(bot, "/start", 555)
    bot, _ = new_bot()
    assert run(bot, "/list", 555) is None
    assert run(bot, "/watch 100001 300001", 555) is None   # buttons are commands too: same owner check


def test_help():
    bot, _ = new_bot()
    assert "Jak szukać" in run(bot, "/help") and "/list" in run(bot, "/help")
    assert "Jak szukać" in run(bot, "hej")                    # text with nothing pending


def test_full_flow_with_typed_dates_and_times():
    """Like luxmed-bot: salon -> service (searched by phrase) -> dates -> times -> results now -> watch."""
    bot, storage = new_bot()
    services = run(bot, "Korzystam z usług salonu – booksy: https://booksy.com/pl-pl/dl/show-business/100001")
    assert "<b>1.</b> Strzyżenie Męskie – 60\xa0zł · 30 min" in services[0]
    assert labels(services) == ["1", "2", "3"] and len(services[1]) == 1     # one row of number buttons
    assert commands(services)[0] == "/pick 100001 300001"

    found = run(bot, "MESKIE")                                 # search by part of the name
    assert labels(found) == ["1", "2"] and "Dziecięce" not in found[0] and "Maszynka" in found[0]
    assert "Nie znalazłem" in run(bot, "koloryzacja")
    assert "wybierz 1–2" in run(bot, "7")                      # typed number outside the last list

    assert "Wpisz datę" in run(bot, commands(found)[0])        # single staffer -> straight to typing dates
    assert "Błąd" in run(bot, "jutro")                         # bad input: question stays open
    times = run(bot, "27.11-28.11")
    assert labels(times)[-1] == "🕐 Wpisz godziny"

    run(bot, commands(times)[-1])
    result = run(bot, "17-19")
    assert "(27.11–28.11, 17:00–19:00)" in result[0]
    assert "Wolne terminy (6)" in result[0] and "17:00" in result[0] and "14:00" not in result[0]
    assert storage.active_monitors() == []                     # searching does not watch

    assert "Powiadomię" in run(bot, commands(result)[-1])
    m = storage.active_monitors()[0]
    assert (m.date_from, m.date_to, m.time_from, m.time_to) == ("2026-11-27", "2026-11-28", 1020, 1140)


def test_typed_service_number_works_like_the_button():
    bot, _ = new_bot()
    run(bot, "https://booksy.com/pl-pl/dl/show-business/100001")
    assert run(bot, "1") == run(bot, "/pick 100001 300001")


def test_preset_time_buttons():
    bot, _ = new_bot()
    run(bot, "/pick 100001 300001")
    times = run(bot, "27.11-28.11")
    assert labels(times)[:4] == ["Dowolna pora", "Rano (do 12)", "Popołudnie (12–17)", "Wieczór (od 17)"]
    result = run(bot, commands(times)[3])                      # evening
    assert "(27.11–28.11, 17:00–24:00)" in result[0] and "Wolne terminy (6)" in result[0]


def test_command_cancels_pending_question():
    bot, _ = new_bot()
    run(bot, "/days 100001 300001 -1")
    run(bot, "/list")
    assert "Jak szukać" in run(bot, "5.12")                    # no longer read as a date


def test_pick_several_staffers_asks_who():
    fix = json.loads(json.dumps(BUSINESS))
    fix["business"]["staff"].append({"id": 777, "name": "Kacper"})
    fix["business"]["service_categories"][0]["services"][0]["variants"][0]["staffer_id"] = [200001, 777]
    bot, _ = new_bot(business=fix)
    reply = run(bot, "/pick 100001 300001")
    assert labels(reply) == ["Dowolny pracownik", "Pracownik 1", "Kacper"]
    assert commands(reply)[2] == "/days 100001 300001 777"
    assert "Strzyżenie Męskie – Kacper" in run(bot, "/find 100001 300001 777 7 0 1440")[0]


def test_find_without_results():
    bot, _ = new_bot(slots={"time_slots": []})
    found = run(bot, "/find 100001 300001 -1 7 0 1440")
    assert "Brak wolnych terminów" in found[0] and labels(found) == ["🔔 Powiadamiaj o nowych terminach",
                                                                   "🤖 Rezerwuj automatycznie, gdy się pojawi"]


def test_list_has_stop_buttons():
    bot, storage = new_bot()
    run(bot, "/watch 100001 300001")
    reply = run(bot, "/list")
    assert "#1" in reply[0] and commands(reply) == ["/stop 1"]
    assert run(bot, "/stop 1") == "✅ Zakończono." and storage.active_monitors() == []
    assert "Brak aktywnych powiadomień" in run(bot, "/list")


@pytest.mark.parametrize("text,expected", [
    ("/watch 100001 999", "nie ma takiej usługi"),
    ("/watch 100001 300001 1", "ten pracownik nie wykonuje"),
    ("/watch 100001 300001 -1 365", "liczba dni"),
    ("/watch 100001 300001 -1 7 1200 900", "zły zakres godzin"),
    ("/watch 100001 300001 -1 20261101-20261120 0 1440", "minął"),
    ("/watch salon 1", "Błąd"),
])
def test_watch_validates(text, expected):
    bot, storage = new_bot()
    assert expected in run(bot, text) and storage.active_monitors() == []


# ---------- telegram ----------

def test_telegram_errors_never_contain_token():
    secret = "123456:SECRET_BOT_TOKEN"

    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    tg = Telegram(secret, transport=httpx.MockTransport(handler))
    with pytest.raises(TelegramError) as e:
        asyncio.run(tg.send(1, "x"))
    assert secret not in str(e.value) and e.value.__cause__ is None
