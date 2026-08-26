@echo off
chcp 65001 >nul 2>&1
title Spotify Account Migrator
cd /d "%~dp0"

echo ==============================================
echo    Spotify Account Migrator
echo ==============================================
echo.

if exist "spotify_migrate.py.txt" if not exist "spotify_migrate.py" ren "spotify_migrate.py.txt" "spotify_migrate.py"

if exist "spotify_migrate.py" goto :fileok
echo [ERROR] spotify_migrate.py not found.
echo This launcher must sit in the SAME FOLDER as the script.
echo.
pause
exit /b 1
:fileok

set "PY="
where py >nul 2>&1 && set "PY=py"
if defined PY goto :pyok
where python >nul 2>&1 && set "PY=python"
if defined PY goto :pyok
echo [ERROR] Python is not installed.
echo.
echo    1. Go to https://www.python.org/downloads/
echo    2. Run the installer
echo    3. IMPORTANT: tick "Add python.exe to PATH" on the first screen
echo    4. Finish, then run this file again
echo.
pause
exit /b 1
:pyok

echo [ok] Python found (%PY%)
echo [..] Checking dependencies...
%PY% -m pip install --quiet --upgrade --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :piperr
echo [ok] Dependencies ready
echo.
goto :run
:piperr
echo [ERROR] Could not install dependencies. Check your internet connection.
pause
exit /b 1

:run
%PY% spotify_migrate.py
echo.
pause
