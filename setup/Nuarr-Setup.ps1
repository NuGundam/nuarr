# ===========================================================================
#  nuarr - Setup wizard
#
#  A normal Windows setup: Next / Back, browse buttons, live progress. Every
#  field is pre-filled from something detected on this machine, so the common
#  case is pressing Next four times.
# ===========================================================================
[CmdletBinding()]
param([switch]$Unattended)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$Root   = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'Nuarr-Engine.ps1')

# ------------------------------------------------------------- palette ----
$C = @{
  Bg     = [Drawing.Color]::FromArgb(0x0D,0x11,0x17)
  Panel  = [Drawing.Color]::FromArgb(0x16,0x1B,0x22)
  Line   = [Drawing.Color]::FromArgb(0x30,0x36,0x3D)
  Fg     = [Drawing.Color]::FromArgb(0xC9,0xD1,0xD9)
  Dim    = [Drawing.Color]::FromArgb(0x8B,0x94,0x9E)
  Acc    = [Drawing.Color]::FromArgb(0x58,0xA6,0xFF)
  Ok     = [Drawing.Color]::FromArgb(0x3F,0xB9,0x50)
  Warn   = [Drawing.Color]::FromArgb(0xD2,0x99,0x22)
  Bad    = [Drawing.Color]::FromArgb(0xF8,0x51,0x49)
}
$FontUI  = New-Object Drawing.Font('Segoe UI', 9.5)
$FontH   = New-Object Drawing.Font('Segoe UI', 15, [Drawing.FontStyle]::Bold)
$FontSub = New-Object Drawing.Font('Segoe UI', 9)
$FontMono= New-Object Drawing.Font('Consolas', 9)

# --------------------------------------------------------------- state ----
$S = [ordered]@{
  Bundle    = $Root
  Python    = $null
  PyVer     = ''
  Gpu       = $null
  Target    = 'C:\nuarr'
  DataDir   = 'C:\ProgramData\nuarr'
  CacheDir  = 'C:\nuarr-cache'
  Port      = 8770
  Libraries = @()
  Arrs      = @()
  PlexUrl   = ''
  PlexToken = ''
  Whisper   = $false
  AtBoot    = $true
  StartNow  = $true
  Shortcut  = $true
  RestoreFrom = ''
  Failed    = $false
}

# ------------------------------------------------------------- helpers ----
function New-Label {
  param($Text,$X,$Y,$W=520,$H=20,$Font=$null,$Color=$null)
  $l = New-Object Windows.Forms.Label
  $l.Text=$Text; $l.Left=$X; $l.Top=$Y; $l.Width=$W; $l.Height=$H
  $l.Font= if($Font){$Font}else{$FontUI}
  $l.ForeColor = if($Color){$Color}else{$C.Fg}
  $l.BackColor=[Drawing.Color]::Transparent
  $l
}
function New-Box {
  param($Text,$X,$Y,$W=380)
  $t = New-Object Windows.Forms.TextBox
  $t.Text="$Text"; $t.Left=$X; $t.Top=$Y; $t.Width=$W
  $t.Font=$FontUI; $t.BackColor=[Drawing.Color]::FromArgb(0x0B,0x0E,0x12)
  $t.ForeColor=$C.Fg; $t.BorderStyle='FixedSingle'
  $t
}
function New-Btn {
  param($Text,$X,$Y,$W=90,$H=26)
  $b = New-Object Windows.Forms.Button
  $b.Text=$Text; $b.Left=$X; $b.Top=$Y; $b.Width=$W; $b.Height=$H
  $b.Font=$FontUI; $b.FlatStyle='Flat'
  $b.BackColor=[Drawing.Color]::FromArgb(0x21,0x26,0x2D); $b.ForeColor=$C.Fg
  $b.FlatAppearance.BorderColor=$C.Line
  $b
}
function New-Check {
  # The local is $cb, NOT $c. PowerShell variable names are case-INSENSITIVE,
  # so a local $c silently shadows the script-level $C palette, and the very
  # next line's $C.Fg then reads the CheckBox - which has no Fg property.
  param($Text,$X,$Y,$W=460,$Checked=$false)
  $cb = New-Object Windows.Forms.CheckBox
  $cb.Text=$Text; $cb.Left=$X; $cb.Top=$Y; $cb.Width=$W; $cb.Height=22
  $cb.Font=$FontUI; $cb.ForeColor=$C.Fg; $cb.BackColor=[Drawing.Color]::Transparent
  $cb.Checked=$Checked
  $cb
}
function Pick-Folder {
  param($Start,$Desc)
  $d = New-Object Windows.Forms.FolderBrowserDialog
  $d.Description=$Desc; $d.ShowNewFolderButton=$true
  if ($Start -and (Test-Path -LiteralPath $Start)) { $d.SelectedPath=$Start }
  if ($d.ShowDialog() -eq 'OK') { return $d.SelectedPath }
  return $null
}

# ---------------------------------------------------------------- form ----
$form = New-Object Windows.Forms.Form
$form.Text='nuarr Setup'
$form.Size = New-Object Drawing.Size(760, 580)
$form.StartPosition='CenterScreen'
$form.FormBorderStyle='FixedDialog'
$form.MaximizeBox=$false
$form.BackColor=$C.Bg
$form.Font=$FontUI
$ico = Join-Path $Root 'program\assets\Nuarr.ico'
if (Test-Path -LiteralPath $ico) { try { $form.Icon = New-Object Drawing.Icon($ico) } catch {} }

