<#
  Nuarr-Uninstall.ps1 - remove nuarr, with an explicit choice about the data.

  THE ONE RULE THIS FILE EXISTS TO KEEP: it never touches a media file. Every
  path it deletes is one Setup created. The library lives wherever you told
  nuarr it lives and is not nuarr's to remove - an uninstaller that could take
  59 TB of media with it is not an uninstaller, it is an accident waiting for
  a tired evening.

  Three tiers, because "uninstall" means different things at different moments:

    program only   the code and the task go, the database stays. This is the
                   reinstall / upgrade case, and it is the default.
    + data         also the database, logs and settings under ProgramData.
                   Everything nuarr learned about the library is lost; the
                   library itself is untouched.
    + cache        also the scratch directory. Only ever holds encodes in
                   progress, so this is safe whenever nothing is running.
#>
[CmdletBinding()]
param(
  [string] $Target   = 'C:\nuarr',
  [string] $DataDir  = 'C:\ProgramData\nuarr',
  [string] $CacheDir = '',
  [string] $TaskName = 'nuarr',
  [int]    $Port     = 8770,
  [switch] $RemoveData,
  [switch] $RemoveCache,
  [switch] $Force            # skip the confirmation prompt
)

$ErrorActionPreference = 'Stop'
function Say { param($m,$k='')
  $col = switch($k){ 'ok'{'Green'} 'warn'{'Yellow'} 'bad'{'Red'} default{'Gray'} }
  Write-Host "  $m" -ForegroundColor $col
}

