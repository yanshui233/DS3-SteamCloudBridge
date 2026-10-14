@echo off
REM ============================================================
REM  SteamCloudBridge - CLOUD ACCEPTANCE CHECK (bait file method)
REM
REM  The question this answers:
REM    If a file is dropped into the game's save folder, does
REM    Steam Cloud register and keep it - or does the cloud
REM    overwrite it on the next sync?  This is the single
REM    assumption the whole bridge depends on, and it has never
REM    been verified on a real machine.
REM
REM  How it works:
REM    A harmless decoy file (custom magic header - NOT a real
REM    savegame, the game cannot read it) is placed next to your
REM    real saves. Then Steam is started once. We check whether
REM    the decoy got registered in remotecache.vdf / cloud_log.
REM    The decoy is deleted right after.
REM    YOUR REAL SAVE FILES ARE NEVER TOUCHED.
REM
REM  Needs Python 3 on this computer (the probe itself does not).
REM  Writes only into .\reports-bait next to this script.
REM
REM  NOTE: messages are ASCII on purpose. See README.md for Chinese.
REM ============================================================
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "OUT=%~dp0reports-bait"
set "TOOL=%~dp0cloud_bait_check.py"
set "APPID=1245620"
set "STATE=%OUT%\bait-%APPID%.json"

echo.
echo   SteamCloudBridge - cloud acceptance check
echo   -----------------------------------------
echo.
echo   This will NOT touch your real save files.
echo.

if not exist "%TOOL%" (
  echo   [ERROR] cloud_bait_check.py is missing.
  echo           Keep this .bat in the same folder as it, then retry.
  goto :fail_pause
)

REM ---- locate a Python 3 --------------------------------------
set "PY="
where py >nul 2>nul
if %errorlevel% equ 0 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if %errorlevel% equ 0 set "PY=python"
)
if not defined PY (
  where python3 >nul 2>nul
  if %errorlevel% equ 0 set "PY=python3"
)
if not defined PY (
  echo   [ERROR] No Python 3 found on this computer.
  echo           This particular check needs Python 3.
  echo           Install it from https://www.python.org/downloads/
  echo           ^(tick "Add python.exe to PATH"^), then re-run me.
  goto :fail_pause
)

REM ---- locate Steam root from the registry --------------------
set "STEAMROOT="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Software\Valve\Steam" /v SteamPath 2^>nul') do set "STEAMROOT=%%B"
if defined STEAMROOT set "STEAMROOT=%STEAMROOT:/=\%"

REM ---- guess the ELDEN RING save folder -----------------------
set "SAVEDIR="
for /d %%D in ("%APPDATA%\EldenRing\*") do if not defined SAVEDIR set "SAVEDIR=%%D"

echo   Target game : ELDEN RING  ^(1245620^)
echo   Steam root  : %STEAMROOT%
echo   Save folder : %SAVEDIR%
echo.
echo   If the save folder above is wrong, type the correct one now
echo   ^(otherwise just press Enter^):
set "INPUT="
set /p "INPUT=  > "
if defined INPUT set "SAVEDIR=%INPUT%"

if not defined SAVEDIR (
  echo   [ERROR] Could not find the save folder. Please type it manually.
  goto :fail_pause
)
if not exist "%SAVEDIR%" (
  echo   [ERROR] That folder does not exist: %SAVEDIR%
  goto :fail_pause
)

if not exist "%OUT%" mkdir "%OUT%" >nul 2>nul

echo.
echo   ============================================================
echo   STEP 1/4   plan   ^(read-only, nothing is changed^)
echo   ============================================================
%PY% "%TOOL%" --mode plan --appid %APPID% --save-dir "%SAVEDIR%" --steam-root "%STEAMROOT%" --state-file "%STATE%"
if errorlevel 1 goto :fail_pause

echo.
echo   Press any key to continue with STEP 2 ...
pause >nul

echo.
echo   ============================================================
echo   STEP 2/4   drop the decoy file
echo   ============================================================
%PY% "%TOOL%" --mode drop --appid %APPID% --save-dir "%SAVEDIR%" --steam-root "%STEAMROOT%" --state-file "%STATE%"
if errorlevel 1 goto :fail_pause

echo.
echo   ------------------------------------------------------------
echo   NOW DO THIS BY HAND:
echo     1. start Steam and sign in as usual
echo     2. wait about 1 minute   ^(no need to launch the game^)
echo     3. close Steam completely
echo   then come back to this window and press any key.
echo   ------------------------------------------------------------
pause >nul

echo.
echo   ============================================================
echo   STEP 3/4   inspect   ^(did the cloud accept the decoy?^)
echo   ============================================================
%PY% "%TOOL%" --mode inspect --appid %APPID% --save-dir "%SAVEDIR%" --steam-root "%STEAMROOT%" --state-file "%STATE%"

echo.
echo   ============================================================
echo   STEP 4/4   cleanup   ^(remove the decoy^)
echo   ============================================================
%PY% "%TOOL%" --mode cleanup --appid %APPID% --save-dir "%SAVEDIR%" --steam-root "%STEAMROOT%" --state-file "%STATE%"

echo.
echo   ------------------------------------------------------------
echo   One last manual step, so the cloud drops the decoy as well:
echo     start Steam once more, then close it.
echo   ------------------------------------------------------------
echo.
echo   DONE. Please send this file back:
echo     reports-bait\bait-1245620-report.json
echo.
start "" "%OUT%"
echo   Press any key to close this window.
pause >nul
exit /b 0

:fail_pause
echo.
pause
exit /b 2
