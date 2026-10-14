@echo off
REM ============================================================
REM  SteamCloudBridge - START THE WIZARD  (this is the one to double-click)
REM
REM  Opens a graphical wizard that walks you through everything:
REM    1) check this computer
REM    2) back up your save
REM    3) light up the cloud channel, then start the game
REM    4) what to do when something goes wrong
REM    5) Dark Souls 2 - it has NO cloud save; use a folder instead
REM
REM  Nothing to memorise - the window explains each step and waits
REM  for you.
REM
REM  FOLDER LAYOUT (since v1.1.1): the top level holds only two things -
REM  this launcher and SteamCloudBridgeGUI.exe. Everything else
REM  (command-line tools, README, single-file .py versions) lives
REM  in the support\ subfolder. You never need to open it unless
REM  the wizard tells you to.
REM
REM  Older packages put every file side by side; this launcher still
REM  works with that layout too.
REM
REM  NOTE: messages are ASCII on purpose - the batch parser and
REM  non-ASCII output do not mix reliably. See README.md for Chinese.
REM ============================================================
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "RT=%~dp0support\runtime\python.exe"

REM ---- the wizard itself: top level first, then the support\ subfolder ----
set "WIZ=%~dp0SteamCloudBridgeGUI.exe"
if not exist "%WIZ%" if exist "%~dp0support\SteamCloudBridgeGUI.exe" set "WIZ=%~dp0support\SteamCloudBridgeGUI.exe"

set "PYF=%~dp0support\SteamCloudBridgeGUI.py"
if not exist "%PYF%" if exist "%~dp0SteamCloudBridgeGUI.py" set "PYF=%~dp0SteamCloudBridgeGUI.py"

set "CMDLINE="

REM ---- preferred path: the bundled exe. It is a windowed program,
REM      so launch it detached and let this console close itself.
if exist "%WIZ%" (
  echo   Starting the wizard ...
  start "" "%WIZ%" %*
  exit /b 0
)

REM ---- fallback: a Python 3 that has tkinter ----
if exist "%PYF%" call :find_python

if not defined CMDLINE (
  echo.
  echo   [ERROR] The wizard is missing.
  echo           Expected: SteamCloudBridgeGUI.exe  or  support\SteamCloudBridgeGUI.exe
  echo           Keep this .bat in the SAME folder as the rest of the package.
  echo.
  echo   Alternative: install Python 3 from https://www.python.org/downloads/
  echo        IMPORTANT - on the first installer screen tick
  echo        "Add python.exe to PATH" before clicking Install.
  echo        Then run this file again.
  goto :fail_pause
)

echo   Starting the wizard ...
echo   (this console window stays open while the wizard runs)
echo.
%CMDLINE% %*
set "RC=%errorlevel%"
if not "%RC%"=="0" goto :fail_pause
exit /b 0

REM ------------------------------------------------------------------
REM  Subroutine: locate a Python 3 and build the command line.
REM  Note the two nested `if` blocks - `%errorlevel%` inside a ( ) block
REM  is expanded too early, so only `&&` is used there.
REM  The bundled single-file needs tkinter; if the found Python lacks it,
REM  the wizard itself will say so instead of failing silently.
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
echo   Press any key to close this window.
pause >nul
exit /b 2