# header strip
$hdr = New-Object Windows.Forms.Panel
$hdr.Dock='Top'; $hdr.Height=68; $hdr.BackColor=$C.Panel
$lblTitle = New-Label 'Welcome' 22 14 600 28 $FontH
$lblSub   = New-Label '' 24 42 660 20 $FontSub $C.Dim
$hdr.Controls.AddRange(@($lblTitle,$lblSub))

# footer strip
$ftr = New-Object Windows.Forms.Panel
$ftr.Dock='Bottom'; $ftr.Height=54; $ftr.BackColor=$C.Panel
$btnBack   = New-Btn '< Back'  460 13 95 29
$btnNext   = New-Btn 'Next >'  562 13 95 29
$btnCancel = New-Btn 'Cancel'  655 13 85 29
$btnNext.BackColor = $C.Acc; $btnNext.ForeColor=[Drawing.Color]::Black
# A FLAT BUTTON KEEPS ITS COLOURS WHEN DISABLED. WinForms only repaints
# disabled buttons in the system style - FlatStyle=Flat with a custom
# BackColor stays exactly as painted, so the accent-blue Install button
# looked pressable through the entire install while Enabled was false.
# The colour has to follow the state by hand.
foreach ($fb in @($btnBack,$btnNext,$btnCancel)) {
  $fb.Add_EnabledChanged({
    $b = $args[0]
    if ($b.Enabled) {
      if ($b -eq $btnNext) { $b.BackColor=$C.Acc; $b.ForeColor=[Drawing.Color]::Black }
      else { $b.BackColor=[Drawing.Color]::FromArgb(0x21,0x26,0x2D); $b.ForeColor=$C.Fg }
    } else {
      $b.BackColor=[Drawing.Color]::FromArgb(0x14,0x18,0x1E); $b.ForeColor=$C.Dim
    }
  })
}
$lblStep = New-Label '' 22 19 380 20 $FontSub $C.Dim
$ftr.Controls.AddRange(@($lblStep,$btnBack,$btnNext,$btnCancel))

# body
$body = New-Object Windows.Forms.Panel
$body.Dock='Fill'; $body.BackColor=$C.Bg; $body.Padding = New-Object Windows.Forms.Padding(22,16,22,10)
$form.Controls.AddRange(@($body,$ftr,$hdr))

# ============================================================ page 0 ======
$p0 = New-Object Windows.Forms.Panel; $p0.Dock='Fill'; $p0.BackColor=$C.Bg
$p0.Controls.Add((New-Label "This will install nuarr on this computer." 0 6 660 22))
$p0.Controls.Add((New-Label ("nuarr standardises a media library so Plex direct-plays everything: it probes,`n" +
  "plans, fixes and verifies each file, listens to audio when the language tag`n" +
  "cannot be trusted, and paces itself against whoever is watching.") 0 30 680 62 $FontSub $C.Dim))
$p0chk = New-Object Windows.Forms.Panel
$p0chk.Left=0; $p0chk.Top=104; $p0chk.Width=690; $p0chk.Height=230
$p0chk.BackColor=$C.Panel
$p0.Controls.Add($p0chk)
$p0.Controls.Add((New-Label "Setup writes a full log to your TEMP folder; the path is shown at the end." 0 348 680 20 $FontSub $C.Dim))
$lblPre = New-Label '' 16 12 650 200 $FontMono
$p0chk.Controls.Add($lblPre)

# ============================================================ page 1 ======
$p1 = New-Object Windows.Forms.Panel; $p1.Dock='Fill'; $p1.BackColor=$C.Bg
$p1.Controls.Add((New-Label 'Program folder' 0 4 300 20))
$txtTarget = New-Box $S.Target 0 26 520
$btnTarget = New-Btn 'Browse...' 530 25 100
$p1.Controls.Add($txtTarget); $p1.Controls.Add($btnTarget)
$p1.Controls.Add((New-Label 'The code. Small, and replaced wholesale on every upgrade.' 0 50 620 18 $FontSub $C.Dim))

$p1.Controls.Add((New-Label 'Data folder' 0 82 300 20))
$txtData = New-Box $S.DataDir 0 104 520
$btnData = New-Btn 'Browse...' 530 103 100
$p1.Controls.Add($txtData); $p1.Controls.Add($btnData)
$p1.Controls.Add((New-Label 'Database, logs, ffmpeg and the Whisper model. Survives uninstall.' 0 128 620 18 $FontSub $C.Dim))

$p1.Controls.Add((New-Label 'Transcode cache' 0 160 300 20))
$txtCache = New-Box $S.CacheDir 0 182 520
$btnCache = New-Btn 'Browse...' 530 181 100
$p1.Controls.Add($txtCache); $p1.Controls.Add($btnCache)
$lblCacheFree = New-Label '' 0 206 620 18 $FontSub $C.Dim
$p1.Controls.Add($lblCacheFree)
$p1.Controls.Add((New-Label 'Scratch space for encodes in flight. Put it on the fastest drive you have,' 0 226 620 18 $FontSub $C.Dim))
$p1.Controls.Add((New-Label 'ideally not on the pool. Safe to lose - nuarr rebuilds it.' 0 242 620 18 $FontSub $C.Dim))

