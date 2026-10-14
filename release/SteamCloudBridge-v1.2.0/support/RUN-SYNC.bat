@echo off
REM ============================================================
REM  SteamCloudBridge - SYNC (daily use)
REM
REM  Double-click me BEFORE you want to play.
REM  What happens:
REM    1) checks the game is not already running
REM    2) checks Steam is running
REM    3) shows you ONE line to type into the Steam console
REM       (this build does NOT auto-type - see README.md for why)
REM    4) makes Steam launch DARK SOULS III for you
REM  Then just play. When you quit the game, Steam uploads the save.
REM
REM  WARNING: do NOT restart Steam before you quit the game - that
REM  drops the channel. If it happens, just double-click me again
REM  (lighting up twice is harmless).
REM
REM  You can also pass a command straight through:
REM    RUN-SYNC.bat daily --appid 374320 --manual --launch
REM    RUN-SYNC.bat daily --appid 374320 --manual
REM    RUN-SYNC.bat --help
REM
REM  NOTE: messages are ASCII on purpose - the batch parser and
REM  non-ASCII output do not mix reliably. See README.md for Chinese.
REM ============================================================
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "EXE=%~dp0SteamCloudBridgeBridge.exe"
set "PYF=%~dp0SteamCloudBridgeBridge.py"
set "RT=%~dp0runtime\python.exe"
set "CMDLINE="
set "RC=0"

set "ARGS=%*"
if not defined ARGS set "ARGS=daily --appid 374320 --manual --manual-wait 240 --launch"

echo.
echo   SteamCloudBridge - sync (daily use)
echo   -----------------------------------
echo.

REM ---- pick how to run it: bundled exe > bundled runtime > PATH python ----
if exist "%EXE%" set "CMDLINE="%EXE%""
if not defined CMDLINE if exist "%PYF%" call :find_python

if not defined CMDLINE (
  if not exist "%PYF%" (
    echo   [ERROR] SteamCloudBridgeBridge.py is missing.
    echo           Keep this .bat in the same folder as the bridge files.
    goto :fail_pause
  )
  echo   [ERROR] No bundled runtime and no Python 3 found on this computer.
  echo.
  echo   This package normally ships SteamCloudBridgeBridge.exe, which runs
  echo   without installing anything. Please check that the file is still
  echo   in this folder, next to RUN-SYNC.bat.
  echo.
  echo   Alternative: install Python 3 from https://www.python.org/downloads/
  echo        IMPORTANT - on the first installer screen tick
  echo        "Add python.exe to PATH" before clicking Install.
  echo        Then run this file again.
  goto :fail_pause
)

echo   Running: %CMDLINE% %ARGS%
echo.
%CMDLINE% %ARGS%
set "RC=%errorlevel%"

echo.
echo   ------------------------------------------------------------
if "%RC%"=="0" (
  echo   [OK] the cloud channel is ON for this Steam session.
  echo   Now play. When you quit the game, Steam uploads your save.
  echo   Do NOT restart Steam before that; if you do, re-run me.
) else (
  echo   [exit code %RC%] it did not finish.
  echo   Exit codes: 3 = game or Steam not in a usable state   ^(retry^)
  echo               4 = the console line was not typed in       ^(retry^)
  echo               1 = failed, see the messages above
  echo   Chinese details: README.md
)
echo   ------------------------------------------------------------

echo.
echo   Press any key to close this window.
pause >nul
exit /b %RC%

REM ------------------------------------------------------------------
REM  Subroutine: locate a Python 3 and build the command line.
REM  Note the two nested `if` blocks - `%errorlevel%` inside a ( ) block
REM  is expanded too early, so only `&&` is used there.
REM ------------------------------------------------------------------
:find_python
if exist "%RT%" (
  set "CMDLINE="%RT%" "%PYF%""
  goto :eof
)
where py >nul 2>nul && set "CMDLINE=py -3 "%PYF%""
if not defined CMDLINE ( where python >nul 2>nul && set "CMDLINE=python "%PYF%"" )
if not defined CMDLINE ( where python3 >nul 2>nul && set "CMDLINE=python3 "%PYF%"" )
goto :eof

:fail_pause
echo.
pause
exit /b 2
