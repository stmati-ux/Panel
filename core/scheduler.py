"""
Auto-scheduler: pelna autonomia publikacji.

Watek w tle budzi sie co 30 minut i sprawdza dla kazdego kanalu:
- czy Automat jest wlaczony (przelacznik w panelu),
- czy panel NIE jest zatrzymany (STOP),
- czy tryb to LIVE (na sucho automat tylko raportuje plan),
- czy dzis jeszcze nie dokladal filmu dla tego kanalu.

Jesli wszystko gra -> doklada JEDEN film na nastepny wolny slot
(publisher.publish_next robi cala reszte: metadane, duplikaty, upload).

Dzieki temu: 1 film / dzien / kanal, samo sie pilnuje, wszystko w raportach.
"""
from __future__ import annotations

import threading
import time
from datetime import date

from core import state, publisher

CHECK_EVERY_S = 30 * 60   # co 30 minut

_started = False


def _today() -> str:
    return date.today().isoformat()


def _tick() -> None:
    st = state.load()
    if not st.get("auto", False):
        return
    if not st.get("running", True):
        return

    mode = st.get("mode", "dry-run")
    auto_log = st.get("auto_log", {})   # "konto:tryb" -> data ostatniej proby

    for account in publisher.CHANNELS:
        key = f"{account}:{mode}"
        if auto_log.get(key) == _today():
            continue   # w tym trybie dzis juz probowano (nie spamujemy)

        publisher.publish_next(account, mode)

        def _mut(s, k=key):
            s.setdefault("auto_log", {})[k] = _today()
        state.update(_mut)


def _loop() -> None:
    time.sleep(20)   # daj serwerowi wstac
    while True:
        try:
            _tick()
        except Exception:
            import traceback
            state.add_report("system", "Blad auto-schedulera", traceback.format_exc(),
                             needs_action=True)
        time.sleep(CHECK_EVERY_S)


def start() -> None:
    """Startuje watek automatu (raz)."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
