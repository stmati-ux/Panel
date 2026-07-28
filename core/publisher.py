"""
Auto-publikacja: bierze nastepny GOTOWY plik mp4 z folderu kanalu,
dopasowuje metadane (tytul/opis/tagi) i planuje go na YouTube na nastepny
wolny slot (1 film/dzien, ta sama godzina co ostatni zaplanowany).

BEZPIECZNIKI:
- Weryfikacja duplikatow: pomija pliki, ktorych tytul JUZ jest na kanale
  (publiczne + zaplanowane) oraz te zapisane w naszym rejestrze wyslanych.
- Tryb 'dry-run' niczego nie wysyla - tylko pokazuje, co by zrobil.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import state, youtube_api

# Konfiguracja kanalow: konto -> folder z gotowymi mp4 + folder metadanych.
CHANNELS = {
    "after3am": {
        "label": "After3AMFiles (horror)",
        "ready": Path(r"D:\Mods\horror_preset\ready"),
        "meta_dir": Path(r"D:\Mods\horror_preset\scripts"),
        "made_for_kids": False,
        "hour": 19,   # godzina publikacji (jak dotychczasowe 19:00)
    },
    "dinovault": {
        "label": "DinoVault (dinozaury)",
        "ready": Path(r"D:\Mods\dinos\ready"),
        "meta_dir": Path(r"D:\Mods\dinos\scripts"),
        "made_for_kids": False,
        "hour": 19,
    },
}

LEDGER = Path(__file__).resolve().parent.parent / "data" / "uploaded.json"


# ---------- metadane ----------

def _parse_meta_file(path: Path) -> dict:
    """Parsuje plik METADANE: bloki '### KLUCZ ###' -> {title, desc, tags}."""
    out = {}
    text = path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"^###\s*(.+?)\s*###\s*$", text, flags=re.MULTILINE)
    # blocks: [przed, KLUCZ1, tresc1, KLUCZ2, tresc2, ...]
    for i in range(1, len(blocks), 2):
        key = blocks[i].strip()
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        title = ""
        tags = []
        desc_lines = []
        mode = None
        for line in body.splitlines():
            s = line.strip()
            if s.upper().startswith("TYTUL:"):
                title = s[6:].strip()
                mode = None
            elif s.upper().startswith("OPIS:"):
                mode = "desc"
                rest = s[5:].strip()
                if rest:
                    desc_lines.append(rest)
            elif s.upper().startswith("TAGI:"):
                tags = [t.strip() for t in s[5:].split(",") if t.strip()]
                mode = None
            elif mode == "desc":
                desc_lines.append(line.rstrip())
        if title:
            out[key.upper()] = {
                "title": title,
                "desc": "\n".join(desc_lines).strip(),
                "tags": tags,
            }
    return out


def load_metadata(account: str) -> dict:
    meta = {}
    d = CHANNELS[account]["meta_dir"]
    if d.exists():
        for f in sorted(d.glob("METADANE*.txt")):
            meta.update(_parse_meta_file(f))
    return meta


def _key_from_filename(name: str) -> str:
    """AUTO_102_READY.mp4 -> AUTO_102 ; DINO_8_READY.mp4 -> DINO_8"""
    stem = Path(name).stem
    stem = re.sub(r"_READY$", "", stem, flags=re.IGNORECASE)
    return stem.upper()


# ---------- rejestr wyslanych ----------

def _load_ledger() -> dict:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_ledger(d: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _core_title(t: str) -> str:
    """Rdzen tytulu do porownan: czesc przed '#', bez emoji/znakow,
    tylko litery/cyfry/spacje, malymi. Odporne na doklejone smieci."""
    t = (t or "").split("#")[0]
    t = re.sub(r"[^0-9a-zA-Z\s]", " ", t)   # usuwa emoji i interpunkcje
    return re.sub(r"\s+", " ", t).strip().casefold()


def _is_duplicate(meta_title: str, existing_titles: list[str]) -> bool:
    """Duplikat, gdy rdzenie sa rowne lub jeden zawiera drugi (min. 12 znakow)."""
    a = _core_title(meta_title)
    if not a:
        return False
    for ex in existing_titles:
        b = _core_title(ex)
        if not b:
            continue
        if a == b:
            return True
        if len(a) >= 12 and (a in b or b in a):
            return True
    return False


# ---------- planowanie ----------

def _next_slot(account: str):
    """Nastepny wolny slot = ostatni zaplanowany + 1 dzien (ta sama godz),
    a jak nic nie zaplanowane -> jutro o skonfigurowanej godzinie."""
    last = youtube_api.last_scheduled_datetime(account)
    if last:
        return last + timedelta(days=1)
    hour = CHANNELS[account]["hour"]
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    return tomorrow.replace(hour=hour, minute=0, second=0, microsecond=0)


def plan_next(account: str) -> dict:
    """Wybiera nastepny plik do wyslania (z weryfikacja duplikatow). Nie wysyla."""
    cfg = CHANNELS.get(account)
    if not cfg:
        return {"ok": False, "reason": f"Nieznany kanal: {account}"}
    ready_dir = cfg["ready"]
    if not ready_dir.exists():
        return {"ok": False, "reason": f"Brak folderu: {ready_dir}"}

    meta = load_metadata(account)
    ledger = _load_ledger().get(account, {})

    # Tytuly JUZ na kanale (publiczne + zaplanowane) -> weryfikacja duplikatow.
    try:
        existing_titles = [v.get("title", "") for v in youtube_api.get_channel_videos(account)]
    except Exception as e:
        return {"ok": False, "reason": f"Nie moge odczytac kanalu (duplikaty): {e}"}

    files = sorted(ready_dir.glob("*.mp4"))
    skipped = []
    for f in files:
        key = _key_from_filename(f.name)
        m = meta.get(key)
        if not m:
            skipped.append(f"{f.name}: brak metadanych ({key})")
            continue
        if f.name in ledger:
            skipped.append(f"{f.name}: juz wyslany przez panel")
            continue
        if _is_duplicate(m["title"], existing_titles):
            skipped.append(f"{f.name}: tytul juz na kanale (duplikat)")
            continue
        # znaleziony kandydat
        slot = _next_slot(account)
        return {
            "ok": True,
            "account": account,
            "file": str(f),
            "filename": f.name,
            "key": key,
            "title": m["title"],
            "desc": m["desc"],
            "tags": m["tags"],
            "slot_dt": slot,
            "slot": slot.strftime("%Y-%m-%d %H:%M"),
            "made_for_kids": cfg["made_for_kids"],
            "skipped": skipped,
        }
    return {"ok": False, "reason": "Brak nowych plikow do wyslania (wszystko juz jest).",
            "skipped": skipped}


def publish_next(account: str, mode: str) -> dict:
    """W setup: pokazuje plan. W live: wysyla i zapisuje w rejestrze.
    Raporty: pokoj 'youtube', kanal = nazwa kanalu."""
    plan = plan_next(account)
    label = CHANNELS.get(account, {}).get("label", account)

    if not plan.get("ok"):
        dlaczego = plan.get("reason", "")
        pominiete = plan.get("skipped", [])
        tresc = (
            f"Wynik: nic nie zaplanowano.\n"
            f"Powod: {dlaczego}\n"
            f"Sprawdzono plikow: {len(pominiete)} — wszystkie juz na kanale lub bez metadanych."
        )
        state.add_report("youtube", "Nic do zaplanowania", tresc,
                         needs_action=False, mode=mode, channel=label)
        return plan

    opis = (
        f"Film:       {plan['title']}\n"
        f"Plik:       {plan['filename']}\n"
        f"Publikacja: {plan['slot']} (sam sie upubliczni)\n"
        f"Oznaczenia: AI: TAK | Dla dzieci: {'TAK' if plan['made_for_kids'] else 'NIE'}"
    )

    if mode != "live":
        state.add_report("youtube", "Plan publikacji (SET-UP, nie wyslano)",
                         opis + "\n\nTryb SET-UP: nic nie poszlo na YouTube. "
                                "Przelacz na LIVE, by wyslac naprawde.",
                         needs_action=False, mode=mode, channel=label)
        return plan

    # LIVE: realny upload zaplanowany
    try:
        slot_iso = plan["slot_dt"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = youtube_api.upload_video(
            file_path=plan["file"],
            title=plan["title"],
            description=plan["desc"],
            tags=plan["tags"],
            made_for_kids=plan["made_for_kids"],
            self_declared_ai=True,
            account=account,
            publish_at=slot_iso,
        )
        led = _load_ledger()
        led.setdefault(account, {})[plan["filename"]] = {
            "video_id": res["id"], "title": plan["title"],
            "slot": plan["slot"], "uploaded": state._now(),
        }
        _save_ledger(led)
        state.add_report("youtube", "Film zaplanowany na YouTube",
                         opis + f"\nLink:       {res['url']}",
                         needs_action=False, mode=mode, channel=label)
        res["plan"] = plan
        return res
    except Exception as e:
        import traceback
        state.add_report("youtube", "BLAD wysylki filmu", traceback.format_exc(),
                         needs_action=True, mode=mode, channel=label)
        return {"ok": False, "reason": str(e)}
