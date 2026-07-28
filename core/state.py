"""
Zarzadzanie stanem panelu.

Stan trzymany jest w jednym pliku JSON (data/state.json) i chroniony
blokada watkow. To celowo proste - latwe do podejrzenia i debugowania.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "state.json"

_lock = threading.RLock()

# Stan poczatkowy - uzywany przy pierwszym uruchomieniu.
DEFAULT_STATE = {
    "mode": "setup",            # "setup" (ustawianie/test) albo "live" (na zywo)
    "running": True,             # glowny wlacznik / czerwony STOP
    "rooms": {
        "youtube": {
            "id": "youtube",
            "name": "YouTube",
            "emoji": "\U0001F3AC",
            "status": "idle",          # idle | working | done | error | stopped
            "enabled": True,
            "description": "Pomysly na filmy, scenariusze, opisy, tagi, harmonogram.",
            "last_run": None,
            "last_output": None,
        },
        "dropshipping": {
            "id": "dropshipping",
            "name": "Dropshipping",
            "emoji": "\U0001F6D2",
            "status": "planned",
            "enabled": False,
            "description": "Research produktow, opisy, dodawanie do Shopify, zamowienia.",
            "last_run": None,
            "last_output": None,
        },
        "trading": {
            "id": "trading",
            "name": "Trading",
            "emoji": "\U0001F4C8",
            "status": "planned",
            "enabled": False,
            "description": "Sygnaly z grupy TG + analiza (Jupiter) + wirtualny portfel.",
            "last_run": None,
            "last_output": None,
        },
    },
    "reports": [],   # lista raportow, najnowsze na gorze
}

MAX_REPORTS = 200


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load() -> dict:
    with _lock:
        if not STATE_FILE.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            save(DEFAULT_STATE)
            return json.loads(json.dumps(DEFAULT_STATE))
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)


def save(state: dict) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(STATE_FILE)


def update(mutator) -> dict:
    """Bezpiecznie modyfikuje stan: wczytuje, wola mutator(state), zapisuje."""
    with _lock:
        state = load()
        mutator(state)
        save(state)
        return state


def add_report(room_id: str, title: str, summary: str,
               needs_action: bool = False, mode: str | None = None,
               channel: str | None = None) -> None:
    """Raport pracy. room_id = pokoj (youtube/dropshipping/system...),
    channel = konkretny kanal/cel w pokoju (np. DinoVault)."""
    def _mut(state):
        if mode is None:
            m = state.get("mode", "setup")
        else:
            m = mode
        entry = {
            "time": _now(),
            "room": room_id,
            "channel": channel,
            "title": title,
            "summary": summary,
            "needs_action": needs_action,
            "mode": m,
        }
        state.setdefault("reports", []).insert(0, entry)
        del state["reports"][MAX_REPORTS:]
    update(_mut)
    # Powiadomienie na Telegram (best effort, nie blokuje panelu).
    try:
        from core import telegram_bot
        telegram_bot.notify_report({
            "room": room_id, "channel": channel, "title": title,
            "summary": summary, "needs_action": needs_action,
            "mode": mode or "",
        })
    except Exception:
        pass


def set_room_status(room_id: str, status: str, output: str | None = None) -> None:
    def _mut(state):
        room = state["rooms"].get(room_id)
        if not room:
            return
        room["status"] = status
        room["last_run"] = _now()
        if output is not None:
            room["last_output"] = output
    update(_mut)
