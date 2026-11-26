"""Texts the bot sends: help, phone login instructions with the bookmarklet, the "/" command menu."""
import urllib.parse

HELP = """<b>Booksy bot</b> – szuka wolnych terminów, powiadamia o nowych i rezerwuje po Twoim potwierdzeniu.

<b>Jak szukać</b>
1. Wyślij link do salonu albo w aplikacji Booksy: salon → Udostępnij → ten bot.
2. Wybierz usługę: stuknij albo wpisz jej numer, albo wpisz część nazwy (np. <i>broda</i>, <i>meskie</i>).
3. Wybierz pracownika (gdy jest kilku).
4. Wpisz datę albo zakres dat, np. <code>5.12</code> lub <code>1.12-15.12</code>.
5. Godziny: przycisk albo „🕐 Wpisz godziny”, np. <code>16-20</code> lub <code>16:30-19:00</code>.
6. Dostajesz wolne terminy od razu. Przycisk „🔔 Powiadamiaj” włącza sprawdzanie co kilka minut –
   napiszę tylko o <b>nowych</b> terminach (np. gdy ktoś odwoła wizytę).
7. „📅 Rezerwuj” pod terminem: bot sprawdza, czy termin jest wolny i ile kosztuje, i pyta o potwierdzenie.
   Terminów z przedpłatą/kaucją bot nie rezerwuje.
8. Gdy nie ma terminów: „🤖 Rezerwuj automatycznie” – bot sam zarezerwuje <b>pierwszy</b> wolny termin,
   który się pojawi (1 wizyta na każdy miesiąc z zakresu), i napisze, co zarezerwował. Wyłączysz w /list.

<b>Komendy</b>
/list – aktywne powiadomienia (z przyciskiem „Zakończ”)
/visits – Twoje wizyty w Booksy (z przyciskiem „Odwołaj”)
/login – podłącz konto Booksy z telefonu
/help – ta pomoc

Powiadomienia z konkretnymi datami kończą się same po ostatnim dniu. Limit: 10 powiadomień, 92 dni.
Gdy wygaśnie logowanie do Booksy, dostaniesz jedną wiadomość – wtedy /login."""

# Run on booksy.com (logged in, phone or PC): catches the X-Access-Token the page itself sends with its next API call.
# Booksy's pages navigate without reloading (checked 2026-11-26), so the hook survives a tap on a category or salon.
# ASCII only and percent-encoded: Safari on iPhone refuses to run a bookmark whose address has raw spaces/Polish letters.
# Hooks fetch and XHR (the mobile site was not checked; the desktop one uses fetch).
BOOKMARKLET_JS = ("(()=>{let d=0;const g=t=>{if(d)return;d=1;const m='/token '+t;"
                  "try{navigator.clipboard.writeText(m)}catch(e){}"
                  "prompt('Skopiuj i wyslij botowi:',m);location.href='https://t.me/BOT'};"
                  "const f=window.fetch;window.fetch=function(i,o){try{const t=new Headers((o&&o.headers)||"
                  "(i instanceof Request?i.headers:void 0)).get('x-access-token');if(t)g(t)}catch(e){}"
                  "return f.apply(this,arguments)};"
                  "const s=XMLHttpRequest.prototype.setRequestHeader;XMLHttpRequest.prototype.setRequestHeader="
                  "function(k,v){if(/^x-access-token$/i.test(k)&&v)g(v);return s.apply(this,arguments)};"
                  "alert('OK. Teraz stuknij dowolna kategorie albo salon.')})()")


def bookmarklet(bot_username: str) -> str:
    return "javascript:" + urllib.parse.quote(BOOKMARKLET_JS.replace("t.me/BOT'", f"t.me/{bot_username}'"), safe="()[]{};,.:/'+=!*_~-|")

LOGIN_HELP = """<b>Jak podłączyć Booksy do bota (z telefonu)</b>

<b>CZĘŚĆ 1 – robisz tylko raz: zapisz zakładkę</b>

<b>iPhone (Safari):</b>
1. Stuknij szary kod na samym dole tej wiadomości. Skopiuje się.
2. Otwórz Safari, wejdź na jakąkolwiek stronę (np. google.pl).
3. Stuknij ikonę <b>Udostępnij</b> (kwadrat ze strzałką w górę) → <b>Dodaj zakładkę</b> → <b>Zachowaj</b>.
4. Stuknij ikonę <b>📖 Zakładki</b> → na dole <b>Edytuj</b> → stuknij zakładkę, którą właśnie dodałeś.
5. W pierwszym polu (nazwa) wpisz: <b>Booksy bot</b>
6. W drugim polu (adres) skasuj wszystko, przytrzymaj palec → <b>Wklej</b>.
7. Stuknij <b>Gotowe</b>.

<b>Android (Chrome):</b>
1. Stuknij szary kod na samym dole tej wiadomości. Skopiuje się.
2. W Chrome na dowolnej stronie: <b>⋮</b> → <b>☆</b> → <b>Edytuj</b>.
3. Nazwa: <b>Booksy bot</b>. Adres: skasuj wszystko i wklej kod. Zapisz.

<b>CZĘŚĆ 2 – za każdym razem, gdy bot napisze, że token wygasł</b>
1. W Safari/Chrome wejdź na <b>booksy.com</b> i zaloguj się (jak zawsze).
2. NIE otwieraj nowej karty. Na tej samej stronie Booksy:
   iPhone: <b>📖 Zakładki</b> → <b>Booksy bot</b>.
   Android: stuknij pasek adresu, wpisz <b>Booksy bot</b>, stuknij zakładkę z gwiazdką.
3. Wyskoczy okienko „OK…” → stuknij <b>OK</b>.
4. Stuknij na stronie Booksy cokolwiek, np. kategorię <b>Barber</b>.
5. Wyskoczy okienko z tekstem <code>/token …</code> → stuknij <b>OK</b>.
6. Otworzy się Telegram. W czacie z botem przytrzymaj palec → <b>Wklej</b> → <b>Wyślij</b>.
7. Bot odpisze „Token zapisany”. Gotowe.

Coś nie działa? Napisz, w którym kroku utknąłeś.

👇 KOD (stuknij, żeby skopiować):
<code>{code}</code>"""

FRIEND_HELP = """

<b>Twoje konto Booksy</b>
Bot korzysta z <b>Twojego</b> konta (regulamin Booksy: jedno konto na osobę). Raz na jakiś czas podłącz je:
/login – instrukcja dla telefonu. Wiadomość z tokenem od razu usuwam. Token daje dostęp do Twojego konta Booksy –
wysyłaj go tylko tu.
Limit: 3 powiadomienia, 1 autorezerwacja. Do tego samego terminu pierwszeństwo ma właściciel bota."""

OWNER_HELP = """

<b>Znajomi</b>
Znajomy pisze do bota /start i podaje hasło (BOT_PASSWORD na serwerze); 3 złe próby = blokada.
Znajomy korzysta z własnego konta Booksy, ale z Twojego IP – ryzyko blokady dotyczy też Ciebie.
/users – lista, /kick ID – usuń (z tokenem i powiadomieniami).
Nowy token Booksy z telefonu: /login."""

BOT_COMMANDS = [{"command": "list", "description": "Aktywne powiadomienia"},
                {"command": "visits", "description": "Twoje wizyty (odwołanie)"},
                {"command": "login", "description": "Podłącz konto Booksy z telefonu"},
                {"command": "help", "description": "Jak korzystać z bota"}]
