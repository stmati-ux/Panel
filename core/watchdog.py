"""
Watchdog ComfyUI: pilnuje, zeby ComfyUI (8188) zawsze zylo.
Jesli padnie (np. OOM), panel podnosi je z powrotem - bez Twojej ingerencji.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from core import control, state

COMFY_DIR = Path(r"D:\ComfyUI")
COMFY_PY = COMFY_DIR / "venv" / "Scripts" / "python.exe"

_started = False


def _start_comfyui() -> None:
    log = COMFY_DIR / "comfyui_run.log"
    # WAZNE: tryb "a" (dopisywanie), NIE "w". Wczesniej kazdy restart KASOWAL log,
    # przez co ginal dowod, dlaczego ComfyUI padlo - nie dalo sie tego zdiagnozowac.
    try:
        # zachowaj ogon poprzedniego uruchomienia jako osobny plik (dowod crasha)
        try:
            if log.exists() and log.stat().st_size > 0:
                prev = log.read_text(encoding="utf-8", errors="replace")[-20000:]
                (COMFY_DIR / "comfyui_crash_last.log").write_text(
                    prev, encoding="utf-8")
        except Exception:
            pass
        with log.open("a", encoding="utf-8") as lf:
            lf.write(f"\n\n===== RESTART {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            subprocess.Popen(
                # --lowvram: nie OOM-uje na 12GB (normalny tryb padal sam Wan po ~5 obrazach).
                # Wolniej, ale z madrym watchdogiem (slow != dead) to juz nie problem.
                [str(COMFY_PY), "main.py", "--port", "8188", "--lowvram"],
                cwd=str(COMFY_DIR), stdout=lf, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        state.add_report("system", "ComfyUI auto-restart",
                         "ComfyUI padło (pewnie brak VRAM) — panel podniósł je z powrotem. "
                         "Generacja znów dostępna.")
    except Exception:
        import traceback
        state.add_report("system", "Nie udało się wstać ComfyUI",
                         traceback.format_exc(), needs_action=True)


def _ping_status(timeout: float) -> str:
    """Rozroznia: 'alive' (odpowiada), 'slow' (timeout - zyje ale zajete),
    'dead' (polaczenie ODRZUCONE - proces nie zyje). Kluczowe rozroznienie:
    slow != dead. Slow gdy generuje = OK. Dead = ZAWSZE restart."""
    import urllib.request, urllib.error, socket
    try:
        urllib.request.urlopen("http://127.0.0.1:8188/queue", timeout=timeout)
        return "alive"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        errno = getattr(reason, "errno", None)
        if isinstance(reason, ConnectionRefusedError) or errno in (10061, 111):
            return "dead"        # proces nie nasluchuje = martwy
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "slow"        # zyje, tylko wolno odpowiada (lowvram loading)
        return "slow"
    except (socket.timeout, TimeoutError):
        return "slow"
    except Exception:
        return "slow"


def _generation_active() -> bool:
    """Czy trwa generacja (pokoj 'working')? Jesli tak - ComfyUI ZYJE, tylko
    jest zajete. NIE restartujemy go wtedy, choćby wolno odpowiadal."""
    try:
        st = state.load()
        return any(r.get("status") == "working" for r in st.get("rooms", {}).values())
    except Exception:
        return False


def _loop() -> None:
    time.sleep(30)
    dead_streak = 0
    while True:
        st = _ping_status(15)
        if st == "alive":
            dead_streak = 0
        elif st == "dead":
            # Proces NIE ZYJE (polaczenie odrzucone) - podnies OD RAZU,
            # nawet podczas 'generacji' (bo ta generacja i tak juz padla z ComfyUI).
            dead_streak += 1
            if dead_streak >= 2:   # 2 potwierdzenia = na pewno martwe
                _start_comfyui()
                dead_streak = 0
                time.sleep(60)
        else:  # "slow" - zyje, tylko zajete (lowvram). NIE restartuj.
            dead_streak = 0
        time.sleep(15)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
