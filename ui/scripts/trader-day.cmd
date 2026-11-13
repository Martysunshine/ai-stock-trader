@echo off
rem Brings the trading day up after sign-in: database, console, stocks paper bot, practice trader.
rem Each opens in its own window and only when it is missing (docs/runbook.md section 9.2).
rem
rem To use it: copy this file into the Windows Startup folder (press Win+R, type shell:startup)
rem and set the user environment variable TRADER_REPO to the repository folder, so the copy can find it.
rem To switch it off: delete the copy there.
title Trading day start
echo Waiting 60 seconds for the network, then starting the trading day...
timeout /t 60 /nobreak >nul
cd /d "%~dp0..\.."
if not exist package.json if defined TRADER_REPO cd /d "%TRADER_REPO%"
if not exist package.json echo Repository not found. Set the user environment variable TRADER_REPO to the repository folder.
if not exist package.json timeout /t 30 >nul
if not exist package.json exit /b 1
call npm run day
timeout /t 30 >nul
