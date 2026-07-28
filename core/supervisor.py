"""
Agent-nadzorca.

Rozdziela zadania do pokoi, pilnuje glownego wlacznika (STOP) i trybu
(dry-run / live), oraz sklada raport zbiorczy. Uruchamia pokoje w tle,
zeby panel sie nie blokowal.
"""
from __future__ import annotations

import threading
import traceback

from core import state
from core.rooms import youtube

# Rejestr pokoi: id -> modul z funkcja run(mode)
ROOMS = {
    "youtube": youtube,
}

_run_lock = threading.Lock()


def _run_room_sync(room_id: str) -> None:
    st = state.load()
    if not st.get("running", True):
        state.add_report(room_id, "Pominieto — STOP aktywny",
                         "Nadzorca nie uruchomil pokoju, bo panel jest zatrzymany (STOP).",
                         needs_action=False)
        return

    room_cfg = st["rooms"].get(room_id, {})
    if not room_cfg.get("enabled", False):
        state.add_report(room_id, "Pominieto — pokoj wylaczony",
                         "Ten pokoj jest wylaczony w panelu.", needs_action=False)
        return

    module = ROOMS.get(room_id)
    if module is None:
        state.add_report(room_id, "Brak implementacji",
                         "Ten pokoj nie ma jeszcze zaimplementowanego agenta.",
                         needs_action=True)
        return

    mode = st.get("mode", "dry-run")
    try:
        module.run(mode)
    except Exception:
        err = traceback.format_exc()
        state.set_room_status(room_id, "error", err)
        state.add_report(room_id, "Blad podczas pracy agenta", err, needs_action=True)


def run_room(room_id: str) -> None:
    """Uruchamia pokoj w osobnym watku (nie blokuje panelu)."""
    def _target():
        with _run_lock:
            _run_room_sync(room_id)
    threading.Thread(target=_target, daemon=True).start()


def run_all() -> None:
    """Nadzorca uruchamia wszystkie wlaczone pokoje po kolei."""
    def _target():
        with _run_lock:
            st = state.load()
            if not st.get("running", True):
                return
            for room_id, cfg in st["rooms"].items():
                if cfg.get("enabled") and room_id in ROOMS:
                    _run_room_sync(room_id)
    threading.Thread(target=_target, daemon=True).start()
