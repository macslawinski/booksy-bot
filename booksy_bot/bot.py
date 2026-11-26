"""Telegram bot: search Booksy services, notify about newly appeared free slots, book a slot after confirmation.

  python -m booksy_bot.bot

Needs in .env: BOOKSY_* (see .env.example), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (the bot tells you yours on /start).
"""
import asyncio
import contextvars
import hmac
import html
import logging
import os
import random
import re
import unicodedata
from argparse import ArgumentTypeError
from collections.abc import Callable
from datetime import date, datetime, timezone

from booksy_bot.api.client import ANY_STAFFER, BooksyClient
from booksy_bot.api.http import ApiChanged, ApiError, SessionExpired
from booksy_bot.api.models import Business, Service, Variant
from booksy_bot import booking
from booksy_bot.booking import BookArgs
from booksy_bot.cli import business_id_arg, fresh_token, load_env, make_client
from booksy_bot.storage import DAY_END, Monitor, Storage
from booksy_bot.telegram import Telegram, TelegramError
from booksy_bot.timezones import SERVICE_TZ
from booksy_bot.query import Query, parse_dates, parse_times
from booksy_bot.scheduler import FastWindow, interval, next_delay
from booksy_bot.texts import BOT_COMMANDS, FRIEND_HELP, HELP, LOGIN_HELP, OWNER_HELP, bookmarklet

log = logging.getLogger("bot")
MAX_MONITORS = 10          # per chat; keeps polling volume modest (luxmed-bot used the same limit)
MAX_AUTOBOOK = 3           # unattended bookings at once; Booksy terms: no mass booking
FRIEND_MAX_MONITORS = 3    # friends share the owner's IP: keep their traffic small
FRIEND_MAX_AUTOBOOK = 1
PASSWORD_TRIES = 3          # wrong bot passwords before the chat is blocked for good
LIST_LIMIT = 30            # service buttons per message; more -> type part of the name

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


WEEKDAYS = ("pon", "wt", "śr", "czw", "pt", "sob", "niedz")


def fmt_slot(iso: str) -> str:
    """'pt 27.11, 14:00' – Polish weekday regardless of the server locale."""
    t = datetime.fromisoformat(iso)
    return f"{WEEKDAYS[t.weekday()]} {t:%d.%m}, {t:%H:%M}"


def fmt_slots(slots: list[str], limit: int = 10) -> str:
    lines = [f"• {fmt_slot(s)}" for s in slots[:limit]]
    if len(slots) > limit:
        lines.append(f"… i {len(slots) - limit} więcej")
    return "\n".join(lines)


def fold(text: str) -> str:
    """Case- and accent-insensitive form for searching ("Męskie" ~ "meskie")."""
    text = unicodedata.normalize("NFKD", text.casefold().replace("ł", "l"))
    return "".join(c for c in text if not unicodedata.combining(c))


async def find_slots(client: BooksyClient, q: Query, now: date | None = None, retries: int | None = None) -> list[str]:
    """Free slot starts (ISO, Europe/Warsaw) inside the query's date window and time range."""
    start, end = q.window(now or today())
    if start > end:
        return []
    r = await client.time_slots(q.business_id, q.variant_id, start, end, q.staffer_id, retries=retries)
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


async def check_monitor(client: BooksyClient, storage: Storage, m: Monitor, now: date | None = None,
                        slots: list[str] | None = None) -> Reply | None:
    """Fetch slots (unless given), remember them, and return a message only when something new appeared (or on the first check)."""
    current = set(slots if slots is not None else await find_slots(client, Query.of(m), now))
    new, first = storage.update_slots(m.id, current)
    if new and not first:
        before = {s[:7] for s in current - set(new)}
        for month in sorted({s[:7] for s in new} - before):
            # shortcut: a sold-out month that gets one cancellation also looks "opened"; the slot count tells them apart
            log.info("monitor %s: month %s opened, %d new slots", m.id, month, sum(s.startswith(month) for s in new))
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


