"""
RADAR: samodzielne skanowanie rynku Solana co kilka minut.

Zrodla (darmowe, publiczne API GeckoTerminal):
- trending_pools  -> co WLASNIE zyskuje uwage
- new_pools       -> swieze tokeny (wczesna faza, najwyzsze ryzyko)

Filtr (twarde progi - lepiej przegapic niz wpasc na rug):
- plynnosc >= $30k
- wolumen 24h >= $50k
- wzrost 1h >= +15% (momentum trwa, nie wygasl)
- deduplikacja: jeden token alarmowany raz na 24h

Kazdy hit: analiza jak przy sygnale z grupy -> wirtualne $100 -> alert TG.
Zrodlo w portfelu: "RADAR" - po tygodniach porownamy skutecznosc
radaru vs grupy Shocked Calls. Dane zdecyduja, czego sluchac.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

from core import state, trading

SCAN_EVERY_S = 180          # co 3 minuty
MIN_LIQ = 30_000            # $
MIN_VOL24 = 50_000          # $
MIN_CHG_H1 = 15.0           # %
MAX_ALERTS_PER_HOUR = 6     # zeby telefon nie wybuchl

_started = False
_alert_times: list[float] = []


def _http(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "panel/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _pools(kind: str) -> list[dict]:
    """kind: 'trending_pools' albo 'new_pools' (GeckoTerminal, Solana)."""
    try:
        data = _http(f"https://api.geckoterminal.com/api/v2/networks/solana/{kind}?page=1")
        return data.get("data", [])
    except Exception:
        return []


def _candidates() -> list[dict]:
    """Pary spelniajace progi radaru."""
    out = []
    for kind in ("trending_pools", "new_pools"):
        for p in _pools(kind):
            a = p.get("attributes", {})
            try:
                liq = float(a.get("reserve_in_usd") or 0)
                vol24 = float((a.get("volume_usd") or {}).get("h24") or 0)
                chg1h = float((a.get("price_change_percentage") or {}).get("h1") or 0)
            except (TypeError, ValueError):
                continue
            if liq >= MIN_LIQ and vol24 >= MIN_VOL24 and chg1h >= MIN_CHG_H1:
                rel = (p.get("relationships") or {})
                base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
                address = base.split("_", 1)[-1] if base else None
                if address:
                    out.append({"address": address, "kind": kind,
                                "name": a.get("name", "?"), "chg1h": chg1h})
    return out


def _rate_ok() -> bool:
    now = time.time()
    while _alert_times and now - _alert_times[0] > 3600:
        _alert_times.pop(0)
    return len(_alert_times) < MAX_ALERTS_PER_HOUR


def _tick() -> None:
    st = state.load()
    if not st.get("running", True):
        return
    for c in _candidates():
        if not _rate_ok():
            return
        # dedup robi record_signal (24h per adres) - ale nie chcemy nawet
        # analizowac w kolko: sprawdzmy szybki cache w danych tradingu
        d = trading._load()
        known = False
        for s in d.get("signals", [])[:100]:
            if s["token"].get("address") == c["address"]:
                t0 = time.mktime(time.strptime(s["time"], "%Y-%m-%d %H:%M:%S"))
                if time.time() - t0 < 86400:
                    known = True
                break
        if known:
            continue
        cards = trading.handle_group_message(
            c["address"],
            source=f"RADAR ({'trending' if c['kind'] == 'trending_pools' else 'nowy token'})")
        if cards:
            _alert_times.append(time.time())


def _loop() -> None:
    time.sleep(30)
    while True:
        try:
            _tick()
        except Exception:
            import traceback
            state.add_report("trading", "Blad radaru", traceback.format_exc(),
                             needs_action=True)
            time.sleep(600)
        time.sleep(SCAN_EVERY_S)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
