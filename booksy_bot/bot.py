"""Telegram bot: search Booksy services, notify about newly appeared free slots, book a slot after confirmation.

  python -m booksy_bot.bot

Needs in .env: BOOKSY_* (see .env.example), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (the bot tells you yours on /start).
"""
import asyncio
import contextvars
import html
import logging
import os
import random
import re
import unicodedata
from argparse import ArgumentTypeError
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from booksy_bot.api.client import ANY_STAFFER, BooksyClient
from booksy_bot.api.http import ApiChanged, ApiError, SessionExpired
from booksy_bot.api.models import Business, Service, Variant
from booksy_bot import booking
from booksy_bot.booking import BookArgs
from booksy_bot.cli import business_id_arg, fresh_token, load_env, make_client
from booksy_bot.storage import DAY_END, Monitor, Storage
from booksy_bot.telegram import Telegram, TelegramError
from booksy_bot.timezones import SERVICE_TZ

log = logging.getLogger("bot")
MAX_MONITORS = 10          # per chat; keeps polling volume modest (luxmed-bot used the same limit)
MAX_AUTOBOOK = 3           # unattended bookings at once; Booksy terms: no mass booking
FRIEND_MAX_MONITORS = 3    # friends share the owner's IP: keep their traffic small
FRIEND_MAX_AUTOBOOK = 1
MAX_DAYS = 92              # salons open bookings up to ~2 months ahead
LIST_LIMIT = 30            # service buttons per message; more -> type part of the name

HELP = """<b>Booksy bot</b> – szuka wolnych terminów, powiadamia o nowych i rezerwuje po Twoim potwierdzeniu.

<b>Jak szukać</b>
1. Wyślij link do salonu albo w aplikacji Booksy: salon → Udostępnij → ten bot.
2. Wybierz usługę z listy albo wpisz część nazwy (np. <i>broda</i>, <i>meskie</i>).
3. Wybierz pracownika (gdy jest kilku).
4. Wpisz datę albo zakres dat, np. <code>5.12</code> lub <code>1.12-15.12</code>.
5. Godziny: przycisk albo „🕐 Wpisz godziny”, np. <code>16-20</code> lub <code>16:30-19:00</code>.
6. Dostajesz wolne terminy od razu. Przycisk „🔔 Powiadamiaj” włącza sprawdzanie co kilka minut –
   napiszę tylko o <b>nowych</b> terminach (np. gdy ktoś odwoła wizytę).
7. „📅 Rezerwuj” pod terminem: bot sprawdza, czy termin jest wolny i ile kosztuje, i pyta o potwierdzenie.
   Terminów z przedpłatą/kaucją bot nie rezerwuje.
8. Gdy nie ma terminów: „🤖 Rezerwuj automatycznie” – bot sam zarezerwuje <b>pierwszy</b> wolny termin,
   który się pojawi, i napisze, co zarezerwował. Potem kończy. Wyłączysz w /list.

<b>Komendy</b>
/list – aktywne powiadomienia (z przyciskiem „Zakończ”)
/help – ta pomoc

Powiadomienia z konkretnymi datami kończą się same po ostatnim dniu. Limit: 10 powiadomień, 92 dni.
Gdy wygaśnie logowanie do Booksy, dostaniesz jedną wiadomość – wpisz nowy token do .env."""

# Run on booksy.com (logged in, phone or PC): catches the X-Access-Token the page itself sends with its next API call.
# Booksy's pages navigate without reloading (checked 2026-11-26), so the hook survives a tap on a category or salon.
BOOKMARKLET = ("javascript:(()=>{const g=t=>{const m='/token '+t;try{navigator.clipboard.writeText(m)}catch(e){}"
               "prompt('Skopiuj i wyślij botowi (zaraz otworzę Telegram):',m);location.href='https://t.me/BOT'};"
               "const f=window.fetch;window.fetch=function(i,o){try{const t=new Headers((o&&o.headers)||"
               "(i instanceof Request?i.headers:void 0)).get('x-access-token');if(t){window.fetch=f;g(t)}}catch(e){}"
               "return f.apply(this,arguments)};alert('OK. Teraz kliknij dowolną kategorię albo salon.')})()")