$p1.Controls.Add((New-Label 'Web port' 0 276 120 20))
$txtPort = New-Box $S.Port 0 298 100
$p1.Controls.Add($txtPort)
$p1.Controls.Add((New-Label 'The dashboard listens here on localhost.' 110 302 500 18 $FontSub $C.Dim))

# ============================================================ page 2 ======
$p2 = New-Object Windows.Forms.Panel; $p2.Dock='Fill'; $p2.BackColor=$C.Bg
$p2.Controls.Add((New-Label 'Add each folder that holds a library - one entry per library, with its kind.' 0 4 640 20))
$lvLib = New-Object Windows.Forms.ListView
$lvLib.Left=0; $lvLib.Top=30; $lvLib.Width=690; $lvLib.Height=250
$lvLib.View='Details'; $lvLib.CheckBoxes=$true; $lvLib.FullRowSelect=$true
$lvLib.BackColor=[Drawing.Color]::FromArgb(0x0B,0x0E,0x12); $lvLib.ForeColor=$C.Fg
$lvLib.Font=$FontUI; $lvLib.BorderStyle='FixedSingle'
[void]$lvLib.Columns.Add('Name',180)
[void]$lvLib.Columns.Add('Path',380)
[void]$lvLib.Columns.Add('Kind',90)
$p2.Controls.Add($lvLib)
$btnLibAdd = New-Btn 'Add folder...' 0 288 120
$btnLibDel = New-Btn 'Remove' 128 288 90
$btnLibKind= New-Btn 'Toggle kind' 226 288 100
$p2.Controls.Add($btnLibAdd); $p2.Controls.Add($btnLibDel); $p2.Controls.Add($btnLibKind)
$p2.Controls.Add((New-Label 'Kind decides how names are parsed - series get S01E01, films get a year.' 0 320 660 18 $FontSub $C.Dim))

# ============================================================ page 3 ======
$p3 = New-Object Windows.Forms.Panel; $p3.Dock='Fill'; $p3.BackColor=$C.Bg
function Add-ArrRow {
  param($Panel,$Title,$Y,$Note)
  $Panel.Controls.Add((New-Label $Title 0 $Y 200 20))
  $u = New-Box '' 0 ($Y+22) 300
  $k = New-Box '' 308 ($Y+22) 240
  # A CREDENTIAL FIELD SHOWS DOTS. This page has been screenshotted twice
  # with live production API keys legible in it. Masking costs nothing -
  # Test proves a key works without anyone reading it - and makes the
  # screenshot leak impossible.
  $k.UseSystemPasswordChar = $true
  $b = New-Btn 'Test' 556 ($Y+21) 70
  $st = New-Label '' 0 ($Y+48) 660 18 $FontSub $C.Dim   # $st, never $s - see Load-Detected
  $st.Text = $Note
  $Panel.Controls.AddRange(@($u,$k,$b,$st))
  @{Url=$u; Key=$k; Btn=$b; Status=$st}
}
$p3.Controls.Add((New-Label 'Detected automatically where possible. Blank means "skip this one".' 0 2 660 18 $FontSub $C.Dim))
$rSon  = Add-ArrRow $p3 'Sonarr    (URL / API key)' 26 ''
$rRad  = Add-ArrRow $p3 'Radarr    (URL / API key)' 108 ''
$rPlex = Add-ArrRow $p3 'Plex      (URL / token)'   190 ''
$p3.Controls.Add((New-Label 'A local Plex server keeps its own token in the registry - Setup reads it there.' 0 262 660 18 $FontSub $C.Dim))
$p3.Controls.Add((New-Label 'nuarr also reads Plex''s transcoder throttle live, which its pacing depends on.' 0 280 660 18 $FontSub $C.Dim))

# ============================================================ page 4 ======
$p4 = New-Object Windows.Forms.Panel; $p4.Dock='Fill'; $p4.BackColor=$C.Bg
$chkBoot  = New-Check 'Start nuarr automatically (scheduled task, runs as SYSTEM)' 0 6 620 $true
$chkStart = New-Check 'Start nuarr when Setup finishes' 0 34 620 $true
$chkLnk   = New-Check 'Put a shortcut to the dashboard on the desktop' 0 62 620 $true
$p4.Controls.AddRange(@($chkBoot,$chkStart,$chkLnk))

$p4.Controls.Add((New-Label 'Audio language detection' 0 102 400 20))
$chkWhisper = New-Check 'Install Whisper so nuarr can identify audio by listening' 0 124 620 $false
$p4.Controls.Add($chkWhisper)
$lblWhy = New-Label '' 18 148 660 54 $FontSub $C.Dim
$p4.Controls.Add($lblWhy)

$p4.Controls.Add((New-Label 'Restore a database (optional)' 0 214 400 20))
$txtRestore = New-Box '' 0 236 520
$btnRestore = New-Btn 'Browse...' 530 235 100
$p4.Controls.AddRange(@($txtRestore,$btnRestore))
$p4.Controls.Add((New-Label 'Point at a nuarr-YYYYMMDD-HHMMSS folder to bring a library back with its history.' 0 260 680 18 $FontSub $C.Dim))
$p4.Controls.Add((New-Label 'Leave blank for a fresh start - nuarr will scan the libraries itself.' 0 278 680 18 $FontSub $C.Dim))

