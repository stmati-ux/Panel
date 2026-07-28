"""
Rug-check ON-CHAIN (RugCheck.xyz) - dziala na SWIEZYCH tokenach BEZ historii cen.

To wlasciwy filtr antyrug: nie patrzy na wiek/historie (bo wtedy moonshot juz minal),
tylko na to, czy token DA SIE zrugnac:
  - lpLockedPct: % zablokowanej plynnosci (locked/burned = dev NIE wyciagnie kasy)
  - risks: konkretne zagrozenia on-chain (mint authority, freeze, koncentracja holderow)
  - score_normalised: ogolny wskaznik ryzyka (nizszy = bezpieczniej)

Endpoint: https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary  (darmowy, bez klucza)
"""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "https://api.rugcheck.xyz/v1/tokens"

# progi filtra (mozna luzowac/zaostrzac)
MIN_LP_LOCKED_PCT = 50.0      # min. % zablokowanej plynnosci
MAX_RISK_SCORE = 40.0         # max score_normalised (nizszy = bezpieczniej)

_cache: dict[str, tuple[float, dict]] = {}   # addr -> (czas, raport)
_CACHE_TTL = 600


def get_report(address: str) -> dict | None:
    """Pobiera raport on-chain tokena (z cache 10 min)."""
    now = time.time()
    if address in _cache and now - _cache[address][0] < _CACHE_TTL:
        return _cache[address][1]
    try:
        req = urllib.request.Request(
            f"{BASE}/{address}/report/summary",
            headers={"User-Agent": "panel/1.0", "Accept": "application/json"})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception:
        return None
    _cache[address] = (now, data)
    return data


def assess(address: str) -> dict:
    """Ocena bezpieczenstwa tokena. Zwraca dict z werdyktem i szczegolami."""
    r = get_report(address)
    if r is None:
        return {"ok": None, "reason": "brak danych RugCheck", "lp_locked": None,
                "score": None, "dangers": []}

    lp_locked = r.get("lpLockedPct")
    score = r.get("score_normalised")
    risks = r.get("risks") or []
    dangers = [x.get("name") for x in risks if x.get("level") == "danger"]

    reasons = []
    ok = True
    if lp_locked is None or lp_locked < MIN_LP_LOCKED_PCT:
        ok = False
        reasons.append(f"plynnosc zablokowana tylko {lp_locked}% (min {MIN_LP_LOCKED_PCT}%)")
    if dangers:
        ok = False
        reasons.append("zagrozenia: " + ", ".join(dangers))
    if score is not None and score > MAX_RISK_SCORE:
        ok = False
        reasons.append(f"score ryzyka {score} (>{MAX_RISK_SCORE})")

    return {
        "ok": ok,
        "reason": "; ".join(reasons) if reasons else "bezpieczny (LP locked, brak zagrozen)",
        "lp_locked": lp_locked,
        "score": score,
        "dangers": dangers,
    }


def is_safe(address: str) -> bool:
    """Czy token przechodzi filtr on-chain (dziala na swiezych tokenach)."""
    a = assess(address)
    return a["ok"] is True