LOGIN_HELP = """<b>Logowanie z telefonu (raz na jakiś czas)</b>
Bot nie loguje się sam (Booksy wymaga CAPTCHA), więc przekazujesz mu token ze swojej zalogowanej przeglądarki.

<b>Jednorazowo – dodaj zakładkę:</b>
1. Stuknij kod poniżej – skopiuje się.
2. Chrome (Android): ⋮ → ☆ (dodaj zakładkę) → ✏️ edytuj → nazwa <b>Booksy bot</b>, w polu adresu wklej kod → zapisz.
   Safari (iPhone): dodaj dowolną stronę do zakładek → edytuj zakładkę → nazwa <b>Booksy bot</b>, adres zastąp kodem.

<b>Za każdym razem:</b>
1. Otwórz <b>booksy.com</b> w tej przeglądarce i zaloguj się (jeśli trzeba).
2. Wpisz w pasek adresu <b>Booksy bot</b> i stuknij zakładkę z listy (nie wyszukiwanie).
3. Stuknij dowolną kategorię albo salon – pojawi się okienko z <code>/token …</code>, tekst jest już w schowku.
4. Otworzy się Telegram – wklej i wyślij. Wiadomość z tokenem od razu usuwam.

<code>{code}</code>"""

FRIEND_HELP = """

<b>Twoje konto Booksy</b>
Bot korzysta z <b>Twojego</b> konta (regulamin Booksy: jedno konto na osobę). Raz na jakiś czas podłącz je:
/login – instrukcja dla telefonu. Wiadomość z tokenem od razu usuwam. Token daje dostęp do Twojego konta Booksy –
wysyłaj go tylko tu.
Limit: 3 powiadomienia, 1 autorezerwacja. Do tego samego terminu pierwszeństwo ma właściciel bota."""

OWNER_HELP = """

<b>Znajomi</b>
/invite – jednorazowy kod zaproszenia, /users – lista, /kick ID – usuń (z tokenem i powiadomieniami).
Nowy token Booksy z telefonu: /login."""

BOT_COMMANDS = [{"command": "list", "description": "Aktywne powiadomienia"},
                {"command": "login", "description": "Podłącz konto Booksy z telefonu"},
                {"command": "help", "description": "Jak korzystać z bota"}]

TIME_CHOICES = [("Dowolna pora", 0, DAY_END), ("Rano (do 12)", 0, 12 * 60),
                ("Popołudnie (12–17)", 12 * 60, 17 * 60), ("Wieczór (od 17)", 17 * 60, DAY_END)]

Buttons = list[list[dict]]
Reply = str | tuple[str, Buttons]


def today() -> date:
    return datetime.now(SERVICE_TZ).date()


def button(label: str, command: str) -> list[dict]:
    """One-button row; pressing it sends `command` back through Bot.handle (callback_data limit: 64 bytes)."""
    return [{"text": label[:60], "callback_data": command}]


def grid(items: list[tuple[str, str]], per_row: int = 5) -> Buttons:
    """Short buttons (label, command) several per row, e.g. service numbers."""
    return [[{"text": label, "callback_data": cmd} for label, cmd in items[i:i + per_row]]
            for i in range(0, len(items), per_row)]


