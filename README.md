# Panel dowodzenia

Prywatny, lokalny dashboard do zarzadzania agentami ("pokojami").
Etap 1: panel + pokoj **YouTube** w trybie "na sucho".

## Uruchomienie

```bash
cd panel
python app.py
```

Potem otworz w przegladarce: **http://127.0.0.1:5000**

Serwer nasluchuje tylko na `127.0.0.1`, wiec panel jest dostepny
wylacznie z tego komputera (nie z sieci, nie z innego urzadzenia).

## Jak to dziala

- **Pokoje** = agenci (na razie: YouTube, Trading; kolejne: Dropshipping).
- **Nadzorca** (`core/supervisor.py`) rozdziela zadania i pilnuje STOP-u i trybu.
- **Tryb "na sucho" (dry-run)** - agent przygotowuje prace, ale niczego nie publikuje.
- **Tryb LIVE** - docelowo realne dzialanie (wymaga podlaczenia YouTube/Composio).
- **STOP** - czerwony przycisk: zatrzymuje uruchamianie agentow.
- **Raporty** - kazde uruchomienie zostawia wpis z podsumowaniem pracy.

## Struktura

```
panel/
  app.py                  # serwer Flask (127.0.0.1)
  core/
    state.py              # stan panelu (data/state.json)
    supervisor.py         # agent-nadzorca
    rooms/
      youtube.py          # pokoj YouTube
  templates/dashboard.html
  data/state.json         # tworzony automatycznie
```

## Nastepne kroki (etap 2)

1. Podlaczyc YouTube przez Composio/OAuth (realna publikacja).
2. Podpiac generowanie scenariuszy przez model (Claude API) - miejsca oznaczone `TODO(etap 2)`.
3. Dodac pokoj Dropshipping (schemat jak w `youtube.py`).
