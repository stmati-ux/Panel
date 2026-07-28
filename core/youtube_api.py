"""
Integracja z YouTube (wlasna apka Google, tryb dev) - obsluga WIELU kont.

Kazde konto Google autoryzujesz RAZ:
    python connect_youtube.py            # domyslne konto "glowne"
    python connect_youtube.py drugie     # kolejne konto pod etykieta "drugie"

Token kazdego konta zapisuje sie osobno: secrets/token_<etykieta>.json
Panel czyta wszystkie konta naraz i pokazuje ich kanaly + statystyki.

WAZNE: kazdy dodatkowy mail musi byc dodany jako "test user" na ekranie zgody
(Google Auth Platform -> Odbiorcy), bo apka jest w trybie testowym.

Pliki:
  secrets/client_secret.json    <- z Google Cloud (jeden dla wszystkich kont)
  secrets/token_<etykieta>.json <- tworzy sie sam po autoryzacji danego konta
"""
from __future__ import annotations

import re
from pathlib import Path

SECRETS_DIR = Path(__file__).resolve().parent.parent / "secrets"
CLIENT_SECRET = SECRETS_DIR / "client_secret.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
]


def has_client_secret() -> bool:
    return CLIENT_SECRET.exists()


def _token_path(label: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", label) or "glowne"
    return SECRETS_DIR / f"token_{safe}.json"


def list_accounts() -> list[str]:
    """Etykiety podlaczonych kont (na podstawie plikow token_*.json)."""
    if not SECRETS_DIR.exists():
        return []
    out = []
    for p in SECRETS_DIR.glob("token_*.json"):
        out.append(p.stem[len("token_"):])
    return sorted(out)


def is_connected() -> bool:
    return len(list_accounts()) > 0


def _load_credentials(label: str):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    tf = _token_path(label)
    if not tf.exists():
        raise RuntimeError(f"Konto '{label}' nie jest podlaczone (brak {tf.name}).")
    creds = Credentials.from_authorized_user_file(str(tf), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tf.write_text(creds.to_json(), encoding="utf-8")
    return creds


def connect(label: str = "glowne") -> str:
    """Jednorazowa autoryzacja jednego konta: otwiera przegladarke, zapisuje token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_SECRET.exists():
        raise RuntimeError(
            f"Brak {CLIENT_SECRET}. Pobierz go z Google Cloud (OAuth client -> Desktop app)."
        )
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    tf = _token_path(label)
    tf.write_text(creds.to_json(), encoding="utf-8")
    return str(tf)


def _youtube_service(label: str):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_load_credentials(label), cache_discovery=False)


def get_channels(label: str) -> list[dict]:
    """Kanaly jednego konta ze statystykami."""
    yt = _youtube_service(label)
    resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
    out = []
    for it in resp.get("items", []):
        sn = it.get("snippet", {})
        stt = it.get("statistics", {})
        out.append({
            "account": label,
            "id": it["id"],
            "title": sn.get("title", ""),
            "thumbnail": sn.get("thumbnails", {}).get("default", {}).get("url", ""),
            "subs": stt.get("subscriberCount", "0"),
            "views": stt.get("viewCount", "0"),
            "videos": stt.get("videoCount", "0"),
        })
    return out


def get_all_channels() -> list[dict]:
    """Kanaly ze WSZYSTKICH podlaczonych kont. Bledy per-konto nie wywalaja calosci."""
    out = []
    for label in list_accounts():
        try:
            out.extend(get_channels(label))
        except Exception as e:
            out.append({"account": label, "error": str(e), "title": f"[blad: {label}]",
                        "subs": "?", "views": "?", "videos": "?"})
    return out


def _uploads_playlist(yt) -> str | None:
    r = yt.channels().list(part="contentDetails", mine=True).execute()
    items = r.get("items", [])
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_channel_videos(account: str, limit: int = 50) -> list[dict]:
    """Ostatnie filmy kanalu z ich statusem i data publikacji (w tym zaplanowane)."""
    yt = _youtube_service(account)
    pl = _uploads_playlist(yt)
    if not pl:
        return []
    ids = []
    req = yt.playlistItems().list(part="contentDetails", playlistId=pl, maxResults=min(limit, 50))
    resp = req.execute()
    for it in resp.get("items", []):
        ids.append(it["contentDetails"]["videoId"])
    if not ids:
        return []
    vids = yt.videos().list(part="snippet,status", id=",".join(ids[:50])).execute()
    out = []
    for it in vids.get("items", []):
        st = it.get("status", {})
        sn = it.get("snippet", {})
        out.append({
            "id": it["id"],
            "title": sn.get("title", ""),
            "privacy": st.get("privacyStatus", ""),
            "publish_at": st.get("publishAt"),          # ustawione tylko dla zaplanowanych
            "published_at": sn.get("publishedAt"),
        })
    return out


def get_scheduled(account: str) -> list[dict]:
    """Filmy ZAPLANOWANE (prywatne z ustawiona data publikacji w przyszlosci)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    out = []
    for v in get_channel_videos(account):
        pa = v.get("publish_at")
        if v.get("privacy") == "private" and pa:
            try:
                dt = datetime.fromisoformat(pa.replace("Z", "+00:00"))
            except Exception:
                continue
            if dt > now:
                v["publish_dt"] = dt
                out.append(v)
    out.sort(key=lambda x: x["publish_dt"])
    return out


def last_scheduled_datetime(account: str):
    """Zwraca date/godzine ostatniego zaplanowanego filmu (albo None)."""
    sched = get_scheduled(account)
    return sched[-1]["publish_dt"] if sched else None


def upload_video(file_path: str, title: str, description: str,
                 tags: list[str] | None = None, privacy: str = "private",
                 made_for_kids: bool = False, self_declared_ai: bool = True,
                 account: str = "glowne", publish_at: str | None = None) -> dict:
    """Wgrywa film na kanal wybranego konta.

    publish_at: ISO8601 (np. '2026-07-24T19:00:00Z') -> film ZAPLANOWANY:
        ladowany jako prywatny i sam upublicznia sie o tej porze (natywne YT).
    privacy (gdy brak publish_at): private|unlisted|public.
    """
    from googleapiclient.http import MediaFileUpload

    yt = _youtube_service(account)
    status = {
        "selfDeclaredMadeForKids": made_for_kids,
        "containsSyntheticMedia": self_declared_ai,
    }
    if publish_at:
        # Zaplanowany: MUSI byc prywatny + data publikacji.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at
    else:
        status["privacyStatus"] = privacy

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags or [],
            "categoryId": "22",
        },
        "status": status,
    }
    media = MediaFileUpload(file_path, chunksize=-1, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _status, response = request.next_chunk()
    vid = response["id"]
    return {"id": vid, "url": f"https://youtu.be/{vid}", "account": account}