if (-not ([Security.Principal.WindowsPrincipal] `
      [Security.Principal.WindowsIdentity]::GetCurrent()
     ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "Run this as Administrator." -ForegroundColor Red; exit 1
}

# ---- work out what is actually there before promising to remove it --------
# Reading the cache path out of the installed config rather than guessing:
# it is the one location Setup lets you put anywhere, so a default would be
# wrong on most machines, and a wrong path here means either missing the
# cache or deleting a directory that was never ours.
if (-not $CacheDir) {
  $cfg = Join-Path $Target 'config.yml'
  if (Test-Path $cfg) {
    $m = Select-String -Path $cfg -Pattern '^\s*cache_dir\s*:\s*(.+?)\s*$' |
         Select-Object -First 1
    if ($m) { $CacheDir = $m.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'") }
  }
}

Write-Host ""
Write-Host "  nuarr uninstall" -ForegroundColor White
Write-Host "  ---------------" -ForegroundColor DarkGray
$plan = [ordered]@{}
$plan['scheduled task'] = $TaskName
$plan['program folder'] = $Target
$plan['data folder']    = if ($RemoveData)  { $DataDir }  else { "$DataDir  (KEEPING)" }
$plan['cache folder']   = if ($RemoveCache) { if($CacheDir){$CacheDir}else{'(not found)'} }
                          else { "$(if($CacheDir){$CacheDir}else{'(not found)'})  (KEEPING)" }
foreach ($k in $plan.Keys) { "  {0,-16} {1}" -f $k, $plan[$k] | Write-Host }
Write-Host ""
Say "Media files are never touched. Your library is not part of this." 'ok'
Write-Host ""

if (-not $Force) {
  $a = Read-Host "  Type REMOVE to continue"
  if ($a -ne 'REMOVE') { Say "Cancelled - nothing was changed." 'warn'; exit 0 }
}

$errs = 0
# NATIVE STDERR IS NOT AN EXCEPTION. With ErrorActionPreference='Stop', anything
# schtasks writes to stderr becomes a terminating NativeCommandError - so
# querying a task that is already gone was reported as "FAILED: The system
# cannot find the file specified", and the run ended "Finished with 2
# problem(s)" after doing exactly what was asked. An absent task is the state
# this script exists to produce; announcing it as a failure teaches you to
# ignore the failure count, which is the one number here worth reading.
function Invoke-SchTasks {
  param([string[]]$Args)
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & schtasks.exe @Args 2>&1 | Out-Null; return $LASTEXITCODE }
  finally { $ErrorActionPreference = $old }
}

function Try-Step { param($what,[scriptblock]$do)
  try { & $do; Say $what 'ok' } catch { $script:errs++; Say "$what - FAILED: $($_.Exception.Message)" 'bad' }
}

# ---- 1. stop it before deleting anything it has open ---------------------
Try-Step "Stopped the scheduled task" {
  if ((Invoke-SchTasks @('/Query','/TN',$TaskName)) -eq 0) {
    Invoke-SchTasks @('/End','/TN',$TaskName) | Out-Null; Start-Sleep 3
  }
}

# A task that is gone still leaves the process if it was started by hand.
# Matched on the launch script's path, never on the image name: killing every
# python.exe on the box would take out anything else the machine is running.
Try-Step "Stopped any leftover nuarr process" {
  $launch = (Join-Path $Target 'launch.py').ToLower()
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='Nuarr.exe'" -EA SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($launch) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
  Start-Sleep 2
}

Try-Step "Removed the scheduled task" {
  if ((Invoke-SchTasks @('/Query','/TN',$TaskName)) -eq 0) {
    Invoke-SchTasks @('/Delete','/TN',$TaskName,'/F') | Out-Null
  }
}

# ---- 2. shortcuts and firewall ------------------------------------------
Try-Step "Removed shortcuts" {
  foreach ($p in @(
      (Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) 'nuarr.lnk'),
      (Join-Path ([Environment]::GetFolderPath('Desktop')) 'nuarr.lnk'),
      (Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'nuarr.lnk'))) {
    if (Test-Path $p) { Remove-Item $p -Force }
  }
}
Try-Step "Removed the firewall rule" {
  Get-NetFirewallRule -DisplayName 'nuarr*' -EA SilentlyContinue |
    Remove-NetFirewallRule -EA SilentlyContinue
}

# ---- 3. the folders ------------------------------------------------------
# Deleting the program folder LAST of the always-on steps, because everything
# above reads config.yml out of it.
Try-Step "Removed the program folder" {
  if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
}

if ($RemoveData) {
  Try-Step "Removed the data folder (database, logs, settings)" {
    if (Test-Path $DataDir) { Remove-Item $DataDir -Recurse -Force }
  }
} else {
  Say "Kept $DataDir - reinstalling over it picks the library back up." 'warn'
}

if ($RemoveCache -and $CacheDir) {
  # A REFUSAL, NOT A DELETE, when the path looks wrong. The cache is the only
  # directory here that the user chose freely, so it is the only one that
  # could plausibly be pointed at something precious - a drive root, or the
  # library itself. Neither is survivable, so neither is attempted.
  $bad = $false
  try {
    $full = [IO.Path]::GetFullPath($CacheDir)
    if ($full -match '^[A-Za-z]:\\?$') { $bad = $true }
    if ((Test-Path $full) -and (Get-ChildItem $full -Recurse -File -Include *.mkv,*.mp4,*.avi,*.m4v -EA SilentlyContinue | Select-Object -First 1)) {
      # Encodes in progress live here too, so a video file alone is not proof.
      # But a cache holding finished media is worth a human looking at it.
      Say "Cache holds video files - not deleting it. Check $full by hand." 'warn'
      $bad = $true
    }
  } catch { $bad = $true }
  if (-not $bad) {
    Try-Step "Removed the cache folder" {
      if (Test-Path $CacheDir) { Remove-Item $CacheDir -Recurse -Force }
    }
  }
} elseif ($CacheDir) {
  Say "Kept $CacheDir" 'warn'
}

Write-Host ""
if ($errs) {
  Say "Finished with $errs problem(s) - see above." 'warn'
  Say "Anything left behind can be removed by hand; nothing is in a half state." 'warn'
} else {
  Say "nuarr has been removed." 'ok'
}
if (-not $RemoveData) {
  Write-Host ""
  Say "To remove the database too:  .\Nuarr-Uninstall.ps1 -RemoveData"
}
Write-Host ""
