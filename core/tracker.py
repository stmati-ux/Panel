"""
TRACKER: sledzenie kazdego wykrytego tokena w CZASIE + wykrywanie rugpulli.

Kazdy token z radaru/grupy trafia tu pod obserwacje. Co kilka minut
zapisujemy migawke (cena, plynnosc, wolumen) do historii. Z tej historii
liczymy:
  - kiedy realnie wystrzelil (pierwsza swieca momentum),
  - gdzie byl szczyt (max cena i ile po wykryciu),
  - czy trwa, czy juz spada,
  - ALARM RUGPULL: nagly spadek plynnosci (tworca wyciaga kase).

Dane: data/tracked.json  (jeden wpis na token, z lista migawek).

Cel: po tygodniach mamy TWARDE dane, ktore odpowiadaja na pytania:
"ile srednio od wykrycia do szczytu?", "jaki % hitow to rugi?",
"jaka regula wejscia/wyjscia zarabia najwiecej" - bez ryzykowania kasy.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from core import trading

DATA = Path(__file__).resolve().parent.parent / "data" / "tracked.json"

SNAP_EVERY_S = 180           # migawka co 3 min
TRACK_HOURS = 48             # sledzimy token przez 48h od wykrycia
RUG_LIQ_DROP = 0.5           # spadek plynnosci o >=50% miedzy migawkami = alarm rug

_lock = threading.RLock()
_started = False


def _load() -> dict:
    with _lock:
        if not DATA.exists():
            return {"tokens": {}}
        try:
            return json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            return {"tokens": {}}


def _save(d: dict) -> None:
    with _lock:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(DATA)


def track(address: str, symbol: str, source: str, card: dict) -> None:
    """Bierze token pod obserwacje (jesli jeszcze nie sledzony)."""
    d = _load()
    if address in d["tokens"]:
        return
    now = time.time()
    d["tokens"][address] = {
        "symbol": symbol,
        "source": source,
        "found_at": now,
        "found_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_price": card.get("price") or 0,
        "entry_liq": card.get("liquidity_usd") or 0,
        "risk": card.get("risk", "?"),
        "chain": card.get("chain", "?"),
        "snaps": [],            # [{t, price, liq, vol24}]
        "peak_price": card.get("price") or 0,
        "peak_at_min": 0,       # ile minut po wykryciu byl szczyt
        "status": "swiezy",     # swiezy | rosnie | szczyt | spadek | RUGPULL | zakonczone
        "rug": False,
    }
    _save(d)


def _snapshot_one(address: str, tok: dict) -> None:
    """Dociaga aktualne dane tokena i aktualizuje jego historie + status."""
    from core.trading import analyze_token
    card = analyze_token(address)
    if not card:
        return
    now = time.time()
    price = card.get("price") or 0
    liq = card.get("liquidity_usd") or 0
    vol24 = card.get("volume24_usd") or 0
    mins = round((now - tok["found_at"]) / 60)

    prev_liq = tok["snaps"][-1]["liq"] if tok["snaps"] else tok["entry_liq"]
    tok["snaps"].append({"t": now, "min": mins, "price": price,
                         "liq": liq, "vol24": vol24})
    tok["snaps"] = tok["snaps"][-500:]

    # szczyt
    if price > tok.get("peak_price", 0):
        tok["peak_price"] = price
        tok["peak_at_min"] = mins

    # ALARM RUGPULL: nagly spadek plynnosci
    if prev_liq and liq < prev_liq * (1 - RUG_LIQ_DROP):
        if not tok.get("rug"):
            tok["rug"] = True
            tok["status"] = "RUGPULL"
            _rug_alert(tok, prev_liq, liq)
        return

    # status wg trajektorii ceny
    entry = tok.get("entry_price") or 0
    if entry:
        if tok["peak_price"] and price < tok["peak_price"] * 0.7:
            tok["status"] = "spadek"
        elif tok["peak_price"] and price >= tok["peak_price"] * 0.98 and price > entry * 1.05:
            tok["status"] = "szczyt"
        elif price > entry * 1.05:
            tok["status"] = "rosnie"
        else:
            tok["status"] = "plaski"


def _rug_alert(tok: dict, prev_liq: float, liq: float) -> None:
    from core import telegram_bot, state
    msg = (f"!! ALARM RUGPULL: ${tok['symbol']}\n"
           f"Plynnosc runela: ${prev_liq:,.0f} -> ${liq:,.0f}\n"
           f"Token wykryty {tok['found_time']}. NIE KUPUJ / uciekaj.")
    telegram_bot.send(msg)
    state.add_report("trading", f"RUGPULL: ${tok['symbol']}", msg,
                     needs_action=True, channel="radar/tracker")


def summary(address: str) -> dict | None:
    """Podsumowanie trajektorii jednego tokena (do wnioskow)."""
    d = _load()
    tok = d["tokens"].get(address)
    if not tok:
        return None
    entry = tok.get("entry_price") or 0
    peak = tok.get("peak_price") or 0
    last = tok["snaps"][-1]["price"] if tok["snaps"] else entry
    last_liq = tok["snaps"][-1]["liq"] if tok["snaps"] else tok.get("entry_liq", 0)
    glitch = _is_glitch(tok)
    return {
        "symbol": tok["symbol"],
        "source": tok["source"],
        "found_time": tok["found_time"],
        "risk": tok.get("risk", "?"),
        "liq": last_liq,
        "gain_to_peak_pct": round((peak / entry - 1) * 100, 1) if entry else 0,
        "peak_at_min": tok.get("peak_at_min", 0),
        "now_from_entry_pct": round((last / entry - 1) * 100, 1) if entry else 0,
        "status": tok["status"],
        "rug": tok["rug"],
        "glitch": glitch,
        "snaps": len(tok["snaps"]),
    }


def insights() -> dict:
    """Zbiorcze wnioski ze WSZYSTKICH sledzonych tokenow - fundament decyzji."""
    d = _load()
    toks = list(d["tokens"].values())
    done = [t for t in toks if t.get("entry_price") and not _is_glitch(t)]
    if not done:
        return {"count": 0}

    import statistics

    def median(xs):
        return round(statistics.median(xs), 1) if xs else None

    rugs = [t for t in done if t.get("rug")]
    # szczyt realnie w <=90 min; powyzej to prawie zawsze pozny bledny tick -> odrzuc
    peaks = [t["peak_at_min"] for t in done
             if t.get("peak_at_min") and t["peak_at_min"] <= 90]
    gains = []
    for t in done:
        e, p = t.get("entry_price") or 0, t.get("peak_price") or 0
        if e:
            g = (p / e - 1) * 100
            gains.append(g)   # done juz bez glitchy (>100x)

    winners = [g for g in gains if g >= 30]

    return {
        "count": len(done),
        "rug_count": len(rugs),
        "rug_pct": round(len(rugs) / len(done) * 100, 1),
        "winners_pct": round(len(winners) / len(done) * 100, 1),
        # MEDIANA (odporna na wyjatki), nie srednia
        "med_gain_to_peak_pct": median(gains),
        "med_minutes_to_peak": median(peaks),
        "best_gain_pct": round(max(gains), 1) if gains else 0,
    }


# Powyzej tego mnoznika token = BLEDNY ODCZYT (glitch swiezego tokena), nie realny
# zysk. 100x (+10 000%) to juz ekstremum nawet dla memecoinow; +mln% to na pewno smiec.
# Takie tokeny WYKLUCZAMY ze statystyk (nie przycinamy - bo jeden zawyza srednia).
GLITCH_MULT = 100.0


def _is_glitch(t: dict) -> bool:
    e = t.get("entry_price") or 0
    if not e:
        return True
    peak = t.get("peak_price") or e
    last = t["snaps"][-1]["price"] if t.get("snaps") else e
    return (peak / e) > GLITCH_MULT or (last / e) > GLITCH_MULT or e <= 0


def exit_strategies() -> dict:
    """Symuluje rozne reguly SPRZEDAZY na wszystkich sledzonych tokenach.
    Odpowiada na pytanie: kiedy sprzedac, zeby zarabiac? Kluczowa nauka bota.
    Glitche (bledne odczyty) sa WYKLUCZONE."""
    d = _load()
    toks = [t for t in d["tokens"].values()
            if t.get("entry_price") and len(t.get("snaps", [])) >= 2
            and not _is_glitch(t)]
    if not toks:
        return {"count": 0}

    def price_at_min(t, target):
        best = None
        for s in t["snaps"]:
            if s["min"] <= target + 2:
                best = s["price"]
            else:
                break
        return best or (t["snaps"][-1]["price"] if t["snaps"] else t["entry_price"])

    def mult_clip(m):
        return max(0.0, min(m, 1000.0))   # ucinamy glitche

    strat = {
        "trzymaj (bez sprzedazy)": [],
        "sprzedaj po 10 min": [],
        "sprzedaj po 15 min": [],
        "sprzedaj po 30 min": [],
        "take-profit +30%": [],
        "take-profit +50%": [],
        "trailing stop -25% od szczytu": [],
        "IDEAL: sprzedaz na szczycie": [],
    }
    for t in toks:
        e = t["entry_price"]
        snaps = t["snaps"]
        last = snaps[-1]["price"]
        strat["trzymaj (bez sprzedazy)"].append(mult_clip(last / e))
        strat["sprzedaj po 10 min"].append(mult_clip(price_at_min(t, 10) / e))
        strat["sprzedaj po 15 min"].append(mult_clip(price_at_min(t, 15) / e))
        strat["sprzedaj po 30 min"].append(mult_clip(price_at_min(t, 30) / e))
        # take-profit: pierwsza chwila gdy osiagnie prog, inaczej ostatnia cena
        for tp, key in ((1.3, "take-profit +30%"), (1.5, "take-profit +50%")):
            hit = next((s["price"] for s in snaps if s["price"] >= e * tp), last)
            strat[key].append(mult_clip(hit / e))
        # trailing stop 25% od biezacego szczytu
        peak = e
        exitp = last
        for s in snaps:
            peak = max(peak, s["price"])
            if s["price"] <= peak * 0.75:
                exitp = s["price"]
                break
        strat["trailing stop -25% od szczytu"].append(mult_clip(exitp / e))
        strat["IDEAL: sprzedaz na szczycie"].append(mult_clip(t.get("peak_price", e) / e))

    out = []
    for name, mults in strat.items():
        avg = sum(mults) / len(mults)
        out.append({"name": name, "avg_pnl_pct": round((avg - 1) * 100, 1),
                    "n": len(mults)})
    # sortuj: najlepsza realna strategia na gorze (IDEAL na koncu jako punkt odniesienia)
    real = [s for s in out if not s["name"].startswith("IDEAL")]
    ideal = [s for s in out if s["name"].startswith("IDEAL")]
    real.sort(key=lambda x: x["avg_pnl_pct"], reverse=True)
    return {"count": len(toks), "strategies": real + ideal}


def _tick() -> None:
    d = _load()
    now = time.time()
    changed = False
    for addr, tok in list(d["tokens"].items()):
        # przestajemy sledzic po TRACK_HOURS albo po rugu
        age_h = (now - tok["found_at"]) / 3600
        if tok.get("rug") or age_h > TRACK_HOURS:
            if tok["status"] not in ("RUGPULL", "zakonczone"):
                tok["status"] = "zakonczone"
                changed = True
            continue
        try:
            _snapshot_one(addr, tok)
            changed = True
        except Exception:
            pass
        time.sleep(1)   # lekko dla API
    if changed:
        _save(d)


def _loop() -> None:
    time.sleep(45)
    while True:
        try:
            _tick()
        except Exception:
            import traceback
            from core import state
            state.add_report("trading", "Blad trackera", traceback.format_exc(),
                             needs_action=True)
            time.sleep(600)
        time.sleep(SNAP_EVERY_S)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
