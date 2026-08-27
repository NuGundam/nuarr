# nuarr — what happens to a file, and when

A map of every stage a file passes through, so you can see what is covered and
what is not. Status is honest: **verified** means I watched it work on a real
file from your library this session; **untested** means the code path exists but
has not been proven.

---

## The pipeline, in order

```
1. SCAN        walk PoolParts + ask Sonarr/Radarr   -> files table
2. HOLD        wait until quiet + not locked        -> eligible
3. GATE        Plex / DrivePool / arr / cache       -> may start?
4. HANDLERS    repair + OCR scripts                 -> drain BEFORE transcodes
5. PROBE       ffprobe, cached                      -> streams + format
6. PLAN        rules.py (ported plugins)            -> actions + reasons
7. ENCODE      ffmpeg to NVMe cache                 -> new file
8. SIZE GATE   must save >=5% or discard            -> keep or bin
9. COMMIT      fileops verified swap                -> library
10. REFRESH    arr re-reads mediainfo (debounced)   -> new codecs known
11. RENAME     arr proposes, nuarr verifies         -> correct filename
```

**Handlers run per file, inside its own job, before the transcode.** Stage 4 is
not a separate queue you have to trigger — every queued file is checked against
every handler's trigger and the qualifying ones run first. If a handler changes
the file, nuarr **re-probes and re-plans** so the encode is decided from what the
file actually became, not what it was.

Handler jobs queued by hand still exist and still drain ahead of transcodes.

Set `run_handlers_inline: false` in `config.yml` to go back to manual-only.

---

## Handlers — the PowerShell scripts

| Kind | Script | Target | Triggers on | What it changes | Status |
|---|---|---|---|---|---|
| `ocr_forced` | Convert-ForcedPgsToSrt.ps1 | `-File` | forced PGS track | OCRs it to `<name>.en.forced.srt`, clears the PGS default/forced flags in place | **verified runs** |
| `embed_srt` | Embed-ForcedSrt.ps1 | `-Roots` | a `.en.forced.srt` beside an mkv | muxes the SRT in as a forced English text track | **verified runs**, 0 candidates |
| `repair_pgs` | Fix-BrokenPgsDuration.ps1 | `-Root -Apply` | duration > 24 h | rewrites the broken container duration | no candidates left |
| `repair_hvcc` | Fix-BadHvcc.ps1 | `-File` or `-Root`, `-Apply` | malformed hvcC box | rewrites the box | **verified runs** — 0 bad found so far |
| `dv_scan` | Find-DolbyVisionProfiles.ps1 | `-Root` | any HEVC tree | **report only** — lists DV profiles | **verified** |
| `pool_map` | Export-PoolDiskMap.ps1 | none | manual / scheduled | writes `_disk-map.txt` per folder | **verified** |

### Things worth knowing

* **ffmpeg/ffprobe are not on PATH** here — they live in the Tdarr node folder.
  nuarr passes `-Ffprobe` / `-Ffmpeg` / `-Mkvmerge` explicitly. Without that the
  scripts die with *"Could not run ffprobe"*.
* **`-Apply` matters.** `repair_pgs` and `repair_hvcc` only report unless it is
  passed. nuarr passes it.
* **`ocr_forced` decides for itself.** On anime *with* English audio it skips and
  leaves the signs to the burn pipeline. That hand-off is deliberate:
  `anime WITH English audio; signs left to burn pipeline, skip`
* **DV → HDR10 is no longer a handler.** The transcoder strips the RPU inline
  with a bitstream filter (lossless, no second pass). `dv_scan` only reports.

---

## Transcode rules, by file type

Ported from `Tdarr_Plugin_ejh_Anime_Plex_Standardize.js` and
`Tdarr_Plugin_ejh_Plex_Standardize.js`. Anime is a profile flag, not a separate
copy of the logic.

### Video

| Condition | Action |
|---|---|
| container is not MKV | remux |
| Dolby Vision profile 7 or 8 | strip RPU (NAL 62), retag `hvc1` — lossless copy |
| HEVC, 10-bit, or HDR | route to h265 (CQ 22) |
| anything else | h264 (CQ 20) |
| codec not h264/hevc/av1 | re-encode |
| HDR | copied untouched — re-encoding loses metadata and breaks DV |

**A re-encode happens for exactly two reasons: a codec Plex cannot direct-play,
or a subtitle that must be burned in.** Nothing else.

