"""
Wspolne akcje sterujace - uzywane i przez panel (app.py), i przez Telegram.
Dzieki temu ta sama komenda dziala z przegladarki i z telefonu.
"""
from __future__ import annotations

import subprocess
import urllib.request

COMFY = "http://127.0.0.1:8188"


def kill_generation() -> list[str]:
    """Awaryjnie przerywa WSZYSTKIE generacje: ComfyUI + skrypty castingu/fabryk."""
    done = []
    # 1) przerwij biezacy render w ComfyUI + wyczysc kolejke
    try:
        urllib.request.urlopen(urllib.request.Request(
            COMFY + "/interrupt", data=b"{}",
            headers={"Content-Type": "application/json"}), timeout=5)
        urllib.request.urlopen(urllib.request.Request(
            COMFY + "/queue", data=b'{"clear": true}',
            headers={"Content-Type": "application/json"}), timeout=5)
        done.append("ComfyUI: przerwano render + wyczyszczono kolejke")
    except Exception:
        done.append("ComfyUI: brak odpowiedzi (moze juz nie renderuje)")
    # 2) ubij skrypty castingu/fabryk PO NAZWIE (nie wszystkie python!)
    ps = (r"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          r"Where-Object { $_.CommandLine -match 'casting|factory|refine' } | "
          r"ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        done.append("Skrypty generujace: zatrzymane")
    except Exception:
        done.append("Skrypty: nie udalo sie ubic")
    return done


def comfyui_alive() -> bool:
    try:
        urllib.request.urlopen(COMFY + "/queue", timeout=4)
        return True
    except Exception:
        return False
