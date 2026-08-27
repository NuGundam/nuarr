<#
  Build-Bundle - turn the live source into a versioned installer.

  ONE NUMBER, READ NOT TYPED. The version lives in app/version.py and this
  script reads it from there. Nothing here takes a -Version parameter, because
  a build that can be told a version is a build that can be told the wrong one:
  you bump the constant, and the bundle, the filenames, the wizard's title bar
  and the update check all follow from that single edit. That was the whole
  point of putting it in a module.

  Produces, in dist\:
      nuarr-<version>.zip     the bundle, unpacked by hand
      Nuarr-Setup-<version>.exe   self-extracting, runs the wizard

  Named for the version so two builds can never sit in a folder looking
  identical - which is exactly how the wrong one gets tested.
#>
[CmdletBinding()]
param(
  [string] $Source = 'C:\nuarr',
  [string] $Bundle = 'P:\BackUp Data\NuarrBackup\program-bundle',
  [string] $Dist   = 'P:\BackUp Data\NuarrBackup\dist',
  [switch] $SkipExe          # zip only; the exe costs ~4 min of makecab
)

$ErrorActionPreference = 'Stop'
function Say { param($m,$k='')
  $col = switch($k){ 'ok'{'Green'} 'warn'{'Yellow'} 'bad'{'Red'} default{'Gray'} }
  Write-Host "  $m" -ForegroundColor $col }

# ---- 1. the version, from the one place it is written --------------------
$vpy = Join-Path $Source 'app\version.py'
if (-not (Test-Path $vpy)) { throw "no version.py at $vpy" }
$m = Select-String -Path $vpy -Pattern '^VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $m) { throw "could not read VERSION out of $vpy" }
$Version = $m.Matches[0].Groups[1].Value
$Built   = Get-Date -Format 'yyyy-MM-dd'
Say "nuarr $Version  (built $Built)" 'ok'

# ---- 2. refresh the program tree -----------------------------------------
$P = Join-Path $Bundle 'program'
Remove-Item (Join-Path $P 'app'), (Join-Path $P 'assets') -Recurse -Force -EA SilentlyContinue
Get-ChildItem $P -File -EA SilentlyContinue | Remove-Item -Force
New-Item -ItemType Directory (Join-Path $P 'app') -Force | Out-Null
Copy-Item (Join-Path $Source 'app\*.py') (Join-Path $P 'app') -Force
Copy-Item (Join-Path $Source 'app\_panes_extracted.html') (Join-Path $P 'app') -Force -EA SilentlyContinue
Copy-Item (Join-Path $Source 'assets') $P -Recurse -Force
foreach ($f in 'launch.py','serve.py','cleanup.py','start-nuarr.cmd','README.md',
                'HANDLERS-AND-RULES.md','untagged-audio.md') {
  $s = Join-Path $Source $f
  if (Test-Path $s) { Copy-Item $s $P -Force }
}
Remove-Item (Join-Path $P 'app\__pycache__') -Recurse -Force -EA SilentlyContinue
Say "copied $((Get-ChildItem (Join-Path $P 'app\*.py')).Count) modules"

# ---- 3. stamp the build date into the COPY, never the source -------------
# The source stays blank on purpose: a checkout is not a build, and a date
# baked into working code would claim it was one.
$bv = Join-Path $P 'app\version.py'
$t  = [IO.File]::ReadAllText($bv)
$t  = $t -replace '(?m)^BUILD_DATE\s*=\s*""', ('BUILD_DATE = "{0}"' -f $Built)
[IO.File]::WriteAllText($bv, $t, [Text.UTF8Encoding]::new($false))
if ((Select-String -Path $bv -Pattern "BUILD_DATE = ""$Built""").Count -ne 1) {
  throw "BUILD_DATE was not stamped - check the placeholder in version.py"
}
Say "stamped BUILD_DATE = $Built"

# ---- 4. the template config: WRITTEN, not copied and not preserved -------
# First attempt at this checked an existing config.yml for credentials and
# refused to build if it found any. It could never fire: step 2 wipes every
# file at the program root, so by the time the check ran the file was always
# gone - and the zip then shipped with no config template at all, which is its
# own bug and a quieter one.
#
# Generating it fixes both at once. There is nothing to leak because nothing
# is copied, and the file is guaranteed present and identical on every build.
# The bundle shipped a live Plex token and three API keys once; the way to not
# do that again is to never be in a position where a real config COULD be
# picked up, rather than to check afterwards whether one was.
$cfg = Join-Path $P 'config.yml'
$tpl = @'
# nuarr configuration - TEMPLATE. Setup writes the real values here from what
# you enter in the wizard, so everything below is a placeholder.
#
# Plex token:            https://support.plex.tv/articles/204059436
# Sonarr / Radarr keys:  Settings -> General -> API Key

plex_direct: true
plex_cross_check: false

# One entry per library folder. kind is "tv" or "movie".
libraries: []
# - name: TV Shows
#   path: D:\TV Shows
#   kind: tv
#   enabled: true

# Sonarr and Radarr instances nuarr should keep in step with.
arrs: []
# - name: Sonarr
#   kind: sonarr
#   url: http://localhost:8989
#   api_key: ""
#   enabled: true

# Scratch space for encodes in progress. Wants fast local storage, off the
# pool, with room for the largest file you expect to convert several times over.
cache_dir: ""
cache_min_free_gb: 100

plex_url: ""
plex_token: ""

# Optional cross-check on Plex. nuarr reads playback straight from Plex.
tautulli_url: ""
tautulli_api_key: ""

