"""Telegram bot: search Booksy services and notify about newly appeared free slots. Notifications only, no booking.

  python -m booksy_bot.bot

Needs in .env: BOOKSY_* (see .env.example), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (the bot tells you yours on /start).
"""
import asyncio
import html
import logging
import os
import random
import re
import unicodedata
from argparse import ArgumentTypeError
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from booksy_bot.api.client import ANY_STAFFER, BooksyClient
from booksy_bot.api.http import ApiChanged, ApiError, SessionExpired
from booksy_bot.api.models import Business, Service, Variant
from booksy_bot.cli import business_id_arg, fresh_token, load_env, make_client
from booksy_bot.storage import DAY_END, Monitor, Storage
from booksy_bot.telegram import Telegram, TelegramError
from booksy_bot.timezones import SERVICE_TZ

log = logging.getLogger("bot")
MAX_MONITORS = 10          # per chat; keeps polling volume modest (luxmed-bot used the same limit)
MAX_DAYS = 60
LIST_LIMIT = 30            # service buttons per message; more -> type part of the name

HELP = """<b>Booksy bot</b> – szuka wolnych terminów i powiadamia o nowych. Niczego nie rezerwuje.

<b>Jak szukać</b>
1. Wyślij link do salonu albo w aplikacji Booksy: salon → Udostępnij → ten bot.
2. Wybierz usługę z listy albo wpisz część nazwy (np. <i>broda</i>, <i>meskie</i>).
3. Wybierz pracownika (gdy jest kilku).
4. Wpisz datę albo zakres dat, np. <code>5.12</code> lub <code>1.12-15.12</code>.
5. Godziny: przycisk albo „🕐 Wpisz godziny”, np. <code>16-20</code> lub <code>16:30-19:00</code>.
6. Dostajesz wolne terminy od razu. Przycisk „🔔 Powiadamiaj” włącza sprawdzanie co kilka minut –
   napiszę tylko o <b>nowych</b> terminach (np. gdy ktoś odwoła wizytę).

<b>Komendy</b>
/list – aktywne powiadomienia (z przyciskiem „Zakończ”)
/help – ta pomoc

Powiadomienia z konkretnymi datami kończą się same po ostatnim dniu. Limit: 10 powiadomień, 60 dni.
Gdy wygaśnie logowanie do Booksy, dostaniesz jedną wiadomość – wpisz nowy token do .env."""

