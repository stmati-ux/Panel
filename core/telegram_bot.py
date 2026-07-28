"""
Telegram: powiadomienia z panelu + zdalne komendy.

Konfiguracja: secrets/telegram.json
    { "token": "123456:AAG...", "chat_id": null, "pair_code": "1234" }

Parowanie (zabezpieczenie, zeby obcy nie przejal bota):
1. Uzytkownik wkleja token -> panel generuje 4-cyfrowy pair_code (w raporcie).
2. Uzytkownik pisze do SWOJEGO bota na TG:  /start 1234
3. Bot zapamietuje chat_id wlasciciela - od tej pory slucha TYLKO jego.

Komendy: /status /generuj_horror /generuj_dino /zaplanuj /stop /wznow /pomoc
Powiadomienia: kazdy raport paneli leci na TG (gdy sparowano).
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

SECRETS = Path(__file__).resolve().parent.parent / "secrets" / "telegram.json"

_started = False


# ---------- konfiguracja ----------

def _cfg() -> dict:
    if not SECRETS.exists():
        return {}
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cfg(cfg: dict) -> None:
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(json.dumps(cfg, indent=1), encoding="utf-8")


def is_configured() -> bool:
    return bool(_cfg().get("token"))


def is_paired() -> bool:
    return bool(_cfg().get("chat_id"))


def setup_token(token: str) -> str:
    """Zapisuje token, generuje kod parowania. Zwraca kod."""
    cfg = _cfg()
    cfg["token"] = token.strip()
    cfg["chat_id"] = None
    cfg["pair_code"] = str(random.randint(1000, 9999))
    _save_cfg(cfg)
    return cfg["pair_code"]


# ---------- wysylanie ----------

def _api(method: str, payload: dict) -> dict:
    cfg = _cfg()
    if not cfg.get("token"):
        raise RuntimeError("Brak tokena Telegram")
    url = f"https://api.telegram.org/bot{cfg['token']}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def send(text: str) -> bool:
    """Wysyla wiadomosc do wlasciciela (jesli sparowano)."""
    cfg = _cfg()
    if not (cfg.get("token") and cfg.get("chat_id")):
        return False
    try:
        _api("sendMessage", {"chat_id": cfg["chat_id"], "text": text[:4000]})
        return True
    except Exception:
        return False


def send_html(text: str) -> bool:
    """Jak send(), ale z formatowaniem HTML (pogrubienia itp.)."""
    cfg = _cfg()
    if not (cfg.get("token") and cfg.get("chat_id")):
        return False
    try:
        _api("sendMessage", {"chat_id": cfg["chat_id"], "text": text[:4000],
                             "parse_mode": "HTML", "disable_web_page_preview": True})
        return True
    except Exception:
        # fallback bez formatowania (gdyby HTML sie wywalil na znakach)
        return send(text.replace("<b>", "").replace("</b>", ""))


ROOM_EMOJI = {
    "youtube": "🎬", "trading": "📈",
    "dropshipping": "🛒", "system": "⚙️",
}


def _short_report(entry: dict) -> str:
    """Zwiezла, czytelna wersja raportu na Telegram (nie sciana tekstu)."""
    room = entry.get("room", "system")
    emoji = ROOM_EMOJI.get(room, "•")
    ch = entry.get("channel")
    title = (entry.get("title") or "").strip()
    summary = entry.get("summary") or ""

    # naglowek: emoji + pokoj (+kanal) + ew. flaga
    head = f"{emoji} {room.capitalize()}"
    if ch:
        head += f" · {ch}"
    if entry.get("needs_action"):
        head += "  ⚠️ WYMAGA CIEBIE"

    # tresc: tylko istotne linie (klucz: wartosc), max 5, krotkie
    lines = []
    for ln in summary.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # pomijamy techniczne tracebacki w powiadomieniu (skrot)
        if ln.startswith(("File \"", "Traceback", "  File", "self.", "return ")):
            continue
        lines.append(ln)
        if len(lines) >= 5:
            break
    body = "\n".join(lines)
    if len(body) > 350:
        body = body[:350] + "…"

    msg = f"{head}\n<b>{title}</b>"
    if body:
        msg += f"\n{body}"
    return msg


def notify_report(entry: dict) -> None:
    """Wolane przez state.add_report - pcha KROTKI raport na TG (best effort)."""
    if not is_paired():
        return
    threading.Thread(target=send_html, args=(_short_report(entry),),
                     daemon=True).start()


# ---------- komendy (long polling) ----------

HELP = (
    "📋 <b>Komendy panelu</b>\n\n"
    "📊 /status — skrót co się dzieje\n"
    "📈 /trading — portfel + reguła sprzedaży\n"
    "🎬 /zaplanuj — zaplanuj filmy na YT\n"
    "🎥 /generuj_horror — fabryka horror\n"
    "🎥 /generuj_dino — fabryka dino\n"
    "⏹ /stop_gen — PRZERWIJ generowanie (ComfyUI+skrypty)\n"
    "🔴 /stop — zatrzymaj cały panel\n"
    "🟢 /wznow — wznów panel\n"
    "❓ /pomoc — ta lista"
)


def _handle(text: str) -> str:
    from core import state, generator, publisher, control   # lazy - bez cykli
    t = text.strip().lower()

    if t.startswith("/status"):
        st = state.load()
        tryb = "🔴 LIVE" if st.get("mode") == "live" else "🔵 SET-UP"
        run = "🟢 działa" if st.get("running") else "⏸ STOP"
        auto = "✅" if st.get("auto") else "—"
        comfy = "🟢" if control.comfyui_alive() else "🔴 padło"
        lines = [f"<b>Panel:</b> {run} · {tryb} · automat {auto}",
                 f"<b>ComfyUI:</b> {comfy}", ""]
        busy = {rid: r.get("status") for rid, r in st["rooms"].items()
                if r.get("status") == "working"}
        if busy:
            lines.append("⚙️ Pracuje: " + ", ".join(
                st["rooms"][r]["name"] for r in busy))
        for acc in generator.FACTORIES:
            q = generator.queue_size(acc)
            mark = " ⏳" if generator.is_running(acc) else ""
            lines.append(f"🎬 {acc}: {q} filmów w paczce{mark}")
        return "\n".join(lines)

    if t.startswith("/trading"):
        from core import tracker
        pf = trading_portfolio()
        ex = tracker.exit_strategies()
        best = ex["strategies"][0] if ex.get("strategies") else None
        msg = [f"📈 <b>Trading (paper)</b>",
               f"Portfel: ${pf['total_in']} → ${pf['total_now']} "
               f"({pf['total_pnl_pct']:+.1f}%)",
               f"Sygnały: {len(pf['rows'])}"]
        if best:
            msg.append(f"⭐ Najlepsza reguła: <b>{best['name']}</b> "
                       f"({best['avg_pnl_pct']:+.1f}%)")
        return "\n".join(msg)

    if t.startswith("/zaplanuj"):
        st = state.load()
        mode = st.get("mode", "setup")
        for acc in publisher.CHANNELS:
            threading.Thread(target=publisher.publish_next, args=(acc, mode),
                             daemon=True).start()
        return f"🎬 Planowanie odpalone (tryb: {mode}). Raporty przyjdą za chwilę."

    if t.startswith("/generuj_horror"):
        generator.run_factory("after3am")
        return "🎥 Fabryka horror odpalona — dam znać raportem."

    if t.startswith("/generuj_dino"):
        generator.run_factory("dinovault")
        return "🎥 Fabryka dino odpalona — dam znać raportem."

    if t.startswith("/stop_gen"):
        done = control.kill_generation()
        state.add_report("system", "PRZERWANO GENEROWANIE (z Telegrama)",
                         "\n".join(done))
        return "⏹ <b>Przerwano generowanie.</b>\n" + "\n".join(done)

    if t.startswith("/stop"):
        def _mut(s): s["running"] = False
        state.update(_mut)
        control.kill_generation()
        state.add_report("system", "STOP (z Telegrama)", "Panel zatrzymany zdalnie.")
        return "🔴 <b>Panel ZATRZYMANY</b> (przerwano też generowanie)."

    if t.startswith("/wznow") or t.startswith("/wznów"):
        def _mut(s): s["running"] = True
        state.update(_mut)
        state.add_report("system", "Wznowiono (z Telegrama)", "Panel wznowiony zdalnie.")
        return "🟢 Panel wznowiony."

    return HELP


def trading_portfolio():
    from core import trading
    return trading.portfolio()


def _poll_loop() -> None:
    offset = 0
    while True:
        cfg = _cfg()
        if not cfg.get("token"):
            time.sleep(30)
            continue
        try:
            url = (f"https://api.telegram.org/bot{cfg['token']}/getUpdates"
                   f"?timeout=50&offset={offset}")
            with urllib.request.urlopen(url, timeout=70) as r:
                data = json.loads(r.read())
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = msg.get("text", "") or ""
                if not chat_id or not text:
                    continue

                # Parowanie: /start <kod>
                if text.strip().lower().startswith("/start"):
                    parts = text.split()
                    if (not cfg.get("chat_id") and len(parts) > 1
                            and parts[1] == cfg.get("pair_code")):
                        cfg["chat_id"] = chat_id
                        _save_cfg(cfg)
                        _api("sendMessage", {"chat_id": chat_id,
                             "text": "Sparowano! Panel bedzie tu wysylal raporty.\n\n" + HELP})
                    elif cfg.get("chat_id") == chat_id:
                        _api("sendMessage", {"chat_id": chat_id, "text": HELP})
                    else:
                        _api("sendMessage", {"chat_id": chat_id,
                             "text": "Podaj kod parowania: /start <kod> (kod jest w panelu)."})
                    continue

                # Tylko wlasciciel:
                if cfg.get("chat_id") != chat_id:
                    continue
                try:
                    reply = _handle(text)
                except Exception as e:
                    reply = f"Blad: {e}"
                try:
                    _api("sendMessage", {"chat_id": chat_id, "text": reply[:4000],
                                         "parse_mode": "HTML",
                                         "disable_web_page_preview": True})
                except Exception:
                    _api("sendMessage", {"chat_id": chat_id,
                                         "text": reply.replace("<b>", "").replace("</b>", "")[:4000]})
        except Exception:
            time.sleep(10)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_poll_loop, daemon=True).start()
