"""
Pokoj Trading: sygnaly z grupy TG -> analiza -> wirtualny portfel.

ZASADA NADRZEDNA: zero prawdziwych transakcji. Kazdy sygnal dostaje
wirtualne $100 i mierzymy, co by z tego bylo. Dopiero dobre statystyki
grupy otwieraja rozmowe o realnych swapach (zawsze z potwierdzeniem).

Dane: data/trading.json  (sygnaly + portfel)
Konfig czytnika grupy: secrets/telegram_user.json {api_id, api_hash, group}
Sesja Telethon: secrets/tg_user.session (tworzy connect_trading.py)
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA = BASE_DIR / "data" / "trading.json"
USER_CFG = BASE_DIR / "secrets" / "telegram_user.json"
SESSION = BASE_DIR / "secrets" / "tg_user"

VIRTUAL_BUY_USD = 100.0   # tyle "kupujemy" wirtualnie na kazdy sygnal

_lock = threading.RLock()
_reader_started = False


# ---------------- storage ----------------

def _load() -> dict:
    with _lock:
        if not DATA.exists():
            return {"signals": []}
        try:
            return json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            return {"signals": []}


def _save(d: dict) -> None:
    with _lock:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(DATA)


# ---------------- parser sygnalow ----------------

# Adres Solana (base58, 32-44 znakow) albo $TICKER
RE_SOL_ADDR = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
RE_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")

STOPWORDS = {"USD", "USDT", "USDC", "SOL", "BTC", "ETH", "PLN", "APY", "ATH"}


def parse_signal(text: str) -> list[dict]:
    """Wyciaga kandydatow (adresy/tickery) z wiadomosci grupy."""
    out = []
    for m in RE_SOL_ADDR.findall(text or ""):
        out.append({"kind": "address", "value": m})
    for m in RE_TICKER.findall(text or ""):
        if m.upper() not in STOPWORDS:
            out.append({"kind": "ticker", "value": m.upper()})
    return out


# ---------------- analiza (DexScreener + Jupiter) ----------------

def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "panel/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def analyze_token(query: str) -> dict | None:
    """Analiza tokena po adresie lub tickerze. Zwraca karte tokena albo None."""
    q = urllib.parse.quote(query)
    if RE_SOL_ADDR.fullmatch(query):
        url = f"https://api.dexscreener.com/latest/dex/tokens/{q}"
    else:
        url = f"https://api.dexscreener.com/latest/dex/search?q={q}"
    try:
        data = _http_json(url)
    except Exception:
        return None
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    # ranking: zywy wolumen wazniejszy niz sama plynnosc; przy tickerach
    # preferuj Solane (grupa i Jupiter to Solana)
    def _rank(p):
        vol = ((p.get("volume") or {}).get("h24")) or 0
        liq = (p.get("liquidity") or {}).get("usd") or 0
        sol_bonus = 1.5 if p.get("chainId") == "solana" else 1.0
        return (vol * 10 + liq) * sol_bonus
    pairs.sort(key=_rank, reverse=True)
    p = pairs[0]

    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol24 = ((p.get("volume") or {}).get("h24")) or 0
    price = float(p.get("priceUsd") or 0)
    chg1h = ((p.get("priceChange") or {}).get("h1")) or 0
    chg24 = ((p.get("priceChange") or {}).get("h24")) or 0
    created = p.get("pairCreatedAt")
    age_days = round((time.time() * 1000 - created) / 86400000, 1) if created else None

    # ocena ryzyka (proste, twarde progi)
    flags = []
    if liq < 20000: flags.append("plynnosc <$20k (latwy rug/slippage)")
    if age_days is not None and age_days < 3: flags.append(f"token ma {age_days} dnia")
    if vol24 < 10000: flags.append("wolumen 24h <$10k (martwy?)")
    if chg1h and chg1h > 50: flags.append(f"+{chg1h}% w 1h - pompa juz trwa")
    risk = "WYSOKIE" if len(flags) >= 2 else ("SREDNIE" if flags else "niskie")

    return {
        "symbol": (p.get("baseToken") or {}).get("symbol", query),
        "name": (p.get("baseToken") or {}).get("name", ""),
        "address": (p.get("baseToken") or {}).get("address", ""),
        "chain": p.get("chainId", "?"),
        "price": price,
        "liquidity_usd": round(liq),
        "volume24_usd": round(vol24),
        "change_1h": chg1h,
        "change_24h": chg24,
        "age_days": age_days,
        "risk": risk,
        "flags": flags,
        "dexscreener": p.get("url", ""),
    }


# Minimalna plynnosc, przy ktorej cena jest w ogole WYKONALNA dla naszej pozycji $100.
# Samo "liq > 0" NIE wystarcza - patrz $SMOLE (zXFvHdb...Ss2D, 27.07.2026): pula miala
# $0.02 plynnosci, a DexScreener podawal cene $26.91 przy wejsciu 0.001174 => "22921x".
# Zabezpieczenie GLITCH_MULT scinalo to do 100x, ale 100x na $100 to nadal fikcyjne
# $10 000, ktore samo jedno zawyzalo wynik CALYCH strategii (trailing "+441%",
# tp_sl "+163%", relaxed "+142%" - wszystkie z tej jednej pozycji).
MIN_LIQ_FOR_PRICE = 1_000.0


def price_and_liq(address: str, chain: str = "solana") -> tuple[float | None, float | None]:
    """Cena ORAZ plynnosc puli w jednym zapytaniu.

    Plynnosc jest potrzebna do policzenia REALNEGO poslizgu (zalezy od glebokosci
    puli wzgledem wielkosci pozycji), a nie plaskiej stalej. Zapisujemy ja przy
    wejsciu i wyjsciu z pozycji, zeby liczyc koszty na faktach - a nie odtwarzac
    po fakcie z migawek trackera, ktory przestaje sledzic token po rugu (co
    przechyla probke w strone tokenow, ktore przezyly)."""
    try:
        data = _http_json(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
        pairs = data.get("pairs") or []
        pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
        if pairs:
            p = pairs[0]
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if liq < MIN_LIQ_FOR_PRICE:
                return None, liq
            return (float(p.get("priceUsd") or 0) or None), liq
    except Exception:
        pass
    return None, None


def price_now(address: str, chain: str = "solana") -> float | None:
    """Aktualna cena tokena (DexScreener). Odrzuca odczyty przy znikomej plynnosci -
    bez realnej puli cena jest artefaktem, nie czyms wykonalnym (patrz: $Jimothy
    E4EdtvP...Tpump, 23.07.2026 - liq=0, cena "skoczyla" 5461x w 3.5 min i wygenerowala
    fikcyjny wynik +3868% w strategiach; oraz $SMOLE, 27.07.2026 - liq=$0.02)."""
    return price_and_liq(address, chain)[0]


# ---------------- sygnaly + wirtualny portfel ----------------

def entry_price_stable(address: str, first_price: float, chain: str = "solana",
                       wait_s: float = 8.0, tol: float = 0.25) -> bool:
    """Sprawdza, czy cena wejscia jest WIARYGODNA: drugi odczyt po kilku sekundach.

    DLACZEGO: na swiezo utworzonej puli DexScreener potrafi zwrocic zupelnie
    oderwana cene. Tak powstal fikcyjny wynik +3868% na $Jimothy - ta sama moneta
    zostala zapisana raz po 0.00005728, a raz po 0.01106 (roznica 193x!).
    Jedna taka smieciowa dana potrafila odwrocic wynik 119 transakcji z minusa
    na plus. Jesli dwa odczyty sie rozjezdzaja - pomijamy sygnal.
    """
    if not first_price or first_price <= 0:
        return False
    time.sleep(wait_s)
    second = price_now(address, chain)
    if not second or second <= 0:
        return False
    ratio = max(second / first_price, first_price / second)
    return ratio <= (1 + tol)


def record_signal(source: str, raw_text: str, card: dict) -> dict:
    """Zapisuje sygnal z wirtualnym kupnem $100 po cenie z chwili sygnalu."""
    d = _load()
    sig = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "raw": (raw_text or "")[:300],
        "token": card,
        "entry_price": card.get("price") or 0,
        "virtual_usd": VIRTUAL_BUY_USD,
    }
    # dedup: ten sam adres w ciagu 24h -> nie dubluj
    for s in d["signals"][:50]:
        if s["token"].get("address") == card.get("address"):
            t0 = datetime.strptime(s["time"], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - t0).total_seconds() < 86400:
                return s
    d["signals"].insert(0, sig)
    d["signals"] = d["signals"][:300]
    _save(d)
    return sig


# PRAWDZIWE moonshoty (10x, 50x, nawet ~100x) SA realne - ich nie wyrzucamy.
# Powyzej 100x (+10 000%) na tych mikro-tokenach to praktycznie zawsze bledny tick
# (cena wejscia zlapana jako ~0). Takie FLAGUJEMY (glitch) i nie liczymy do sum.
SANITY_MAX_MULT = 100.0


def portfolio() -> dict:
    """Wycena wirtualnego portfela: kazdy sygnal = $100 w chwili sygnalu.
    Odrzuca absurdalne odczyty (glitch swiezych tokenow) z sum."""
    d = _load()
    rows = []
    total_in = total_now = 0.0
    glitches = 0
    for s in d["signals"]:
        entry = s.get("entry_price") or 0
        addr = s["token"].get("address")
        cur = price_now(addr) if (addr and entry) else None
        bad = False
        dead = False
        if entry and cur:
            mult = cur / entry
            if mult > SANITY_MAX_MULT or mult < 0:
                bad = True                      # niewiarygodny odczyt (glitch)
                value, pnl = None, None
            else:
                value = s["virtual_usd"] * mult
                pnl = (mult - 1) * 100
        elif entry and not cur:
            # BRAK ceny moze byc rate-limit DexScreener, nie tylko rug.
            # Nie zgadujemy $0 - liczymy neutralnie (bez zmian), zeby nie falszowac.
            value, pnl = None, None
        else:
            value, pnl = None, None
        if entry and not bad:
            total_in += s["virtual_usd"]
            total_now += value if value is not None else s["virtual_usd"]
        if bad:
            glitches += 1
        rows.append({**s, "price_now": cur, "value_now": value,
                     "pnl_pct": pnl, "glitch": bad})
    return {
        "rows": rows,
        "total_in": round(total_in, 2),
        "total_now": round(total_now, 2),
        "total_pnl_pct": round((total_now / total_in - 1) * 100, 1) if total_in else 0,
        "glitches": glitches,
    }


def _is_known_clone(symbol: str | None, address: str | None, window_s: float = 172800) -> bool:
    """Token o tej samej nazwie/tickerze juz byl sygnalizowany na INNYM adresie
    w ciagu ostatnich `window_s` sekund (domyslnie 48h). Na Solanie/pump.fun
    kazdy moze stworzyc token o dowolnej nazwie - kopie pod hype oryginalu
    (patrz: 4 rozne kontrakty "$Jimothy" jednoczesnie w danych, 23.07.2026)
    sa czesto niskiej jakosci albo prostym oszustwem. Lepiej przegapic niz
    zafalszowac dane kolejnym duplikatem nazwy."""
    if not symbol:
        return False
    d = _load()
    now = time.time()
    for s in d["signals"][:150]:
        tok = s.get("token", {})
        if tok.get("symbol") != symbol or tok.get("address") == address:
            continue
        try:
            t0 = time.mktime(time.strptime(s["time"], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if now - t0 < window_s:
            return True
    return False


def handle_group_message(text: str, source: str = "grupa") -> list[dict]:
    """Pelny obieg: tekst -> parser -> analiza -> zapis -> alert TG."""
    from core import state, telegram_bot
    found = parse_signal(text)
    cards = []
    seen = set()
    for f in found[:3]:   # max 3 tokeny z jednej wiadomosci
        card = analyze_token(f["value"])
        if not card or card.get("address") in seen:
            continue
        seen.add(card.get("address"))
        # ANTY-KLON: ta sama nazwa co token juz sygnalizowany na innym adresie
        # w ciagu ostatnich 48h -> pomijamy (patrz docstring _is_known_clone).
        if _is_known_clone(card.get("symbol"), card.get("address")):
            state.add_report(
                "trading", f"Pominieto ${card.get('symbol', '?')} - mozliwy klon nazwy",
                f"Token o tej samej nazwie byl juz sygnalizowany na innym adresie "
                f"w ciagu ostatnich 48h. Kopie pod hype oryginalu sa czesto niskiej "
                f"jakosci lub oszustwem - pomijam, zeby nie zasmiecac danych.",
                needs_action=False)
            continue
        # BRAMKA JAKOSCI DANYCH: nie otwieramy pozycji na niepewnej cenie.
        # Lepiej stracic okazje niz zasmiecic statystyki fikcyjnym wynikiem.
        if not entry_price_stable(card.get("address"), card.get("price"),
                                  card.get("chain", "solana")):
            state.add_report(
                "trading", f"Pominieto ${card.get('symbol', '?')} - niepewna cena",
                "Dwa odczyty ceny rozjechaly sie o ponad 25% w kilka sekund "
                "(swieza pula albo zly odczyt zrodla). Nie otwieram pozycji, "
                "zeby nie zafalszowac statystyk.", needs_action=False)
            continue
        sig = record_signal(source, text, card)
        try:
            from core import strategies, tracker
            strategies.enter_all(card)          # pojedynek strategii (paper forward)
            tracker.track(card["address"], card["symbol"], source, card)
        except Exception:
            pass
        cards.append(card)
        flags = ("\n".join("  ! " + fl for fl in card["flags"])) or "  (brak ostrzezen)"
        msg = (f"SYGNAL: ${card['symbol']} ({card['chain']})\n"
               f"Cena: ${card['price']:.8g} | 1h: {card['change_1h']}% | 24h: {card['change_24h']}%\n"
               f"Plynnosc: ${card['liquidity_usd']:,} | Vol24: ${card['volume24_usd']:,}"
               f" | Wiek: {card['age_days']} dni\n"
               f"RYZYKO: {card['risk']}\n{flags}\n"
               f"Wirtualnie kupiono $100 (paper trading).\n{card['dexscreener']}")
        telegram_bot.send(msg)
        state.add_report("trading", f"Sygnal: ${card['symbol']} (ryzyko: {card['risk']})",
                         msg, needs_action=False, channel=source)
        # bierzemy token pod obserwacje w czasie (trajektoria + rug-alarm)
        try:
            from core import tracker
            if card.get("address"):
                tracker.track(card["address"], card["symbol"], source, card)
        except Exception:
            pass
    return cards


# ---------------- czytnik grupy (Telethon) ----------------

def _groups(cfg: dict) -> list:
    """Lista grup - obsluguje stary klucz 'group' i nowy 'groups'."""
    if cfg.get("groups"):
        return list(cfg["groups"])
    if cfg.get("group"):
        return [cfg["group"]]
    return []


def reader_configured() -> bool:
    if not USER_CFG.exists():
        return False
    try:
        c = json.loads(USER_CFG.read_text(encoding="utf-8"))
        return bool(c.get("api_id") and c.get("api_hash") and _groups(c))
    except Exception:
        return False


def session_exists() -> bool:
    return SESSION.with_suffix(".session").exists()


def start_reader() -> None:
    """Startuje watek Telethon nasluchujacy grupy (gdy skonfigurowano)."""
    global _reader_started
    if _reader_started or not (reader_configured() and session_exists()):
        return
    _reader_started = True

    def _run():
        import asyncio
        from telethon import TelegramClient, events
        from core import state

        cfg = json.loads(USER_CFG.read_text(encoding="utf-8"))
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = TelegramClient(str(SESSION), int(cfg["api_id"]), cfg["api_hash"],
                                loop=loop)

        groups = _groups(cfg)

        @client.on(events.NewMessage(chats=groups))
        async def _on_msg(event):
            try:
                try:
                    chat = await event.get_chat()
                    src = getattr(chat, "title", None) or "grupa"
                except Exception:
                    src = "grupa"
                handle_group_message(event.raw_text or "", source=src)
            except Exception:
                import traceback
                state.add_report("trading", "Blad obslugi sygnalu",
                                 traceback.format_exc(), needs_action=True)

        async def _main():
            await client.start()
            names = ", ".join(cfg.get("group_names") or [str(g) for g in groups])
            state.add_report("trading", "Czytnik grup POLACZONY",
                             f"Nasluchuje: {names}. Kazdy token z tych grup "
                             "dostanie analize + wpis do wirtualnego portfela.")
            await client.run_until_disconnected()

        try:
            loop.run_until_complete(_main())
        except Exception:
            import traceback
            state.add_report("trading", "Czytnik grupy padl", traceback.format_exc(),
                             needs_action=True)

    threading.Thread(target=_run, daemon=True).start()