# ============================================================ page 5 ======
$p5 = New-Object Windows.Forms.Panel; $p5.Dock='Fill'; $p5.BackColor=$C.Bg
$bar = New-Object Windows.Forms.ProgressBar
$bar.Left=0; $bar.Top=8; $bar.Width=690; $bar.Height=18; $bar.Style='Continuous'; $bar.Maximum=100
$p5.Controls.Add($bar)
$lblNow = New-Label '' 0 32 690 20
$p5.Controls.Add($lblNow)
$lstLog = New-Object Windows.Forms.ListBox
$lstLog.Left=0; $lstLog.Top=58; $lstLog.Width=690; $lstLog.Height=290
$lstLog.BackColor=[Drawing.Color]::FromArgb(0x0B,0x0E,0x12); $lstLog.ForeColor=$C.Dim
$lstLog.Font=$FontMono; $lstLog.BorderStyle='FixedSingle'
$lstLog.DrawMode='OwnerDrawFixed'; $lstLog.ItemHeight=16
$lstLog.Add_DrawItem({
  param($sender,$e)
  $e.DrawBackground()
  if ($e.Index -ge 0) {
    $txt = $sender.Items[$e.Index]
    $col = $C.Dim
    if ($txt -like '[OK]*')   { $col = $C.Ok }
    if ($txt -like '[!]*')    { $col = $C.Warn }
    if ($txt -like '[X]*')    { $col = $C.Bad }
    $br = New-Object Drawing.SolidBrush($col)
    $e.Graphics.DrawString($txt, $FontMono, $br, 2, $e.Bounds.Top)
    $br.Dispose()
  }
})
$p5.Controls.Add($lstLog)

# ============================================================ page 6 ======
$p6 = New-Object Windows.Forms.Panel; $p6.Dock='Fill'; $p6.BackColor=$C.Bg
$lblDone = New-Label '' 0 8 690 28 $FontH
$p6.Controls.Add($lblDone)
$lblDoneSub = New-Label '' 0 44 690 260 $FontUI $C.Dim
$p6.Controls.Add($lblDoneSub)
# THE LAST PAGE'S ACTIONS BELONG ON THE FINISH BUTTON, not on buttons of
# their own. As two buttons they were easy to walk straight past - you read
# the summary, click Finish, and the wizard closes having done neither.
# Ticked checkboxes that fire on Finish are what every Windows installer
# does, and for the same reason: the common case then needs no extra click.
$chkOpenUI  = New-Check 'Open the nuarr dashboard' 0 314 400 $true
$chkReadme  = New-Check 'Show the README'          0 340 400 $false
$chkShowLog = New-Check 'Open the setup log'       0 366 400 $false
$p6.Controls.AddRange(@($chkOpenUI,$chkReadme,$chkShowLog))

$pages = @($p0,$p1,$p2,$p3,$p4,$p5,$p6)
foreach ($p in $pages) { $p.Visible=$false; $body.Controls.Add($p) }

$titles = @(
  @('Welcome',        'Setup will install nuarr and connect it to your library'),
  @('Locations',      'Where the program, its data and its scratch space live'),
  @('Libraries',      'Which folders nuarr manages'),
  @('Integrations',   'Sonarr, Radarr and Plex'),
  @('Options',        'How it starts, and what else to install'),
  @('Installing',     'This takes a few minutes'),
  @('Finished',       '')
)

# --------------------------------------------------------------- logic ----
$script:Page = 0

function Log-Line {
  param($Text,$Level='info')
  $tag = switch ($Level) { 'ok' {'[OK] '} 'warn' {'[!]  '} 'err' {'[X]  '} default {'     '} }
  [void]$lstLog.Items.Add("$tag$Text")
  $lstLog.TopIndex = $lstLog.Items.Count - 1
  $lblNow.Text = $Text
  [Windows.Forms.Application]::DoEvents()
}
$script:LogSink = ${function:Log-Line}

