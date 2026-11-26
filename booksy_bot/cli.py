"""Prototype: print free slots for one service.

  python -m booksy_bot.cli slots <business_id> <service_variant_id> [--staffer ID] [--days 7]

Config comes from environment or .env (see .env.example). The access token is copied by hand from the browser,
because Booksy's password login requires hCaptcha.
"""
import argparse
import asyncio
import logging
import os
import re
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

from booksy_bot.api.client import ANY_STAFFER, BASE_URL, BooksyClient
from booksy_bot.api.http import HttpCore, SessionExpired


ENV_PATH = Path(".env")


def parse_env(path: Path = ENV_PATH) -> dict[str, str]:
    # shortcut: minimal KEY=VALUE parser (no quotes/multiline); switch to python-dotenv if .env grows.
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            out[key.strip()] = value.strip()
    return out


def load_env(path: Path = ENV_PATH) -> None:
    for k, v in parse_env(path).items():
        os.environ.setdefault(k, v)


def fresh_token() -> str:
    """Re-read .env on every request so a new token works without restarting the bot."""
    return parse_env().get("BOOKSY_ACCESS_TOKEN") or os.environ.get("BOOKSY_ACCESS_TOKEN", "")


def make_client(token_provider=lambda: os.environ["BOOKSY_ACCESS_TOKEN"],
                fingerprint: str | None = None) -> tuple[BooksyClient, HttpCore]:
    missing = [k for k in ("BOOKSY_API_KEY", "BOOKSY_ACCESS_TOKEN") if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing in .env: {', '.join(missing)} (see .env.example)")
    headers = {"X-Api-Key": os.environ["BOOKSY_API_KEY"],
               "X-Fingerprint": fingerprint or os.environ.get("BOOKSY_FINGERPRINT") or str(uuid.uuid4()),
               "Accept-Language": "pl", "Accept": "application/json"}
    if os.environ.get("BOOKSY_APP_VERSION"):
        headers["X-App-Version"] = os.environ["BOOKSY_APP_VERSION"]
    http = HttpCore(BASE_URL, headers, token_provider=token_provider, token_header="X-Access-Token")
    return BooksyClient(http), http


def business_id_arg(value: str) -> int:
    """Accept a salon id or its URL: booksy.com/pl-pl/100001_salon-name_..."""
    m = re.search(r"/(\d+)_", value) or re.search(r"/show-business/(\d+)", value)  # page URL or app "share" link
    if m:
        return int(m.group(1))
    if value.isdigit():
        return int(value)
    raise argparse.ArgumentTypeError("expected salon id or booksy.com salon URL")


async def cmd_services(a: argparse.Namespace) -> None:
    client, http = make_client()
    try:
        b = (await client.business(a.business_id)).business
    except SessionExpired:
        sys.exit("token expired: log in at booksy.com and copy a fresh X-Access-Token into .env")
    finally:
        await http.aclose()
    staff = {s.id: s.name for s in b.staff}
    print(f"{b.name} (id {b.id})\n")
    print(f"{'service_variant_id':>18}  {'min':>4}  {'cena':>10}  usługa  [pracownicy]")
    for s in b.bookable_services():
        for v in s.variants:
            pay = "  ⚠ przedpłata/kaucja" if v.needs_payment else ""
            who = ", ".join(f"{staff.get(i, '?')} ({i})" for i in v.staffer_ids)
            print(f"{v.id:>18}  {v.duration or '':>4}  {v.service_price or '':>10}  {s.name}  [{who}]{pay}")


async def cmd_slots(a: argparse.Namespace) -> None:
    client, http = make_client()
    start = date.today()
    try:
        r = await client.time_slots(a.business_id, a.service_variant_id, start, start + timedelta(days=a.days - 1), a.staffer)
    except SessionExpired:
        sys.exit("token expired: log in at booksy.com and copy a fresh X-Access-Token into .env")
    finally:
        await http.aclose()
    if not any(d.slots for d in r.days):
        print("no free slots")
    for d in r.days:
        if d.slots:
            print(f"{d.day:%a %Y-%m-%d}: " + ", ".join(f"{s:%H:%M}" for s in d.starts()))


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(prog="booksy_bot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("services", help="list salon services, variant ids and staff")
    v.add_argument("business_id", type=business_id_arg, help="salon id or booksy.com salon URL")
    v.set_defaults(run=cmd_services)
    s = sub.add_parser("slots", help="list free slots")
    s.add_argument("business_id", type=business_id_arg, help="salon id or booksy.com salon URL")
    s.add_argument("service_variant_id", type=int, help="from `services`")
    s.add_argument("--staffer", type=int, default=ANY_STAFFER, help="staffer id; default any")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(run=cmd_slots)
    for p in (v, s):
        p.add_argument("-v", action="store_true", help="debug logs (secrets masked)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.WARNING, format="%(levelname)s %(message)s")
    asyncio.run(a.run(a))


if __name__ == "__main__":
    main()
