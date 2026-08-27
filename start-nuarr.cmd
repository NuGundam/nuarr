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
REM log instead of appending to it forever (server.out had reached 1.6 MB).
REM
REM Running this by hand still shows a console window. That is expected. To
REM start nuarr without one:  Start-ScheduledTask -TaskName nuarr
cd /d C:\nuarr
start "" "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\pythonw.exe" "C:\nuarr\launch.py"
