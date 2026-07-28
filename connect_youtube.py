"""
Podlaczenie konta YouTube do panelu (jednorazowo per konto).

Glowne konto:
    python connect_youtube.py
Kolejne konto (z innego maila) - podaj etykiete:
    python connect_youtube.py drugie

Otworzy sie przegladarka -> zaloguj na wybrane konto Google i kliknij "Zezwol".
Kazde konto zapisuje sie osobno; panel pokaze wszystkie kanaly razem.
"""
import sys
from core import youtube_api


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "glowne"

    if not youtube_api.has_client_secret():
        print("[!] Brak pliku secrets/client_secret.json - najpierw wgraj go z Google Cloud.")
        return

    if label in youtube_api.list_accounts():
        print(f"[i] Konto '{label}' jest juz podlaczone.")
        print(f"    Aby podlaczyc na nowo: usun panel/secrets/token_{label}.json i uruchom ponownie.")
        print(f"    Aby dodac INNE konto: python connect_youtube.py <inna_etykieta>")
        return

    print(f"Podlaczam konto '{label}'. Otwieram przegladarke - zaloguj sie i kliknij 'Zezwol'...")
    print("(Jesli masz kilka kont Google, wybierz wlasciwe. Musi byc dodane jako test user!)")
    path = youtube_api.connect(label)
    print(f"[ok] Gotowe! Token zapisany: {path}\n")

    print("Twoje kanaly na tym koncie:")
    for ch in youtube_api.get_channels(label):
        print(f"  - {ch['title']}  (suby: {ch['subs']}, wyswietlenia: {ch['views']}, filmy: {ch['videos']})")

    print("\nPodlaczone konta:", ", ".join(youtube_api.list_accounts()))
    print("Aby dodac kolejny mail: python connect_youtube.py <etykieta>")


if __name__ == "__main__":
    main()