def fmt_slot(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a %d.%m %H:%M")


def fmt_slots(slots: list[str], limit: int = 10) -> str:
    lines = [fmt_slot(s) for s in slots[:limit]]
    if len(slots) > limit:
        lines.append(f"… i {len(slots) - limit} więcej")
    return "\n".join(lines)


def fmt_minutes(m: int) -> str:
    return f"{m // 60}:{m % 60:02d}"


def fold(text: str) -> str:
    """Case- and accent-insensitive form for searching ("Męskie" ~ "meskie")."""
    text = unicodedata.normalize("NFKD", text.casefold().replace("ł", "l"))
    return "".join(c for c in text if not unicodedata.combining(c))


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


async def find_slots(client: BooksyClient, q: Query, now: date | None = None) -> list[str]:
    """Free slot starts (ISO, Europe/Warsaw) inside the query's date window and time range."""
    start, end = q.window(now or today())
    if start > end:
        return []
    r = await client.time_slots(q.business_id, q.variant_id, start, end, q.staffer_id)
    return sorted(dt.isoformat() for d in r.days if start <= d.day <= end for dt in d.starts()
                  if q.time_from <= dt.hour * 60 + dt.minute <= q.time_to)


def describe(m: Monitor) -> str:
    who = "dowolny pracownik, " if m.staffer_id == ANY_STAFFER else ""   # a chosen staffer is part of service_name
    return f"#{m.id} {'🤖 ' if m.autobook else ''}<b>{html.escape(m.service_name)}</b> – {html.escape(m.business_name)} ({who}{Query.of(m).scope()})"


def book_buttons(business_id: int, variant_id: int, staffer_id: int, slots: list[str], limit: int = 5) -> Buttons:
    return [button(f"📅 Rezerwuj {fmt_slot(s)}", f"/book {BookArgs.of(business_id, variant_id, staffer_id, s).encode()}")
            for s in slots[:limit]]


def salon_link(url: str) -> str:
    return f'\n<a href="{html.escape(url)}">Otwórz salon na Booksy</a>'


async def check_monitor(client: BooksyClient, storage: Storage, m: Monitor, now: date | None = None) -> Reply | None:
    """Fetch slots, remember them, and return a message only when something new appeared (or on the first check)."""
    current = set(await find_slots(client, Query.of(m), now))
    new, first = storage.update_slots(m.id, current)
    if m.autobook and current:
        return await autobook(client, storage, m, sorted(current))
    if first and m.autobook:
        return f"🤖 Zarezerwuję automatycznie pierwszy wolny termin: {describe(m)}"
    if first:
        return f"🔔 Powiadomię o nowych terminach: {describe(m)}"
    if new:
        return (f"🆕 Nowe terminy – {describe(m)}\n{fmt_slots(new)}{salon_link(m.business_url)}",
                book_buttons(m.business_id, m.variant_id, m.staffer_id, new))
    return None


async def autobook(client: BooksyClient, storage: Storage, m: Monitor, slots: list[str]) -> Reply | None:
    """Book the earliest free slot in a month without a bot booking in this salon yet (1 visit a month).
    booking.book re-checks the slot, refuses payments and never books the same slot twice.
    Booked -> ends once every month of the date range has a visit. Slot gone meanwhile -> try again
    next check. Anything else -> back to notifications only."""
    taken = storage.booked_months(m.business_id, m.chat_id)
    slots = [s for s in slots if s[:7] not in taken
             and (storage.booking_status(m.business_id, s) or ('failed',))[0] == 'failed']   # not someone else's
    if not slots:
        return None
    b = BookArgs.of(m.business_id, m.variant_id, m.staffer_id, slots[0])
    try:
        msg = await booking.book(client, storage, b, m.chat_id)
    except ValueError as e:
        log.info("monitor %s: autobook %s skipped: %s", m.id, slots[0], e)
        return None
    if storage.booking_status(b.business_id, b.start.isoformat()) == ("confirmed", m.chat_id):
        start, end = Query.of(m).window(today())
        months = {f"{start.year + (start.month - 1 + i) // 12}-{(start.month - 1 + i) % 12 + 1:02d}"
                  for i in range((end.year - start.year) * 12 + end.month - start.month + 1)}
        if months <= storage.booked_months(m.business_id, m.chat_id):
            storage.deactivate(m.chat_id, m.id)
            return f"🤖 {msg}\n{describe(m)}{salon_link(m.business_url)}"
        return (f"🤖 {msg}\n{describe(m)}{salon_link(m.business_url)}\n"
                "Pilnuję dalej pozostałych miesięcy z zakresu – 1 wizyta na miesiąc (zakończysz w /list).")
    storage.stop_autobook(m.id)
    return (f"⚠️ Autorezerwacja wyłączona, dalej tylko powiadamiam – {describe(m)}\n{msg}\n\n"
            f"Wolne terminy:\n{fmt_slots(slots)}{salon_link(m.business_url)}",
            book_buttons(m.business_id, m.variant_id, m.staffer_id, slots))


def find_variant(b: Business, variant_id: int) -> tuple[Service, Variant]:
    for s in b.bookable_services():
        for v in s.variants:
            if v.id == variant_id:
                return s, v
    raise ValueError("nie ma takiej usługi w tym salonie")


_api: contextvars.ContextVar[BooksyClient] = contextvars.ContextVar("api")   # Booksy client of the person being served


class Bot:
    def __init__(self, tg: Telegram, client: BooksyClient, storage: Storage, owner_chat_id: int | None,
                 friend_client: Callable[[int], BooksyClient] | None = None):
        """client = the owner's Booksy account (.env); friend_client(chat_id) = a client with that friend's own token."""
        self.tg, self.client, self.storage, self.owner = tg, client, storage, owner_chat_id
        self._friend_client = friend_client
        self._friend_clients: dict[int, BooksyClient] = {}
        self.username = "Booksy_booking_bot"   # replaced by getMe at start; used in the /login bookmarklet
        self._alerted: set[str] = set()    # one alert per problem until it is resolved
        # What typed text means right now, per chat: ("service", salon) / ("dates", args) / ("times", args).
        # shortcut: in memory, lost on restart (user just sends the link again); persist if that becomes annoying.
        self._waiting: dict[int, tuple[str, str]] = {}

    @property
    def api(self) -> BooksyClient:
        return _api.get(self.client)

    def client_for(self, chat_id: int) -> BooksyClient:
        if chat_id == self.owner or self._friend_client is None:
            return self.client
        if chat_id not in self._friend_clients:
            self._friend_clients[chat_id] = self._friend_client(chat_id)
        return self._friend_clients[chat_id]

    def is_friend(self, chat_id: int) -> bool:
        return chat_id != self.owner and self.storage.user(chat_id) is not None

    def token_message(self, chat_id: int) -> str:
        if chat_id == self.owner:
            return "🔑 Token Booksy wygasł. Podłącz konto ponownie: /login (albo nowy BOOKSY_ACCESS_TOKEN w .env)."
        return "🔑 Brak ważnego tokenu Booksy – podłącz konto: /login."

    # ---------- commands ----------

    async def handle(self, chat_id: int, text: str, name: str = "") -> Reply | None:
        if self.owner is None:
            log.warning("message from chat %s; set TELEGRAM_CHAT_ID=%s in .env and restart", chat_id, chat_id)
            return f"Twój chat id: <code>{chat_id}</code>\nDopisz <code>TELEGRAM_CHAT_ID={chat_id}</code> do .env i uruchom bota ponownie."
        text = text.strip()
        if chat_id != self.owner and not self.is_friend(chat_id):
            parts = text.split()
            if len(parts) == 2 and parts[0] == "/start" and self.storage.redeem_invite(parts[1], chat_id, name):
                log.info("friend %s joined", chat_id)
                await self.tg.send(self.owner, f"👋 Dołączył(a) {html.escape(name) or chat_id} (<code>{chat_id}</code>).")
                return "Witaj! Najpierw podłącz swoje konto Booksy." + FRIEND_HELP
            return None  # private bot: strangers without a valid invite are ignored silently
        token = _api.set(self.client_for(chat_id))
        try:
            return await self._dispatch(chat_id, text)
        except SessionExpired:
            return self.token_message(chat_id)
        except (ValueError, ArgumentTypeError, ApiError) as e:
            return f"Błąd: {html.escape(str(e))[:300]}"
        finally:
            _api.reset(token)

    async def _dispatch(self, chat_id: int, text: str) -> Reply:
        if not text.startswith("/"):
            link = re.search(r"\S*booksy\.com\S*", text)   # pasted link or "share" text from the Booksy app
            if link:
                return await self.cmd_services(chat_id, link.group(0))
            if chat_id in self._waiting:
                return await self.typed(chat_id, text)
            return HELP
        self._waiting.pop(chat_id, None)                   # any command or button press ends a pending question
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/help"):
            return HELP + (OWNER_HELP if chat_id == self.owner else FRIEND_HELP)
        if cmd == "/login":
            return LOGIN_HELP.replace("{code}", html.escape(BOOKMARKLET.replace("/BOT'", f"/{self.username}'"), quote=False))
        if cmd == "/token":
            if len(args) != 1 or len(args[0]) < 20:
                return "Wyślij: <code>/token WARTOŚĆ</code> (jak go zdobyć z telefonu: /login)."
            self.storage.set_token(chat_id, args[0], os.environ.get("BOOKSY_FINGERPRINT") if chat_id == self.owner else None)
            self._alerted.discard(f"token:{chat_id}")
            return "✅ Token zapisany, wiadomość z nim usunięta. Wyślij link do salonu, żeby zacząć."
        if cmd == "/invite" and chat_id == self.owner:
            code = self.storage.add_invite()
            return (f"Jednorazowe zaproszenie – niech znajomy napisze do bota:\n<code>/start {code}</code>\n"
                    "Znajomy korzysta z własnego konta Booksy, ale z Twojego IP (Wyse) – ryzyko blokady dotyczy też Ciebie.")
        if cmd == "/users" and chat_id == self.owner:
            us = self.storage.users()
            return "\n".join(f"<code>{c}</code> {html.escape(n)} – {'token ✅' if t else 'bez tokenu'}"
                             for c, n, t in us if c != self.owner) or "Brak znajomych."
        if cmd == "/kick" and chat_id == self.owner and len(args) == 1 and args[0].isdigit() and int(args[0]) != self.owner:
            self._friend_clients.pop(int(args[0]), None)
            return "Usunięto." if self.storage.remove_user(int(args[0])) else "Nie ma takiego użytkownika."
        if cmd == "/services" and len(args) == 1:
            return await self.cmd_services(chat_id, args[0])
        if cmd == "/pick" and len(args) == 2:
            return await self.cmd_pick(chat_id, args)
        if cmd == "/days" and len(args) == 3:
            return self.ask_days(chat_id, " ".join(args))
        if cmd == "/hours" and len(args) == 4:
            return self.ask_times(" ".join(args))
        if cmd == "/asktimes" and len(args) == 4:
            self._waiting[chat_id] = ("times", " ".join(args))
            return "Wpisz zakres godzin, np. <code>16-20</code> albo <code>16:30-19:00</code>"
        if cmd == "/find" and len(args) == 6:
            return await self.cmd_find(Query.parse(args))
        if cmd == "/watch" and len(args) in (2, 3, 4, 6):
            return await self.cmd_watch(chat_id, Query.parse(args))
        if cmd == "/auto" and len(args) == 6:
            return await self.cmd_auto(Query.parse(args))
        if cmd == "/autoon" and len(args) == 6:
            return await self.cmd_watch(chat_id, Query.parse(args), autobook=True)
        if cmd == "/book" and len(args) == 4:
            return await self.cmd_book(BookArgs.parse(args))
        if cmd == "/confirm" and len(args) == 4:
            return await booking.book(self.api, self.storage, BookArgs.parse(args), chat_id)
        if cmd == "/list":
            ms = self.storage.chat_monitors(chat_id)
            if not ms:
                return "Brak aktywnych powiadomień. Wyślij link do salonu, żeby zacząć."
            return "\n".join(describe(m) for m in ms), [button(f"❌ Zakończ #{m.id} {m.service_name}", f"/stop {m.id}") for m in ms]
        if cmd == "/stop" and len(args) == 1 and args[0].lstrip("#").isdigit():
            ok = self.storage.deactivate(chat_id, int(args[0].lstrip("#")))
            return "Zakończono." if ok else "Nie ma takiego aktywnego powiadomienia."
        return "Nie rozumiem tej komendy.\n\n" + HELP

    async def typed(self, chat_id: int, text: str) -> Reply:
        """Free text answering the last question; on a parse error the question stays open (just type again)."""
        kind, ctx = self._waiting[chat_id]
        if kind == "service":
            return await self.cmd_services(chat_id, ctx, phrase=text)
        if kind == "dates":
            start, end = parse_dates(text, today())
            del self._waiting[chat_id]
            return self.ask_times(f"{ctx} {start:%Y%m%d}-{end:%Y%m%d}")
        start, end = parse_times(text)
        del self._waiting[chat_id]
        return await self.cmd_find(Query.parse(f"{ctx} {start} {end}".split()))

    async def cmd_services(self, chat_id: int, salon: str, phrase: str | None = None) -> Reply:
        """Numbered list in the message (readable on a phone), short number buttons underneath."""
        b = (await self.api.business(business_id_arg(salon))).business
        self._waiting[chat_id] = ("service", str(b.id))   # typed text now searches this salon's services
        found = [(c.name, s, v) for c in b.service_categories for s in c.services
                 if s.active and s.is_available_for_customer_booking for v in s.variants]
        if not found:
            return f"<b>{html.escape(b.name)}</b>: brak usług do rezerwacji online."
        if phrase:
            found = [x for x in found if fold(phrase) in fold(x[1].name)]
            if not found:
                return f"Nie znalazłem „{html.escape(phrase)}” w {html.escape(b.name)}. Wpisz inną część nazwy."
        lines = [f"💈 <b>{html.escape(b.name)}</b>", "🔎 Pasujące usługi:" if phrase else "Wybierz numer usługi:"]
        category = None
        for n, (cat, s, v) in enumerate(found[:LIST_LIMIT], 1):
            if cat and cat != category:
                lines.append(f"\n<b>{html.escape(cat)}</b>")
                category = cat
            price = (v.service_price or "?").replace(",00", "")
            lines.append(f"<b>{n}.</b> {html.escape(s.name)} – {html.escape(price)} · {v.duration or '?'} min"
                         + (" ⚠" if v.needs_payment else ""))
        lines.append("")
        if len(found) > LIST_LIMIT:
            lines.append(f"Pokazuję {LIST_LIMIT} z {len(found)}.")
        if any(v.needs_payment for _, _, v in found):
            lines.append("⚠ = przedpłata/kaucja")
        lines.append("🔎 Albo wpisz część nazwy usługi.")
        picks = [(str(n), f"/pick {b.id} {v.id}") for n, (_, _, v) in enumerate(found[:LIST_LIMIT], 1)]
        return "\n".join(lines), grid(picks)

    async def cmd_pick(self, chat_id: int, args: list[str]) -> Reply:
        """Service chosen: ask for a staffer when several do it, otherwise go straight to the date range."""
        b = (await self.api.business(int(args[0]))).business
        service, variant = find_variant(b, int(args[1]))
        if len(variant.staffer_ids) <= 1:
            return self.ask_days(chat_id, f"{b.id} {variant.id} {ANY_STAFFER}")
        staff = {s.id: s.name for s in b.staff}
        rows = [button("Dowolny pracownik", f"/days {b.id} {variant.id} {ANY_STAFFER}")]
        rows += [button(staff.get(i, str(i)), f"/days {b.id} {variant.id} {i}") for i in variant.staffer_ids]
        return f"<b>{html.escape(service.name)}</b> – u kogo?", rows

    def ask_days(self, chat_id: int, prefix: str) -> Reply:
        self._waiting[chat_id] = ("dates", prefix)
        return "Kiedy? Wpisz datę albo zakres dat, np. <code>5.12</code> albo <code>1.12-15.12</code>"

    def ask_times(self, prefix: str) -> Reply:
        rows = [button(label, f"/find {prefix} {a} {b}") for label, a, b in TIME_CHOICES]
        return "O jakiej porze?", rows + [button("🕐 Wpisz godziny", f"/asktimes {prefix}")]

    async def lookup(self, q: Query) -> tuple[Business, str]:
        """Validate the query against the salon and return it with a readable service name (incl. staffer)."""
        b = (await self.api.business(q.business_id)).business
        service, variant = find_variant(b, q.variant_id)
        name = service.name
        if q.staffer_id != ANY_STAFFER:
            if q.staffer_id not in variant.staffer_ids:
                raise ValueError("ten pracownik nie wykonuje tej usługi")
            name += f" – {next((s.name for s in b.staff if s.id == q.staffer_id), q.staffer_id)}"
        return b, name

    async def cmd_find(self, q: Query) -> Reply:
        """Search now (like luxmed-bot), then offer to keep watching the same query."""
        b, name = await self.lookup(q)
        slots = await find_slots(self.api, q)
        head = f"<b>{html.escape(name)}</b> – {html.escape(b.name)} ({q.scope()})\n"
        body = f"Wolne terminy ({len(slots)}):\n{fmt_slots(slots)}{salon_link(b.page_url)}" if slots else "Brak wolnych terminów."
        watch = [button("🔔 Powiadamiaj o nowych terminach", f"/watch {q.encode()}")]
        if not slots:
            return head + body, watch + [button("🤖 Rezerwuj automatycznie, gdy się pojawi", f"/auto {q.encode()}")]
        return head + body, book_buttons(q.business_id, q.variant_id, q.staffer_id, slots) + watch

    async def cmd_auto(self, q: Query) -> Reply:
        """Explain what autobook will do and ask once more: it books a real appointment without asking again."""
        b, name = await self.lookup(q)
        variant = find_variant(b, q.variant_id)[1]
        if variant.needs_payment:
            raise ValueError("ta usługa wymaga przedpłaty/kaucji – autorezerwacja niemożliwa, wybierz „Powiadamiaj”")
        rule = ("<b>1 wizyta na każdy miesiąc z zakresu</b>, potem kończę. Wizyt zarezerwowanych ręcznie nie widzę – "
                "jeśli masz już wizytę w którymś miesiącu, zacznij zakres od kolejnego.")
        return (f"🤖 <b>{html.escape(name)}</b> – {html.escape(b.name)} ({q.scope()})\n"
                f"Gdy pojawi się wolny termin, zarezerwuję <b>pierwszy</b> bez pytania "
                f"({html.escape(variant.service_price or '?')}, płatność na miejscu) i dam znać. {rule}\n"
                "Odwołać wizytę możesz w aplikacji Booksy.",
                [button("✅ Włącz autorezerwację", f"/autoon {q.encode()}")])

    async def cmd_book(self, b: BookArgs) -> Reply:
        """First step: dry_run shows whether the slot is still free and what it costs; nothing is booked yet."""
        r = await booking.check(self.api, b)
        when = f"{b.start:%a %d.%m %H:%M}"
        if r.needs_payment:
            return f"💳 {when} wymaga przedpłaty albo kaucji – bot nie płaci. Zarezerwuj ręcznie w Booksy."
        price = f", {html.escape(r.appointment.total)}" if r.appointment.total else ""
        return (f"Rezerwować <b>{when}</b>{price}?\n"
                "Płatność na miejscu, nic do zapłaty teraz.", [button("✅ Tak, rezerwuj", f"/confirm {b.encode()}")])

    async def cmd_watch(self, chat_id: int, q: Query, autobook: bool = False) -> Reply:
        if q.expired(today()):
            raise ValueError("ten zakres dat już minął")
        active = self.storage.chat_monitors(chat_id)
        friend = chat_id != self.owner
        max_monitors, max_auto = (FRIEND_MAX_MONITORS, FRIEND_MAX_AUTOBOOK) if friend else (MAX_MONITORS, MAX_AUTOBOOK)
        if len(active) >= max_monitors:
            raise ValueError(f"limit {max_monitors} powiadomień – zakończ któreś w /list")
        if autobook and sum(m.autobook for m in active) >= max_auto:
            raise ValueError(f"limit {max_auto} autorezerwacji naraz – zakończ którąś w /list")
        b, name = await self.lookup(q)
        if autobook and find_variant(b, q.variant_id)[1].needs_payment:
            raise ValueError("ta usługa wymaga przedpłaty/kaucji – autorezerwacja niemożliwa")
        m = self.storage.add_monitor(chat_id, b.id, b.name, b.page_url, q.variant_id, name, q.staffer_id, q.days,
                                     q.date_from and q.date_from.isoformat(), q.date_to and q.date_to.isoformat(),
                                     q.time_from, q.time_to, autobook)
        return await check_monitor(self.api, self.storage, m) or f"🔔 Powiadomię o nowych terminach: {describe(m)}"

    # ---------- loops ----------

    async def alert(self, key: str, chat_id: int, text: str) -> None:
        if key not in self._alerted:
            self._alerted.add(key)
            await self.tg.send(chat_id, text)

    async def check_all(self, fast: bool = False) -> None:
        """fast = inside the booking-opening window: only autobook monitors, no pauses between them."""
        expired: set[int] = set()
        # the owner first: in the same cycle their monitors book before friends' (priority on the same barber)
        for m in sorted(self.storage.active_monitors(), key=lambda m: m.chat_id != self.owner):
            if (fast and not m.autobook) or m.chat_id in expired:
                continue
            if Query.of(m).expired(today()):
                self.storage.deactivate(m.chat_id, m.id)
                await self.tg.send(m.chat_id, f"⌛ Zakres dat minął – kończę {describe(m)}")
                continue
            try:
                msg = await check_monitor(self.client_for(m.chat_id), self.storage, m)
            except SessionExpired:
                expired.add(m.chat_id)                        # one person's token: the others go on
                await self.alert(f"token:{m.chat_id}", m.chat_id, "Powiadomienia wstrzymane. " + self.token_message(m.chat_id))
                continue
            except ApiChanged as e:
                log.error("monitor %s: %s", m.id, e)
                await self.alert(f"changed:{m.id}", m.chat_id, f"⚠️ Booksy zmienił format odpowiedzi – powiadomienie #{m.id} może nie działać. Szczegóły w logu.")
                continue
            except ApiError as e:
                log.warning("monitor %s: %s", m.id, e)  # outage / limit: try next cycle
                continue
            self._alerted.discard(f"token:{m.chat_id}")
            self._alerted.discard(f"changed:{m.id}")
            if msg:
                text, buttons = msg if isinstance(msg, tuple) else (msg, None)
                await self.tg.send(m.chat_id, text, buttons)
            if not fast:
                await asyncio.sleep(random.uniform(2, 5))  # spread requests

    async def monitor_loop(self, min_s: float, max_s: float, window: "FastWindow | None" = None) -> None:
        while True:
            now = datetime.now(SERVICE_TZ)
            fast = bool(window and window.active(now))
            try:
                await self.check_all(fast)
            except TelegramError as e:
                log.warning("telegram: %s", e)
            await asyncio.sleep(next_delay(datetime.now(SERVICE_TZ), min_s, max_s, window))

    async def updates_loop(self) -> None:
        offset = None
        while True:
            try:
                updates = await self.tg.updates(offset)
            except TelegramError as e:
                log.warning("telegram: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if cb := u.get("callback_query"):          # inline button pressed
                    msg, text = cb.get("message") or {}, cb.get("data")
                    try:
                        await self.tg.call("answerCallbackQuery", callback_query_id=cb["id"])  # stop the spinner
                    except TelegramError as e:
                        log.warning("telegram: %s", e)
                else:
                    msg = u.get("message") or {}
                    text = msg.get("text")
                chat_id = (msg.get("chat") or {}).get("id")
                if not text or chat_id is None:
                    continue
                chat = msg.get("chat") or {}
                reply = await self.handle(chat_id, text, chat.get("first_name") or chat.get("username") or "")
                if text.startswith("/token") and msg.get("message_id") and not cb:
                    try:   # the token gives access to their Booksy account: do not leave it in the chat history
                        await self.tg.call("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
                    except TelegramError as e:
                        log.warning("telegram: %s", e)
                if reply:
                    text, buttons = reply if isinstance(reply, tuple) else (reply, None)
                    try:
                        await self.tg.send(chat_id, text, buttons)
                    except TelegramError as e:
                        log.warning("telegram: %s", e)


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


def next_delay(now: datetime, min_s: float, max_s: float, window: FastWindow | None) -> float:
    if window and window.active(now):
        return window.seconds * random.uniform(0.7, 1.3)        # jitter: not a fixed machine rhythm
    delay = random.uniform(min_s, max_s)
    to_start = window and window.seconds_to_start(now)
    return min(delay, to_start) if to_start is not None else delay   # wake up exactly when the window opens


async def run() -> None:
    load_env()
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("missing TELEGRAM_BOT_TOKEN in .env")
    owner = os.environ.get("TELEGRAM_CHAT_ID")
    storage = Storage(os.environ.get("BOT_DB", "booksy_bot.sqlite3"))
    owner_id = int(owner) if owner else None
    # the owner's token: the latest /token from the phone, else .env (re-read on every request)
    client, http = make_client(token_provider=lambda: (owner_id and (storage.user(owner_id) or ("", None))[1]) or fresh_token())
    tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    def friend_client(chat_id: int) -> BooksyClient:
        # shortcut: friends' HTTP clients are never closed; fine for a few long-lived friends.
        fingerprint = (storage.user(chat_id) or ("", None, None))[2]
        return make_client(token_provider=lambda: (storage.user(chat_id) or ("", None))[1], fingerprint=fingerprint)[0]

    bot = Bot(tg, client, storage, owner_id, friend_client)
    try:
        bot.username = (await tg.call("getMe"))["username"]
    except (TelegramError, KeyError) as e:
        log.warning("telegram getMe: %s", e)
    lo, hi = interval()
    window = FastWindow.parse(os.environ["FAST_WINDOW"]) if os.environ.get("FAST_WINDOW") else None
    try:
        await tg.call("setMyCommands", commands=BOT_COMMANDS)   # the "/" menu in Telegram
    except TelegramError as e:
        log.warning("telegram: %s", e)
    log.info("bot started; checking every %.0f-%.0f min%s", lo / 60, hi / 60,
             f"; autobook every {window.seconds:.0f}s on days {window.first_day}-{window.last_day} "
             f"{window.start:%H:%M}-{window.end:%H:%M}" if window else "")
    try:
        await asyncio.gather(bot.updates_loop(), bot.monitor_loop(lo, hi, window))
    finally:
        await http.aclose()
        await tg.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs, and the Telegram URL contains the bot token
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
