@echo off
REM ============================================================
REM  SteamCloudBridge - CROSS-MACHINE round trip (stage 2/2: verify)
REM
REM  WHAT IT DOES  (no game is launched, nothing is played):
REM    * checks Steam is logged in as the EXPECTED account
REM    * shows you ONE line to type into the Steam console
REM      (this build does NOT auto-type - auto-typing never worked
REM       reliably on other machines, so manual is the default)
REM    * then watches the Steam logs by itself for up to 4 minutes
REM    * compares the downloaded bytes with expected.json
REM    * writes reports-cross\cross_bundle.zip   <- send that back
REM
REM  BEFORE YOU RUN IT (see README.md for the Chinese walkthrough):
REM    1) Steam must be RUNNING and logged in as the account shown in
REM       expected.json - NOT your own account.
REM    2) DARK SOULS III must be closed. Do not launch it afterwards
REM       until your friend tells you the cloud copy was restored.
REM    3) expected.json must sit next to this file.
REM    4) It will ask you to type one line into the Steam console.
REM       That is normal, not an error. Just type it and wait.
REM    5) Leave this window open until it says "done".
REM
REM  NOTE: messages are ASCII on purpose. See README.md for Chinese.
REM ============================================================
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "EXP=%~dp0expected.json"
set "OUT=%~dp0reports-cross"
set "EXE=%~dp0SteamCloudBridgeBridge.exe"
set "PYF=%~dp0SteamCloudBridgeBridge.py"

echo.
echo   SteamCloudBridge - cross-machine verify (stage 2/2)
echo   -------------------------------------------------
echo.

if not exist "%EXP%" (
  echo   [ERROR] expected.json is missing in this folder.
  echo           Ask your friend to send it together with this package,
  echo           and keep it next to RUN-CROSS-VERIFY.bat.
  goto :failed_pause
)

if exist "%EXE%" (
  echo   [1/2] running bundled program ^(no Python needed^) ...
  echo.
  "%EXE%" xfer-verify --expected "%EXP%" --out-dir "%OUT%" --attempts 3 --wait 120 --manual --manual-wait 240 %*
  if errorlevel 1 goto :failed
  goto :done
)

if not exist "%PYF%" (
  echo   [ERROR] SteamCloudBridgeBridge.py is missing.
  echo           Keep this .bat in the same folder as the .py file.
  goto :failed_pause
)

set "RT=%~dp0runtime\python.exe"
if exist "%RT%" (
  echo   [1/2] running with the bundled Python runtime ...
  echo.
  "%RT%" "%PYF%" xfer-verify --expected "%EXP%" --out-dir "%OUT%" --attempts 3 --wait 120 --manual --manual-wait 240
  if errorlevel 1 goto :failed
  goto :done
)

set "PY="
where py >nul 2>nul
if %errorlevel% equ 0 set "PY=py -3"
if defined PY goto :have_py
where python >nul 2>nul
if %errorlevel% equ 0 set "PY=python"
if defined PY goto :have_py
where python3 >nul 2>nul
if %errorlevel% equ 0 set "PY=python3"
if defined PY goto :have_py

echo   [ERROR] No bundled program and no Python 3 on this computer.
echo.
echo   This package normally ships with SteamCloudBridgeBridge.exe, which
echo   runs without installing anything. Please check that the file is
echo   still in this folder, next to RUN-CROSS-VERIFY.bat.
goto :failed_pause

:have_py
echo   [1/2] running cross-machine verify with "%PY%" ...
echo.
%PY% "%PYF%" xfer-verify --expected "%EXP%" --out-dir "%OUT%" --attempts 3 --wait 120 --manual --manual-wait 240 %*
if errorlevel 1 goto :failed

:done
echo.
echo   [2/2] done. Opening the report folder ...
echo.
echo   Send this back:       reports-cross\cross_bundle.zip
echo.
echo   IMPORTANT: do NOT launch DARK SOULS III yet. Wait until your
echo   friend confirms the cloud copy has been restored to his save.
echo.
start "" "%OUT%"
echo   Press any key to close this window.
pause >nul
exit /b 0

:failed
echo.
echo   [NOT PASSED] the check did not succeed - read the lines above.
echo   Common causes and what to do:
echo     * "not logged in as this account" - logout, login as the account
echo       shown in expected.json, then run this file again.
echo     * "Steam is not running"          - start Steam, login, retry.
echo     * "DARK SOULS III is running"     - close the game completely
echo       (Task Manager, search "DarkSouls", zero rows), then retry.
echo     * "file already exists"           - delete that file, retry.
echo     * it asks you to type one or two  - type each line into the
echo       console and just wait; the tool keeps watching by itself.
echo     * still reports timeout            - just send cross_bundle.zip
echo       back as it is; the logs inside tell the whole story.
echo.
echo   Either way, please still send back reports-cross\cross_bundle.zip.
echo.
if exist "%OUT%" start "" "%OUT%"
pause
exit /b 2

:failed_pause
echo.
pause
exit /b 2
