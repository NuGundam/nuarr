<#
    Set-Version.ps1 - stamp the version on the LIVE install, deliberately.

    WHY THIS EXISTS
    ---------------
    The version number is part of what gets verified, not just a label applied
    at build time. Two moments need it set, and they set it to different
    things:

      -Version <next>   BEFORE a release. The live install runs the new code
                        UNDER THE NUMBER IT WILL SHIP AS, so what is checked
                        on this machine is exactly what goes out - including
                        the number in the header and in the update check.

      -Rollback <prev>  AFTER the release is on GitHub. The live install is
                        put back to the PREVIOUS released version while
                        keeping the new code, so the updater sees the release
                        as available and the whole self-update path - check,
                        stage, apply helper, restart, housecleaning - can be
                        exercised for real instead of assumed.

    Doing this by hand meant editing version.py, remembering to restart, and
    remembering which of the two moments this was. Three things to forget.

    USAGE
      powershell -File tools\Set-Version.ps1 -Version 1.9.1
      powershell -File tools\Set-Version.ps1 -Version 1.9.0 -Reason rollback
      powershell -File tools\Set-Version.ps1 -Show
#>
[CmdletBinding()]
param(
  [string]$Version,
  [ValidateSet('verify', 'rollback')]
  [string]$Reason = 'verify',
  [switch]$Show,
  [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$vf   = Join-Path $root 'app\version.py'
if (-not (Test-Path -LiteralPath $vf)) { throw "version.py not found at $vf" }

function Get-Current {
  $t = [IO.File]::ReadAllText($vf)
  if ($t -match '(?m)^VERSION\s*=\s*"([^"]+)"') { return $Matches[1] }
  throw "could not find the VERSION line in $vf"
}

if ($Show -or -not $Version) {
  $cur = Get-Current
  Write-Host "installed version : $cur"
  try {
    $u = Invoke-RestMethod 'http://127.0.0.1:8770/api/updates?force=1' -TimeoutSec 60
    Write-Host "github latest     : $($u.latest)"
    Write-Host "update offered    : $($u.update_available)"
  } catch { Write-Host "update check      : (nuarr not reachable)" }
  if (-not $Version) { return }
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
  throw "version must be MAJOR.MINOR.PATCH, got '$Version'"
}

$old = Get-Current
# READ, REPLACE THE ONE LINE, WRITE. Not a line-by-line rebuild: splicing this
# file with Get-Content/Set-Content has mangled non-ASCII in its comments
# before. Regex on the whole text, UTF-8 with BOM preserved as python wrote it.
$text = [IO.File]::ReadAllText($vf)
$new  = [regex]::Replace($text, '(?m)^VERSION\s*=\s*"[^"]+"', "VERSION = `"$Version`"", 1)
if ($new -eq $text -and $old -ne $Version) { throw "the VERSION line did not change - aborting" }
[IO.File]::WriteAllText($vf, $new, (New-Object Text.UTF8Encoding $false))

$what = if ($Reason -eq 'rollback') {
  "rolled back $old -> $Version (so the updater offers the release)"
} else {
  "stamped $old -> $Version (verify this build before releasing)"
}
Write-Host $what

if (-not $NoRestart) {
  schtasks /End /TN nuarr 2>&1 | Out-Null
  Start-Sleep -Seconds 4
  schtasks /Run /TN nuarr 2>&1 | Out-Null
  Write-Host "nuarr restarted - give it ~30s, then check the header"
}
