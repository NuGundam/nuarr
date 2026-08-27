# ===========================================================================
#  nuarr setup - the part that does the work
#
#  Kept separate from the wizard so every step is a plain function that can be
#  called, logged and tested on its own. The UI only ever calls these; it does
#  not know how anything is installed.
# ===========================================================================

Set-StrictMode -Version 2.0

# --------------------------------------------------------------- logging ---
$script:LogPath = Join-Path $env:TEMP ("nuarr-setup-{0}.log" -f (Get-Date -f 'yyyyMMdd-HHmmss'))
$script:LogSink = $null      # the wizard sets this to a scriptblock

function Write-Step {
  param([string]$Text, [ValidateSet('info','ok','warn','err')][string]$Level='info')
  $line = "{0}  {1}" -f (Get-Date -f 'HH:mm:ss'), $Text
  try { Add-Content -Path $script:LogPath -Value $line -Encoding UTF8 } catch {}
  if ($script:LogSink) { & $script:LogSink $Text $Level }
}

# ------------------------------------------------------------ prereqs ------
function Invoke-SchTasks {
  <#  schtasks with its stderr treated as an ANSWER, not an emergency.

      The wizard runs under ErrorActionPreference = Stop, and schtasks reports
      "no such task" by printing "ERROR: The system cannot find the file
      specified." to stderr. Stop turns that into a terminating error, so on
      any machine that had never had a nuarr task - which is every machine
      Setup exists for - the install died on its very first query, before the
      first progress tick. It never failed on the dev box, because the dev box
      has the task; the third dev-box-shaped hole in as many days.

      An absent task is a normal answer to a question this installer has to
      ask. The exit code says everything; stderr here is commentary. #>
  param([string[]]$TaskArgs)
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & schtasks.exe @TaskArgs 2>&1 | Out-Null; return $LASTEXITCODE }
  finally { $ErrorActionPreference = $old }
}

function Find-Python {
  <#  The newest CPython 3.11+ we can find, preferring the py launcher.
      Returns @{Exe;Version} or $null. #>
  $cands = @()
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) {
    foreach ($tag in '-3.13','-3.12','-3.14','-3.11','-3') {
      try {
        $v = & $py.Source $tag -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) {
          $exe = & $py.Source $tag -c "import sys;print(sys.executable)" 2>$null
          if ($exe) { $cands += @{Exe=$exe.Trim(); Version=$v.Trim()} }
        }
      } catch {}
    }
  }
  foreach ($name in 'python','python3') {
    # $cmd, not $c - see the note in New-Check about case-insensitive names
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) {
      try {
        $v = & $cmd.Source -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($v) { $cands += @{Exe=$cmd.Source; Version=$v.Trim()} }
      } catch {}
    }
  }
  $ok = $cands | Where-Object {
    $p = $_.Version -split '\.'
    [int]$p[0] -eq 3 -and [int]$p[1] -ge 11
  }
  if (-not $ok) { return $null }
  # newest minor wins
  $ok | Sort-Object { [int](($_.Version -split '\.')[1]) } -Descending | Select-Object -First 1
}

