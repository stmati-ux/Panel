"""
Panel dowodzenia - lokalny, prywatny dashboard.

WAZNE: serwer nasluchuje TYLKO na 127.0.0.1, wiec strona jest dostepna
wylacznie z tego komputera. Nie da sie na nia wejsc z sieci / z innego urzadzenia.

Uruchomienie:
    python app.py
Potem otworz w przegladarce:  http://127.0.0.1:5000
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from core import state, supervisor, youtube_api, publisher, scheduler, telegram_bot

app = Flask(__name__)


def _reset_orphaned_statuses():
    """Po restarcie panelu watki generacji nie zyja - status 'working'
    zostalby na zawsze. Resetujemy go przy starcie. Przy okazji dokladamy
    nowe pokoje z DEFAULT_STATE, ktorych nie ma w starym state.json."""
    def _mut(s):
        for room_ in s.get("rooms", {}).values():
            if room_.get("status") == "working":
                room_["status"] = "idle"
        for rid, room_ in state.DEFAULT_STATE["rooms"].items():
            if rid not in s.get("rooms", {}):
                s["rooms"][rid] = dict(room_)
    state.update(_mut)


_reset_orphaned_statuses()
scheduler.start()       # automat publikacji (dziala tylko gdy wlaczony w panelu)
telegram_bot.start()    # nasluch komend TG (jesli skonfigurowano token)

from core import trading as _trading
_trading.start_reader()  # czytnik grupy sygnalowej (jesli podlaczono konto TG)

from core import radar as _radar
_radar.start()           # radar rynku Solana (trending + nowe tokeny, co 3 min)

from core import watchdog as _watchdog
_watchdog.start()        # pilnuje ComfyUI - podnosi je gdy padnie

from core import strategies as _strategies
_strategies.start()      # pojedynek strategii tradingowych (paper, na zywo)

from core import tracker as _tracker
_tracker.start()         # sledzenie trajektorii tokenow + alarm rugpull


@app.route("/telegram/setup", methods=["POST"])
def telegram_setup():
    """Zapisuje token bota i generuje kod parowania."""
    token = request.form.get("token", "").strip()
    if token and ":" in token:
        code = telegram_bot.setup_token(token)
        state.add_report("system", "Telegram: token zapisany",
                         f"Teraz napisz do SWOJEGO bota na Telegramie:\n"
                         f"    /start {code}\n"
                         f"Po sparowaniu raporty beda przychodzic na telefon.")
    else:
        state.add_report("system", "Telegram: zly token",
                         "Token wyglada niepoprawnie (powinien zawierac ':').",
                         needs_action=True)
    return _back()

HOST = "127.0.0.1"   # tylko lokalnie - prywatne
PORT = 5000

# Ladne nazwy do raportow: pokoj + kanal.
ROOM_LABELS = {
    "youtube": "YouTube",
    "dropshipping": "Dropshipping",
    "system": "System",
    # legacy (stare raporty pisane per-konto):
    "after3am": "YouTube",
    "dinovault": "YouTube",
}
LEGACY_CHANNELS = {"after3am": "After3AMFiles", "dinovault": "DinoVault"}


def _back():
    return redirect(request.referrer or url_for("dashboard"))


def _pretty_reports(reports, room_filter: str | None = None, limit: int = 30):
    """Ujednolica raporty do wyswietlenia: pokoj, kanal, naglowek."""
    out = []
    for r in reports:
        room = r.get("room", "system")
        mapped_room = ROOM_LABELS.get(room, room.capitalize())
        if room_filter and mapped_room.lower() != room_filter.lower() \
                and room != room_filter:
            continue
        channel = r.get("channel") or LEGACY_CHANNELS.get(room)
        rr = dict(r)
        rr["room_label"] = mapped_room
        rr["channel_label"] = channel
        out.append(rr)
        if len(out) >= limit:
            break
    return out


@app.route("/")
def dashboard():
    st = state.load()
    yt = st.get("youtube", {})
    reports = _pretty_reports(st.get("reports", []), limit=8)
    return render_template("dashboard.html", st=st, yt=yt, reports=reports,
                           active="dash")


@app.route("/room/<room_id>")
def room(room_id):
    st = state.load()
    if room_id not in st.get("rooms", {}):
        abort(404)
    if room_id == "youtube":
        from core import generator
        yt = st.get("youtube", {})
        reports = _pretty_reports(st.get("reports", []), room_filter="youtube",
                                  limit=20)
        gen = {acc: {"running": generator.is_running(acc),
                     "queue": generator.queue_size(acc)}
               for acc in generator.FACTORIES}
        return render_template("room_youtube.html", st=st, yt=yt,
                               reports=reports, active=room_id,
                               room=st["rooms"][room_id], gen=gen)
    if room_id == "trading":
        from core import trading
        import json as _json
        group_name = ""
        if trading.USER_CFG.exists():
            try:
                _c = _json.loads(trading.USER_CFG.read_text(encoding="utf-8"))
                group_name = ", ".join(_c.get("group_names") or
                                       ([_c["group_name"]] if _c.get("group_name") else []))
            except Exception:
                pass
        reports = _pretty_reports(st.get("reports", []), room_filter="trading",
                                  limit=15)
        from core import tracker, strategies
        tracked = tracker._load().get("tokens", {})
        tracked_rows = sorted(
            (tracker.summary(a) for a in tracked),
            key=lambda s: s and s.get("gain_to_peak_pct", 0), reverse=True)
        tracked_rows = [t for t in tracked_rows if t][:25]
        return render_template("room_trading.html", st=st, active=room_id,
                               room=st["rooms"][room_id],
                               reader_on=trading.reader_configured() and trading.session_exists(),
                               group_name=group_name,
                               pf=trading.portfolio(),
                               insights=tracker.insights(),
                               exits=tracker.exit_strategies(),
                               standings=strategies.standings(),
                               tracked=tracked_rows,
                               reports=reports)
    return render_template("room_other.html", st=st, active=room_id,
                           room=st["rooms"][room_id])


@app.route("/api/state")
def api_state():
    return jsonify(state.load())


@app.route("/youtube/refresh", methods=["POST"])
def youtube_refresh():
    """Pobiera kanaly + statystyki + harmonogram i zapisuje w stanie."""
    try:
        channels = youtube_api.get_all_channels()
        err = None
    except Exception as e:
        channels = []
        err = str(e)

    from datetime import timedelta
    schedule = {}
    for acc in youtube_api.list_accounts():
        try:
            sched = youtube_api.get_scheduled(acc)
            last = sched[-1]["publish_dt"] if sched else None
            nxt = (last + timedelta(days=1)) if last else None
            schedule[acc] = {
                "items": [
                    {"when": s["publish_dt"].strftime("%Y-%m-%d %H:%M"), "title": s["title"]}
                    for s in sched
                ],
                "last": last.strftime("%Y-%m-%d %H:%M") if last else None,
                "next_slot": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
            }
        except Exception as e:
            schedule[acc] = {"items": [], "error": str(e)}

    def _mut(s):
        s["youtube"] = {
            "connected": youtube_api.is_connected(),
            "accounts": youtube_api.list_accounts(),
            "channels": channels,
            "schedule": schedule,
            "refreshed": state._now(),
            "error": err,
        }
    state.update(_mut)
    return _back()


@app.route("/auto", methods=["POST"])
def toggle_auto():
    """Wlacza/wylacza automat codziennej publikacji."""
    def _mut(s):
        s["auto"] = not s.get("auto", False)
    st = state.update(_mut)
    if st.get("auto"):
        state.add_report("system", "Automat WLACZONY",
                         "Panel bedzie sam, raz dziennie, dokladal 1 film na kanal "
                         "(nastepny wolny slot). Realna wysylka tylko w trybie LIVE.")
    else:
        state.add_report("system", "Automat wylaczony",
                         "Codzienna publikacja zatrzymana. Przyciski reczne dzialaja dalej.")
    return _back()


@app.route("/publish/<account>", methods=["POST"])
def publish(account):
    """Zaplanuj nastepny gotowy film na dany kanal (respektuje tryb setup/live)."""
    st = state.load()
    if not st.get("running", True):
        state.add_report("youtube", "Pominieto — STOP aktywny",
                         "Panel jest zatrzymany (STOP).",
                         channel=LEGACY_CHANNELS.get(account, account))
        return _back()
    mode = st.get("mode", "setup")

    def _work():
        publisher.publish_next(account, mode)
    import threading
    threading.Thread(target=_work, daemon=True).start()
    return _back()


@app.route("/generate/<account>", methods=["POST"])
def generate_videos(account):
    """Odpala fabryke nowych filmow dla kanalu (w tle)."""
    from core import generator
    st = state.load()
    if not st.get("running", True):
        state.add_report("youtube", "Pominieto — STOP aktywny",
                         "Panel jest zatrzymany (STOP).")
        return _back()
    generator.run_factory(account)
    return _back()


@app.route("/trading/test", methods=["POST"])
def trading_test():
    """Reczny test sygnalu - dziala jak wiadomosc z grupy."""
    from core import trading
    text = request.form.get("text", "").strip()
    if text:
        def _work():
            cards = trading.handle_group_message(text, source="test reczny")
            if not cards:
                state.add_report("trading", "Nie znaleziono tokena",
                                 f"W tekscie nie bylo rozpoznawalnego $TICKERA ani "
                                 f"adresu, albo DexScreener nie zna tokena:\n{text[:200]}")
        import threading
        threading.Thread(target=_work, daemon=True).start()
    return _back()


@app.route("/room/<room_id>/run", methods=["POST"])
def room_run(room_id):
    supervisor.run_room(room_id)
    return _back()


@app.route("/mode", methods=["POST"])
def set_mode():
    new_mode = request.form.get("mode", "setup")
    if new_mode not in ("setup", "live"):
        new_mode = "setup"

    def _mut(s):
        s["mode"] = new_mode
    state.update(_mut)
    return _back()


def _kill_generation() -> list[str]:
    """Awaryjnie przerywa WSZYSTKIE generacje: ComfyUI + skrypty castingu/fabryk."""
    done = []
    # 1) przerwij biezacy render w ComfyUI + wyczysc kolejke
    try:
        import urllib.request as _u
        _u.urlopen(_u.Request("http://127.0.0.1:8188/interrupt", data=b"{}",
                              headers={"Content-Type": "application/json"}), timeout=5)
        _u.urlopen(_u.Request("http://127.0.0.1:8188/queue", data=b'{"clear": true}',
                              headers={"Content-Type": "application/json"}), timeout=5)
        done.append("ComfyUI: przerwano render i wyczyszczono kolejke")
    except Exception:
        done.append("ComfyUI: brak odpowiedzi (moze juz nie renderuje)")
    # 2) ubij skrypty castingu i fabryk (PO NAZWIE PLIKU, nie wszystkie python!)
    import subprocess as _sp
    ps = (r"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          r"Where-Object { $_.CommandLine -match 'casting|factory' } | "
          r"ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        _sp.run(["powershell", "-NoProfile", "-Command", ps], timeout=15,
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        done.append("Skrypty castingu/fabryk: zatrzymane")
    except Exception:
        done.append("Skrypty: nie udalo sie ubic")
    return done


@app.route("/stop-generation", methods=["POST"])
def stop_generation():
    """Czerwony przycisk: NATYCHMIAST przerywa cokolwiek sie generuje."""
    def _mut(s):
        for room_ in s["rooms"].values():
            if room_.get("status") == "working":
                room_["status"] = "przerwano"
    state.update(_mut)
    done = _kill_generation()
    state.add_report("system", "PRZERWANO GENEROWANIE",
                     "Zatrzymano wszystkie generacje:\n" + "\n".join(f"  - {d}" for d in done)
                     + "\n\nPanel dziala dalej (publikacje/planowanie bez zmian).")
    return _back()


@app.route("/stop", methods=["POST"])
def stop():
    def _mut(s):
        s["running"] = False
        for room_ in s["rooms"].values():
            if room_.get("status") == "working":
                room_["status"] = "stopped"
    state.update(_mut)
    _kill_generation()   # pelny STOP zatrzymuje tez to, co sie generuje
    state.add_report("system", "STOP — panel zatrzymany",
                     "Nacisnieto czerwony przycisk STOP. Przerwano generacje, agenci nie beda "
                     "uruchamiani dopoki nie wznowisz.")
    return _back()


@app.route("/resume", methods=["POST"])
def resume():
    def _mut(s):
        s["running"] = True
    state.update(_mut)
    state.add_report("system", "Wznowiono prace",
                     "Panel wznowiony. Agenci moga dzialac.")
    return _back()


@app.route("/reports/clear", methods=["POST"])
def clear_reports():
    def _mut(s):
        s["reports"] = []
    state.update(_mut)
    return _back()


if __name__ == "__main__":
    print(f"Panel dziala prywatnie na http://{HOST}:{PORT}  (tylko ten komputer)")
    app.run(host=HOST, port=PORT, debug=False)