# Where to look for newer nuarr releases, as owner/name. Empty means nuarr
# never contacts GitHub.
update_repo: ""
'@
[IO.File]::WriteAllText($cfg, $tpl, [Text.UTF8Encoding]::new($false))

# Belt and braces: the template is a literal above, but it is also a thing a
# future edit could get wrong, and a leaked token is not a mistake worth
# discovering from a bug report.
$leak = Select-String -Path $cfg -Pattern '^\s*(plex_token|api_key|tautulli_api_key)\s*:\s*(?!\s*""\s*$)\S+'
if ($leak) { throw "the config template has a non-empty credential in it - refusing to build" }
Say "wrote a credential-free config template"

# ---- 5. bundle.json ------------------------------------------------------
@{ name='nuarr'; version=$Version; built=$Built
   app_files=(Get-ChildItem (Join-Path $P 'app\*.py')).Count
} | ConvertTo-Json | Set-Content (Join-Path $Bundle 'bundle.json') -Encoding UTF8

# ---- 6. the zip ----------------------------------------------------------
New-Item -ItemType Directory $Dist -Force | Out-Null
$zipName = "nuarr-$Version.zip"
$zip     = Join-Path $Dist $zipName
Remove-Item $zip -Force -EA SilentlyContinue
$sz = 'C:\Program Files\7-Zip\7z.exe'
if (-not (Test-Path $sz)) { throw "7-Zip not found at $sz" }
& $sz a -tzip -mx=7 -bso0 -bsp0 $zip (Join-Path $Bundle '*') | Out-Null
Say ("zip: {0}  ({1:N1} MB)" -f $zipName, ((Get-Item $zip).Length/1MB)) 'ok'

if ($SkipExe) { Say "skipping the exe (-SkipExe)" 'warn'; return }

# ---- 7. the self-extracting exe ------------------------------------------
# NOT IEXPRESS. IExpress built this once, and past roughly 200 MB its cab
# writer TRUNCATES the payload and exits success: the 214 MB bundle shipped as
# a 133 MB cab that failed on the first machine to run it, with no error at
# build time whatsoever. A packager that corrupts silently near a size the
# payload will certainly grow past cannot be kept.
#
# The replacement is the least machinery that can work: a ~10 KB C# stub
# compiled by the csc.exe every Windows ships, with the zip APPENDED to the
# exe and a 16-byte trailer recording where it starts. The stub streams the
# zip out, extracts it with System.IO.Compression - which fails loudly on
# truncation, unlike the cab codec - and runs Setup.cmd. Elevation comes from
# the embedded manifest. Build time: about a second, and the exe is the zip
# plus 10 KB instead of a recompression that could lie.
$exeName = "Nuarr-Setup-$Version.exe"
$exe     = Join-Path $Dist $exeName
$csc     = "$env:SystemRoot\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { throw ".NET Framework csc.exe not found - cannot build the stub" }
$stubSrc = Join-Path $PSScriptRoot 'sfx_stub.cs'
$stubMan = Join-Path $PSScriptRoot 'sfx_stub.manifest'
foreach ($x in $stubSrc, $stubMan) { if (-not (Test-Path $x)) { throw "missing $x" } }
$W = 'C:\nuarrbuild'
Remove-Item $W -Recurse -Force -EA SilentlyContinue
New-Item -ItemType Directory $W -Force | Out-Null
$stubExe = Join-Path $W 'stub.exe'
$fw = "$env:SystemRoot\Microsoft.NET\Framework64\v4.0.30319"
& $csc /nologo /target:winexe /optimize+ /win32manifest:"$stubMan" `
    /r:"$fw\System.IO.Compression.dll" `
    /r:"$fw\System.IO.Compression.FileSystem.dll" `
    /r:"$fw\System.Windows.Forms.dll" `
    /r:"$fw\System.Drawing.dll" `
    /out:"$stubExe" "$stubSrc"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $stubExe)) { throw "csc failed building the stub" }
Say ("stub compiled ({0:N0} KB)" -f ((Get-Item $stubExe).Length/1KB))

# stub + zip + trailer, streamed - and then PROVEN, not assumed. The entire
# reason this section exists is a builder that lied about its output, so this
# one checks its own: the assembled exe must be exactly stub+zip+16 bytes,
# and the trailer must read back.
Remove-Item $exe -Force -EA SilentlyContinue
$out = [IO.File]::Create($exe)
try {
  $in = [IO.File]::OpenRead($stubExe)
  try { $in.CopyTo($out) } finally { $in.Dispose() }
  $zipStart = $out.Position
  $in = [IO.File]::OpenRead($zip)
  try { $in.CopyTo($out) } finally { $in.Dispose() }
  $out.Write([BitConverter]::GetBytes([long]$zipStart), 0, 8)
  $out.Write([Text.Encoding]::ASCII.GetBytes('NUARRSFX'), 0, 8)
} finally { $out.Dispose() }
$want = (Get-Item $stubExe).Length + (Get-Item $zip).Length + 16
$got  = (Get-Item $exe).Length
if ($got -ne $want) { throw "assembled exe is $got bytes, expected $want - not shipping it" }
Remove-Item $W -Recurse -Force -EA SilentlyContinue
Say ("exe: {0}  ({1:N1} MB, verified stub+zip+trailer)" -f $exeName, ($got/1MB)) 'ok'
Write-Host ""
Say "done - dist\ now holds $zipName and $exeName" 'ok'