function Refresh-Prereqs {
  $sb = New-Object Text.StringBuilder
  $py = Find-Python
  if ($py) {
    $S.Python = $py.Exe; $S.PyVer = $py.Version
    # The outer parentheses matter: without them PowerShell reads the comma as
    # a second argument to AppendLine, so -f gets one value for two slots.
    [void]$sb.AppendLine(("  Python          {0}" -f $py.Version))
    # its own line: a full interpreter path wraps and breaks the column
    [void]$sb.AppendLine(("                  {0}" -f $py.Exe))
  } else {
    # NOT A DEAD END. This page used to disable Next here with an instruction
    # to go install Python by hand - while the README promised the installer
    # handled it, and the bundle sat there holding an installer it never ran.
    # Now the missing case states what Setup will do, and the install itself
    # happens as the first step of Run-Install, where its progress is visible.
    $bundledPy = Get-ChildItem (Join-Path $Root 'python') -Filter 'python-*.exe' `
                   -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($bundledPy) {
      [void]$sb.AppendLine(("  Python          not installed - Setup will install {0}" -f $bundledPy.BaseName))
    } else {
      [void]$sb.AppendLine("  Python          NOT FOUND")
      [void]$sb.AppendLine("                  install Python 3.11+ and tick 'Add python.exe to PATH'")
    }
  }
  $gpu = Test-Nvidia
  $S.Gpu = $gpu
  [void]$sb.AppendLine("  GPU             {0}" -f $(if($gpu){$gpu}else{'no NVIDIA GPU detected'}))
  $wh = Join-Path $Root 'wheels'
  $nw = if (Test-Path -LiteralPath $wh) { (Get-ChildItem $wh -File).Count } else { 0 }
  [void]$sb.AppendLine("  Bundled wheels  {0} packages" -f $nw)
  $ff = Join-Path $Root 'ffmpeg'
  $ffn = if (Test-Path -LiteralPath $ff) { (Get-ChildItem $ff -Directory | Select-Object -First 1).Name } else { 'none' }
  [void]$sb.AppendLine("  Bundled ffmpeg  {0}" -f $ffn)
  $mkv = Join-Path $Root 'mkvtoolnix'
  $mkn = if (Test-Path -LiteralPath $mkv) { (Get-ChildItem $mkv -Filter *.exe).Count } else { 0 }
  [void]$sb.AppendLine(("  Bundled MKV     {0}" -f $(if($mkn){"mkvmerge, mkvpropedit, mkvextract"}else{"none"})))
  [void]$sb.AppendLine("  Administrator   yes")
  [void]$sb.AppendLine("")
  $existing = Test-Path -LiteralPath (Join-Path $S.Target 'launch.py')
  if ($existing) {
    [void]$sb.AppendLine("  An existing install was found at $($S.Target).")
    [void]$sb.AppendLine("  Setup will upgrade it in place and keep your database.")
  } else {
    [void]$sb.AppendLine("  No existing install found - this will be a fresh one.")
  }
  $lblPre.Text = $sb.ToString()
  # Next stays live when Python is merely MISSING-BUT-BUNDLED - only a machine
  # with no Python AND no bundled installer is genuinely stuck.
  $btnNext.Enabled = [bool]$py -or [bool](Get-ChildItem (Join-Path $Root 'python') `
      -Filter 'python-*.exe' -ErrorAction SilentlyContinue)
}

function Load-Detected {
  # THE LIBRARY LIST STARTS EMPTY, ON PURPOSE.
  #
  # It used to run Find-Libraries and tick everything it found. On the dev
  # box that read as magic; on the first other machine it read as a mess -
  # the heuristic swept up P:\Video_Temp and friends, pre-ticked, and one
  # missed checkbox away from nuarr managing a scratch folder. A wrong
  # pre-selection is worse than none: unticking demands the user audit a
  # list they never asked for, against a heuristic they cannot see.
  #
  # Choosing libraries is THE decision this wizard exists to collect. It is
  # not a place to guess. Add folder... is one click per library.
  $lvLib.Items.Clear()
  # arrs
  # NOT $s / $r: PowerShell names are case-insensitive, so a local $s shadows
  # the script-level $S state object and every later $S.Something reads the
  # wrong thing. Cost an "unhandled exception" dialog to learn once.
  $son = Find-ArrConfig -Kind sonarr
  # DETECTED CREDENTIALS ARE USED, NOT SHOWN - they ride in $S and apply
  # silently when the field is left blank; typing a key still overrides.
  $rSon.Url.Text = $son.Url; $S.DetSonKey = $son.ApiKey
  $rSon.Status.Text = if ($son.Found) { 'API key found in Sonarr''s config.xml - leave blank to use it' } else { 'Not found locally - paste the URL and key if you have them' }
  $rad = Find-ArrConfig -Kind radarr
  $rRad.Url.Text = $rad.Url; $S.DetRadKey = $rad.ApiKey
  $rRad.Status.Text = if ($rad.Found) { 'API key found in Radarr''s config.xml - leave blank to use it' } else { 'Not found locally - paste the URL and key if you have them' }
  $tok = Find-PlexToken
  $rPlex.Url.Text = 'http://localhost:32400'
  $S.DetPlexTok = $tok
  $rPlex.Status.Text = if ($tok) { 'Token found on the local Plex server - leave blank to use it' } else { 'No local Plex server - paste a token if you have one' }
  # whisper
  if ($S.Gpu) {
    $chkWhisper.Enabled = $true
    $lblWhy.Text = "Downloads about 2 GB (faster-whisper and the CUDA runtime) plus a 464 MB model.`nWithout it nuarr works normally; the audio-language feature simply stays off.`nDetected: $($S.Gpu)"
  } else {
    $chkWhisper.Enabled = $false; $chkWhisper.Checked = $false
    $lblWhy.Text = "No NVIDIA GPU detected, so language detection would run on the CPU at a`ncrawl. Left off. nuarr works normally without it."
  }
  # cache free space
  $free = Get-FreeGB $S.CacheDir
  $lblCacheFree.Text = if ($free) { "$free GB free on that drive" } else { '' }
}

function Collect-Page1 {
  $S.Target   = $txtTarget.Text.Trim()
  $S.DataDir  = $txtData.Text.Trim()
  $S.CacheDir = $txtCache.Text.Trim()
  $p = 0
  if (-not [int]::TryParse($txtPort.Text.Trim(), [ref]$p) -or $p -lt 1 -or $p -gt 65535) {
    [Windows.Forms.MessageBox]::Show('The port must be a number between 1 and 65535.','nuarr Setup') | Out-Null
    return $false
  }
  $S.Port = $p
  foreach ($d in @($S.Target,$S.DataDir,$S.CacheDir)) {
    if (-not $d) { [Windows.Forms.MessageBox]::Show('All three folders are required.','nuarr Setup')|Out-Null; return $false }
  }
  return $true
}