> **The space-saver is switched off.** Re-encoding a file purely because its
> bitrate was high lost ground here: 60 files came out *larger* (Goof Troop
> +33%, Batman: Caped Crusader +49%) because NVENC needs more bits than the
> original x264 encoder at the same CQ. High bitrate is not a defect. Set
> `spaceSaver.enabled = True` in `rules.py` to bring it back — the maxrate cap
> and the 5% size gate are still there if you do.

Two guards on every re-encode:
* **maxrate cap** at 1.5× source (floor 2 Mbps) — an uncapped CQ encode of an
  efficient source *grows*.
* **size gate** — output must save ≥5% or it is discarded and the original kept.

### Audio — policy differs by library

| Library | Keeps |
|---|---|
| **Anime** | **dual audio**: original language (jpn/und) **+** English |
| **Live action / Animation** | **best English only** |
| either, no English present | falls back to the original language — never left silent |
| either, only one audio track | kept as-is whatever it is |

Verified on real files:

```
ANIME  Saiyuki S01E18      eng + jpn  -> KEEP both      (dual audio)
ANIME  Vinland Saga S02E15 eng + jpn  -> KEEP both      (dual audio)
LIVE   The Blacklist S04E17 eng 5.1 + eng 2.0 -> KEEP the 5.1 only
LIVE   The Cleaning Lady S03E04 fre + eng     -> KEEP eng only
```

Then, within whatever survives:

| Condition | Action |
|---|---|
| title matches commentary | drop |
| duplicate language | keep the best only (channels → already-compatible → bitrate) |
| >6 channels | E-AC3 5.1 @ 640k |
| ≤2 channels, not AAC | AAC @ 160k |
| surround, not E-AC3 | E-AC3 @ 640k |
| AAC stereo / E-AC3 surround | copy |

### Subtitles

| Condition | Action |
|---|---|
| language not eng/und | drop |
| kept image sub flagged default/forced | **clear the flags** — otherwise Plex burns it on the CPU at playback |
| signs/forced track, anime, and already re-encoding | **burn in** (GPU, once) |
| image sub | burn even on a copy-eligible file — Plex would otherwise burn it live every play |
| HDR | never burn — it would force a re-encode |
| **untitled** default-flagged track | **never burned** — see below |
| no English audio survives | flag the best English *text* sub as default |

> **Why untitled tracks are not burned.** The plugin's guard against grabbing a
> full-dialogue track matches on the track *title*. Your EMBER/Prof releases ship
> untitled default-flagged PGS, so the guard could never fire and a full dialogue
> track would have been painted permanently into the picture. nuarr now refuses
> to burn a default-flagged track that has no title.

---

## Known gaps

1. ~~Handlers are not auto-queued.~~ **Done** — every queued file now runs the
   handlers it qualifies for, before the transcode, with a re-probe if one
   changed the file. Triggers: forced PGS → `ocr_forced`; HEVC → `repair_hvcc`;
   duration >24 h → `repair_pgs`; `.en.forced.srt` beside the file → `embed_srt`.
2. **`retitleTracks` not ported.** The plugins rewrite track titles to match the
   real codec (the "AAC track still labelled Opus" fix). Cosmetic, not done.
3. **CSV-driven scripts not wired**: `Repair-ForcedSrt.ps1` and
   `Neutralize-StragglerFlags.ps1` are batch tools driven by a prior scan's CSV,
   which does not fit the per-file job model.
4. **No broken hvcC found yet.** `repair_hvcc` runs correctly (single file and
   whole-tree), but every file checked so far reports `bad: 0`. The earlier
   "334 candidates" figure was just *every HEVC file*, not files with a known
   fault — so there may be nothing here to fix.
5. **Orphaned `.en.forced.srt` sidecars.** Several exist with no matching `.mkv`
   (Hajime no Ippo, Mushi-Shi, My Home Hero). Renames and upgrades moved the
   video out from under them, so `embed_srt` will never pick them up and that OCR
   work is stranded.
6. **`pool_disk` is stale** for ~38,800 files after a per-library rescan bug.
   The scanner is fixed; the data needs one full "Rescan all libraries".

---

## Reading a job log

```
PIPELINE: probe -> handlers -> plan -> encode -> commit -> arr refresh -> rename
  handlers this file needs: none
PLAN: remux (stream copy): clear default/forced on image sub 0
  [subtitle] clear default/forced on image sub 0
        why: Plex burns flagged image subs on the CPU at playback...
  [note] video copied untouched — re-encoding HDR would lose metadata
```

`[note]` lines record what was deliberately **not** done, which is usually the
answer to "why did this file come out like that".
