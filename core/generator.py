"""
Generacja nowych filmow przez fabryki (factory*.py na D:/ComfyUI).

Panel odpala fabryke w tle (venv ComfyUI), fabryka czyta stories_queue.json
(paczka historii przygotowana w D:/Mods/<preset>/), pomija filmy ktore juz sa,
a nowe README laduja w <preset>/ready/ - skad bierze je publisher.

Jedna fabryka na raz (GPU!). Kokoro+Whisper+SDXL potrzebuja VRAM - nie odpalac
rownolegle z innymi zadaniami generowania.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from core import state

COMFY_DIR = Path(r"D:\ComfyUI")
VENV_PY = COMFY_DIR / "venv" / "Scripts" / "python.exe"

FACTORIES = {
    "after3am": {
        "script": "factory.py",
        "ready": Path(r"D:\Mods\horror_preset\ready"),
        "queue": Path(r"D:\Mods\horror_preset\stories_queue.json"),
        "label": "After3AMFiles (horror)",
    },
    "dinovault": {
        "script": "factory_dino.py",
        "ready": Path(r"D:\Mods\dinos\ready"),
        "queue": Path(r"D:\Mods\dinos\stories_queue.json"),
        "label": "DinoVault (dinozaury)",
    },
}

_gen_lock = threading.Lock()
_running: dict[str, bool] = {}


def is_running(account: str) -> bool:
    return _running.get(account, False)


def queue_size(account: str) -> int:
    """Ile historii w paczce czeka na wyprodukowanie (bez juz zrobionych)."""
    import json
    cfg = FACTORIES.get(account)
    if not cfg or not cfg["queue"].exists():
        return 0
    try:
        stories = json.loads(cfg["queue"].read_text(encoding="utf-8"))
    except Exception:
        return 0
    prefix = "AUTO_" if account == "after3am" else "DINO_"
    todo = 0
    for n in stories:
        if not (cfg["ready"] / f"{prefix}{n}_READY.mp4").exists():
            todo += 1
    return todo


def run_factory(account: str) -> None:
    """Odpala fabryke w tle. Raportuje start i wynik."""
    cfg = FACTORIES.get(account)
    if not cfg:
        return

    def _work():
        if not _gen_lock.acquire(blocking=False):
            state.add_report("youtube", "Generacja pominieta",
                             "Inna fabryka juz pracuje - GPU obsluzy jedna na raz. "
                             "Sprobuj ponownie po jej zakonczeniu.",
                             needs_action=False, channel=cfg["label"])
            return
        _running[account] = True
        try:
            before = set(p.name for p in cfg["ready"].glob("*_READY.mp4"))
            todo = queue_size(account)
            state.add_report("youtube", "Start generacji filmow",
                             f"Fabryka: {cfg['script']}\nDo wyprodukowania: {todo} filmow.\n"
                             f"Kazdy film to glos + 4 obrazy + montaz (~4-8 min/film).",
                             channel=cfg["label"])
            t0 = time.time()
            log_path = COMFY_DIR / f"_panel_{cfg['script']}.log"
            with log_path.open("w", encoding="utf-8") as lf:
                proc = subprocess.run(
                    [str(VENV_PY), cfg["script"]],
                    cwd=str(COMFY_DIR), stdout=lf, stderr=subprocess.STDOUT,
                    timeout=3 * 3600,
                )
            after = set(p.name for p in cfg["ready"].glob("*_READY.mp4"))
            new = sorted(after - before)
            took = (time.time() - t0) / 60
            if proc.returncode == 0 and new:
                state.add_report("youtube", "Generacja zakonczona",
                                 f"Nowe filmy ({len(new)}), gotowe do publikacji:\n" +
                                 "\n".join(f"  {n}" for n in new) +
                                 f"\nCzas: {took:.0f} min. Automat zaplanuje je po 1 dziennie.",
                                 channel=cfg["label"])
            elif proc.returncode == 0:
                state.add_report("youtube", "Generacja: nic nowego",
                                 "Fabryka przeszla, ale nie powstal zaden nowy plik "
                                 "(wszystko z paczki juz istnialo?).",
                                 channel=cfg["label"])
            else:
                tail = "\n".join(log_path.read_text(encoding="utf-8",
                                                    errors="replace").splitlines()[-15:])
                state.add_report("youtube", "BLAD generacji",
                                 f"Fabryka zakonczyla sie bledem (kod {proc.returncode}).\n"
                                 f"Koncowka logu ({log_path.name}):\n{tail}",
                                 needs_action=True, channel=cfg["label"])
        except subprocess.TimeoutExpired:
            state.add_report("youtube", "BLAD generacji - timeout",
                             "Fabryka przekroczyla 3h i zostala przerwana.",
                             needs_action=True, channel=cfg["label"])
        except Exception:
            import traceback
            state.add_report("youtube", "BLAD generacji", traceback.format_exc(),
                             needs_action=True, channel=cfg["label"])
        finally:
            _running[account] = False
            _gen_lock.release()

    threading.Thread(target=_work, daemon=True).start()