function Collect-Page2 {
  $libs = @()
  foreach ($it in $lvLib.Items) {
    if ($it.Checked) {
      $libs += [pscustomobject]@{ Name=$it.Text; Path=$it.SubItems[1].Text; Kind=$it.SubItems[2].Text }
    }
  }
  if ($libs.Count -eq 0) {
    $a = [Windows.Forms.MessageBox]::Show(
      "No libraries are ticked. nuarr will install but have nothing to work on.`n`nCarry on anyway?",
      'nuarr Setup','YesNo','Warning')
    if ($a -ne 'Yes') { return $false }
  }
  $S.Libraries = $libs
  return $true
}

function Collect-Page3 {
  # Typed beats detected; detected beats nothing. A URL is still required
  # either way - a key with no server is not a connection.
  $arrs = @()
  $sk = $rSon.Key.Text.Trim(); if (-not $sk) { $sk = "$($S.DetSonKey)".Trim() }
  $rk = $rRad.Key.Text.Trim(); if (-not $rk) { $rk = "$($S.DetRadKey)".Trim() }
  if ($sk -and $rSon.Url.Text.Trim()) { $arrs += [pscustomobject]@{Name='Sonarr';Kind='sonarr';Url=$rSon.Url.Text.Trim();ApiKey=$sk} }
  if ($rk -and $rRad.Url.Text.Trim()) { $arrs += [pscustomobject]@{Name='Radarr';Kind='radarr';Url=$rRad.Url.Text.Trim();ApiKey=$rk} }
  $S.Arrs = $arrs
  $S.PlexUrl = $rPlex.Url.Text.Trim()
  $pt = $rPlex.Key.Text.Trim(); if (-not $pt) { $pt = "$($S.DetPlexTok)".Trim() }
  $S.PlexToken = $pt
  return $true
}

function Collect-Page4 {
  $S.AtBoot   = $chkBoot.Checked
  $S.StartNow = $chkStart.Checked
  $S.Shortcut = $chkLnk.Checked
  $S.Whisper  = $chkWhisper.Checked
  $S.RestoreFrom = $txtRestore.Text.Trim()
  return $true
}

function Show-Page {
  param([int]$i)
  $script:Page = $i
  foreach ($p in $pages) { $p.Visible = $false }
  $pages[$i].Visible = $true
  $lblTitle.Text = $titles[$i][0]
  $lblSub.Text   = $titles[$i][1]
  $lblStep.Text  = "Step $($i+1) of $($pages.Count)"
  $btnBack.Enabled = ($i -gt 0 -and $i -lt 5)
  switch ($i) {
    0 { $btnNext.Text='Next >';    $btnNext.Enabled=$true; Refresh-Prereqs }
    4 { $btnNext.Text='Install' }
    5 {
        $btnNext.Enabled=$false; $btnBack.Enabled=$false
        # CANCEL STAYS ALIVE, and takes the accent - it is the only thing on
        # the page that can still be done. Write-Step pumps the message loop,
        # so the click lands mid-install; the flag is honoured at the next
        # step boundary rather than mid-file, which is the difference between
        # stopping and corrupting.
        $btnCancel.Enabled=$true
        $btnCancel.BackColor=$C.Acc; $btnCancel.ForeColor=[Drawing.Color]::Black
      }
    6 { $btnNext.Text='Finish'; $btnNext.Enabled=$true; $btnCancel.Visible=$false }
    default { $btnNext.Text='Next >'; $btnNext.Enabled=$true }
  }
}

