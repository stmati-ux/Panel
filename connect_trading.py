"""
Podlaczenie TWOJEGO konta Telegram do czytania grupy z sygnalami.
Metoda: KOD QR (bez kodow w wiadomosciach!).

Uruchomienie (PowerShell):
    cd C:\\Users\\mstez\\OneDrive\\Desktop\\optiaio\\panel
    python connect_trading.py

Co sie stanie:
1. Otworzy sie obrazek z kodem QR.
2. Na telefonie: Telegram -> Ustawienia -> Urzadzenia -> "Podlacz urzadzenie"
   -> zeskanuj kod z ekranu.
3. Skrypt pokaze liste Twoich grup - wybierz te z sygnalami.
"""
import asyncio
import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG = BASE / "secrets" / "telegram_user.json"
SESSION = BASE / "secrets" / "tg_user"
QR_PNG = BASE / "secrets" / "qr_login.png"


async def main():
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    import qrcode

    print("=== Podlaczenie konta Telegram (metoda QR) ===\n")
    cfg = {}
    if CFG.exists():
        try:
            cfg = json.loads(CFG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    api_id = cfg.get("api_id") or input("api_id (z my.telegram.org): ").strip()
    api_hash = cfg.get("api_hash") or input("api_hash: ").strip()

    client = TelegramClient(str(SESSION), int(api_id), api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        print("\nGeneruje kod QR...")
        print("Na telefonie przygotuj skaner: Telegram -> Ustawienia -> Urzadzenia")
        print("-> 'Podlacz urzadzenie'. Skanuj OD RAZU po pojawieniu sie kodu!\n")
        qr = await client.qr_login()
        logged = False
        for attempt in range(10):   # QR wygasa co ~30s - odswiezamy czesto
            # QR w konsoli (skanuj ten!):
            q = qrcode.QRCode(border=1)
            q.add_data(qr.url)
            q.print_ascii(invert=True)
            # ...i zapasowo jako obrazek:
            png = QR_PNG.parent / f"qr_login_{attempt+1}.png"
            qrcode.make(qr.url).save(str(png))
            os.startfile(str(png))
            print(f"[kod {attempt+1}/10] SKANUJ TERAZ - odswieze za 25 sekund...")
            try:
                await asyncio.wait_for(qr.wait(), timeout=25)
                logged = True
                break
            except asyncio.TimeoutError:
                print("Odswiezam kod...\n")
                await qr.recreate()
            except SessionPasswordNeededError:
                pwd = input("Masz wlaczona weryfikacje dwuetapowa - podaj haslo: ")
                await client.sign_in(password=pwd)
                logged = True
                break
        # sprzatanie starych QR
        for p in QR_PNG.parent.glob("qr_login_*.png"):
            try: p.unlink()
            except Exception: pass
        if not logged:
            print("\n[!] Nie zeskanowano kodu. Uruchom skrypt jeszcze raz.")
            await client.disconnect()
            return

    me = await client.get_me()
    print(f"\n[ok] Zalogowano jako: {me.first_name} (@{me.username})\n")

    print("Twoje grupy/kanaly:")
    dialogs = []
    async for d in client.iter_dialogs(limit=60):
        if d.is_group or d.is_channel:
            dialogs.append(d)
    for i, d in enumerate(dialogs):
        print(f"  [{i}] {d.name}")

    raw = input("\nNumery grup z sygnalami (mozna kilka, po przecinku, np. 2,7): ").strip()
    idxs = [int(x) for x in raw.replace(" ", "").split(",") if x != ""]
    chosen = [dialogs[i] for i in idxs]

    cfg = {"api_id": int(api_id), "api_hash": api_hash,
           "groups": [g.id for g in chosen],
           "group_names": [g.name for g in chosen]}
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    await client.disconnect()
    try:
        QR_PNG.unlink()
    except Exception:
        pass
    print("\n[ok] Zapisano grupy:")
    for g in chosen:
        print(f"  - {g.name}")
    print("Teraz zrestartuj panel (albo powiedz Claude'owi) - czytnik wystartuje sam.")


if __name__ == "__main__":
    asyncio.run(main())
