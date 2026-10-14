@echo off
REM ============================================================
REM  SteamCloudBridge - read-only machine probe (one click)
REM
REM  * READS ONLY: it never writes to Steam folders or savegames.
REM  * It writes one report into .\reports next to this script.
REM  * Keep this file together with SteamCloudBridgeProbe.*
REM
REM  NOTE: messages are ASCII on purpose - the batch parser and
REM  non-ASCII output do not mix reliably. See README.md for Chinese.
REM ============================================================
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "OUT=%~dp0reports"
set "EXE=%~dp0SteamCloudBridgeProbe.exe"
set "PYF=%~dp0SteamCloudBridgeProbe.py"

echo.
echo   SteamCloudBridge - read-only probe
echo   --------------------------------------------------
echo.

if exist "%EXE%" (
  echo   [1/2] running bundled program ^(no Python needed^) ...
  echo.
  "%EXE%" --zip --out-dir "%OUT%"
  if errorlevel 1 goto :failed
  goto :done
)

if not exist "%PYF%" (
  echo   [ERROR] SteamCloudBridgeProbe.py is missing.
  echo           Keep this .bat in the same folder as the .py file.
  goto :failed_pause
)

REM --- fallback 2: bundled portable Python runtime (nothing to install) ---
set "RT=%~dp0runtime\python.exe"
if exist "%RT%" (
  echo   [1/2] running with the bundled Python runtime ...
  echo.
  "%RT%" "%PYF%" --zip --out-dir "%OUT%"
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
echo   still in this folder, next to RUN-PROBE.bat.
echo.
echo   Alternative: install Python 3 from https://www.python.org/downloads/
echo        IMPORTANT - on the first installer screen tick
echo        "Add python.exe to PATH" before clicking Install.
echo        Then run this file again.
goto :failed_pause

:have_py
echo   [1/2] running probe with "%PY%" ...
echo.
%PY% "%PYF%" --zip --out-dir "%OUT%"
if errorlevel 1 goto :failed

:done
echo.
echo   [2/2] done. Opening the report folder ...
echo.
echo   Send this back:       reports\machine_profile_bundle.zip
echo.
echo   Do NOT send:          reports\private\machine_profile.json
echo                         ^(it is the un-redacted one: account ID
echo                          and full local paths are still inside^)
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
