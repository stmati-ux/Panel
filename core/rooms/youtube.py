"""
Pokoj YouTube.

Etap 1 (teraz): tryb "na sucho" (dry-run) - agent przygotowuje plan tresci
(pomysl na film, zarys scenariusza, opis, tagi, sugerowany czas publikacji)
i sklada raport. NIC nie jest publikowane.

Etap 2 (pozniej, po podlaczeniu Composio/YouTube OAuth):
- generowanie tekstu przez model (miejsce oznaczone TODO nizej),
- realne wrzucanie na kanal (miejsce oznaczone TODO nizej).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from core import state

ROOM_ID = "youtube"

# Prosta pula pomyslow na start. Docelowo zastapi to generacja przez model.
_TEMATY = [
    ("3 bledy, przez ktore Twoj kanal nie rosnie", "poradnik"),
    ("Testuje popularny trend przez 7 dni", "eksperyment"),
    ("Zbudowalem to w 24h - efekt zaskakuje", "vlog"),
    ("Rzeczy, ktorych zalowalem na starcie", "storytelling"),
    ("Poradnik dla poczatkujacych - krok po kroku", "poradnik"),
]

_TAGI_BAZOWE = ["poradnik", "tutorial", "polska", "2026", "howto"]


def _zbuduj_plan() -> dict:
    tytul, format_ = random.choice(_TEMATY)
    # TODO(etap 2): tu podpiac realne generowanie scenariusza przez model
    # (Claude API / Composio), zamiast szkieletu ponizej.
    scenariusz = [
        "Hook (0-5s): mocne zdanie, ktore zatrzymuje widza.",
        "Wprowadzenie (5-20s): co widz wyniesie z filmu.",
        "Rozwiniecie: 3 glowne punkty z przykladami.",
        "Zakonczenie: podsumowanie + zacheta do subskrypcji.",
    ]
    opis = (
        f"{tytul}\n\n"
        "W tym filmie pokazuje krok po kroku, jak to zrobic.\n"
        "Zostaw suba i daj lapke w gore, jesli pomoglo!\n\n"
        "#shorts" if format_ == "eksperyment" else f"{tytul}\n\nSubskrybuj po wiecej."
    )
    tagi = _TAGI_BAZOWE + [format_]
    publikacja = (datetime.now() + timedelta(days=1)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    return {
        "tytul": tytul,
        "format": format_,
        "scenariusz": scenariusz,
        "opis": opis,
        "tagi": tagi,
        "sugerowana_publikacja": publikacja.strftime("%Y-%m-%d %H:%M"),
    }


def run(mode: str) -> dict:
    """Uruchamia pokoj YouTube. Zwraca wynik pracy (dict)."""
    state.set_room_status(ROOM_ID, "working")

    plan = _zbuduj_plan()

    linie = [
        f"Tytul:  {plan['tytul']}",
        f"Format: {plan['format']}",
        "Scenariusz:",
        *[f"  - {s}" for s in plan["scenariusz"]],
        f"Tagi:   {', '.join(plan['tagi'])}",
        f"Publikacja (sugerowana): {plan['sugerowana_publikacja']}",
    ]
    output = "\n".join(linie)

    if mode == "live":
        # TODO(etap 2): realna publikacja przez YouTube API / Composio.
        # Na razie brak polaczenia -> zglaszamy to jako zadanie dla Ciebie.
        state.set_room_status(ROOM_ID, "done", output)
        state.add_report(
            ROOM_ID,
            "Plan gotowy — brak polaczenia z YouTube",
            output + "\n\n[!] Tryb LIVE wybrany, ale kanal YouTube nie jest jeszcze "
            "podlaczony (Composio/OAuth). Plan przygotowany, publikacja wstrzymana.",
            needs_action=True,
            mode=mode,
        )
    else:
        state.set_room_status(ROOM_ID, "done", output)
        state.add_report(
            ROOM_ID,
            "Plan tresci przygotowany (na sucho)",
            output + "\n\n[ok] Tryb DRY-RUN: nic nie opublikowano. To podglad jakosci.",
            needs_action=False,
            mode=mode,
        )

    return plan
