@echo off
REM ============================================================
REM  SteamCloudBridge - one-click machine probe (read-only)
REM  Runs the portable probe and writes reports into reports\
REM  Requires: Python 3.9+ (https://www.python.org/downloads/)
REM ============================================================
setlocal
chcp 65001 >nul 2>nul

set "HERE=%~dp0"
set "ROOT=%HERE%.."
set "ENTRY=%ROOT%\tools\machine_probe.py"
if exist "%ROOT%\dist\SteamCloudBridgeProbe.py" set "ENTRY=%ROOT%\dist\SteamCloudBridgeProbe.py"

if not exist "%ENTRY%" (
  echo [ERROR] probe entry not found: %ENTRY%
  pause
  exit /b 2
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%ENTRY%" %*
  goto :finish
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%ENTRY%" %*
  goto :finish
)

where python3 >nul 2>nul
if %errorlevel%==0 (
  python3 "%ENTRY%" %*
  goto :finish
)

echo.
echo [ERROR] Python 3 not found on PATH.
echo Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
echo.
pause
exit /b 2

:finish
set "RC=%errorlevel%"
echo.
if not "%RC%"=="0" (
  echo [exit %RC%] something went wrong, see messages above.
  pause
) else (
  echo Done. Reports are in: %ROOT%\reports
  echo   machine_profile.json        -- full, keep local only
  echo   machine_profile.share.json  -- redacted, safe to share
  echo   machine_profile.md          -- human readable summary
  pause
)
exit /b %RC%
