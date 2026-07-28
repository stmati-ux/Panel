@echo off
title Panel dowodzenia (auto-restart)
cd /d "%~dp0"
echo Otwieram panel w przegladarce za chwile...
start "" http://127.0.0.1:5000
:loop
echo.
echo [%date% %time%] Startuje panel na http://127.0.0.1:5000
echo.
python app.py
echo.
echo [%date% %time%] Panel sie zatrzymal. Restart za 3 sekundy... (zamknij okno aby zakonczyc)
timeout /t 3 /nobreak >nul
goto loop
