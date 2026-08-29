@echo off
REM nuarr - MANUAL launcher, kept only for convenience.
REM
REM The SCHEDULED TASK no longer uses this file. It runs pythonw.exe against
REM launch.py directly, because cmd.exe is a CONSOLE application and Task
REM Scheduler therefore gave it a visible black window at logon, plus a
REM conhost.exe - the "Windows Command Processor (3)" family in Task Manager.
REM No CREATE_NO_WINDOW flag can suppress that: the console belongs to the
REM launcher, created before any nuarr code runs.
REM
REM Everything this script used to do - stdout redirection for pythonw, which
REM has no stdout of its own, and unbuffered output so a crash traceback is not
REM lost in a buffer - now lives in launch.py, which additionally ROTATES the
REM log instead of appending to it forever.
REM
REM NOTHING HERE IS HARD-CODED ANY MORE, and that was a real bug: this file
REM shipped in the installer with the BUILD MACHINE's paths inside it -
REM "C:\nuarr" and one particular user's Python - so on any install that put
REM nuarr elsewhere, or found Python somewhere else, it failed with
REM "Windows cannot find ...\pythonw.exe". Worse, a self-update copied that
REM same file over whatever the installer had written. It now finds its own
REM folder (%~dp0) and looks for an interpreter instead of assuming one.
REM
REM Running this by hand still shows a console window. That is expected. To
REM start nuarr without one:  Start-ScheduledTask -TaskName nuarr

setlocal
cd /d "%~dp0"

set "PYW="

REM 1. An interpreter shipped beside nuarr, if this install has one.
if exist "%~dp0python\pythonw.exe" set "PYW=%~dp0python\pythonw.exe"

REM 2. Whatever is on PATH - the normal case for a standard Python install.
if not defined PYW for %%P in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:P"

REM 3. The two places the Python.org installer actually puts it: per-user
REM    (the default) and all-users. Newest first, so a box with several
REM    versions gets the one nuarr was built against or later.
if not defined PYW for %%V in (314 313 312 311) do (
  if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe"
  if not defined PYW if exist "%ProgramFiles%\Python%%V\pythonw.exe" set "PYW=%ProgramFiles%\Python%%V\pythonw.exe"
  if not defined PYW if exist "%ProgramFiles(x86)%\Python%%V\pythonw.exe" set "PYW=%ProgramFiles(x86)%\Python%%V\pythonw.exe"
)

REM 4. The py launcher's windowed twin, which knows about every install.
if not defined PYW where pyw.exe >nul 2>&1 && set "PYW=pyw.exe"

if not defined PYW (
  echo.
  echo   Could not find pythonw.exe on this machine.
  echo.
  echo   nuarr needs Python 3.11 or newer. Install it from python.org and
  echo   tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

echo Starting nuarr with "%PYW%"
start "" "%PYW%" "%~dp0launch.py"
endlocal
