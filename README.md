# booksy-bot

Bot na Telegramie, który pilnuje wolnych terminów w wybranym salonie na [Booksy](https://booksy.com) i daje znać, gdy coś się zwolni.
Inspirowany [dyrkin/luxmed-bot](https://github.com/dyrkin/luxmed-bot).

> Projekt prywatny, nieoficjalny, niepowiązany z Booksy. Używaj tylko na własnym koncie, z rzadkim odpytywaniem
> (domyślnie co 5–10 min) — regulamin Booksy pozwala zablokować konto za korzystanie niezgodne z przeznaczeniem.

## Co umie

- Wklejasz link do salonu (z przeglądarki albo „Udostępnij” z aplikacji) → wybierasz usługę, pracownika, daty i godziny.
- Bot od razu pokazuje wolne terminy, a po kliknięciu „Powiadamiaj” sprawdza je cyklicznie i pisze o nowych.
- Alert, gdy sesja Booksy wygaśnie.
- Tylko powiadomienia — rezerwacji jeszcze nie ma.

## Uruchomienie

Wymaga Pythona 3.12+.

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Linux/macOS: .venv/bin/python
cp .env.example .env
```

Uzupełnij `.env`:

- `TELEGRAM_BOT_TOKEN` — od [@BotFather](https://t.me/BotFather).
- `TELEGRAM_CHAT_ID` — zostaw puste, uruchom bota i wyślij mu `/start`; odpowie Twoim chat id. Bot obsługuje tylko ten jeden czat.
- `BOOKSY_API_KEY`, `BOOKSY_ACCESS_TOKEN` — zaloguj się na booksy.com, F12 → Network → dowolne żądanie do
  `pl.booksy.com/core/...` → nagłówki `X-Api-Key` i `X-Access-Token`. Bot sam się nie loguje (logowanie wymaga hCaptcha);
  po wygaśnięciu tokenu wklej nowy do `.env` — restart niepotrzebny.

Start:

```sh
.venv/Scripts/python -m booksy_bot.bot
```

Komendy: `/help`, `/list` (aktywne obserwacje), `/stop N`.

## CLI (bez Telegrama)

```sh
.venv/Scripts/python -m booksy_bot.cli services <id_salonu|URL>
.venv/Scripts/python -m booksy_bot.cli slots <id_salonu|URL> <service_variant_id> [--staffer ID] [--days 7]
```

## Testy

```sh
.venv/Scripts/python -m pytest -q
```

## Bezpieczeństwo

`.env`, pliki `*.har` i baza `*.sqlite3` są w `.gitignore` — zawierają tokeny i dane konta. Nie commituj ich.
