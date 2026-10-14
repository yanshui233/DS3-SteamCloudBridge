@echo off
REM ============================================================
REM  SteamCloudBridge - TARGETED probe (4b round): two appids only
REM
REM  * 374320   DARK SOULS III   (Auto-Cloud, *.sl2)
REM  * 1245620  ELDEN RING       (same mechanism + same *.sl2 type)
REM
REM  Auto mode only picks games that already show cloud traces, and
REM  DS3 shows none until its cloud toggle is flipped - so it gets
REM  skipped. This script names the two targets explicitly.
REM
REM  * READS ONLY: never writes to Steam folders or savegames.
REM  * Writes one report into .\reports-4b next to this script.
REM  * Keep this file together with SteamCloudBridgeProbe.*
REM
REM  NOTE: messages are ASCII on purpose. See README.md for Chinese.
REM ============================================================
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "OUT=%~dp0reports-4b"
set "EXE=%~dp0SteamCloudBridgeProbe.exe"
set "PYF=%~dp0SteamCloudBridgeProbe.py"

echo.
echo   SteamCloudBridge - targeted probe (DS3 374320 / ELDEN RING 1245620)
echo   ------------------------------------------------------------------
echo.

if exist "%EXE%" (
  echo   [1/2] running bundled program ^(no Python needed^) ...
  echo.
  "%EXE%" --appid 374320 --appid 1245620 --zip --out-dir "%OUT%"
  if errorlevel 1 goto :failed
  goto :done
)

if not exist "%PYF%" (
  echo   [ERROR] SteamCloudBridgeProbe.py is missing.
  echo           Keep this .bat in the same folder as the .py file.
  goto :failed_pause
)

set "RT=%~dp0runtime\python.exe"
if exist "%RT%" (
  echo   [1/2] running with the bundled Python runtime ...
  echo.
  "%RT%" "%PYF%" --appid 374320 --appid 1245620 --zip --out-dir "%OUT%"
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

echo   [ERROR] No bundled runtime and no Python 3 on this computer.
echo.
echo   This package normally ships with SteamCloudBridgeProbe.exe, which
echo   runs without installing anything. Please check that the file is
echo   still in this folder, next to RUN-PROBE-TARGET.bat.
goto :failed_pause

:have_py
echo   [1/2] running targeted probe with "%PY%" ...
echo.
%PY% "%PYF%" --appid 374320 --appid 1245620 --zip --out-dir "%OUT%"
if errorlevel 1 goto :failed

:done
echo.
echo   [2/2] done. Opening the report folder ...
echo.
echo   Send this back:       reports-4b\machine_profile_bundle.zip
echo.
echo   Do NOT send:          reports-4b\private\machine_profile.json
echo                         ^(it is the un-redacted one^)
echo.
start "" "%OUT%"
echo   Press any key to close this window.
pause >nul
exit /b 0

:failed
echo.
echo   [FAILED] the probe exited with an error - see messages above.
echo   Tip: press Win+R, type cmd, cd into this folder and run this
echo        file from there so the window stays open.
goto :failed_pause

:failed_pause
echo.
pause
exit /b 2
