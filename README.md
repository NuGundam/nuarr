# nuarr

**Standardise a media library so Plex plays it without transcoding.**

nuarr watches a Plex library, works out which files will make Plex re-encode on
the fly, and rewrites them so it doesn't have to — while getting out of the way
of anyone actually watching.

Native Windows. Python and FastAPI, one scheduled task, a web UI on port 8770.
No Docker, no Node, no service wrapper.

Running against a 12-disk pool: **39,556 files, 59.97 TB, 2.36 TB saved.**

**[▶ Interactive presentation](https://nugundam.github.io/nuarr/presentation.html)** —
a guided tour of the whole system with animated session cards and live-styled
panels, no install needed.

![The dashboard](docs/screenshots/dashboard.png)

---

## Why it exists

A Plex transcode is usually avoidable. It happens because one thing about a
file is wrong for the client — a subtitle format that forces a burn-in, an
audio codec a TV cannot decode, a container it will not seek in — and the whole
video gets re-encoded to fix it. Standardise those few things ahead of time and
the same file direct-plays.

Doing that across 40,000 files raises a second problem, which is most of what
nuarr is: **converting a library and streaming from it compete for the same
disks.** A tool that fixes your library by making it stutter while it works has
not helped.

## What makes it different

Most of the engineering here is in *not being noticed*.

### It knows how much buffer each viewer actually has

Not "is someone watching" — how many seconds they have banked, per session,
against a floor computed for that stream: scaled by its bitrate, capped under
what that client has been observed to hold, and capped again under Plex's own
`TranscoderThrottleBuffer`. A viewer four minutes ahead is not the same as one
four seconds ahead, and nuarr treats them differently.

![The job gate](docs/screenshots/job-gate.png)

### It yields the exact spindle being read

Per-disk, not per-pool. One viewer occupies one of twelve disks; work continues
at full speed on the other eleven. On the disk being read, nuarr lowers its I/O
priority, paces its file copies, and — below the floor — suspends the encode
outright and gives the disk back.

### Everything is hysteretic

Pausing an encode is *what makes a viewer's buffer recover*, so a single
threshold oscillates: it pauses, the buffer recovers, it resumes, the buffer
drops, forever. Every decision here has separate stop and start points and a
dwell time. Measured: 36 state changes an hour became 2.

### It states its reasoning

Every hold says what is holding it, what number it is waiting on, and what will
clear it. If nuarr is slowing down, the panel tells you which viewer, on which
disk, with how much buffer, and at what speed nuarr is running as a result.

---

## What it does to files

Configurable per library and per content kind — anime, animation and live
action are not the same problem.

| | |
|---|---|
| **Video** | Re-encode only when the codec or profile forces a transcode. NVENC / QSV / AMF / CPU, probed by test-encode rather than trusted from `ffmpeg -encoders` |
| **Audio** | Keep what plays, transcode what doesn't, drop what nobody needs |
| **Subtitles** | Convert forced PGS to SRT by OCR so it stops forcing a video burn-in |
| **Language tags** | Detect mislabelled audio with Whisper and fix the tag with `mkvpropedit` — metadata only, no re-encode |
| **Containers** | Remux to a container Plex will seek in |

![Codec settings](docs/screenshots/settings-codec.png)

Nothing is deleted. Replacements go through a staged copy that is verified
before the original is moved to the Recycle Bin.

### Rules, and a check that they held

Every file is checked against the rules it was supposed to satisfy, and the
check self-heals: a finding is queued, fixed, and re-verified.

![Rule check](docs/screenshots/settings-rules.png)

---

## Requirements

- Windows 10 / 11 / Server 2019+
- Python 3.13 — the installer bundles it if missing
- ffmpeg — bundled
- MKVToolNix command-line tools (`mkvmerge`, `mkvpropedit`, `mkvextract`) —
  bundled; an existing MKVToolNix install is used instead if present
- Plex, and Sonarr / Radarr if you want nuarr to keep names in step with them
- A scratch directory on fast local storage, off the pool

A GPU is optional. nuarr probes what the machine can actually do by
test-encoding two seconds with each encoder family, because `ffmpeg -encoders`
lists what was compiled in, not what works — a build will happily advertise
QSV and AMF on a box with only an NVIDIA card.

## Install

Download the installer from [Releases](../../releases) and run it. The wizard
finds Plex, Sonarr and Radarr on the machine, tests each connection before it
continues, and lets you pick which folders are libraries.

To remove it, `Uninstall.cmd` sits next to `Setup.cmd`. It keeps your database
by default so reinstalling picks the library back up, and it never touches
media.

## From source

```
git clone https://github.com/NuGundam/nuarr
cd nuarr
pip install -r requirements.txt
copy config.example.yml config.yml    # then edit it
python launch.py
```

Then open <http://127.0.0.1:8770>.

## Configuration

`config.yml` holds the libraries, the arr connections and the Plex details.
Everything else is in the UI under Settings, and takes effect without a
restart.

**`config.yml` is gitignored.** It holds your Plex token and API keys. Do not
commit it.

---

## Status

Version 1.0.1. Built for one library and one server, and honest about it: it
has run against a 12-disk StableBit pool with Plex, Sonarr and Radarr on the
same machine, and nowhere else. Interfaces it depends on — Plex's session
fields, DrivePool's placement — are read defensively, but a setup unlike that
one will find edges.

Bug reports with the relevant lines from `Settings → Logs` are welcome.

## Licence

MIT. See [LICENSE](LICENSE).
