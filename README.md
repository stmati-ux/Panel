# Panel — lokalny dashboard do automatyzacji procesów

Aplikacja Flask, która zarządza kilkoma niezależnymi obszarami automatyzacji
z jednego interfejsu i utrzymuje je w ruchu bez nadzoru. Powstała jako narzędzie
operacyjne do realnego użytku — nie jako projekt demonstracyjny — i działa
w trybie ciągłym.

Serwer nasłuchuje wyłącznie na `127.0.0.1`: panel jest z założenia prywatny
i niedostępny z sieci.

---

## Problem, który rozwiązuje

Kilka procesów wymagało stałego pilnowania: publikacji treści według harmonogramu,
monitoringu danych rynkowych w krótkich odstępach, restartu ciężkich procesów
zewnętrznych po awarii. Każdy z nich miał osobne skrypty, osobne logi i wymagał
obecności przy komputerze.

Panel scala to w jeden system z jednym stanem, jednym wyłącznikiem awaryjnym
i powiadomieniami na telefon.

---

## Architektura

Aplikacja dzieli się na **pokoje** — niezależne obszary z własną logiką i widokiem,
spięte wspólnym nadzorcą i wspólnym stanem.

```
app.py                    serwer Flask, routing, widoki pokoi
core/
  supervisor.py           nadzorca: rozdziela zadania, pilnuje STOP-u i trybu pracy
  state.py                stan aplikacji (JSON) — blokady wątków, zapis atomowy
  control.py              wspólne akcje sterujące dla panelu i Telegrama
  telegram_bot.py         bot: powiadomienia push, komendy, sterowanie z telefonu
  watchdog.py             monitoring procesu zewnętrznego + automatyczny restart

  # pokój YouTube
  scheduler.py            autonomiczna publikacja wg harmonogramu
  publisher.py            wybór materiału, wykrywanie duplikatów, rejestr wysyłek
  youtube_api.py          OAuth 2.0, YouTube Data API
  generator.py            orkiestracja zewnętrznych pipeline'ów produkcyjnych

  # pokój Trading (paper trading, zero realnych pieniędzy)
  radar.py                cykliczny skan rynku z filtrowaniem progowym
  trading.py              portfel wirtualny, czytnik sygnałów z Telegrama
  tracker.py              migawki cen/płynności, wykrywanie anomalii
  strategies.py           równoległe testowanie 7 strategii wyjścia
  rugcheck.py             weryfikacja on-chain (blokada płynności, uprawnienia)

templates/                widoki (Jinja2)
```

**Tryby pracy.** `setup` przygotowuje i raportuje plan, ale niczego nie wykonuje
na zewnątrz; `live` działa realnie. Czerwony **STOP** wstrzymuje wszystkie zadania
natychmiast, z panelu albo z Telegrama.

---

## Rzeczy, które warto obejrzeć w kodzie

**Trwałość stanu — [`core/state.py`](core/state.py)**
Stan aplikacji zapisywany jest atomowo (zapis do pliku tymczasowego + `replace`)
pod blokadą wątków. Dzięki temu przerwanie procesu w trakcie zapisu nie zostawia
uszkodzonego pliku — istotne, bo zapisuje kilka wątków w tle jednocześnie.

**Rzetelność danych — [`core/trading.py`](core/trading.py), [`core/strategies.py`](core/strategies.py)**
Najciekawsza część projektu. System dwukrotnie pokazywał świetne wyniki, które
okazały się artefaktami pomiaru:

- odczyty cen pochodzące z rynków bez realnej płynności generowały fikcyjne zyski
  (jedna pozycja: +3868%), przewracając statystyki wszystkich strategii naraz;
- zabezpieczenie ścinające podejrzany odczyt do stałego limitu nadal doliczało
  fikcyjną kwotę — ograniczenie wartości nie wystarcza, taki odczyt trzeba
  **odrzucić**, nie przyciąć.

Poprawki są w kodzie opisane wraz z przyczyną i datą, żeby nikt (łącznie z autorem)
nie cofnął ich przypadkiem. Model kosztów transakcyjnych liczony jest z faktycznej
głębokości rynku zamiast stałego założenia.

**Odporność na awarie — [`core/watchdog.py`](core/watchdog.py)**
Monitoruje ciężki proces zewnętrzny i podnosi go po awarii, ale rozróżnia
„nie odpowiada, bo pracuje" od „nie żyje" — restart następuje dopiero po dwóch
potwierdzeniach, żeby nie przerwać trwającego zadania.

**Integracje zewnętrzne**
YouTube Data API (OAuth 2.0, planowane publikacje, wykrywanie duplikatów tytułów),
Telegram (Bot API + Telethon), DexScreener, GeckoTerminal, RugCheck.
Obsługa limitów zapytań, timeoutów i odpowiedzi częściowych.

---

## Uruchomienie

```bash
pip install -r requirements.txt
python app.py
```

Panel: **http://127.0.0.1:5000**

Konfiguracja integracji trafia do katalogu `secrets/` (nieśledzonego przez git):

| plik | zawartość |
|---|---|
| `client_secret.json` | OAuth client z Google Cloud (Desktop app) |
| `token_<kanal>.json` | tworzony automatycznie po autoryzacji |
| `telegram.json` | token bota z @BotFather |
| `telegram_user.json` | `api_id` / `api_hash` do czytnika grup |

Pomocnicze: `connect_youtube.py` i `connect_trading.py` przeprowadzają przez
autoryzację krok po kroku.

---

## Uwagi

Pokój Trading działa **wyłącznie na wirtualnych pieniądzach**. Zasada zapisana
w kodzie: realne środki wchodzą w grę dopiero po udowodnieniu przewagi na
odpowiednio dużej próbce. Do dziś żadna strategia tego progu nie przekroczyła —
i tak jest to opisane w wynikach.

Projekt powstawał iteracyjnie, pod bieżące potrzeby. Część rozwiązań jest
świadomie prosta (stan w JSON zamiast bazy, brak warstwy ORM), bo skala tego
nie wymagała.
