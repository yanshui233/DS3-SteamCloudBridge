@echo off
REM ============================================================
REM  SteamCloudBridge - CROSS-MACHINE round trip (stage 1/2: push)
REM
REM  *** RUN THIS ON YOUR OWN MACHINE - never on the other one. ***
REM
REM  WHAT IT DOES
REM    * guards: refuses to run while Steam is running, refuses cross-account
REM    * backs up + snapshots the current save, writes the payload atomically
REM    * starts Steam, lights the cloud channel, uploads, verifies the upload
REM    * writes xfer-out\expected.json  <- the contract the other side checks
REM
REM  BEFORE YOU RUN IT
REM    1) FULLY EXIT STEAM first (tray icon -> Exit). The tool refuses to
REM       write a save while Steam is running.
REM    2) Put your payload at  payload\DS30000.sl2  (next to this file).
REM       It MUST differ from the copy already in the cloud, otherwise the
REM       run is a no-op and proves nothing.
REM
REM  AFTER IT FINISHES
REM    * send xfer-out\expected.json + the release package to the other side
REM    * run it AGAIN later with the real save and --label restore to put
REM      the cloud copy back  (see README.md).
REM
REM  NOTE: messages are ASCII on purpose. See README.md for Chinese.
REM ============================================================
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "SRC=%~dp0payload\DS30000.sl2"
set "OUT=%~dp0xfer-out"
set "BK=%~dp0xfer-backup"
set "BRIDGE=%~dp0..\tools\bridge.py"

echo.
echo   SteamCloudBridge - cross-machine push (stage 1/2)
echo   -----------------------------------------------
echo.

if not exist "%SRC%" (
  echo   [ERROR] payload not found:  payload\DS30000.sl2
  echo           Create a  payload  folder next to this file and drop your
  echo           .sl2 in as DS30000.sl2, then run this file again.
  goto :failed_pause
)

if not exist "%BRIDGE%" (
  echo   [ERROR] tools\bridge.py not found - run this from the repository.
  goto :failed_pause
)

set "PY="
where py >nul 2>nul
if %errorlevel% equ 0 set "PY=py -3"
if defined PY goto :have_py
where python >nul 2>nul
if %errorlevel% equ 0 set "PY=python"
if defined PY goto :have_py

echo   [ERROR] No Python 3 found on PATH.
goto :failed_pause

:have_py
echo   [1/2] pushing payload + uploading (this starts Steam by itself) ...
echo.
%PY% "%BRIDGE%" xfer-push --appid 374320 --source "%SRC%" --out "%OUT%" ^
  --backup-dir "%BK%" --snapshot-dir "%BK%" --journal "%BK%\journal.jsonl" ^
  --label test --yes
if errorlevel 1 goto :failed

echo.
echo   [2/2] done. Contract written to:
echo         %OUT%\expected.json
echo.
echo   Next: send expected.json + the release package to the other machine,
echo         and have them run RUN-CROSS-VERIFY.bat.
echo.
echo   Press any key to close this window.
pause >nul
exit /b 0

:failed
echo.
echo   [FAILED] push did not succeed - read the lines above.
echo   If the cloud copy is now uncertain, re-run with your REAL save:
echo     %PY% "%BRIDGE%" xfer-push --appid 374320 --source ^<your real .sl2^> ^
echo         --out "%OUT%" --backup-dir "%BK%" --label restore --yes
echo.
echo   Press any key to close this window.
pause >nul
exit /b 2

:failed_pause
echo.
echo   [STOPPED] nothing was written and nothing was uploaded.
echo   Press any key to close this window.
pause >nul
exit /b 2
