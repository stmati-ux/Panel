"""
POJEDYNEK STRATEGII (paper, na żywo do przodu).

Każdy nowy sygnał (z grupy albo radaru) otwiera pozycję $100 w KAŻDEJ strategii.
Każda strategia zamyka pozycję wg WŁASNEJ reguły. Porównujemy, która zarabia.

Strategie:
- "hold"       — Trzymaj (nigdy nie sprzedaje) — punkt odniesienia.
- "sell_10min" — Sprzedaj po ~10 min (wyuczona reguła).
- "adaptive"   — Uczy się: sprzedaje po tylu minutach, ile AKTUALNIE wychodzi
                 najlepiej z symulacji (tracker.exit_strategies). "Uczy się od
                 pozostałych" — bierze najlepszy realny czas wyjścia z danych.

To jest TEST W PRZÓD: reguła musi potwierdzić się na nowych sygnałach, nie tylko
wstecz. Dopiero stabilny plus tu → rozmowa o prawdziwych pieniądzach.

Dane: data/strategies.json
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "strategies.json"
VIRTUAL_USD = 100.0
GLITCH_MULT = 100.0   # spójnie z trackerem: >100x = błędny odczyt, pomijamy

_lock = threading.RLock()
_started = False

DEFS = {
    "sell_10min":  {"name": "Sprzedaj po 10 min",      "kind": "time", "minutes": 10},
    "adaptive":    {"name": "Adaptacyjna (uczy się)",  "kind": "adaptive"},
    "strict_10min": {"name": "FILTR antyrug + sprzedaj 10 min",
                     "kind": "time", "minutes": 10, "filter": "strict"},
    # Wariant testowy: sama plynnosc, BEZ RugCheck. Sprawdza, czy przewaga
    # strategii "strict" bierze sie z RugCheck, czy wystarczy przyzwoita plynnosc.
    "relaxed_10min": {"name": "GŁĘBOKA PULA (≥$100k) + sprzedaj 10 min",
                      "kind": "time", "minutes": 10, "filter": "relaxed"},
    # Zamiast sztywnego czasu: wyjscie gdy trafi TP/SL, albo timeout po max_minutes.
    # Test hipotezy: sztywne 10 min sprzedaje za wczesnie w trakcie pompy (nie
    # lapie gory) i za pozno po zjezdzie (nie tnie strat) - patrz analiza 23.07.2026.
    "tp_sl": {"name": "Take-profit 50% / stop-loss 25%", "kind": "tp_sl",
              "tp_pct": 50.0, "sl_pct": -25.0, "max_minutes": 15},
    # Zlecenie LIMIT zamiast market: nie wchodzimy po cenie sygnalu, tylko
    # skladamy zlecenie -5% pod nia i czekamy az rynek zejdzie do tego poziomu
    # (albo nie wejdziemy wcale). Test: czy dyscyplina wejscia poprawia wynik
    # netto, czy tylko zmniejsza liczbe transakcji bez realnej roznicy.
    "limit_5pct": {"name": "Zlecenie limit -5% + sprzedaj 10 min", "kind": "limit",
                   "limit_offset_pct": 5.0, "fill_timeout_min": 10, "minutes": 10},
    # Trailing stop: zamiast sztywnego TP/SL, stop-loss "podaza" za szczytem ceny.
    # Przed aktywacja (+20%) chroni twardy stop-loss (-25%, jak w tp_sl). Po
    # aktywacji: wyjscie gdy cena spadnie trail_pct% od najwyzszego dotychczas
    # punktu - lapie gore pompy bez sztywnego sufitu i bez oddawania calego
    # zysku przy odwrocie. Timeout 30 min jako zabezpieczenie.
    "trailing": {"name": "Trailing stop (aktywacja +20%, trail 15%)",
                 "kind": "trailing", "activate_pct": 20.0, "trail_pct": 15.0,
                 "hard_sl_pct": -25.0, "max_minutes": 30},
}
# WYGASZONE: "hold" (Trzymaj) - lekcja odrobiona, -66% na 97 pozycjach.
# HOUSEM: sprzedany po 11 min z +27%, dzis -99,7% od wyjscia (rug). Trzymanie zabija.
# Historia zostaje w data/strategies.json, ale strategia nie otwiera juz nowych pozycji.


# ===== REALNE KOSZTY TRANSAKCJI (poslizg + oplaty) =====
# Bez tego paper trading klamie: pokazuje "czysta" cene, ktorej realnie nie dostaniesz.
# Na pump.fun/Solana przy $100 wejscia w token o plynnosci 8-50k:
SLIPPAGE_PCT = 1.5   # poslizg na KAZDA strone (kupujesz drozej, sprzedajesz taniej)
FEE_PCT = 0.5        # oplata DEX/Jupiter + priority fee, na kazda strone
COST_SIDE = (SLIPPAGE_PCT + FEE_PCT) / 100.0   # 2% na strone => ~3.9% w obie


def net_mult(gross_mult: float) -> float:
    """Mnoznik NETTO po kosztach: kupno drozej o COST_SIDE, sprzedaz taniej o COST_SIDE."""
    return gross_mult * (1 - COST_SIDE) / (1 + COST_SIDE)


def net_pnl_pct(gross_pnl_pct: float) -> float:
    """Przelicza zysk brutto (%) na NETTO (%) po poslizgach i oplatach."""
    return round((net_mult(1 + gross_pnl_pct / 100.0) - 1) * 100, 1)


# ===== KOSZT ZALEZNY OD GLEBOKOSCI PULI (drugi, dokladniejszy model) =====
# Plaskie SLIPPAGE_PCT nie zalezy od tego, jak duza jest pula wzgledem pozycji -
# a to wlasnie decyduje o poslizgu. Na AMM stalego iloczynu jedna strona puli to
# ~liq/2, wiec poslizg ~ 2*pozycja/liq. Pomiar na 27-29.07.2026: realne pule mialy
# $30k-$860k => poslizg 0.0-0.7%, czyli plaskie 1.5% ZAWYZALO koszty o ~2.4 pp.
# Ale gdy pula wysycha miedzy wejsciem a wyjsciem, koszt wyjscia rosnie drastycznie -
# i to jest przypadek, ktorego plaski model w ogole nie widzi.
# UWAGA: ten model liczymy ROWNOLEGLE (pole pnl_net_liq_pct), NIE zastepujemy nim
# dotychczasowego - inaczej stracilibysmy porownywalnosc z juz zebranymi danymi.
DEX_FEE_PCT = 0.5          # oplata DEX + priority fee, na strone
MAX_SLIPPAGE_PCT = 50.0    # sufit: przy wyschnietej puli wyjscie jest nierealne


def slippage_pct_for_liq(liq: float | None) -> float | None:
    """Poslizg (%) na JEDNA strone dla pozycji VIRTUAL_USD w puli o danej plynnosci.
    None = brak danych o plynnosci (NIE zgadujemy, nie karzemy domyslna wartoscia -
    taka kara raz juz zafalszowala analize, 29.07.2026)."""
    if liq is None or liq <= 0:
        return None
    return min(2.0 * VIRTUAL_USD / liq * 100.0, MAX_SLIPPAGE_PCT)


def net_pnl_liq_pct(gross_pnl_pct: float, entry_liq: float | None,
                    exit_liq: float | None) -> float | None:
    """Wynik NETTO wg glebokosci puli przy wejsciu i wyjsciu. None gdy brak danych."""
    s_in = slippage_pct_for_liq(entry_liq)
    s_out = slippage_pct_for_liq(exit_liq)
    if s_in is None or s_out is None:
        return None
    c_in = (s_in + DEX_FEE_PCT) / 100.0
    c_out = (s_out + DEX_FEE_PCT) / 100.0
    gross_mult = 1 + gross_pnl_pct / 100.0
    return round((gross_mult * (1 - c_in) / (1 + c_out) - 1) * 100, 1)


# ===== FILTR ANTYRUG ON-CHAIN (dziala na SWIEZYCH tokenach, bez historii) =====
# NIE patrzymy na wiek (bo wtedy moonshot juz minal) - patrzymy czy token DA SIE
# zrugnac: zablokowana plynnosc + brak zagrozen on-chain (RugCheck.xyz).
# Minimalna plynnosc zeby w ogole dalo sie handlowac (nie martwy token):
FILTER_MIN_LIQ = 8_000


def passes_strict_filter(card: dict) -> bool:
    """Filtr antyrug: on-chain (RugCheck) - LP zablokowana + brak zagrozen.
    Dziala na swiezych tokenach bez historii cen."""
    from core import rugcheck
    liq = card.get("liquidity_usd") or 0
    if liq < FILTER_MIN_LIQ:      # totalnie martwy/pusty token - odrzuc
        return False
    addr = card.get("address")
    if not addr:
        return False
    return rugcheck.is_safe(addr)


# Wariant GLEBOKIEJ PULI (do 29.07.2026 nazywany "luznym", prog $15k).
# DLACZEGO ZMIANA: radar i tak wymaga plynnosci >= $30k, wiec prog $15k przepuszczal
# 100% sygnalow - wariant mial DOKLADNIE te same 20 tokenow co "sell_10min", czyli
# nie testowal niczego i marnowal miejsce w pojedynku.
# Nowy prog $100k (mediana realnych pul z 27-29.07 to ok. $80k, wiec dzieli probke
# mniej wiecej na pol - obie grupy zbieraja dane w podobnym tempie).
# HIPOTEZA DO SPRAWDZENIA: czy wchodzenie tylko w GRUBE pule realnie poprawia wynik?
# Grubsza pula = mniejszy poslizg na wejsciu I wyjsciu (poslizg ~ 2*pozycja/plynnosc)
# oraz mniejsza szansa, ze pula wyschnie zanim zdazymy wyjsc.
FILTER_DEEP_MIN_LIQ = 100_000
FILTER_RELAXED_MIN_LIQ = FILTER_DEEP_MIN_LIQ   # alias wstecznie zgodny


def passes_relaxed_filter(card: dict) -> bool:
    return (card.get("liquidity_usd") or 0) >= FILTER_DEEP_MIN_LIQ


def _load() -> dict:
    with _lock:
        if not DATA.exists():
            base = {"strategies": {k: {"name": v["name"], "positions": []}
                                   for k, v in DEFS.items()}}
            return base
        try:
            return json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            return {"strategies": {k: {"name": v["name"], "positions": []}
                                   for k, v in DEFS.items()}}


def _save(d: dict) -> None:
    with _lock:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(DATA)


def enter_all(card: dict) -> None:
    """Nowy sygnał -> otwiera pozycję $100 w każdej strategii."""
    entry = card.get("price") or 0
    if entry <= 0:
        return
    addr = card.get("address")
    sym = card.get("symbol", "?")
    now = time.time()
    d = _load()
    # filtry liczymy RAZ na sygnal (RugCheck to zapytanie sieciowe)
    ok = {"strict": passes_strict_filter(card),
          "relaxed": passes_relaxed_filter(card)}
    for key, sdef in DEFS.items():
        s = d["strategies"].setdefault(key, {"name": sdef["name"], "positions": []})
        # strategie z filtrem biora TYLKO tokeny przechodzace SWOJ filtr
        f = sdef.get("filter")
        if f and not ok.get(f):
            continue
        # dedup: ten sam token otwarty w ciagu 24h -> nie dubluj
        if any(p["address"] == addr and now - p["entry_ts"] < 86400
               for p in s["positions"]):
            continue
        if sdef["kind"] == "limit":
            # nie wchodzimy po cenie sygnalu - skladamy zlecenie ponizej i czekamy na fill
            limit_price = entry * (1 - sdef["limit_offset_pct"] / 100.0)
            s["positions"].insert(0, {
                "symbol": sym, "address": addr,
                "signal_price": entry, "limit_price": limit_price,
                "entry_price": None, "entry_ts": now,
                "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "entry_liq": card.get("liquidity_usd"),
                "status": "pending", "exit_price": None, "exit_ts": None, "pnl_pct": None,
                "fill_ts": None,
            })
        else:
            s["positions"].insert(0, {
                "symbol": sym, "address": addr,
                "entry_price": entry, "entry_ts": now,
                "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "entry_liq": card.get("liquidity_usd"),
                "status": "open", "exit_price": None, "exit_ts": None, "pnl_pct": None,
            })
        s["positions"] = s["positions"][:500]
    _save(d)


def _best_adaptive_minutes() -> int:
    """Ile minut trzymac wg AKTUALNIE najlepszej czasowej reguly z danych."""
    try:
        from core import tracker
        ex = tracker.exit_strategies()
        best_min, best_val = 10, -1e9
        for s in ex.get("strategies", []):
            # bierzemy tylko reguly czasowe "sprzedaj po X min"
            if s["name"].startswith("sprzedaj po") and "min" in s["name"]:
                mins = int("".join(c for c in s["name"] if c.isdigit()))
                if s["avg_pnl_pct"] > best_val:
                    best_val, best_min = s["avg_pnl_pct"], mins
        return best_min
    except Exception:
        return 10


def _tick() -> None:
    from core import trading
    d = _load()
    adaptive_min = _best_adaptive_minutes()
    now = time.time()
    changed = False
    price_cache = {}

    def _get(addr):
        """(cena, plynnosc) z jednego zapytania, cache na tick."""
        if addr not in price_cache:
            price_cache[addr] = trading.price_and_liq(addr)
        return price_cache[addr]

    def _mark_exit(p, liq):
        """Zapisuje plynnosc przy wyjsciu + rownolegly wynik wg glebokosci puli."""
        p["exit_liq"] = liq
        p["pnl_net_liq_pct"] = net_pnl_liq_pct(p["pnl_pct"], p.get("entry_liq"), liq)

    for key, sdef in DEFS.items():
        s = d["strategies"].get(key)
        if not s:
            continue
        for p in s["positions"]:
            kind = sdef["kind"]

            if kind == "limit":
                addr = p["address"]
                if p["status"] == "pending":
                    age_min = (now - p["entry_ts"]) / 60
                    cur, liq = _get(addr)
                    if cur and cur <= p["limit_price"]:
                        p["status"] = "open"
                        p["entry_price"] = cur          # wypelnione na limicie (lub lepiej)
                        p["fill_ts"] = now
                        p["entry_ts"] = now             # zegar trzymania startuje od wypelnienia
                        p["entry_liq"] = liq            # realne wejscie to FILL, nie sygnal
                        changed = True
                    elif age_min >= sdef["fill_timeout_min"]:
                        p["status"] = "expired"          # zlecenie nigdy sie nie wypelnilo
                        changed = True
                    continue
                elif p["status"] == "open":
                    age_min = (now - p["entry_ts"]) / 60
                    if age_min >= sdef.get("minutes", 10):
                        cur, liq = _get(addr)
                        if cur and p["entry_price"]:
                            mult = cur / p["entry_price"]
                            if mult > GLITCH_MULT:
                                continue
                            p["status"] = "closed"
                            p["exit_price"] = cur
                            p["exit_ts"] = now
                            p["pnl_pct"] = round((mult - 1) * 100, 1)
                            p["pnl_net_pct"] = round((net_mult(mult) - 1) * 100, 1)
                            _mark_exit(p, liq)
                            changed = True
                    continue
                else:
                    continue

            if p["status"] != "open":
                continue

            if kind == "trailing":
                addr = p["address"]
                cur, liq = _get(addr)
                if not (cur and p["entry_price"]):
                    continue
                mult = cur / p["entry_price"]
                if mult > GLITCH_MULT:
                    continue
                pnl_now = (mult - 1) * 100
                age_min = (now - p["entry_ts"]) / 60

                peak_mult = max(p.get("peak_mult", 1.0), mult)
                if peak_mult != p.get("peak_mult"):
                    p["peak_mult"] = peak_mult
                    changed = True
                peak_pnl = (peak_mult - 1) * 100
                drawdown_pct = (mult / peak_mult - 1) * 100   # <= 0

                activated = peak_pnl >= sdef["activate_pct"]
                hit_hard_sl = (not activated) and pnl_now <= sdef["hard_sl_pct"]
                hit_trail = activated and drawdown_pct <= -sdef["trail_pct"]
                timed_out = age_min >= sdef["max_minutes"]

                if hit_hard_sl or hit_trail or timed_out:
                    p["status"] = "closed"
                    p["exit_price"] = cur
                    p["exit_ts"] = now
                    p["pnl_pct"] = round(pnl_now, 1)
                    p["pnl_net_pct"] = round((net_mult(mult) - 1) * 100, 1)
                    _mark_exit(p, liq)
                    p["exit_reason"] = ("trailing-stop" if hit_trail else
                                         ("stop-loss" if hit_hard_sl else "timeout"))
                    changed = True
                continue

            if kind == "tp_sl":
                addr = p["address"]
                cur, liq = _get(addr)
                if not (cur and p["entry_price"]):
                    continue   # brak wiarygodnej ceny (albo liq=0) - sprobuj nastepnym tickiem
                mult = cur / p["entry_price"]
                if mult > GLITCH_MULT:
                    continue
                pnl_now = (mult - 1) * 100
                age_min = (now - p["entry_ts"]) / 60
                hit_tp = pnl_now >= sdef["tp_pct"]
                hit_sl = pnl_now <= sdef["sl_pct"]
                timed_out = age_min >= sdef["max_minutes"]
                if hit_tp or hit_sl or timed_out:
                    p["status"] = "closed"
                    p["exit_price"] = cur
                    p["exit_ts"] = now
                    p["pnl_pct"] = round(pnl_now, 1)
                    p["pnl_net_pct"] = round((net_mult(mult) - 1) * 100, 1)
                    _mark_exit(p, liq)
                    p["exit_reason"] = "take-profit" if hit_tp else ("stop-loss" if hit_sl else "timeout")
                    changed = True
                continue

            hold_min = (sdef.get("minutes") if kind == "time"
                        else adaptive_min if kind == "adaptive" else None)
            if hold_min is None:      # "hold" nigdy nie zamyka
                continue
            age_min = (now - p["entry_ts"]) / 60
            if age_min >= hold_min:
                # zamknij po biezacej cenie
                addr = p["address"]
                cur, liq = _get(addr)
                if cur and p["entry_price"]:
                    mult = cur / p["entry_price"]
                    if mult > GLITCH_MULT:   # błędny tick -> nie zamykaj na śmieciu
                        continue
                    p["status"] = "closed"
                    p["exit_price"] = cur
                    p["exit_ts"] = now
                    p["pnl_pct"] = round((mult - 1) * 100, 1)                 # brutto
                    p["pnl_net_pct"] = round((net_mult(mult) - 1) * 100, 1)   # po kosztach
                    _mark_exit(p, liq)
                    changed = True
    if changed:
        _save(d)


def _tracker_last_prices() -> tuple[dict, set]:
    """Ostatnia znana cena z migawek trackera + zbior adresow potwierdzonych rugow.
    Uzywane gdy DexScreener chwilowo nie zwraca ceny (rate limit) - zamiast zgadywac."""
    try:
        from core import tracker
        toks = tracker._load().get("tokens", {})
    except Exception:
        return {}, set()
    last = {}
    rugged = set()
    for addr, t in toks.items():
        snaps = t.get("snaps") or []
        if snaps:
            last[addr] = snaps[-1].get("price")
        if t.get("rug"):
            rugged.add(addr)
    return last, rugged


def standings() -> list[dict]:
    """Ranking strategii: wartość portfela + statystyki. Do wyświetlenia."""
    from core import trading
    d = _load()
    out = []
    price_cache = {}
    last_known, rugged_addrs = _tracker_last_prices()
    for key, sdef in DEFS.items():
        s = d["strategies"].get(key, {"positions": []})
        pos = s.get("positions", [])
        total_in = 0.0
        total_val = 0.0
        closed = [p for p in pos if p["status"] == "closed"]
        openp = [p for p in pos if p["status"] == "open"]
        wins = 0
        gross_val = 0.0          # ile byloby BEZ poslizgow/oplat (do porownania)
        net_list = []            # do MEDIANY - odporna na pojedyncze wyskoki
        for p in closed:
            if p.get("pnl_pct") is None:
                continue
            total_in += VIRTUAL_USD
            net = p.get("pnl_net_pct")
            if net is None:      # pozycje sprzed wprowadzenia kosztow - przelicz
                net = net_pnl_pct(p["pnl_pct"])
            total_val += VIRTUAL_USD * (1 + net / 100)
            gross_val += VIRTUAL_USD * (1 + p["pnl_pct"] / 100)
            net_list.append(net)
            if net > 0:          # wygrana = na plusie PO kosztach
                wins += 1
        # pozycje otwarte wyceniamy biezaco
        dead = 0
        for p in openp:
            addr = p["address"]
            if addr not in price_cache:
                price_cache[addr] = trading.price_now(addr)
            cur = price_cache[addr]
            # Fallback gdy DexScreener chwilowo nie zwraca ceny (rate limit przy
            # wielu tokenach naraz): uzyj ostatniej znanej ceny z migawek trackera.
            if not cur and addr in last_known:
                cur = last_known[addr]
            total_in += VIRTUAL_USD
            raw_mult = (cur / p["entry_price"]) if (cur and p["entry_price"]) else None
            # NIE scinamy podejrzanego odczytu do GLITCH_MULT - scarty 100x to nadal
            # fikcyjne $10 000, ktore samo jedno przewraca wynik calej strategii
            # (patrz $SMOLE 27.07.2026). Taki odczyt traktujemy jak BRAK ceny.
            if raw_mult is not None and raw_mult > GLITCH_MULT:
                raw_mult = None
            if raw_mult is not None:
                total_val += VIRTUAL_USD * net_mult(raw_mult)   # zeby wyjsc, tez placisz
                gross_val += VIRTUAL_USD * raw_mult
            elif addr in rugged_addrs:
                total_val += 0.0        # potwierdzony rug = ~$0
                gross_val += 0.0
                dead += 1
            else:
                # brak jakiejkolwiek ceny i NIE potwierdzony rug -> nie zgaduj,
                # licz jak wejscie (neutralnie), zeby nie falszowac wyniku.
                total_val += VIRTUAL_USD
                gross_val += VIRTUAL_USD
        pnl = round((total_val / total_in - 1) * 100, 1) if total_in else 0.0
        pnl_gross = round((gross_val / total_in - 1) * 100, 1) if total_in else 0.0
        # MEDIANA - typowa transakcja. Odporna na pojedyncze wyskoki: jeden
        # smieciowy odczyt (+3868% na $Jimothy) potrafil odwrocic SREDNIA ze 119
        # transakcji z minusa na plus, ale mediany nie rusza. To jest uczciwsza
        # miara tego, czego naprawde mozna sie spodziewac po jednej transakcji.
        med = None
        if net_list:
            xs = sorted(net_list)
            n = len(xs)
            med = round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2, 1)
        winrate = round(wins / len(closed) * 100) if closed else None
        note = ""
        if key == "adaptive":
            note = f"trzyma teraz {_best_adaptive_minutes()} min"
        out.append({
            "key": key, "name": sdef["name"],
            "in": round(total_in), "val": round(total_val, 1), "pnl_pct": pnl,
            "pnl_gross_pct": pnl_gross,   # bez kosztow - do porownania
            "median_pct": med,            # TYPOWA transakcja (odporna na wyskoki)
            "closed": len(closed), "open": len(openp), "winrate": winrate,
            "dead": dead, "note": note,
        })
    out.sort(key=lambda x: x["pnl_pct"], reverse=True)
    return out


def _loop() -> None:
    time.sleep(30)
    while True:
        try:
            _tick()
        except Exception:
            pass
        time.sleep(60)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
