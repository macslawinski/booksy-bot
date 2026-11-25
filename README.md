# booksy-bot

Bot na Telegramie, który pilnuje wolnych terminów w wybranym salonie na [Booksy](https://booksy.com) daje znać, gdy coś się zwolni, i może zarezerwować termin.
Inspirowany [dyrkin/luxmed-bot](https://github.com/dyrkin/luxmed-bot).

> Projekt prywatny, nieoficjalny, niepowiązany z Booksy. Używaj tylko na własnym koncie, z rzadkim odpytywaniem
> (domyślnie co 5–10 min) — regulamin Booksy pozwala zablokować konto za korzystanie niezgodne z przeznaczeniem.

## Co umie

- Wklejasz link do salonu (z przeglądarki albo „Udostępnij” z aplikacji) → wybierasz usługę, pracownika, daty i godziny.
- Bot od razu pokazuje wolne terminy, a po kliknięciu „Powiadamiaj” sprawdza je cyklicznie i pisze o nowych.
- „📅 Rezerwuj” pod terminem: bot sprawdza termin (`dry_run`, niczego nie rezerwuje), pokazuje cenę i pyta o potwierdzenie.
- „🤖 Rezerwuj automatycznie”, gdy nie ma terminów: bot sam rezerwuje pierwszy wolny termin, który się pojawi
  w podanym zakresie dat — 1 wizyta na każdy miesiąc z zakresu, potem kończy (najwyżej 3 autorezerwacje naraz).
- Okno zapisów (`FAST_WINDOW`): dla salonów, które otwierają kalendarz o stałej porze, bot w tym oknie sprawdza
  autorezerwacje co kilka sekund.
- Alert, gdy sesja Booksy wygaśnie.

### Zasady bezpieczeństwa rezerwacji

- Terminy z przedpłatą, kaucją albo opłatą za odwołanie: bot ich nie rezerwuje, tylko powiadamia.
- Przed każdą rezerwacją świeże sprawdzenie terminu i godziny.
- Jedna próba na termin, zapisana w bazie przed wysłaniem — podwójne kliknięcie nie robi drugiej wizyty.
- Brak pewnej odpowiedzi od Booksy (timeout, błąd serwera) → bez ponawiania; bot prosi o sprawdzenie wizyt w aplikacji.
- Bot nie widzi wizyt zarezerwowanych ręcznie w aplikacji.

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

Opcjonalnie w `.env`:

- `CHECK_MINUTES=5-10` — jak często sprawdzać (losowo z zakresu, minimum 2).
- `FAST_WINDOW=1-3 19:55-20:35 10` — dni miesiąca, godziny i co ile sekund (minimum 5) sprawdzać autorezerwacje,
  np. gdy salon otwiera zapisy 1–3 dnia miesiąca ok. 20:00. Poza oknem bot budzi się dokładnie na jego początek.

## Znajomi (wspólny bot)

Właściciel (`TELEGRAM_CHAT_ID`) zaprasza znajomych komendą `/invite` (jednorazowy kod; znajomy wysyła `/start KOD`).
Każdy znajomy podłącza **własne** konto Booksy (`/token WARTOŚĆ`, wiadomość jest od razu usuwana) — regulamin Booksy
to jedno konto na osobę. Znajomy ma osobne powiadomienia i rezerwacje, limit 3 powiadomień i 1 autorezerwacji.
Właściciel ma pierwszeństwo: w każdym cyklu jego autorezerwacje idą pierwsze, znajomi dostają kolejne wolne terminy.
`/users` – lista, `/kick ID` – usunięcie (token i powiadomienia).

Uwaga: wszystkie konta łączą się z jednego IP, a tokeny znajomych leżą w bazie bota — zapraszaj tylko zaufane osoby.

## Home Assistant OS (dodatek)

Bot może działać 24/7 jako lokalny dodatek HA (`homeassistant-addon/`). Wymaga dodatku **Samba share** w HA.

1. Zaloguj się w Windows do udziału `\\homeassistant.local\addons` (login i hasło z konfiguracji Samba share).
2. Pierwsza instalacja (kopiuje też `.env` — tylko klucze bota — i bazę):
   ```powershell
   powershell -File homeassistant-addon\deploy.ps1 -WithData
   ```
3. W HA: Ustawienia → Aplikacje → Zainstaluj aplikację → ⋮ → Sprawdź aktualizacje → **Booksy bot** (Lokalne) → Zainstaluj → Uruchom.
4. Zatrzymaj bota na komputerze — dwa naraz kłócą się o wiadomości z Telegrama.

Aktualizacja kodu: `deploy.ps1` (bez `-WithData`, inaczej nadpisze bazę) i „Przebuduj” przy dodatku.
Dane bota leżą w `/homeassistant/booksy-bot/` (`.env`, baza) — token zmieniasz tam, np. dodatkiem File editor.

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
