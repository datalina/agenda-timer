@echo off
title Agenda Timer bot
rem Opens Discord (if it isn't already open), then starts the bot.
rem Close this window or press Ctrl+C to stop the bot.

if exist "%LOCALAPPDATA%\Discord\Update.exe" start "" "%LOCALAPPDATA%\Discord\Update.exe" --processStart Discord.exe

cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" bot.py

echo.
echo The bot has stopped. If you see an error above, copy it before closing this window.
pause
