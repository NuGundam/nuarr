@echo off
REM  nuarr uninstaller. Removes the program, task, shortcut and cache;
REM  KEEPS the database at C:\ProgramData\nuarr unless you pass -RemoveData.
copy /Y "%~dp0Nuarr-Uninstall.ps1" "%TEMP%\Nuarr-Uninstall.ps1" >nul
start "" powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\Nuarr-Uninstall.ps1" -Target "C:\nuarr" -DataDir "C:\ProgramData\nuarr" %*