BOT_COMMANDS = [{"command": "list", "description": "Aktywne powiadomienia"},
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
    return f"#{m.id} <b>{html.escape(m.service_name)}</b> – {html.escape(m.business_name)} ({who}{Query.of(m).scope()})"


def salon_link(url: str) -> str:
    return f'\n<a href="{html.escape(url)}">Otwórz salon na Booksy</a>'


async def check_monitor(client: BooksyClient, storage: Storage, m: Monitor, now: date | None = None) -> str | None:
    """Fetch slots, remember them, and return a message only when something new appeared (or on the first check)."""
    current = set(await find_slots(client, Query.of(m), now))
    new, first = storage.update_slots(m.id, current)
    if first:
        return f"🔔 Powiadomię o nowych terminach: {describe(m)}"
    if new:
        return f"🆕 Nowe terminy – {describe(m)}\n{fmt_slots(new)}{salon_link(m.business_url)}"
    return None


def find_variant(b: Business, variant_id: int) -> tuple[Service, Variant]:
    for s in b.bookable_services():
        for v in s.variants:
            if v.id == variant_id:
                return s, v
    raise ValueError("nie ma takiej usługi w tym salonie")


class Bot:
    def __init__(self, tg: Telegram, client: BooksyClient, storage: Storage, owner_chat_id: int | None):
        self.tg, self.client, self.storage, self.owner = tg, client, storage, owner_chat_id
        self._alerted: set[str] = set()    # one alert per problem until it is resolved
        # What typed text means right now, per chat: ("service", salon) / ("dates", args) / ("times", args).
        # shortcut: in memory, lost on restart (user just sends the link again); persist if that becomes annoying.
        self._waiting: dict[int, tuple[str, str]] = {}

    # ---------- commands ----------

    async def handle(self, chat_id: int, text: str) -> Reply | None:
        if self.owner is None:
            log.warning("message from chat %s; set TELEGRAM_CHAT_ID=%s in .env and restart", chat_id, chat_id)
            return f"Twój chat id: <code>{chat_id}</code>\nDopisz <code>TELEGRAM_CHAT_ID={chat_id}</code> do .env i uruchom bota ponownie."
        if chat_id != self.owner:
            return None  # single-user bot: ignore strangers silently
        try:
            return await self._dispatch(chat_id, text.strip())
        except SessionExpired:
            return "🔑 Token Booksy wygasł. Zaloguj się na booksy.com i wpisz nowy BOOKSY_ACCESS_TOKEN do .env (bez restartu)."
        except (ValueError, ArgumentTypeError, ApiError) as e:
            return f"Błąd: {html.escape(str(e))[:300]}"

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
            return HELP
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
        b = (await self.client.business(business_id_arg(salon))).business
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
        b = (await self.client.business(int(args[0]))).business
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
        b = (await self.client.business(q.business_id)).business
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
        slots = await find_slots(self.client, q)
        head = f"<b>{html.escape(name)}</b> – {html.escape(b.name)} ({q.scope()})\n"
        body = f"Wolne terminy ({len(slots)}):\n{fmt_slots(slots)}{salon_link(b.page_url)}" if slots else "Brak wolnych terminów."
        return head + body, [button("🔔 Powiadamiaj o nowych terminach", f"/watch {q.encode()}")]

    async def cmd_watch(self, chat_id: int, q: Query) -> str:
        if q.expired(today()):
            raise ValueError("ten zakres dat już minął")
        if len(self.storage.chat_monitors(chat_id)) >= MAX_MONITORS:
            raise ValueError(f"limit {MAX_MONITORS} powiadomień – zakończ któreś w /list")
        b, name = await self.lookup(q)
        m = self.storage.add_monitor(chat_id, b.id, b.name, b.page_url, q.variant_id, name, q.staffer_id, q.days,
                                     q.date_from and q.date_from.isoformat(), q.date_to and q.date_to.isoformat(),
                                     q.time_from, q.time_to)
        return await check_monitor(self.client, self.storage, m) or f"🔔 Powiadomię o nowych terminach: {describe(m)}"

    # ---------- loops ----------

    async def alert(self, key: str, chat_id: int, text: str) -> None:
        if key not in self._alerted:
            self._alerted.add(key)
            await self.tg.send(chat_id, text)

    async def check_all(self) -> None:
        for m in self.storage.active_monitors():
            if Query.of(m).expired(today()):
                self.storage.deactivate(m.chat_id, m.id)
                await self.tg.send(m.chat_id, f"⌛ Zakres dat minął – kończę {describe(m)}")
                continue
            try:
                msg = await check_monitor(self.client, self.storage, m)
            except SessionExpired:
                await self.alert("token", m.chat_id, "🔑 Token Booksy wygasł – powiadomienia wstrzymane. "
                                 "Zaloguj się na booksy.com i wpisz nowy BOOKSY_ACCESS_TOKEN do .env (bez restartu).")
                return
            except ApiChanged as e:
                log.error("monitor %s: %s", m.id, e)
                await self.alert(f"changed:{m.id}", m.chat_id, f"⚠️ Booksy zmienił format odpowiedzi – powiadomienie #{m.id} może nie działać. Szczegóły w logu.")
                continue
            except ApiError as e:
                log.warning("monitor %s: %s", m.id, e)  # outage / limit: try next cycle
                continue
            self._alerted.discard("token")
            self._alerted.discard(f"changed:{m.id}")
            if msg:
                await self.tg.send(m.chat_id, msg)
            await asyncio.sleep(random.uniform(2, 5))  # spread requests

    async def monitor_loop(self, min_s: float, max_s: float) -> None:
        while True:
            try:
                await self.check_all()
            except TelegramError as e:
                log.warning("telegram: %s", e)
            await asyncio.sleep(random.uniform(min_s, max_s))

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
                reply = await self.handle(chat_id, text)
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


async def run() -> None:
    load_env()
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("missing TELEGRAM_BOT_TOKEN in .env")
    owner = os.environ.get("TELEGRAM_CHAT_ID")
    client, http = make_client(token_provider=fresh_token)
    tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    bot = Bot(tg, client, Storage(os.environ.get("BOT_DB", "booksy_bot.sqlite3")), int(owner) if owner else None)
    lo, hi = interval()
    try:
        await tg.call("setMyCommands", commands=BOT_COMMANDS)   # the "/" menu in Telegram
    except TelegramError as e:
        log.warning("telegram: %s", e)
    log.info("bot started; checking every %.0f-%.0f min", lo / 60, hi / 60)
    try:
        await asyncio.gather(bot.updates_loop(), bot.monitor_loop(lo, hi))
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