# ------------------------------------------------------------ the work ----
function Run-Install {
  $steps = 9
  $n = 0
  function Tick { param($what)
    if ($script:CancelInstall) { throw 'CANCELLED' }
    $script:n++; $bar.Value=[math]::Min(100,[int](100*$script:n/$steps)); Write-Step $what }
  $script:n = 0
  try {
    Write-Step "Setup log: $script:LogPath"
    Write-Step ("Target {0} | Data {1} | Cache {2} | Port {3}" -f $S.Target,$S.DataDir,$S.CacheDir,$S.Port)

    [void](Invoke-SchTasks @('/Query','/TN','nuarr'))
    if ($LASTEXITCODE -eq 0) { Write-Step "Stopping the running nuarr"; [void](Invoke-SchTasks @('/End','/TN','nuarr')); Start-Sleep 3 }

    Tick "Creating folders"
    foreach ($d in @($S.Target,$S.DataDir,$S.CacheDir)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    Write-Step "Folders ready" 'ok'

    Tick "Copying the program"
    Install-Program -Bundle $S.Bundle -Target $S.Target

    if (-not $S.Python) {
      Tick "Installing Python (bundled)"
      $got = Install-Python -Bundle $S.Bundle
      if (-not $got) { throw "Python could not be installed - see the log" }
      $S.Python = $got.Exe; $S.PyVer = $got.Version
    }
    Tick "Installing Python packages"
    Install-Packages -Bundle $S.Bundle -Python $S.Python

    Tick "Restoring ffmpeg"
    Install-Ffmpeg -Bundle $S.Bundle -DataDir $S.DataDir

    Tick "Installing the MKVToolNix tools"
    Install-MkvTools -Bundle $S.Bundle

    Tick "Writing the configuration"
    Write-NuarrConfig -Target $S.Target -Cfg @{
      Libraries=$S.Libraries; Arrs=$S.Arrs; CacheDir=$S.CacheDir
      PlexUrl=$S.PlexUrl; PlexToken=$S.PlexToken
    }

    Tick "Restoring the database"
    if ($S.RestoreFrom) { Restore-Database -From $S.RestoreFrom -DataDir $S.DataDir -Target $S.Target }
    else { Write-Step "Fresh install - nuarr will build its own database on first scan" }
    Test-DatabaseHealth -Python $S.Python -DataDir $S.DataDir

    Tick "Audio language detection"
    if ($S.Whisper) { [void](Install-Whisper -Python $S.Python -DataDir $S.DataDir) }
    else { Write-Step "Whisper not selected - audio language detection stays off" }

    Tick "Registering the scheduled task"
    if ($S.AtBoot) { New-NuarrTask -Python $S.Python -Target $S.Target -AtBoot }
    else { New-NuarrTask -Python $S.Python -Target $S.Target }
    $bv = '0.0.0'
    try { $bv = (Get-Content (Join-Path $Root 'bundle.json') -Raw | ConvertFrom-Json).version } catch {}
    Install-Uninstaller -Target $S.Target -DataDir $S.DataDir -Version $bv -Port $S.Port
    if ($S.Shortcut) { New-Shortcuts -Target $S.Target -Port $S.Port }

    Tick "Starting nuarr"
    if ($S.StartNow) {
      if (-not (Start-Nuarr -Port $S.Port)) { Write-Step "Started, but not answering yet - it may still be scanning" 'warn' }
    } else { Write-Step "Not started, as asked. Run the 'nuarr' task when ready." }

    $bar.Value = 100
    Write-Step "Setup complete" 'ok'
    $S.Failed = $false
  } catch {
    $S.Failed = $true
    Write-Step ("FAILED: " + $_.Exception.Message) 'err'
    Write-Step "Nothing was removed. Fix the problem and run Setup again." 'err'
  }
}

# ------------------------------------------------------------- wiring -----
$btnTarget.Add_Click({ $p = Pick-Folder $txtTarget.Text 'Where should the nuarr program live?'; if ($p) { $txtTarget.Text=$p } })
$btnData.Add_Click({   $p = Pick-Folder $txtData.Text   'Where should nuarr keep its database and logs?'; if ($p) { $txtData.Text=$p } })
$btnCache.Add_Click({
  $p = Pick-Folder $txtCache.Text 'Where should the transcode cache go?'
  if ($p) { $txtCache.Text=$p; $f = Get-FreeGB $p; $lblCacheFree.Text = if($f){"$f GB free on that drive"}else{''} }
})
$btnRestore.Add_Click({ $p = Pick-Folder $txtRestore.Text 'Pick a nuarr-YYYYMMDD-HHMMSS backup folder'; if ($p) { $txtRestore.Text=$p } })

$btnLibAdd.Add_Click({
  $p = Pick-Folder 'C:\' 'Pick a media folder'
  if (-not $p) { return }
  $name = Split-Path $p -Leaf
  $kind = if ($name -match 'movie|film') { 'movie' } else { 'tv' }
  $it = New-Object Windows.Forms.ListViewItem($name)
  [void]$it.SubItems.Add($p); [void]$it.SubItems.Add($kind); $it.Checked=$true
  [void]$lvLib.Items.Add($it)
})
$btnLibDel.Add_Click({ foreach ($it in @($lvLib.SelectedItems)) { $lvLib.Items.Remove($it) } })
$btnLibKind.Add_Click({
  foreach ($it in @($lvLib.SelectedItems)) {
    $it.SubItems[2].Text = if ($it.SubItems[2].Text -eq 'tv') { 'movie' } else { 'tv' }
  }
})

$rSon.Btn.Add_Click({
  $rSon.Status.Text='Testing...'; $rSon.Status.ForeColor=$C.Dim
  $r = Test-ArrConnection -Url $rSon.Url.Text -ApiKey $(if($rSon.Key.Text.Trim()){$rSon.Key.Text}else{$S.DetSonKey}) -Kind sonarr
  $rSon.Status.Text = $r.Detail; $rSon.Status.ForeColor = if($r.Ok){$C.Ok}else{$C.Bad}
})
$rRad.Btn.Add_Click({
  $rRad.Status.Text='Testing...'; $rRad.Status.ForeColor=$C.Dim
  $r = Test-ArrConnection -Url $rRad.Url.Text -ApiKey $(if($rRad.Key.Text.Trim()){$rRad.Key.Text}else{$S.DetRadKey}) -Kind radarr
  $rRad.Status.Text = $r.Detail; $rRad.Status.ForeColor = if($r.Ok){$C.Ok}else{$C.Bad}
})
$rPlex.Btn.Add_Click({
  $rPlex.Status.Text='Testing...'; $rPlex.Status.ForeColor=$C.Dim
  $r = Test-PlexConnection -Url $rPlex.Url.Text -Token $(if($rPlex.Key.Text.Trim()){$rPlex.Key.Text}else{$S.DetPlexTok})
  $rPlex.Status.Text = $r.Detail; $rPlex.Status.ForeColor = if($r.Ok){$C.Ok}else{$C.Bad}
})

$btnBack.Add_Click({ if ($script:Page -gt 0) { Show-Page ($script:Page-1) } })
$script:CancelInstall = $false
$btnCancel.Add_Click({
  # TWO DIFFERENT PROMISES. Before the install, quitting genuinely changes
  # nothing. DURING it, files are already landing, so the honest offer is
  # "stop at the next step boundary" - the flag is read by Tick, never
  # mid-file, so a half-written copy cannot be the result.
  if ($script:Page -eq 5) {
    $a = [Windows.Forms.MessageBox]::Show(
      "Stop the install?`n`nSetup stops after the step it is on. Anything " +
      "already put in place stays until you run Setup again or Uninstall.",
      'nuarr Setup','YesNo','Warning')
    if ($a -eq 'Yes') {
      $script:CancelInstall = $true
      $btnCancel.Enabled = $false
      $btnCancel.Text = 'Stopping...'
    }
    return
  }
  $a = [Windows.Forms.MessageBox]::Show('Quit Setup? Nothing has been changed yet.','nuarr Setup','YesNo','Question')
  if ($a -eq 'Yes') { $form.Close() }
})

$btnNext.Add_Click({
  # A HANDLER THAT THROWS gets you the generic WinForms "Unhandled exception"
  # box, which names a property and nothing else - no file, no line. Catching
  # here turns any slip into a message that says where it happened.
  try {
    Advance-Wizard
  } catch {
    $where = $_.ScriptStackTrace -split "`n" | Select-Object -First 3
    $msg = "{0}`n`n{1}" -f $_.Exception.Message, ($where -join "`n")
    try { Add-Content -Path $script:LogPath -Value "WIZARD ERROR: $msg" -Encoding UTF8 } catch {}
    [Windows.Forms.MessageBox]::Show($msg, 'nuarr Setup - internal error', 'OK', 'Error') | Out-Null
  }
})

function Advance-Wizard {
  switch ($script:Page) {
    0 { Load-Detected; Show-Page 1 }
    1 { if (Collect-Page1) { Show-Page 2 } }
    2 { if (Collect-Page2) { Show-Page 3 } }
    3 { if (Collect-Page3) { Show-Page 4 } }
    4 {
        if (-not (Collect-Page4)) { return }
        Show-Page 5
        Run-Install
        if ($S.Failed -and $script:CancelInstall) {
          # A cancel is not a failure, and the failure text lied about it -
          # "the machine is as it was" stops being true once files have landed.
          $lblDone.Text = 'Setup was stopped'
          $lblDone.ForeColor = $C.Warn
          $lblDoneSub.Text = "Stopped between steps, at your request.`n`nAnything already put in place is still at $($S.Target).`nRun Setup again to finish the job, or Uninstall.cmd to clear it away.`n`nSetup log:`n$script:LogPath"
          $chkOpenUI.Checked = $false; $chkOpenUI.Enabled = $false
          $chkReadme.Checked = $false; $chkReadme.Enabled = $false
          $chkShowLog.Checked = $false
        } elseif ($S.Failed) {
          $lblDone.Text = 'Setup did not finish'
          $lblDone.ForeColor = $C.Bad
          $lblDoneSub.Text = "Something went wrong and Setup stopped.`n`nThe log has the exact error:`n`n$script:LogPath"
          $chkOpenUI.Checked = $false; $chkOpenUI.Enabled = $false
          $chkReadme.Checked = $false; $chkReadme.Enabled = $false
          $chkShowLog.Checked = $true    # on failure the log is the point
        } else {
          $lblDone.Text = 'nuarr is installed'
          $lblDone.ForeColor = $C.Ok
          $libn = $S.Libraries.Count
          $arrn = $S.Arrs.Count
          $lblDoneSub.Text = @"
Dashboard    http://127.0.0.1:$($S.Port)/
Program      $($S.Target)
Data         $($S.DataDir)
Cache        $($S.CacheDir)

Libraries    $libn managed
Integrations $arrn arr(s)$(if($S.PlexToken){' + Plex'}else{''})
Whisper      $(if($S.Whisper){'installed'}else{'not installed'})
Autostart    $(if($S.AtBoot){'yes, at boot'}else{'at logon'})

The first scan takes a while on a large library. Watch it happen
on the dashboard - nothing is modified until you let it.

Setup log    $script:LogPath
"@
        }
        Show-Page 6
      }
    6 {
        # FINISH DOES THE THINGS THAT WERE TICKED. Ordered deliberately: the
        # dashboard first so the browser is already loading while the rest
        # opens, and every one wrapped, because a missing README or a browser
        # that will not launch must not turn the last click of a successful
        # install into an error dialog.
        if (-not $S.Failed) {
          if ($chkOpenUI.Checked) {
            try { Start-Process ("http://127.0.0.1:{0}/" -f $S.Port) } catch {}
          }
          if ($chkReadme.Checked) {
            $rm = Join-Path $S.Target 'README.md'
            try { if (Test-Path $rm) { Start-Process notepad.exe $rm } } catch {}
          }
        }
        if ($chkShowLog.Checked) {
          try { Start-Process notepad.exe $script:LogPath } catch {}
        }
        $form.Close()
      }
  }
}

Show-Page 0
[void]$form.ShowDialog()