function Install-Python {
  <#  Install the bundled CPython silently, then find it again.

      THE README PROMISED THIS AND THE WIZARD DID NOT DO IT. "The installer
      bundles it if missing" was written; what shipped was a disabled Next
      button and an instruction to go install Python by hand - the exact
      dance a wizard exists to remove, discovered the first time the bundle
      met a machine that was not the dev box.

      Silent flags: InstallAllUsers because nuarr runs as a scheduled task
      under SYSTEM, and a per-user install of the launching interpreter would
      vanish from that context. PrependPath so the next Find-Python (and the
      user's own shell) sees it. No test suite, no docs - this Python exists
      to run one application.

      Waits on the process and then RE-RUNS Find-Python rather than assuming
      a path: the installer chooses its own directory by version, and
      hardcoding it here would break on the day the bundled version bumps. #>
  param([string]$Bundle)
  $inst = Get-ChildItem (Join-Path $Bundle 'python') -Filter 'python-*.exe' `
            -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $inst) { Write-Step "no bundled Python installer found" 'bad'; return $null }
  # Refuse an unsigned or tampered binary outright. This file came out of a
  # zip that has travelled who knows where; two seconds of verification
  # against the PSF's signature is cheap insurance on the thing that is about
  # to run elevated.
  $sig = Get-AuthenticodeSignature $inst.FullName
  if ($sig.Status -ne 'Valid') {
    Write-Step "bundled Python installer failed signature check ($($sig.Status)) - not running it" 'bad'
    return $null
  }
  Write-Step "Installing $($inst.BaseName) (bundled, silent)..."
  $p = Start-Process $inst.FullName -ArgumentList `
        '/quiet','InstallAllUsers=1','PrependPath=1','Include_test=0','Include_doc=0' `
        -Wait -PassThru
  if ($p.ExitCode -ne 0) {
    Write-Step "Python installer exited $($p.ExitCode)" 'bad'; return $null
  }
  # The PATH change lands in the registry, not in this process. Pull the
  # machine PATH fresh so Find-Python can see what was just installed
  # without the user having to restart Setup.
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
              [Environment]::GetEnvironmentVariable('Path','User')
  $found = Find-Python
  if ($found) { Write-Step "Python $($found.Version) installed" 'ok' }
  return $found
}

function Test-Nvidia {
  $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if (-not $smi) { return $null }
  try {
    $n = & $smi.Source --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1
    if ($n) { return $n.Trim() }
  } catch {}
  return $null
}

function Get-FreeGB {
  param([string]$Path)
  try {
    $root = [System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue))
    if (-not $root) { $root = [System.IO.Path]::GetPathRoot($Path) }
    $d = Get-PSDrive -Name $root.TrimEnd(':\') -ErrorAction SilentlyContinue
    if ($d) { return [math]::Round($d.Free/1GB, 1) }
  } catch {}
  return $null
}

# ------------------------------------------------- library auto-detect -----
function Find-Libraries {
  <#  Look for media folders on every fixed/network drive. Guesses tv vs movie
      from the folder name, which is what the arrs already encode in it. #>
  $hits = @()
  $names = @{
    'tv'    = @('TV Shows','TV','Shows','Series','Anime Shows','Animated Shows','Anime')
    'movie' = @('Movies','Films','Anime Movies','Animated Movies','4K Movies')
  }
  $drives = Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
            Where-Object { $_.Root -match '^[A-Z]:\\$' -and $_.Used -ne $null }
  foreach ($d in $drives) {
    foreach ($kind in $names.Keys) {
      foreach ($n in $names[$kind]) {
        $p = Join-Path $d.Root $n
        if (Test-Path -LiteralPath $p -PathType Container) {
          $hits += [pscustomobject]@{ Name=$n; Path=$p; Kind=$kind; Enabled=$true }
        }
      }
    }
  }
  $hits | Sort-Object Path -Unique
}

function Find-ArrConfig {
  <#  Sonarr and Radarr keep their API key in config.xml under ProgramData.
      Reading it means the user does not have to go and copy it by hand. #>
  param([ValidateSet('sonarr','radarr')][string]$Kind)
  $port = if ($Kind -eq 'sonarr') { 8989 } else { 7878 }
  $leaf = if ($Kind -eq 'sonarr') { 'Sonarr' } else { 'Radarr' }
  foreach ($base in @($env:ProgramData, $env:APPDATA, 'C:\ProgramData')) {
    if (-not $base) { continue }
    $xml = Join-Path $base "$leaf\config.xml"
    if (Test-Path -LiteralPath $xml) {
      try {
        [xml]$x = Get-Content -LiteralPath $xml -Raw
        $key = $x.Config.ApiKey
        $p   = $x.Config.Port
        if ($p) { $port = [int]$p }
        if ($key) {
          return @{ Url = "http://localhost:$port"; ApiKey = "$key"; Found = $true }
        }
      } catch {}
    }
  }
  return @{ Url = "http://localhost:$port"; ApiKey = ''; Found = $false }
}

function Find-PlexToken {
  <#  A local Plex Media Server stores its own token in the registry. This is
      the only place it can be read from - it cannot be derived from a URL. #>
  foreach ($k in @('HKCU:\Software\Plex, Inc.\Plex Media Server')) {
    try {
      $v = (Get-ItemProperty -Path $k -Name PlexOnlineToken -ErrorAction Stop).PlexOnlineToken
      if ($v) { return "$v" }
    } catch {}
  }
  return ''
}

function Test-ArrConnection {
  param([string]$Url, [string]$ApiKey, [ValidateSet('sonarr','radarr')][string]$Kind)
  if (-not $Url -or -not $ApiKey) { return @{Ok=$false; Detail='URL and API key are both needed'} }
  try {
    $u = "{0}/api/v3/system/status?apikey={1}" -f $Url.TrimEnd('/'), $ApiKey
    $r = Invoke-RestMethod -Uri $u -TimeoutSec 8 -ErrorAction Stop
    return @{Ok=$true; Detail=("{0} {1}" -f $r.appName, $r.version)}
  } catch {
    return @{Ok=$false; Detail=$_.Exception.Message}
  }
}

function Test-PlexConnection {
  <#  SAY WHAT IS ACTUALLY WRONG, because the common failure is not exotic.

      This used to return $_.Exception.Message raw, so the single most likely
      real-world case - a remote Plex with the token box still empty, which is
      EVERY install on a machine that is not the Plex server itself, since the
      registry read only works locally - surfaced as "The remote server
      returned an error: (401) Unauthorized." True, unreadable, and silent
      about the one thing the person needs to know: the URL is fine, the
      token is what is missing.

      The token now travels as a header rather than in the query string -
      Plex accepts both, but a credential in a URL ends up in proxy logs and
      screenshots (this very page has been screenshotted with the field
      visible), and the header version does not. #>
  param([string]$Url, [string]$Token)
  if (-not $Url) { return @{Ok=$false; Detail='No server URL'} }
  try {
    $hdr = @{ 'Accept' = 'application/json' }
    if ($Token) { $hdr['X-Plex-Token'] = $Token }
    $r = Invoke-RestMethod -Uri $Url.TrimEnd('/') -Headers $hdr -TimeoutSec 8 -ErrorAction Stop
    $name = $r.MediaContainer.friendlyName
    $ver  = $r.MediaContainer.version
    return @{Ok=$true; Detail=("{0} - {1}" -f $name, $ver)}
  } catch {
    $code = 0
    try { $code = [int]$_.Exception.Response.StatusCode } catch {}
    if ($code -eq 401 -and -not $Token) {
      return @{Ok=$false; Detail=('The server answered - it needs a token. ' +
        'Sign in at plex.tv, play anything, and see support article 204059436 ' +
        'for where to copy it from.')}
    }
    if ($code -eq 401) {
      return @{Ok=$false; Detail='The server answered but rejected this token - re-copy it.'}
    }
    if ($code -eq 404) {
      return @{Ok=$false; Detail='Something answered, but it does not look like Plex - check the port.'}
    }
    return @{Ok=$false; Detail=('No answer from that address - check the URL ' +
      'and that Plex is running. (' + $_.Exception.Message + ')')}
  }
}

# ------------------------------------------------------- install steps -----
function Install-Program {
  param([string]$Bundle, [string]$Target)
  Write-Step "Copying the program to $Target"
  $src = Join-Path $Bundle 'program'
  if (-not (Test-Path -LiteralPath $src)) { throw "No program folder in the bundle: $src" }
  New-Item -ItemType Directory -Force -Path $Target | Out-Null
  # /NJH /NJS quiet header+summary, /NP no per-file percentage
  $r = robocopy $src $Target /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1
  if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE" }
  $n = (Get-ChildItem -LiteralPath $Target -Recurse -File).Count
  Write-Step "Program in place - $n files" 'ok'
}

function Install-Packages {
  param([string]$Bundle, [string]$Python)
  $wheels = Join-Path $Bundle 'wheels'
  $req    = Join-Path $Bundle 'requirements.txt'
  if (-not (Test-Path -LiteralPath $req)) { throw "No requirements.txt in the bundle" }
  Write-Step "Installing Python packages (offline first)"
  & $Python -m pip install --disable-pip-version-check --no-input `
      --no-index --find-links $wheels -r $req 2>&1 |
    ForEach-Object { if ($_ -match 'Successfully|ERROR|error:') { Write-Step "  $_" } }
  if ($LASTEXITCODE -ne 0) {
    Write-Step "Offline install incomplete - retrying with the network" 'warn'
    & $Python -m pip install --disable-pip-version-check --no-input `
        --find-links $wheels -r $req 2>&1 |
      ForEach-Object { if ($_ -match 'Successfully|ERROR|error:') { Write-Step "  $_" } }
    if ($LASTEXITCODE -ne 0) { throw "pip could not install the requirements" }
  }
  Write-Step "Packages installed" 'ok'
}

function Install-Ffmpeg {
  <#  Put the build where the app actually looks: DataDir\ffmpeg\bin.

      The resolver in ffmpeg_update.py is two lines: if bin\ffmpeg.exe
      exists, use it; otherwise fall back to SETTINGS.ffmpeg - whose default
      is a path from the dev machine. This function used to mirror the
      bundle verbatim, landing the exes in ffmpeg\jellyfin-7.1.4\, a folder
      NOTHING reads. On the dev box the updater had long since created bin\
      itself, so the misplacement was invisible; on a fresh machine every
      ffmpeg-dependent feature reported "missing" while a perfectly good
      build sat one directory over. #>
  param([string]$Bundle, [string]$DataDir)
  $src = Join-Path $Bundle 'ffmpeg'
  if (-not (Test-Path -LiteralPath $src)) { Write-Step "No ffmpeg in the bundle - skipping" 'warn'; return }
  $exeSrc = Get-ChildItem -LiteralPath $src -Recurse -Filter ffmpeg.exe | Select-Object -First 1
  if (-not $exeSrc) { Write-Step "Bundle ffmpeg folder holds no ffmpeg.exe" 'warn'; return }
  $bin = Join-Path $DataDir 'ffmpeg\bin'
  Write-Step "Installing the bundled ffmpeg build"
  New-Item -ItemType Directory -Force -Path $bin | Out-Null
  robocopy $exeSrc.DirectoryName $bin /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy failed copying ffmpeg" }
  $ff = Join-Path $bin 'ffmpeg.exe'
  $fp = Join-Path $bin 'ffprobe.exe'
  if ((Test-Path $ff) -and (Test-Path $fp)) {
    $v = (& $ff -version 2>$null | Select-Object -First 1)
    Write-Step ("ffmpeg at {0} ({1})" -f $ff, ($v -replace '^ffmpeg version (\S+).*','$1')) 'ok'
  } else {
    Write-Step "ffmpeg/ffprobe did not land in bin - encodes will not work" 'bad'
  }
}

function Install-MkvTools {
  <#  Put the three MKVToolNix CLI tools where every default already looks.

      Six nuarr modules call mkvmerge, mkvpropedit or mkvextract - remuxes,
      the metadata-only language-tag fast path, subtitle extraction for OCR -
      and none of it was in the bundle. Same failure as the Python one this
      installer just had: the dev box has MKVToolNix installed, so nothing
      ever noticed the bundle did not.

      Installed to C:\Program Files\MKVToolNix because that is the exact path
      config.py defaults to and jobs.py probes for mkvextract - putting the
      files where the code already looks beats teaching the code a second
      location. The three exes are statically linked (verified: they run from
      a bare folder with no DLLs beside them), so three files IS the install.

      A real MKVToolNix installation is left alone: it has an uninstaller
      these files would confuse, and clobbering a user's own software is not
      this installer's call. #>
  param([string]$Bundle)
  $src = Join-Path $Bundle 'mkvtoolnix'
  if (-not (Test-Path -LiteralPath $src)) {
    Write-Step "No MKVToolNix in the bundle - skipping" 'warn'; return
  }
  $dst = Join-Path $env:ProgramFiles 'MKVToolNix'
  if (Test-Path (Join-Path $dst 'mkvmerge.exe')) {
    $v = & (Join-Path $dst 'mkvmerge.exe') --version 2>$null | Select-Object -First 1
    Write-Step ("MKVToolNix already present ({0}) - keeping it" -f $v) 'ok'
    return
  }
  Write-Step "Installing the MKVToolNix command-line tools"
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  Copy-Item (Join-Path $src '*') $dst -Force
  $v = & (Join-Path $dst 'mkvmerge.exe') --version 2>$null | Select-Object -First 1
  if ($v) { Write-Step ("{0} at {1}" -f $v, $dst) 'ok' }
  else { Write-Step "mkvmerge did not answer after the copy" 'bad' }
}

function Write-NuarrConfig {
  <#  config.yml is written by hand rather than with a YAML library, because
      the installer must not depend on the packages it is about to install. #>
  param([string]$Target, [hashtable]$Cfg)
  # A YAML PLAIN SCALAR TAKES THE PATH AS-IS. Escaping the backslashes wrote
  # "P:\\Anime Shows", which PyYAML reads back literally - a folder with two
  # backslashes in its name, which does not exist. Only a double-QUOTED scalar
  # would need escaping, and none of these are quoted.
  $q = { param($s) if ($null -eq $s) { '' } else { "$s" } }
  $sb = New-Object System.Text.StringBuilder
  [void]$sb.AppendLine("# written by nuarr Setup on $(Get-Date -f 'yyyy-MM-dd HH:mm')")
  [void]$sb.AppendLine("plex_direct: true")
  [void]$sb.AppendLine("plex_cross_check: false")
  # `libraries:` on one line and `[]` on the next is NOT valid YAML - the bare
  # flow sequence after a null-valued key is a ScannerError. PyYAML threw, the
  # app''s loader swallowed the throw into "no config at all", every setting
  # fell back to its dev-box default, and the first zero-library install died
  # trying to create E:\nuarr-cache on a machine with no E: drive. The empty
  # case has to be `libraries: []` on ONE line; only the non-empty case gets
  # the block form.
  if (-not $Cfg.Libraries -or $Cfg.Libraries.Count -eq 0) {
    [void]$sb.AppendLine("libraries: []")
  } else {
    [void]$sb.AppendLine("libraries:")
  }
  foreach ($l in $Cfg.Libraries) {
    [void]$sb.AppendLine("- name: $($l.Name)")
    [void]$sb.AppendLine("  path: $(& $q $l.Path)")
    [void]$sb.AppendLine("  kind: $($l.Kind)")
    [void]$sb.AppendLine("  enabled: true")
  }
  # (empty case emitted above, inline)
  $anyArr = @($Cfg.Arrs | Where-Object { $_.ApiKey }).Count -gt 0
  if ($anyArr) { [void]$sb.AppendLine("arrs:") } else { [void]$sb.AppendLine("arrs: []") }
  $any = $false
  foreach ($a in $Cfg.Arrs) {
    if (-not $a.ApiKey) { continue }
    $any = $true
    [void]$sb.AppendLine("- name: $($a.Name)")
    [void]$sb.AppendLine("  kind: $($a.Kind)")
    [void]$sb.AppendLine("  url: $($a.Url)")
    [void]$sb.AppendLine("  api_key: $($a.ApiKey)")
    [void]$sb.AppendLine("  enabled: true")
  }
  # (empty case emitted above, inline)
  [void]$sb.AppendLine("cache_dir: $(& $q $Cfg.CacheDir)")
  [void]$sb.AppendLine("cache_min_free_gb: 100")
  if ($Cfg.PlexUrl)   { [void]$sb.AppendLine("plex_url: $($Cfg.PlexUrl)") }
  if ($Cfg.PlexToken) { [void]$sb.AppendLine("plex_token: $($Cfg.PlexToken)") }
  $path = Join-Path $Target 'config.yml'
  if (Test-Path -LiteralPath $path) {
    $bak = "$path.replaced-$(Get-Date -f 'yyyyMMdd-HHmmss')"
    Copy-Item -LiteralPath $path -Destination $bak -Force
    Write-Step "Kept the previous config as $(Split-Path $bak -Leaf)" 'warn'
  }
  [System.IO.File]::WriteAllText($path, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
  Write-Step "Wrote config.yml - $($Cfg.Libraries.Count) libraries" 'ok'
}

function Restore-Database {
  param([string]$From, [string]$DataDir, [string]$Target)
  if (-not $From) { return }
  $db = Join-Path $From 'nuarr.db'
  if (-not (Test-Path -LiteralPath $db)) { Write-Step "No nuarr.db in $From - skipping restore" 'warn'; return }
  Write-Step "Restoring the database from $(Split-Path $From -Leaf)"
  New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
  $live = Join-Path $DataDir 'nuarr.db'
  if (Test-Path -LiteralPath $live) {
    Copy-Item -LiteralPath $live -Destination "$live.before-restore" -Force
    Write-Step "  kept the existing database as nuarr.db.before-restore"
  }
  Remove-Item -LiteralPath "$live-wal","$live-shm" -Force -ErrorAction SilentlyContinue
  Copy-Item -LiteralPath $db -Destination $live -Force
  $cfg = Join-Path $From 'config.yml'
  if (Test-Path -LiteralPath $cfg) {
    Copy-Item -LiteralPath $cfg -Destination (Join-Path $Target 'config.yml') -Force
    Write-Step "  config.yml came from the backup too, replacing the one just written" 'warn'
  }
  Write-Step "Database restored" 'ok'
}

function Test-DatabaseHealth {
  param([string]$Python, [string]$DataDir)
  $db = Join-Path $DataDir 'nuarr.db'
  if (-not (Test-Path -LiteralPath $db)) { return }
  $out = & $Python -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" $db 2>&1
  if ("$out".Trim() -eq 'ok') { Write-Step "Database integrity check: ok" 'ok' }
  else { Write-Step "Database integrity check said: $out" 'warn' }
}

function Install-Whisper {
  param([string]$Python, [string]$DataDir)
  Write-Step "Installing the Whisper language-ID stack (this is the slow one)"
  & $Python -m pip install --disable-pip-version-check --no-input `
      faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12 2>&1 |
    ForEach-Object { if ($_ -match 'Successfully|ERROR|error:') { Write-Step "  $_" } }
  if ($LASTEXITCODE -ne 0) {
    Write-Step "Whisper install failed - audio language detection stays off" 'warn'
    return $false
  }
  $model = Join-Path $DataDir 'whisper'
  New-Item -ItemType Directory -Force -Path $model | Out-Null
  Write-Step "Fetching the 'small' model into $model"
  $code = @"
import os
os.environ['HF_HOME'] = r'$model'
from huggingface_hub import snapshot_download
snapshot_download('Systran/faster-whisper-small', cache_dir=r'$model')
print('model ready')
"@
  $tmp = Join-Path $env:TEMP 'nuarr_whisper_fetch.py'
  [System.IO.File]::WriteAllText($tmp, $code, (New-Object System.Text.UTF8Encoding($false)))
  & $Python $tmp 2>&1 | ForEach-Object { Write-Step "  $_" }
  Remove-Item $tmp -Force -ErrorAction SilentlyContinue
  Write-Step "Whisper ready" 'ok'
  return $true
}

function New-NuarrTask {
  <#  A scheduled task rather than a service, running as SYSTEM.

      Honest trade-off, because the old comment claimed the opposite: SYSTEM
      survives logoff and reboots unattended, but it CANNOT see mapped drive
      letters - those belong to the login session that mapped them. Local
      disks and pool mounts are fine; a P: that is really \\server\share is
      invisible to the task even though Explorer shows it. The library-add
      check explains this and points at UNC paths, which name the share
      itself and work from any account that has share permissions. #>
  param([string]$Python, [string]$Target, [switch]$AtBoot)
  Write-Step "Creating the 'nuarr' scheduled task"
  [void](Invoke-SchTasks @('/Query','/TN','nuarr'))
  if ($LASTEXITCODE -eq 0) {
    [void](Invoke-SchTasks @('/End','/TN','nuarr'))
    [void](Invoke-SchTasks @('/Delete','/TN','nuarr','/F'))
    Write-Step "  replaced the existing task"
  }
  $launch = Join-Path $Target 'launch.py'
  $act = New-ScheduledTaskAction -Execute $Python -Argument "`"$launch`"" -WorkingDirectory $Target
  $trg = if ($AtBoot) { New-ScheduledTaskTrigger -AtStartup } else { New-ScheduledTaskTrigger -AtLogOn }
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
           -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
           -RestartInterval (New-TimeSpan -Minutes 1)
  $prn = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  Register-ScheduledTask -TaskName 'nuarr' -Action $act -Trigger $trg -Settings $set `
      -Principal $prn -Description 'nuarr - media library standardiser' -Force | Out-Null
  Write-Step "Task created (runs as SYSTEM, highest privileges)" 'ok'
}

function Start-Nuarr {
  param([int]$Port = 8770, [int]$WaitSeconds = 60)
  Write-Step "Starting nuarr"
  [void](Invoke-SchTasks @('/Run','/TN','nuarr'))
  $url = "http://127.0.0.1:$Port/"
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $WaitSeconds) {
    Start-Sleep -Seconds 2
    try {
      $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 4 -ErrorAction Stop
      if ($r.StatusCode -eq 200) {
        Write-Step ("nuarr is answering on {0} after {1:n0}s" -f $url, $sw.Elapsed.TotalSeconds) 'ok'
        return $true
      }
    } catch {}
  }
  Write-Step "nuarr did not answer within ${WaitSeconds}s - check the log at $script:LogPath" 'warn'
  return $false
}

function New-Shortcuts {
  param([string]$Target, [int]$Port)
  try {
    $ws = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath('CommonDesktopDirectory')
    $lnk = $ws.CreateShortcut((Join-Path $desktop 'nuarr.lnk'))
    $lnk.TargetPath = "http://127.0.0.1:$Port/"
    $ico = Join-Path $Target 'assets\Nuarr.ico'
    if (Test-Path -LiteralPath $ico) { $lnk.IconLocation = $ico }
    $lnk.Save()
    Write-Step "Desktop shortcut created" 'ok'
  } catch { Write-Step "Could not create the shortcut: $($_.Exception.Message)" 'warn' }
}

function Install-Uninstaller {
  <#  Install the REAL uninstaller, and tell Windows it exists.

      What shipped before was a hand-rolled batch stub that knew about the
      task and the program folder and nothing else - not the shortcut, not
      the cache, not the firewall rule - while the full Nuarr-Uninstall.ps1
      sat in the bundle and never reached the target machine. Worse, a global
      search-and-replace had baked PowerShell syntax into the batch text, so
      two of its lines were noise to cmd.exe. The first real uninstall left
      the desktop icon and the cache folder behind, which is how both facts
      were discovered.

      The wrapper copies the ps1 to TEMP and STARTS it detached before
      exiting: a batch file is read incrementally, so a wrapper that stayed
      alive inside C:\nuarr would hold the folder open against its own
      deletion. The ps1 self-elevates, so the Programs and Features entry -
      which Windows runs unelevated - works from a double-click too. #>
  param([string]$Target, [string]$DataDir, [string]$Version, [int]$Port)
  $src = Join-Path $PSScriptRoot 'Nuarr-Uninstall.ps1'
  if (Test-Path -LiteralPath $src) {
    Copy-Item $src (Join-Path $Target 'Nuarr-Uninstall.ps1') -Force
  } else {
    Write-Step "Nuarr-Uninstall.ps1 missing from the bundle" 'warn'; return
  }
  $cmd = @"
@echo off
REM  nuarr uninstaller. Removes the program, task, shortcut and cache;
REM  KEEPS the database at $DataDir unless you pass -RemoveData.
copy /Y "%~dp0Nuarr-Uninstall.ps1" "%TEMP%\Nuarr-Uninstall.ps1" >nul
start "" powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\Nuarr-Uninstall.ps1" -Target "$Target" -DataDir "$DataDir" %*
"@
  $p = Join-Path $Target 'Uninstall-nuarr.cmd'
  [System.IO.File]::WriteAllText($p, $cmd, (New-Object System.Text.ASCIIEncoding))
  Write-Step "Uninstaller installed to $p" 'ok'

  # The Programs and Features entry. EstimatedSize is a DWORD in KB.
  try {
    $rk = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\nuarr'
    New-Item -Path $rk -Force | Out-Null
    $sz = 0
    try {
      $sz = [int](((Get-ChildItem $Target,$DataDir -Recurse -File -EA SilentlyContinue |
                    Measure-Object Length -Sum).Sum) / 1KB)
    } catch {}
    $vals = @{
      DisplayName     = 'nuarr'
      DisplayVersion  = $Version
      Publisher       = 'NuGundam'
      InstallLocation = $Target
      DisplayIcon     = (Join-Path $Target 'assets\Nuarr.ico')
      UninstallString = "`"$p`""
      URLInfoAbout    = 'https://github.com/NuGundam/nuarr'
      InstallDate     = (Get-Date -Format 'yyyyMMdd')
    }
    foreach ($k in $vals.Keys) { New-ItemProperty -Path $rk -Name $k -Value $vals[$k] -Force | Out-Null }
    foreach ($k in 'NoModify','NoRepair') { New-ItemProperty -Path $rk -Name $k -Value 1 -PropertyType DWord -Force | Out-Null }
    if ($sz) { New-ItemProperty -Path $rk -Name EstimatedSize -Value $sz -PropertyType DWord -Force | Out-Null }
    Write-Step "Registered in Programs and Features" 'ok'
  } catch {
    Write-Step "Could not register in Programs and Features: $($_.Exception.Message)" 'warn'
  }
}