def range_months(m: Monitor) -> set[str]:
    """'YYYY-MM' of every month the monitor's date range (from today on) touches."""
    start, end = Query.of(m).window(today())
    return {f"{start.year + (start.month - 1 + i) // 12}-{(start.month - 1 + i) % 12 + 1:02d}"
            for i in range((end.year - start.year) * 12 + end.month - start.month + 1)}


async def autobook(client: BooksyClient, storage: Storage, m: Monitor, slots: list[str]) -> Reply | None:
    """Book the earliest free slot in a month without a bot booking in this salon yet (1 visit a month).
    booking.book re-checks the slot, refuses payments and never books the same slot twice.
    Booked -> ends once every month of the date range has a visit. Slot gone meanwhile -> try again
    next check. Anything else -> back to notifications only."""
    taken = storage.booked_months(m.business_id, m.chat_id)
    slots = [s for s in slots if s[:7] not in taken
             and (storage.booking_status(m.business_id, s) or ('failed',))[0] in ('failed', 'cancelled')]   # not someone else's
    if not slots:
        return None
    try:
        taken |= await booking.visit_months(client, m.business_id)    # visits booked by hand in the app
    except SessionExpired:
        raise
    except ApiError as e:
        log.warning("monitor %s: visit list unavailable (%s), using the bot's own bookings only", m.id, e)  # do not miss the slot
    if range_months(m) <= taken:
        storage.deactivate(m.chat_id, m.id)
        return f"✅ Każdy miesiąc z zakresu ma już wizytę w tym salonie – kończę {describe(m)}"
    slots = [s for s in slots if s[:7] not in taken]
    if not slots:
        return None
    b = BookArgs.of(m.business_id, m.variant_id, m.staffer_id, slots[0])
    try:
        msg = await booking.book(client, storage, b, m.chat_id)
    except ValueError as e:
        log.info("monitor %s: autobook %s skipped: %s", m.id, slots[0], e)
        return None
    if storage.booking_status(b.business_id, b.start.isoformat()) == ("confirmed", m.chat_id):
        if range_months(m) <= storage.booked_months(m.business_id, m.chat_id) | taken:
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
        # Joining = /start + BOT_PASSWORD (unset = nobody can join). chat_id -> tries left while asking for it.
        self.password = os.environ.get("BOT_PASSWORD", "")
        self._alerted: set[str] = set()    # one alert per problem until it is resolved
        self._prechecked: date | None = None   # day of the last pre-window token check
        # What typed text means right now, per chat: ("service", salon) / ("dates", args) / ("times", args).
        # shortcut: in memory, lost on restart (user just sends the link again); persist if that becomes annoying.
        self._waiting: dict[int, tuple[str, str]] = {}
        self._listed: dict[int, list[str]] = {}            # chat -> /pick commands of the last numbered service list

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
            return await self.join(chat_id, text, name)
        token = _api.set(self.client_for(chat_id))
        try:
            return await self._dispatch(chat_id, text)
        except SessionExpired:
            return self.token_message(chat_id)
        except (ValueError, ArgumentTypeError, ApiError) as e:
            return f"⚠️ Błąd: {html.escape(str(e))[:300]}"
        finally:
            _api.reset(token)

    async def join(self, chat_id: int, text: str, name: str) -> str | None:
        """Strangers: /start, then BOT_PASSWORD; 3 wrong -> blocked for good (no guessing). Anything else: silence."""
        if not self.password or self.storage.is_blocked(chat_id):
            return None  # private bot
        tries = self.storage.join_tries(chat_id)
        if text.split()[:1] == ["/start"]:
            if tries is None:
                self.storage.set_join_tries(chat_id, PASSWORD_TRIES)
            return "🔒 Podaj hasło do bota (wiadomość od razu usunę)."
        if tries is None:
            return None
        if hmac.compare_digest(text.encode(), self.password.encode()):
            self.storage.set_join_tries(chat_id, None)
            return await self.welcome(chat_id, name)
        if tries > 1:
            self.storage.set_join_tries(chat_id, tries - 1)
            return f"❌ Złe hasło. Zostało prób: {tries - 1}."
        self.storage.set_join_tries(chat_id, None)
        self.storage.block(chat_id)
        log.warning("chat %s blocked after wrong passwords", chat_id)
        await self.send(self.owner, f"⚠️ {PASSWORD_TRIES}× złe hasło od {html.escape(name) or chat_id} "
                                       f"(<code>{chat_id}</code>) – zablokowany.")
        return "❌ Złe hasło. Dostęp zablokowany."

    async def welcome(self, chat_id: int, name: str) -> str:
        self.storage.add_user(chat_id, name)
        log.info("friend %s joined", chat_id)
        await self.send(self.owner, f"👋 Dołączył(a) {html.escape(name) or chat_id} (<code>{chat_id}</code>).")
        return "👋 Witaj! Najpierw podłącz swoje konto Booksy: /login" + FRIEND_HELP

    async def _dispatch(self, chat_id: int, text: str) -> Reply | None:
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
            return LOGIN_HELP.replace("{code}", bookmarklet(self.username))
        if cmd == "/token":
            if len(args) != 1 or len(args[0]) < 20:
                return "Wyślij: <code>/token WARTOŚĆ</code> (jak go zdobyć z telefonu: /login)."
            self.storage.set_token(chat_id, args[0], os.environ.get("BOOKSY_FINGERPRINT") if chat_id == self.owner else None)
            self._alerted.discard(f"token:{chat_id}")
            return "✅ Token zapisany, wiadomość z nim usunięta. Wyślij link do salonu, żeby zacząć."
        if cmd == "/users" and chat_id == self.owner:
            us = self.storage.users()
            return "\n".join(f"<code>{c}</code> {html.escape(n) or '(bez imienia)'} – {'token ✅' if t else 'bez tokenu ⚠️'}"
                             for c, n, t in us if c != self.owner) or "Brak znajomych."
        if cmd == "/kick" and chat_id == self.owner and len(args) == 1 and args[0].isdigit() and int(args[0]) != self.owner:
            who = int(args[0])
            if gone := self._friend_clients.pop(who, None):
                await gone.aclose()
            self.storage.set_join_tries(who, None)
            removed = self.storage.remove_user(who)
            self.storage.block(who)          # the password is shared: without this they could just join again
            return "✅ Usunięto i zablokowano." if removed else "Nie ma takiego użytkownika (czat zablokowany)."
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
        if cmd == "/visits":
            vs = sorted((v for v in (await self.api.my_bookings()).bookings if v.active), key=lambda v: v.booked_from)
            if not vs:
                return "Brak nadchodzących wizyt."
            lines = [f"• {fmt_slot(v.booked_from.isoformat())} – {html.escape(v.business.name)}" for v in vs]
            return ("Twoje wizyty w Booksy:\n" + "\n".join(lines),
                    [button(f"❌ Odwołaj {fmt_slot(v.booked_from.isoformat())}", f"/cancel {v.appointment_uid}") for v in vs])
        if cmd == "/cancel" and len(args) == 1 and args[0].isdigit():
            a = await booking.cancellable(self.api, args[0])
            return (f"Odwołać wizytę <b>{a.booked_from:%d.%m %H:%M}</b> – {html.escape(a.business.name)}"
                    + (f", {html.escape(a.total)}" if a.total else "") + "?",
                    [button("✅ Tak, odwołaj", f"/cancelok {args[0]}")])
        if cmd == "/cancelok" and len(args) == 1 and args[0].isdigit():
            return await booking.cancel(self.api, self.storage, chat_id, args[0])
        if cmd == "/stop" and len(args) == 1 and args[0].lstrip("#").isdigit():
            ok = self.storage.deactivate(chat_id, int(args[0].lstrip("#")))
            return "✅ Zakończono." if ok else "Nie ma takiego aktywnego powiadomienia."
        return "Nie rozumiem tej komendy. Pomoc: /help"

    async def typed(self, chat_id: int, text: str) -> Reply | None:
        """Free text answering the last question; on a parse error the question stays open (just type again)."""
        kind, ctx = self._waiting[chat_id]
        if kind == "service":
            picks = self._listed.get(chat_id, [])
            if text.strip().isdigit():                     # typed number = same as tapping that button
                n = int(text)
                if 1 <= n <= len(picks):
                    del self._waiting[chat_id]
                    return await self.handle(chat_id, picks[n - 1])
                return f"Nie ma usługi nr {n} – wybierz 1–{len(picks)}."
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
        lines.append("Stuknij albo wpisz numer, albo wpisz część nazwy usługi 🔎")
        picks = [(str(n), f"/pick {b.id} {v.id}") for n, (_, _, v) in enumerate(found[:LIST_LIMIT], 1)]
        self._listed[chat_id] = [cmd for _, cmd in picks]
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
        return head + body, (book_buttons(q.business_id, q.variant_id, q.staffer_id, slots) + watch
                             + [button("🤖 Rezerwuj automatycznie", f"/auto {q.encode()}")])

    async def cmd_auto(self, q: Query) -> Reply:
        """Explain what autobook will do and ask once more: it books a real appointment without asking again."""
        b, name = await self.lookup(q)
        variant = find_variant(b, q.variant_id)[1]
        if variant.needs_payment:
            raise ValueError("ta usługa wymaga przedpłaty/kaucji – autorezerwacja niemożliwa, wybierz „Powiadamiaj”")
        rule = ("<b>1 wizyta na każdy miesiąc z zakresu</b> (liczą się też wizyty w tym salonie umówione w aplikacji), "
                "potem kończę.")
        return (f"🤖 <b>{html.escape(name)}</b> – {html.escape(b.name)} ({q.scope()})\n"
                f"Zarezerwuję <b>pierwszy</b> wolny termin (także już dostępny) bez pytania "
                f"({html.escape((variant.service_price or '?').replace(',00', ''))}, płatność na miejscu) i dam znać. {rule}\n"
                "Odwołać wizytę możesz w /visits.",
                [button("✅ Włącz autorezerwację", f"/autoon {q.encode()}")])

    async def cmd_book(self, b: BookArgs) -> Reply:
        """First step: dry_run shows whether the slot is still free and what it costs; nothing is booked yet."""
        r = await booking.check(self.api, b)
        when = fmt_slot(b.start.isoformat())
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

    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None:
        """Send, but a failed message (bot blocked by that person, Telegram down) only gets logged: loops go on."""
        try:
            await self.tg.send(chat_id, text, buttons)
        except TelegramError as e:
            log.warning("telegram send to %s: %s", chat_id, e)

    async def alert(self, key: str, chat_id: int, text: str) -> None:
        if key not in self._alerted:
            self._alerted.add(key)
            await self.send(chat_id, text)

    async def token_expired(self, chat_id: int, prefix: str) -> None:
        """One alert per expiry; logs how long the token lived (Booksy does not say)."""
        key = f"token:{chat_id}"
        if key in self._alerted:
            return
        note = ""
        if set_at := self.storage.token_set_at(chat_id):
            hours = (datetime.now(timezone.utc) - set_at).total_seconds() / 3600
            log.info("token of %s expired %.1f h after it was set (%s UTC)", chat_id, hours, set_at)
            age = f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} dni"
            note = f"\n(Poprzedni token działał ok. {age}.)"
        await self.alert(key, chat_id, prefix + self.token_message(chat_id) + note)

    async def pre_window_check(self) -> None:
        """Before the booking window: a real request per autobook person, so an expired token is fixed in time."""
        done: set[int] = set()
        for m in self.storage.active_monitors():
            if not m.autobook or m.chat_id in done:
                continue
            done.add(m.chat_id)
            try:
                await self.client_for(m.chat_id).time_slots(m.business_id, m.variant_id, today(), today(), m.staffer_id)
            except SessionExpired:
                self._alerted.discard(f"token:{m.chat_id}")    # remind again even if told hours ago
                await self.token_expired(m.chat_id, "⏰ Za niecałą godzinę otwarcie zapisów – autorezerwacja nie zadziała. ")
            except ApiError as e:
                log.warning("pre-window check %s: %s", m.chat_id, e)

    async def check_all(self, fast: bool = False) -> None:
        """fast = inside the booking-opening window: only autobook monitors, no pauses between them."""
        expired: set[int] = set()
        # fast: one time_slots per query per cycle, through whichever account asks first (fewer requests from our IP).
        # Not outside the window, so each person's token is still checked there before the window opens.
        fetched: dict[Query, list[str]] = {}
        # the owner first: in the same cycle their monitors book before friends' (priority on the same barber)
        for m in sorted(self.storage.active_monitors(), key=lambda m: m.chat_id != self.owner):
            if (fast and not m.autobook) or m.chat_id in expired:
                continue
            if Query.of(m).expired(today()):
                self.storage.deactivate(m.chat_id, m.id)
                await self.send(m.chat_id, f"⌛ Zakres dat minął – kończę {describe(m)}")
                continue
            client, q = self.client_for(m.chat_id), Query.of(m)
            try:
                if fast and q not in fetched:
                    fetched[q] = await find_slots(client, q, retries=0)
                msg = await check_monitor(client, self.storage, m, slots=fetched.get(q))
            except SessionExpired:
                expired.add(m.chat_id)                        # one person's token: the others go on
                await self.token_expired(m.chat_id, "Powiadomienia wstrzymane. ")
                continue
            except ApiChanged as e:
                log.error("monitor %s: %s", m.id, e)
                await self.alert(f"changed:{m.id}", m.chat_id, f"⚠️ Booksy zmienił format odpowiedzi – powiadomienie #{m.id} może nie działać. Szczegóły w logu.")
                continue
            except ApiError as e:
                log.warning("monitor %s: %s", m.id, e)  # outage / limit: try next cycle
                continue
            except Exception:                           # e.g. a slot at a nonexistent DST time: skip this monitor only
                log.exception("monitor %s", m.id)
                continue
            self._alerted.discard(f"token:{m.chat_id}")
            self._alerted.discard(f"changed:{m.id}")
            if msg:
                text, buttons = msg if isinstance(msg, tuple) else (msg, None)
                await self.send(m.chat_id, text, buttons)
            if not fast:
                await asyncio.sleep(random.uniform(2, 5))  # spread requests

    async def monitor_loop(self, min_s: float, max_s: float, window: "FastWindow | None" = None) -> None:
        while True:
            now = datetime.now(SERVICE_TZ)
            fast = bool(window and window.active(now))
            try:
                if window and window.precheck_due(now) and self._prechecked != now.date():
                    self._prechecked = now.date()
                    await self.pre_window_check()
                await self.check_all(fast)
            except Exception:                           # the loop must outlive any bug: it is what books on the 1st
                log.exception("monitor loop")
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
                offset = u["update_id"] + 1             # advanced first: a message that crashes us is not retried forever
                try:
                    await self.on_update(u)
                except Exception:
                    log.exception("update %s", u.get("update_id"))

    async def on_update(self, u: dict) -> None:
        """One Telegram update: a message or a button press."""
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
            return
        chat = msg.get("chat") or {}
        secret = text.startswith("/token") or self.storage.join_tries(chat_id) is not None   # Booksy token or bot password
        reply = await self.handle(chat_id, text, chat.get("first_name") or chat.get("username") or "")
        if secret and msg.get("message_id") and not cb:
            try:   # do not leave secrets in the chat history
                await self.tg.call("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
            except TelegramError as e:
                log.warning("telegram: %s", e)
        if reply:
            text, buttons = reply if isinstance(reply, tuple) else (reply, None)
            await self.send(chat_id, text, buttons)


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
        for c in bot._friend_clients.values():
            await c.aclose()
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
