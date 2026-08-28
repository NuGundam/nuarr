r"""
nuarr - transcode rules, ported from the Tdarr plugins

Source:
    Tdarr_Plugin_ejh_Anime_Plex_Standardize.js   (939 lines)
    Tdarr_Plugin_ejh_Plex_Standardize.js         (659 lines)

Both share one CONFIG block; the anime plugin adds signs/songs burn-in and the
dialogue-subtitle default. That difference is a PROFILE here rather than a
second copy of the logic, so a fix lands in one place instead of two.

Everything below keeps the original comments, because they record *why* each
number is what it is - and several of them encode expensive lessons (the
1193-hour duration corruption, the arr h265 re-download loop, Plex burning PGS
on the CPU at playback).

Nothing in this module touches a file. It reads a probe and returns a PLAN:
an ordered list of actions with a human reason for each, which is what the
dashboard shows and what the job log records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- config ----
CONFIG: dict[str, Any] = {
    "container": "mkv",

    # ---- Video targets (NVENC) --------------------------------------------
    "videoTargets": {
        "h264": {"encoder": "h264_nvenc", "preset": "p5", "cq": 20},   # SDR 8-bit
        "h265": {"encoder": "hevc_nvenc", "preset": "p5", "cq": 22},   # 10-bit/HDR/HEVC
    },
    # Extra NVENC quality flags (Ampere/RTX A5000): B-frames + B-ref, adaptive
    # quantization, and rate-control lookahead. ~5-15% better compression at the
    # same CQ for minimal speed cost. Temporal AQ is H.264-only on NVENC.
    # TEMPORAL AQ IS ON BOTH PATHS NOW, not just h264. It was h264-only, which
    # looks like an oversight rather than a decision - the flag is supported on
    # HEVC from Turing on, and this box is Ampere. Measured on a 32 Mbps source:
    # 0.6% smaller, identical SSIM, and very slightly FASTER than without it.
    # A small win, but a free one.
    "nvencExtra": {
        "h264": "-bf 3 -b_ref_mode middle -spatial-aq 1 -temporal-aq 1 -rc-lookahead 20",
        "h265": "-bf 3 -b_ref_mode middle -spatial-aq 1 -temporal-aq 1 -rc-lookahead 20",
    },
    # Cap encode bitrate relative to the SOURCE bitrate. A CQ encode of a low-
    # bitrate source can balloon (1.7 Mbps source -> 4.5 Mbps out = 268%),
    # wasting space and failing the size gate so the work is discarded.
    "maxrateFactor": 1.5,
    "maxrateFloorMbps": 2,

    "routeToH265OnHevc": True,
    "routeToH265On10bit": True,
    "routeToH265OnHDR": True,

    # AV1 IS NOT TREATED AS A DESTINATION FORMAT.
    #
    # It was listed alongside h264/hevc as "a codec Plex direct-plays", so an
    # AV1 file was passed through untouched forever. That is true only for
    # clients that can decode it, and the ones that cannot fall back to a CPU
    # transcode - the exact thing this whole pipeline exists to avoid. Until
    # AV1 decode is universal across the clients on this server, it is a source
    # format to convert away from, not a target to leave alone.
    #
    # Bit depth is preserved either way: a 10-bit AV1 becomes 10-bit HEVC
    # (Main 10 / p010le), an 8-bit AV1 becomes 8-bit HEVC. See Plan.ten_bit.
    "convertAv1": True,
    # And it must go to h265 explicitly. Without this an 8-bit AV1 would fall
    # through to the h264 default - re-encoding to an OLDER codec, which is a
    # size and quality regression for no reason. 10-bit AV1 already reached
    # h265 via routeToH265On10bit; this covers the 8-bit case.
    "routeToH265OnAv1": True,

    # ---- Audio -------------------------------------------------------------
    "stereoBitrate": 160,          # kbps for AAC 2.0/mono
    "surroundBitrate": 640,        # kbps for E-AC3 5.1
    "surroundMaxChannels": 6,      # downmix anything above this to 6 (5.1)
    "copyStereoIfCodec": ["aac"],
    "copySurroundIfCodec": ["eac3"],
    # E-AC3 above this is re-encoded down to surroundBitrate even though the
    # CODEC is already the right one. Being eac3 is not sufficient: Plex's EAE
    # decoder rejects high-bitrate E-AC3 outright - "Cannot group in blocks of
    # 6!" looping thousands of times a second - because frames at high rates
    # carry fewer than the 6 audio blocks EAE insists on. Verified on this
    # library, five for five:
    #
    #     1536k  Hatsune Miku            web transcode failed to load
    #     1536k  Constantine (WhiteRhino) web transcode stuttered
    #     1536k  Kizumonogatari Part 2   EAE error flood, session confirmed
    #      640k  Constantine (EDGE2020)  plays
    #      640k  nuarr-made tracks       play (Blindspot batch, tested)
    #
    # ffmpeg's decoder accepts all of them, which is why every local check
    # passed while Plex spun. 2,712 tracks in this library sit above the
    # threshold. 960 rather than 641 so 768k tracks - which have not been seen
    # to fail - are left alone until proven guilty.
    "eac3MaxBitrateK": 960,
    # Codecs that carry no generation loss. They report no bit_rate to ffprobe
    # (variable by nature), which is exactly why they need naming rather than
    # measuring - see the rank tuple in decide().
    #
    # NOT plain "dts": that is lossy, and only the DTS-HD MA *profile* is
    # lossless. ffprobe reports both as codec_name 'dts', so the profile check
    # in decide() is what separates them - listing the codec here would have
    # promoted every lossy DTS track while fixing TrueHD.
    "losslessCodecs": ["truehd", "mlp", "flac", "alac", "pcm_s16le",
                       "pcm_s24le", "pcm_s32le", "pcm_bluray", "pcm_dvd"],
    # AUDIO POLICY differs by library - it did not before, which meant every
    # live-action and animated show kept its Japanese/Chinese/Korean tracks.
    #
    #   anime      : DUAL AUDIO - original language + English.
    #                If only one track exists, keep it whatever it is.
    #   everything : best ENGLISH only. If there is no English at all,
    #   else         fall back to the original language rather than stripping
    #                the file silent.
    "animeAudioLangs": ["jpn", "ja", "und", "eng", "en"],
    "englishLangs": ["eng", "en"],
    # legacy/global list, still used as the anime fallback
    "keepAudioLangs": ["und", "jpn", "ja", "zho", "chi", "zh", "kor", "ko", "eng", "en"],
    "commentaryPattern": re.compile(r"comment|commentary", re.I),
    # Keep only the BEST audio track per language. Best = most channels, then
    # already-compatible (copied, no re-encode), then bitrate.
    "dedupeAudioPerLang": True,

    # ---- Subtitles ---------------------------------------------------------
    "keepSubLangs": ["eng", "en"],
    # Many releases (esp. anime) leave PGS/ASS tracks untagged; for an
    # English-subbed show those ARE the English tracks.
    "keepUndeterminedSubs": True,
    "burnEnabled": True,
    "removeBurnedSub": True,
    # WAS False, ON A PREMISE THAT WAS NEVER TRUE. The reasoning was
    # "burning forces a re-encode and re-encoding destroys HDR" - but an
    # encode only loses HDR when nobody tells it not to. nuarr now states the
    # colour tags explicitly and re-emits the mastering-display and
    # content-light metadata (encoders.hdr_args), so an HDR burn comes out
    # tagged PQ/BT.2020 with its HDR10 blocks intact.
    #
    # What that false premise COST: on an HDR file nothing was ever burned,
    # so the signs track stayed pictures - and because subocr skipped signs
    # on the grounds that "the encoder will burn them", no text version was
    # made either. Plex then painted the pictures on the CPU at playback,
    # which is a full 4K HDR transcode: the single most expensive outcome
    # this system exists to prevent, reached by trying to protect HDR.
    #
    # Dolby Vision is the real exception and is handled separately: its RPU
    # cannot survive any re-encode, so a DV file burns as its HDR10 base
    # layer - the same thing the strip-DV rule produces on purpose.
    "burnOnHDR": True,
    # Burning redraws pixels -> forces a full re-encode. With this ON, signs are
    # burned only when the video is being re-encoded anyway.
    "burnOnlyWhenEncoding": True,
    # Image subs flagged default/forced make Plex burn them at playback (= a
    # transcode). Strip those flags so Plex direct-plays; track stays selectable.
    "neutralizeKeptImageSubFlags": True,
    # An image sub whose job is already done by a TEXT sub is dead weight: it
    # cannot be searched, styled or restyled, it is the reason Plex burns on the
    # CPU, and it is the reason the OCR queue exists. Releases that merge two
    # sources ship both - a USABD PGS pair beside an Astral ASS pair, one
    # dialogue and one signs each - so the text version is right there and
    # somebody already did the transcription by hand, better than Tesseract
    # will. Drop the redundant image track instead of OCR'ing a worse copy of a
    # subtitle the file already contains. Matched per language AND per role, so
    # a dialogue PGS is never dropped because a SIGNS text track exists.
    "dropRedundantImageSubs": True,

    # COMMENTARY SUBTITLES GO THE SAME WAY THE COMMENTARY AUDIO ALREADY DOES.
    #
    # The audio rule has always dropped these - "director commentary is not
    # something you sit down to watch, and it makes the file bigger" - but the
    # subtitle side kept them, so a disc that shipped a commentary track in both
    # forms lost the audio and kept the transcription of it. The Empire Strikes
    # Back carries two: 'Commentary by director George Lucas...' and 'Commentary
    # by cast and crew (SDH)', 4,162 and 3,740 display sets of somebody talking
    # over a film nobody is watching that way.
    #
    # Matched on the TITLE, using the same pattern as the audio rule so the two
    # can never disagree about what commentary is.
    "dropCommentarySubs": True,
    # With English audio, only a FORCED track (signs, songs, foreign lines
    # inside English dialogue) shows itself. Full dialogue and SDH stay in the
    # file and stay selectable - SDH is the one you want in a noisy room - but
    # neither switches itself on. Turn this off to leave release flags alone.
    "autoShowForcedOnly": True,
    # Text subtitles are written ahead of picture subtitles. Plex offers tracks
    # in file order and takes the earlier one when nothing else separates them,
    # so a PGS ahead of the SRT gets picked - and a picked picture track is a
    # CPU burn on every play. Costs a stream-copy remux on the 30 files in the
    # library that are in the wrong order, and nothing at all on the rest.
    "orderSubsTextFirst": True,
    # Plex burns PGS on the CPU at playback -> heavy buffering at 4K. Burn it
    # here on the GPU once instead.
    "alwaysBurnImageSubs": True,
    # Only burn a (likely) ENGLISH sub. Prevents burning a German/French forced
    # track from a multi-language release.
    "burnLangGuard": True,
    "scalePgsToVideo": True,
    # Japanese-only anime: set the best English TEXT dialogue sub as default so
    # Plex shows it. Never an image sub (Plex CPU-burn), never a signs track.
    "forceEngSubWhenNoEngAudio": True,

    # ---- Dolby Vision -> HDR10 --------------------------------------------
    # Losslessly remove the DV RPU (HEVC NAL 62) via -c copy for Profile 7/8 so
    # it direct-plays as HDR10. Profile 5 is left untouched.
    "stripDV": True,
    "stripDVProfiles": [7, 8],
    "dvRemoveNalTypes": "62",
    "dvRetagHvc1": True,
    # OFF, AND IT HAS TO BE OFF - this was a permanent rework loop.
    #
    # The idea was a safety net: if ffprobe failed to report DV side data, fall
    # back to believing "[DV HDR10]" in the filename. Harmless while stripping
    # DV was a no-op that never changed what ffprobe saw.
    #
    # It stopped being harmless the moment the strip started working. The file
    # loses its DV, ffprobe correctly reports none - and the FILENAME still says
    # DV, because Sonarr and Radarr name from the media info they recorded at
    # import and have no idea anything changed. So the plan claims profile 8 on
    # a file with no Dolby Vision anywhere in it, queues a full remux to remove
    # something that is not there, commits an identical file, and the name still
    # says DV. Forever. 12 of the 269 repaired files had already been queued for
    # a second pointless pass - 343 GB - when this was caught.
    #
    # The net is also unnecessary here: this ffmpeg (7.1.4-Jellyfin) reports
    # dv_profile for both Profile 7 and Profile 8, verified on real files in
    # this library. The probe is the evidence; the filename is a rumour, and it
    # goes stale the moment we act on it.
    "detectDVFromName": False,

    # ---- Space-saver -------------------------------------------------------
    # Re-encode OVERSIZED video. Triggers ONLY when overall bitrate is above the
    # ceiling for the resolution.
    #
    # THIS WAS DISABLED, AND THE CEILINGS ARE WHY. The original note recorded
    # that 60 files came out LARGER (Goof Troop +33%, Batman +49%) - true, and
    # re-measured three separate ways since. But the old 1080 ceiling was 10
    # Mbps, and 10 Mbps is well below the point where re-encoding pays. A
    # break-even sweep on this library, SDR, 60 s samples, h264 -> h264 with the
    # settings this box actually uses:
    #
    #     12.2 Mbps  +34%      <- the old ceiling was letting THESE in
    #     14.0 Mbps  -17%   SSIM 0.959
    #     20.2 Mbps   -5%   SSIM 0.920   <- visible loss for nothing
    #     22.5 Mbps  -39%   SSIM 0.964
    #     33.3 Mbps  -53%   SSIM 0.971
    #
    # So the mechanism was never wrong; it was aimed at files with no fat on
    # them. 22 Mbps is the first point where the saving is large AND the quality
    # cost is back under control.
    #
    # Tiers with no measurement of their own are set deliberately high. There is
    # no SDR 4K h264 data here at all, so 2160 is parked well out of the way
    # rather than guessed at.
    # OFF AGAIN, PENDING A BETTER APPROACH. Everything below is kept because it
    # is measured, not guessed - turning this back on is a one-word change, and
    # the numbers that justify the ceilings are recorded above so nobody has to
    # rediscover them.
    #
    # Why it went back off: with animation excluded and the ceiling at a level
    # that does not wreck the picture, only 17 files in Movies and TV Shows
    # qualify. That is not a library-scale saving, and it is not worth carrying
    # a destructive rule for. The open question is a gentler method - a bitrate
    # target rather than a quality target, or a CPU x265 pass - which is worth
    # measuring properly before anything is re-enabled.
    "spaceSaver": {
        "enabled": False,
        # Keep SOURCE codec on shrink (x264 stays x264) -> avoids arr h265
        # re-download loops. Measured cost of this choice: h264 -> h264 saves
        # roughly 20 points less than h264 -> hevc and holds a lower SSIM. It is
        # kept anyway because a codec change on 500 files is a bigger risk to
        # the arrs than the extra space is worth.
        "forceH265": False,
        # WARNING: re-encoding HDR loses metadata & breaks DV. Keep false.
        # This only stops the SPACE SAVER choosing HDR files for being large -
        # a burn-in still re-encodes HDR normally, which is a different path.
        "includeHDR": False,
        "triggerFactor": 1.0,
        "maxMbps": {"2160": 40, "1080": 22, "720": 12, "sd": 6},
        # DRAWN ANIMATION IS EXCLUDED, on the evidence of watching the output.
        #
        # The 5-file pilot: the live-action episodes were fine, the anime was
        # not - dark scenes fell apart once anything moved. That is the known
        # weak spot of a quality-targeted encode on this content. Cel animation
        # is large flat areas and smooth gradients, which a CQ encoder reads as
        # "cheap" and hands very few bits; in a dark scene those few bits become
        # visible banding, and motion turns the banding into crawling blocks.
        #
        # Every anime data point collected here says the same thing:
        #   Oshi no Ko    -73% - the hardest compression of the five, and the
        #                        one that looked worst
        #   Mardock       SSIM 0.920 - worst in the break-even sweep
        #   Turn A Gundam GREW, repeatedly
        #   Aristocats    SSIM 0.959 for only -17%
        #
        # Live action absorbs this because film grain and detail keep the
        # encoder honest. Animation has nothing to hide behind.
        "skipKinds": ["anime", "animation"],
    },

    # ---- Safety: prevent broken-duration output ----------------------------
    # Caps output at real runtime + buffer so a runaway far-future frame (the
    # "1193h duration" corruption seen on PGS-burn re-encodes) is trimmed
    # instead of poisoning the file.
    "trimToRealDuration": True,
    "trimBufferSec": 120,

    # ---- Track titles ------------------------------------------------------
    "retitleTracks": True,
    "retitleIncludeLanguage": True,
    "retitleIncludeVideoResolution": True,
    "retitleTriggerRemux": True,
}

# Burn target is the first subtitle matching an enabled variant, by priority.
SIGNS_VARIANTS = [
    {"name": "Signs & Songs", "enabled": True,
     "patterns": [re.compile(r"sign", re.I), re.compile(r"song", re.I),
                  re.compile(r"s\s*&\s*s", re.I), re.compile(r"s\s*/\s*s", re.I)],
     "flags": []},
    {"name": "Forced", "enabled": True,
     "patterns": [re.compile(r"force", re.I)], "flags": ["forced"]},
    # Guarded so it won't grab a full-dialogue track that happens to be default.
    {"name": "Default flag (non-dialogue)", "enabled": True,
     "patterns": [], "flags": ["default"],
     "exclude": [re.compile(r"full|dialog|dialogue|complete", re.I)]},
]

# Titles that mean "this track is signs/songs, not dialogue". Deliberately the
# same vocabulary subocr.SIGNS_RE uses - the two modules have to agree on what a
# signs track is or one will drop what the other was about to burn.
SIGNS_TITLE_RE = re.compile(
    r"sign|song|s\s*&\s*s|s\s*/\s*s|typeset|caption|forced|foreign|\btitles\b", re.I)

# Tracks this system created by OCR'ing an image sub - see subocr.embed(),
# which names them "<label> (OCR)".
OCR_MADE_RE = re.compile(r"\(OCR\)", re.I)

def _ch_name(n: int) -> str:
    """Speaker layouts by the name people use for them.

    The channel ceiling is configurable now, so "down to 5.1" can no longer be
    written into the sentence - at a ceiling of 2 it would be a lie, and the
    plan lines are the thing a human reads to decide whether to trust the file.
    """
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(int(n),
                                                            f"{int(n)} channels")


IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "vobsub"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt"}
ENGLISH_ISH = {"eng", "en", "und", "", None}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


# ------------------------------------------------------------------ plan ----
@dataclass
class Action:
    kind: str          # video | audio | subtitle | container | metadata
    what: str
    why: str
    detail: str = ""


@dataclass
class Plan:
    needed: bool = False
    encode: bool = False           # True = NVENC re-encode, False = stream copy
    target: str | None = None      # h264 | h265
    reasons: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    burn_index: int | None = None
    keep_audio: list[int] = field(default_factory=list)
    keep_subs: list[int] = field(default_factory=list)
    # Codec name of EVERY subtitle stream, in subtitle order, so the executor
    # knows which ones the output container can actually hold. Carried on the
    # plan rather than re-probed at build time because the plan is what gets
    # persisted and replayed.
    sub_codecs: list[str] = field(default_factory=list)
    skip_reason: str | None = None
    # Per-track audio decisions, so the executor can DO what the plan says.
    # {"idx": 0, "to": "copy"|"eac3"|"aac", "br": 640, "ch": 6}
    audio_ops: list[dict] = field(default_factory=list)
    strip_dv: bool = False
    # Observations that explain the plan but are NOT reasons to touch the file.
    notes: list[str] = field(default_factory=list)
    # Source overall bitrate, so the encoder can cap against it.
    source_mbps: float = 0.0
    burn_image: bool = False        # burn target is PGS/VOBSUB, not text
    default_sub: int | None = None  # source sub index to flag as default
    clear_flags: list[int] = field(default_factory=list)  # image subs to neutralise
    # This encode is for COMPATIBILITY, not for size - so it is allowed to grow.
    #
    # The size gate discards any re-encode that does not save at least 5%, which
    # is right for a shrink job and wrong for this one. Converting AV1 to HEVC
    # makes the file BIGGER by design: AV1 is the more efficient codec, so the
    # same picture costs more bits in HEVC. Measured across 55 of these:
    # 0 shrank at all, median 101%, max 140% - every single one discarded, the
    # GPU time thrown away, and the file left in the codec we were trying to
    # get rid of.
    #
    # Space was never the point. The point is that not every client decodes
    # AV1, and the ones that cannot make Plex transcode on the CPU. A 35%
    # larger file that direct-plays is the trade being made deliberately.
    grow_ok: bool = False
    # Source is 10-bit, so the encode must STAY 10-bit.
    #
    # This was computed in decide() and used only to route to h265, then thrown
    # away - the executor never learned of it. With no -pix_fmt and no
    # -profile:v on the command, output depth was left to ffmpeg's format
    # negotiation, and the subtitle burn-in path negotiates down: overlay
    # defaults to 8-bit, so a 10-bit source came out yuv420p / Main. Anime is
    # exactly where that shows, as banding in gradients and dark scenes.
    ten_bit: bool = False
    # SOURCE audio index -> ISO language to write onto it.
    #
    # Only ever fills in a BLANK. The executor writes these as container
    # metadata during the remux it was doing anyway, so naming a language
    # costs nothing on a file that had other work; on a file that had none it
    # is the only reason to touch it, and still a stream copy.
    audio_lang_tags: dict = field(default_factory=dict)
    # THE ENCODER SETTINGS THIS PLAN WAS DECIDED WITH, carried rather than
    # looked up again.
    #
    # They are per library now (see codecpolicy), and build_ffmpeg has only a
    # source path and a plan - it does not know which library the file came
    # from, and giving it the job of working that out again would be a second
    # place the policy is resolved. Two lookups is one too many: a plan decided
    # under CQ 20 and executed under CQ 22 is a file that does not match what
    # the queue said it would be, and the difference would be invisible.
    #
    # Stamped at decide() time, persisted with the plan, replayed at dispatch.
    # Empty means "plan stored before this existed" and build_ffmpeg falls back
    # to CONFIG, so old queued jobs still run.
    venc: dict = field(default_factory=dict)
    # Size of the source file when this plan was decided. The plan is persisted
    # at enqueue and replayed at dispatch, and in between the arrs are free to
    # upgrade the file - same path, different bytes. A plan whose track indices
    # and burn target describe a file that no longer exists must not run;
    # dispatch compares this against the file on disk and re-decides on
    # mismatch. Zero means "unknown" (plans stored before the field existed)
    # and disables the check rather than failing it.
    src_size: int = 0
    # Index of the subtitle that should carry forced+default: the one track
    # allowed to appear without being asked for. See autoShowForcedOnly.
    forced_sub: int | None = None
    # SOURCE disposition flags of every subtitle stream, in subtitle order,
    # as ffmpeg -disposition strings ("original+hearing_impaired", "0"...).
    # Carried so the executor can state every kept sub's flags EXPLICITLY:
    # the Jellyfin ffmpeg build was verified inventing default=1 on the first
    # subtitle of a remux whatever -default_mode said, which meant every
    # rewrite could plant the exact violation (a sub auto-showing over
    # English audio) that a second full pass then had to clear. Explicit
    # per-stream flags override the muxer and make one pass enough.
    sub_disps: list = field(default_factory=list)

    def add(self, kind: str, what: str, why: str, detail: str = "") -> None:
        self.actions.append(Action(kind, what, why, detail))
        self.needed = True

    def to_dict(self) -> dict:
        return {
            "needed": self.needed, "encode": self.encode, "target": self.target,
            "burn_index": self.burn_index, "keep_audio": self.keep_audio,
            "keep_subs": self.keep_subs, "sub_codecs": self.sub_codecs,
            "skip_reason": self.skip_reason,
            "audio_ops": self.audio_ops, "strip_dv": self.strip_dv,
            "notes": self.notes, "source_mbps": self.source_mbps,
            "burn_image": self.burn_image, "default_sub": self.default_sub,
            "clear_flags": self.clear_flags, "ten_bit": self.ten_bit,
            "grow_ok": self.grow_ok, "src_size": self.src_size,
            "forced_sub": self.forced_sub, "sub_disps": self.sub_disps,
            "venc": self.venc, "audio_lang_tags": self.audio_lang_tags,
            "actions": [{"kind": a.kind, "what": a.what, "why": a.why,
                         "detail": a.detail} for a in self.actions],
        }

    def summary(self) -> str:
        # One line, read by someone who has not been staring at ffmpeg all day.
        # "remux (stream copy): track 0: eac3 1536k -> E-AC3 640k" said what a
        # muxer does; this says what happens to the film.
        if self.skip_reason:
            return f"nothing done — {self.skip_reason}"
        if not self.needed:
            return "already set up correctly"
        mode = ("Rebuilding the picture" if self.encode
                else "Keeping the picture as-is")
        return f"{mode} · " + "; ".join(a.what for a in self.actions[:4])


def plan_from_dict(d: dict) -> Plan:
    """Rebuild a plan stored in the database.

    The plan is decided at ENQUEUE time and persisted, so a restart resumes with
    the same decision it showed you - rather than re-deciding later against a
    file that may have changed in between.
    """
    p = Plan(needed=d.get("needed", False), encode=d.get("encode", False),
             target=d.get("target"), burn_index=d.get("burn_index"),
             keep_audio=d.get("keep_audio") or [],
             keep_subs=d.get("keep_subs") or [],
        sub_codecs=d.get("sub_codecs") or [],
             skip_reason=d.get("skip_reason"),
             audio_ops=d.get("audio_ops") or [],
             strip_dv=bool(d.get("strip_dv")),
             notes=d.get("notes") or [],
             source_mbps=float(d.get("source_mbps") or 0),
             burn_image=bool(d.get("burn_image")),
             default_sub=d.get("default_sub"),
             clear_flags=d.get("clear_flags") or [],
             ten_bit=bool(d.get("ten_bit")),
             grow_ok=bool(d.get("grow_ok")),
             src_size=int(d.get("src_size") or 0),
             forced_sub=d.get("forced_sub"),
             # plans stored before this field existed load as [] and the
             # executor falls back to the old behaviour for them
             sub_disps=d.get("sub_disps") or [],
             # Likewise: {} means "decided before codec settings were per
             # library", and build_ffmpeg falls back to the global constants.
             venc=d.get("venc") or {},
             # JSON turns dict keys into strings; the executor indexes by int.
             audio_lang_tags={int(k): v for k, v
                              in (d.get("audio_lang_tags") or {}).items()})
    p.actions = [Action(a.get("kind", ""), a.get("what", ""), a.get("why", ""),
                        a.get("detail", "")) for a in d.get("actions") or []]
    return p


# ------------------------------------------------------------- helpers -----
def _res_tier(height: int) -> str:
    if height >= 1700:
        return "2160"
    if height >= 900:
        return "1080"
    if height >= 650:
        return "720"
    return "sd"


def _is_hdr(v: dict) -> bool:
    """HDR is the transfer function (PQ / HLG), not the colour gamut.

    This used to also count color_primaries == "bt2020" as HDR, which is a
    routine container tag on 10-bit SDR anime (transfer bt2020-10) - and it
    was the ONLY thing blocking signs&songs burns on those files: the veto
    said "would destroy HDR" about video with no HDR in it. Mastering-display
    side data stays as a second signal for true HDR10 whose transfer tag a
    bad mux stripped.
    """
    if v.get("color_transfer") in HDR_TRANSFERS:
        return True
    return any("mastering" in str(sd.get("side_data_type", "")).lower()
               for sd in v.get("side_data_list") or [])


def _is_10bit(v: dict) -> bool:
    return "10" in (v.get("pix_fmt") or "") or (v.get("bits_per_raw_sample") == "10")


def _dv_profile(v: dict, filename: str = "") -> int | None:
    for sd in v.get("side_data_list") or []:
        if "dv_profile" in sd:
            return int(sd["dv_profile"])
    if CONFIG["detectDVFromName"] and re.search(r"\bDV\b|dolby.?vision", filename, re.I):
        return 8         # assume the common profile when only the name says so
    return None


def _disp(s: dict, flag: str) -> bool:
    return bool((s.get("disposition") or {}).get(flag))


# Title words that name a language, for tracks whose LANGUAGE TAG is empty.
#
# Fansub and dual-audio muxes routinely ship audio with no language tag at all
# and the language written in the track TITLE instead - "Eng", "Jap",
# "Japanese AAC 2.0". Grouping those by tag alone put both tracks under 'und',
# and dedupeAudioPerLang then treated a dual-audio pair as one language with a
# spare: caught live on Dragon Ball DAIMA S01E06, whose plan read "remove the
# spare und audio 1" - audio 1 being the Japanese track of a dual-audio anime,
# the exact track the whole dual-audio policy exists to keep.
# Spellings the ISO table does not carry: informal codes, native names, and
# regional words that name a language in practice. These OVERRIDE the table.
_TITLE_LANG_EXTRA = {
    "jap": "jpn", "espanol": "spa", "latino": "spa", "castilian": "spa",
    "francais": "fre", "deutsch": "ger", "italiano": "ita",
    "brazilian": "por", "portugues": "por",
    "mandarin": "chi", "cantonese": "chi",
    "nihongo": "jpn",
}
# ISO 639-2 codes that are also everyday English words. A track titled
# "May contain spoilers" is not Malay audio and "Director's run-through" is
# not Rundi; these codes only ever count when the tag is what carries them.
_TITLE_LANG_SKIP = {
    "may", "her", "run", "war", "art", "ton", "ave", "div", "mis", "son",
    "sun", "man", "den", "day", "sad", "bad", "fan", "fat", "per", "him",
    "car", "was", "ace", "got", "mad", "map", "pro", "rap", "sam", "ter",
    "tem", "zap", "and", "new", "mus", "kin", "twi",
}
_TITLE_LANG: dict | None = None


def _title_lang_map() -> dict:
    """word -> ISO 639-2 code, for every language the settings page knows.

    Built once from the same _iso639.json the language picker uses, so the
    guesser and the policy UI can never disagree about what exists: every
    single-word English name ("Japanese", "Wolof", "Thai") and every 3-letter
    code, minus the codes that read as ordinary English words, plus the
    informal spellings above.
    """
    global _TITLE_LANG
    if _TITLE_LANG is None:
        m: dict = {}
        try:
            from . import langpolicy
            for e in langpolicy.iso_languages():
                c = str(e.get("c") or "").lower()
                if not c or c == "und":
                    continue
                if c not in _TITLE_LANG_SKIP:
                    m[c] = c
                n = str(e.get("n") or "").strip().lower()
                # single plain words only: "Modern Greek (1453-)" must not
                # teach the guesser that 'modern' is a language
                if n and n.isalpha() and len(n) >= 4:
                    m.setdefault(n, c)
        except Exception:
            pass
        m.update(_TITLE_LANG_EXTRA)
        _TITLE_LANG = m
    return _TITLE_LANG


def _lang_guess(s: dict) -> str:
    """The tag when it exists; the language NAMED IN THE TITLE when it does
    not. Returns '' only when neither says anything.

    Word rules, tuned against false hits rather than for recall: a full
    language name matches anywhere in the title ("English AAC 2.0",
    "Mandarin, Taiwan"), but a bare 3-letter code only matches as the FIRST
    word ("Eng AAC") - deep in a sentence a three-letter match is far more
    likely to be an ordinary word than a language code.
    """
    lg = _lang(s)
    if lg and lg != "und":
        return lg
    m = _title_lang_map()
    for idx, w in enumerate(re.findall(r"[a-z]+", _title(s).lower())):
        if w in m and (len(w) >= 4 or idx == 0):
            return m[w]
    return ""


def _lang(s: dict) -> str:
    return ((s.get("tags") or {}).get("language") or "").lower()


def _title(s: dict) -> str:
    return (s.get("tags") or {}).get("title") or ""


def _short(t: str, n: int = 80) -> str:
    """A track title fit to sit inside a sentence.

    Plain truncation cut mid-bracket - "Full Subtitles [Kaleido-" - which reads
    like the name is broken rather than shortened. Drop the release-group
    bracket first, since it is the least useful part, and only then trim.

    THE LIMIT WAS 26, AND IT WAS CUTTING REAL INFORMATION.
    #
    # These strings end up on a card that is the full width of the page, with
    # room for a whole sentence and then some - and they were being clipped to
    # a width chosen for nothing in particular. "Japanese 2.0 Opus (Do…)" hides
    # the very thing that distinguishes it from the other Japanese 2.0 Opus
    # track the same line is deciding between, which is the one question the
    # reader has.
    #
    # 80 is long enough for every audio and subtitle title in this library and
    # short enough that a pathological one - some releases put an entire
    # sentence in the title field - cannot push the layout around. The bracket
    # strip above still removes the release group first, so the characters that
    # do get spent are the ones worth reading.
    """
    t = re.sub(r"\s*[\[(][^\])]*[\])]?\s*$", "", (t or "").strip()) or (t or "")
    t = t.strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def pick_burn_target(subs: list[dict]) -> tuple[int | None, str]:
    """First subtitle matching an enabled signs/forced variant, by priority."""
    for variant in SIGNS_VARIANTS:
        if not variant["enabled"]:
            continue
        for i, s in enumerate(subs):
            title, lang = _title(s), _lang(s)
            if any(rx.search(title or "") for rx in variant.get("exclude", [])):
                continue
            hit = any(rx.search(title) or rx.search(lang)
                      for rx in variant["patterns"])
            hit = hit or any(_disp(s, f) for f in variant["flags"])
            if not hit:
                continue
            # An UNTITLED default-flagged track is ambiguous: the exclude list
            # ("full", "dialogue"...) matches on the title, so with no title the
            # guard cannot fire. On this library those are the FULL DIALOGUE
            # PGS tracks - burning one would paint every line of dialogue
            # permanently into the picture. Only trust the default flag when
            # there is a title to judge.
            if variant.get("flags") == ["default"] and not title.strip():
                continue
            if CONFIG["burnLangGuard"]:
                ok = (lang in ENGLISH_ISH or lang in ("jpn", "ja")
                      or re.search(r"english", title, re.I))
                if not ok:
                    continue
            return i, variant["name"]
    return None, ""


def describe() -> dict:
    r"""The live rules, as data, for the dashboard.

    GENERATED FROM CONFIG, never hand-written. A rules page typed out by hand
    is wrong the first time anyone edits a number and nobody notices - and this
    is exactly the page someone would trust when deciding why a file was
    treated the way it was. Every value below is read from CONFIG at call time,
    so the page cannot disagree with the engine.

    Rows carry `anime` and `other` where the two profiles differ, and a single
    `both` where they do not. The anime/live-action split is smaller than it
    looks: it is entirely audio language selection and subtitle handling. Video
    is identical for both.
    """
    C = CONFIG
    t = C["videoTargets"]
    burn_names = [v["name"] for v in SIGNS_VARIANTS if v.get("enabled")]

    def yn(v):
        return "yes" if v else "no"

    return {
        "profiles": {
            "anime": r"any path under a folder starting with 'Anime' "
                     r"(P:\Anime Shows, P:\Anime Movies)",
            "other": "everything else - live action and western animation",
        },
        "pool": [
            {"k": "passthrough (stream copy)", "both":
                "no re-encode needed: the video already direct-plays and no "
                "subtitle has to be burned in. Container, tracks and flags are "
                "rewritten; the video bitstream is copied untouched."},
            {"k": "encode (NVENC)", "both":
                "the video cannot be left as it is - an unsupported codec, or a "
                "subtitle that must be painted into the picture."},
            {"k": "skipped at enqueue", "both":
                "the plan came back with nothing to do, so no job is created at "
                "all rather than queueing a no-op."},
        ],
        "video": [
            {"k": "re-encode when", "both":
                "codec is not " + ", ".join(["h264", "hevc"]
                                            + ([] if C["convertAv1"] else ["av1"]))
                + (" (AV1 included - not every client decodes it)"
                   if C["convertAv1"] else "")
                + ", or a subtitle is being burned in"},
            {"k": "target codec", "both":
                f"h265 when the source is HEVC"
                + (", 10-bit" if C["routeToH265On10bit"] else "")
                + (", HDR" if C["routeToH265OnHDR"] else "")
                + (" or AV1" if C["routeToH265OnAv1"] else "")
                + "; otherwise h264"},
            {"k": "encoder", "both":
                f"{t['h265']['encoder']} cq {t['h265']['cq']} preset "
                f"{t['h265']['preset']}  /  {t['h264']['encoder']} "
                f"cq {t['h264']['cq']} preset {t['h264']['preset']}"},
            {"k": "bit depth", "both":
                "preserved. A 10-bit source stays 10-bit (Main 10 / p010le), "
                "including through a subtitle burn-in"},
            {"k": "bitrate cap", "both":
                f"{C['maxrateFactor']}x the source bitrate, floor "
                f"{C['maxrateFloorMbps']} Mbps - stops a CQ encode ballooning "
                f"past the original"},
            {"k": "shrink by bitrate", "both":
                "DISABLED. Re-encoding purely because a file is large came out "
                "bigger on 60 measured files - NVENC needs more bits than x264 "
                "at the same CQ. Bitrate alone is not a defect."
                if not C["spaceSaver"]["enabled"] else
                f"enabled above {C['spaceSaver']['maxMbps']} Mbps by tier"},
            {"k": "Dolby Vision", "both":
                (f"profiles {C['stripDVProfiles']} have the RPU removed "
                 f"losslessly (NAL {C['dvRemoveNalTypes']}, stream copy) so it "
                 f"direct-plays as HDR10. Profile 5 is left alone."
                 if C["stripDV"] else "left untouched")},
            {"k": "HDR", "both":
                "never re-encoded - it would lose the metadata and break DV"},
        ],
        "audio": [
            {"k": "which languages are kept",
             "anime": "DUAL AUDIO - original + English ("
                      + ", ".join(C["animeAudioLangs"]) + ")",
             "other": "best ENGLISH only (" + ", ".join(C["englishLangs"])
                      + "); with no English at all, the ORIGINAL language is "
                        "kept and other dubs are dropped — and if the original "
                        "is unknown, every track is kept rather than guessing"},
            {"k": "single-track files", "both":
                "kept as-is whatever the language - never made silent"},
            {"k": "audio with no language tag", "both":
                ("nuarr LISTENS to the track and writes down what it hears. "
                 "Three 30-second windows are taken from the middle of the "
                 "file - never the start, where a silent cold open or a "
                 "production sting would identify as whatever the model "
                 "expects - and all three have to agree before the answer is "
                 "used. Disagreement is reported as unknown rather than "
                 "settled by majority vote. Why bother: an empty tag is not "
                 "neutral, because Sonarr, Radarr and most players read it as "
                 "English, so a Japanese track presents itself as English and "
                 "the language policy can never act on it. Metadata could not "
                 "answer this - a RAW release has no subtitles to reason from, "
                 "and English dubs ship with English subtitles, so every "
                 "inference rule broke on one case or the other. A tag that "
                 "already states a language is NEVER overwritten. Metadata "
                 "only: the audio is stream-copied, so it rides along with "
                 "whatever remux the file was already getting. Switched per "
                 "library on the Audio codec tab")},
            {"k": "duplicates per language", "both":
                ("only the best track per language is kept: most channels, then "
                 "already-compatible, then bitrate"
                 if C["dedupeAudioPerLang"] else "all kept")},
            {"k": "commentary", "both": "dropped - dead weight for direct play"},
            {"k": "stereo", "both":
                f"copied when already {'/'.join(C['copyStereoIfCodec'])}, "
                f"otherwise AAC at {C['stereoBitrate']} kbps"},
            {"k": "surround", "both":
                f"copied when already {'/'.join(C['copySurroundIfCodec'])}, "
                f"otherwise E-AC3 at {C['surroundBitrate']} kbps"},
            {"k": "high-bitrate E-AC3", "both":
                f"re-encoded down to {C['surroundBitrate']} kbps when above "
                f"{C['eac3MaxBitrateK']} kbps, even though the codec is "
                f"already right - Plex's EAE decoder rejects high-rate E-AC3 "
                f"frames outright and loops instead of playing. Verified on "
                f"this library: every 1536k track failed the web player, "
                f"every 640k track played"},
            {"k": "more than 5.1", "both":
                f"downmixed to {C['surroundMaxChannels']} channels so Plex "
                f"direct-plays"},
        ],
        "subs": [
            {"k": "languages kept", "both":
                ", ".join(C["keepSubLangs"])
                + (" plus untagged tracks - many releases leave the English "
                   "PGS/ASS untagged" if C["keepUndeterminedSubs"] else "")},
            {"k": "burn into the picture",
             "anime": ("signs & songs are burned in: " + ", ".join(burn_names)
                       + ". This is the anime-specific behaviour and it forces "
                         "a re-encode, so it only happens when "
                       + ("the video is being re-encoded anyway"
                          if C["burnOnlyWhenEncoding"] else "needed")
                       if C["burnEnabled"] else "disabled"),
             "other": ("same burn rules apply, but a live-action release "
                       "rarely carries a signs/songs track, so in practice "
                       "nothing is burned"
                       if C["burnEnabled"] else "disabled")},
            {"k": "image subs (PGS/VOBSUB)", "both":
                ("always burned when re-encoding - Plex burns PGS on the CPU at "
                 "playback, which is a transcode at 4K"
                 if C["alwaysBurnImageSubs"] else "kept as tracks")},
            {"k": "image subs a text track already covers", "both":
                ("dropped - if the release ships its own ASS/SRT for the same "
                 "language and the same job (dialogue, signs, SDH or "
                 "sing-along), the picture-based copy is redundant and only "
                 "costs an OCR pass to read back. Matched per ROLE, so an SDH "
                 "image sub is never dropped for a plain dialogue text track. "
                 "Subtitles this system OCR'd itself do NOT count - only a "
                 "human-authored track does."
                 if C["dropRedundantImageSubs"] else "kept alongside the text")},
            {"k": "default/forced flags on kept image subs", "both":
                ("cleared - a flagged image sub makes Plex burn it at playback. "
                 "The track stays selectable."
                 if C["neutralizeKeptImageSubFlags"] else "left as-is")},
            {"k": "burned track afterwards", "both":
                "removed from the output" if C["removeBurnedSub"]
                else "kept as a track"},
            {"k": "burn language guard", "both":
                ("only a likely-English sub is burned, so a German or French "
                 "forced track is not painted in"
                 if C["burnLangGuard"] else "off")},
            {"k": "no English audio survives",
             "anime": ("the best English TEXT dialogue sub is flagged default "
                       "so Plex shows it - never an image sub, never a signs "
                       "track" if C["forceEngSubWhenNoEngAudio"] else "off"),
             "other": "same rule, but rarely reached - English audio is kept "
                      "whenever it exists"},
            {"k": "English PGS dialogue (background OCR)", "both":
                "converted to an SRT text track by the purple subocr queue, "
                "muxed in FIRST so it tops Plex's subtitle list; the original "
                "PGS is kept but demoted to default=0/forced=0. Signs & songs "
                "tracks are excluded - by title where one exists, by cue "
                "density (under ~6 cues/min) for the 3,780 untitled ones - "
                "because OCR cannot recover their positioning and they only "
                "work burned in. Skipped entirely when an English text sub "
                "already exists"},
            {"k": "burn on HDR", "both": yn(C["burnOnHDR"])
                + " - the colour tags and the HDR10 mastering-display and "
                  "content-light metadata are re-stated on the output, so the "
                  "re-encode keeps its HDR. Dolby Vision's per-frame layer "
                  "cannot survive any encode, so a DV file burns as its HDR10 "
                  "base layer - what the strip-DV rule produces anyway"},
        ],
        "safety": [
            {"k": "output duration", "both":
                (f"capped at the real runtime + {C['trimBufferSec']}s. A PGS "
                 f"burn-in once produced a 1193-hour duration; the cap trims "
                 f"the runaway frame instead of poisoning the file."
                 if C["trimToRealDuration"] else "not capped")},
            {"k": "container", "both": C["container"]},
            {"k": "size check", "both":
                "an encode that comes out larger than the source is discarded "
                "rather than committed"},
        ],
    }


def is_anime(path: str = "", title: str = "") -> bool:
    r"""One definition of "is this anime", used everywhere.

    There were two, and they disagreed. enqueue() tested `"\Anime" in path`,
    which is right on Windows. The fallback in _run() - used whenever a job
    arrives without a stored plan - tested `"/Anime" in job.path`, with a
    FORWARD slash, which can never match a `P:\Anime Shows\...` path. So a file
    replanned at runtime was treated as live action, and the live-action audio
    policy keeps English only: the Japanese track would have been dropped from
    a subtitled anime, leaving it unwatchable.

    Matching on the library FOLDER rather than the title, because "anime" in a
    show's name is not evidence and the folder is how the library is actually
    organised here (Anime Shows, Anime Movies).

    DIRECTORY SEGMENTS ONLY, and this matters. A plain substring test on the
    whole path matches the FILENAME too, so `P:\Movies\Anime Club (2020).mkv` -
    a live-action film - came back as anime and would have been given the
    dual-audio policy. Caught by a truth-table check, not in production.

    THE FOLDER IS NOW THE FLOOR, NOT THE WHOLE ANSWER.

    contentkind asks the arrs what TheTVDB and TMDB say about the title, which
    catches an anime shelved outside an Anime folder. It can only ever ADD to
    this verdict, never subtract - see the asymmetry note in contentkind.py.
    Measured on this library: 282 files in Animated Shows are anime by
    metadata, and 127 files in Anime Shows have no animation genre at all, so
    trusting metadata alone would have stripped their Japanese audio.
    """
    parts = [s for s in (path or "").replace("/", "\\").split("\\") if s]
    # Drop the filename: only folders say which library a file belongs to.
    folders = parts[:-1]
    if any(s.lower().startswith("anime") for s in folders):
        return True
    try:
        from . import contentkind
        return contentkind.by_path(path) == "anime"
    except Exception:
        return False


# ------------------------------------------------------- language policy ----
_KIND_WORD = {"anime": "anime", "animation": "animation",
              "live": "live action"}

# 2- and 3-letter forms of the same language. Probes are inconsistent about
# which they use - the old constants listed BOTH ("jpn" and "ja", "eng" and
# "en") for exactly this reason - so a policy naming one form must match the
# other, or a file tagged "ja" would be silently dropped by a policy that says
# "jpn".
_ISO_PAIRS = [
    ("eng", "en"), ("jpn", "ja"), ("kor", "ko"), ("zho", "zh"), ("chi", "zh"),
    ("fra", "fr"), ("fre", "fr"), ("deu", "de"), ("ger", "de"), ("spa", "es"),
    ("ita", "it"), ("por", "pt"), ("rus", "ru"), ("nld", "nl"), ("dut", "nl"),
    ("swe", "sv"), ("nor", "no"), ("dan", "da"), ("fin", "fi"), ("pol", "pl"),
    ("ces", "cs"), ("cze", "cs"), ("hun", "hu"), ("tur", "tr"), ("ara", "ar"),
    ("heb", "he"), ("hin", "hi"), ("tam", "ta"), ("tel", "te"), ("mal", "ml"),
    ("tha", "th"), ("vie", "vi"), ("ind", "id"), ("ell", "el"), ("gre", "el"),
    ("ukr", "uk"), ("ron", "ro"), ("rum", "ro"), ("bul", "bg"), ("hrv", "hr"),
    ("srp", "sr"), ("slk", "sk"), ("slo", "sk"), ("cat", "ca"), ("isl", "is"),
]
_ISO_EQ: dict[str, set[str]] = {}
for _a, _b in _ISO_PAIRS:
    _ISO_EQ.setdefault(_a, set()).update({_a, _b})
    _ISO_EQ.setdefault(_b, set()).update({_a, _b})
# chi/zho and fre/fra style aliases have to reach each other too
for _a, _b in _ISO_PAIRS:
    for _c in list(_ISO_EQ[_a]):
        _ISO_EQ[_a] |= _ISO_EQ.get(_c, set())
    _ISO_EQ[_b] = _ISO_EQ[_a]


def _expand(codes) -> set[str]:
    """A keep-list plus every equivalent spelling of each entry."""
    out: set[str] = set()
    for c in codes or []:
        c = str(c).strip().lower()
        if not c:
            continue
        out |= _ISO_EQ.get(c, {c})
    return out


def _policy(library: str, side: str, loaded: dict | None = None,
            path: str = "") -> dict:
    """The policy for one LIBRARY and side.

    `loaded` is the whole policy dict when the caller already has one - which
    the impact scan does, because it needs to plan 39,000 files under a policy
    that is NOT the stored one. It also keeps the planner from doing a database
    read per file, which is what the first version of this did.

    Falls back, in order: the library's stored policy, the default for the kind
    its NAME implies, then live action. Never raises - a settings lookup
    failing must not stop a file being planned.
    """
    try:
        from . import langpolicy
        pol = loaded if isinstance(loaded, dict) else langpolicy.load()
        got = (pol.get(library) or {}).get(side) if library else None
        if got:
            return got
        kind = langpolicy.kind_for(path=path, library=library)
        return langpolicy.KIND_DEFAULTS[kind][side]
    except Exception:
        try:
            from .langpolicy import KIND_DEFAULTS      # type: ignore
            return KIND_DEFAULTS["live"][side]
        except Exception:
            return {"keep_original": True, "langs": ["eng"],
                    "keep_untagged": True}


# -------------------------------------------------------------- decide -----
def _orig_codes(name: str | None) -> set[str]:
    """ISO codes for a provider language name, or empty when unknown.

    Kept behind a function so rules.py does not import origlang at module
    scope - origlang reaches into the database and the arrs, and decide() is a
    pure function that must stay importable and testable on its own.
    """
    if not name:
        return set()
    try:
        from .origlang import codes_for
        return codes_for(name)
    except Exception:
        return set()


def decide(probe: dict, *, anime: bool = False, filename: str = "",
           size_bytes: int = 0, orig_lang: str = "", kind: str = "",
           library: str = "", policy: dict | None = None,
           file_id: int = 0) -> Plan:
    """Turn an ffprobe result into a plan. Pure function - no side effects.

    `orig_lang` is the language the TITLE was made in, as the metadata provider
    names it ("Japanese", "Korean"). Optional on purpose: pass "" and the
    keep-original flag has nothing to act on, so the policy falls back to
    keeping what is there rather than guessing.

    `kind` is anime | animation | live and selects which language policy
    applies. `anime=True` is still accepted and still means the anime policy,
    so every existing caller and every stored plan keeps working - callers are
    migrated to `kind` one at a time rather than in one edit that has to be
    right everywhere at once.
    """
    if not kind:
        try:
            from . import langpolicy
            kind = langpolicy.kind_for(path=filename, library=library)
        except Exception:
            kind = "live"
        if anime:
            kind = "anime"          # explicit flag always wins
    p = Plan()
    p.src_size = int(size_bytes or 0)   # what these decisions were made about
    streams = probe.get("streams") or []
    fmt = probe.get("format") or {}

    video = [s for s in streams if s.get("codec_type") == "video"
             and not _disp(s, "attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    # In subtitle order, matching keep_subs indices. The executor needs these to
    # know which subs the OUTPUT container can hold - mov_text cannot go into
    # Matroska, and copying it there fails the whole job.
    p.sub_codecs = [(s.get("codec_name") or "").lower() for s in subs]
    # The flags each subtitle carries NOW, as -disposition strings, so the
    # executor can re-assert them explicitly on every kept track. Only the
    # flags ffmpeg's -disposition option accepts by name are carried.
    _DISP_OK = ("default", "forced", "original", "hearing_impaired", "dub",
                "comment", "lyrics", "karaoke", "visual_impaired", "captions",
                "descriptions", "metadata")
    p.sub_disps = ["+".join(f for f in _DISP_OK
                            if (s.get("disposition") or {}).get(f)) or "0"
                   for s in subs]

    if not video:
        p.skip_reason = "no decodable video stream"
        return p

    v = video[0]
    height = int(v.get("height") or 0)
    vcodec = (v.get("codec_name") or "").lower()
    hdr, ten_bit = _is_hdr(v), _is_10bit(v)
    duration = float(fmt.get("duration") or 0)
    overall_mbps = (int(fmt.get("bit_rate") or 0) / 1_000_000) if fmt.get("bit_rate") else (
        (size_bytes * 8 / duration / 1_000_000) if duration and size_bytes else 0)

    p.source_mbps = overall_mbps

    # ---- container -------------------------------------------------------
    fname = (fmt.get("format_name") or "")
    if "matroska" not in fname:
        p.add("container", f"repackage as {CONFIG['container']}",
              "MKV is the container Plex plays without converting anything",
              f"source container: {fname}")

    # PER-LIBRARY CODEC SETTINGS. Falls back to the old constants if the store
    # cannot be read, so a database problem degrades to the previous behaviour
    # rather than to no behaviour.
    try:
        from . import codecpolicy
        VP = codecpolicy.for_library(library, "video")
        AP = codecpolicy.for_library(library, "audio")
    except Exception:
        VP = AP = {}

    def _v(key, fallback):
        val = VP.get(key)
        return fallback if val is None else val

    def _a(key, fallback):
        val = AP.get(key)
        return fallback if val is None else val

    # Resolved once, because several branches below need them and the audio
    # section needs the channel ceiling as an int rather than the string the
    # picker stores.
    max_ch = int(_a("surround_max_channels", CONFIG["surroundMaxChannels"]))
    surr_br = int(_a("surround_bitrate", CONFIG["surroundBitrate"]))
    stereo_br = int(_a("stereo_bitrate", CONFIG["stereoBitrate"]))
    eac3_max = int(_a("eac3_max_bitrate_k", CONFIG["eac3MaxBitrateK"]))
    copy_surround = list(_a("copy_surround_if", CONFIG["copySurroundIfCodec"]))
    copy_stereo = list(_a("copy_stereo_if", CONFIG["copyStereoIfCodec"]))

    # ---- Dolby Vision ----------------------------------------------------
    dv = _dv_profile(v, filename)
    dv_profiles = [int(x) for x in _v("strip_dv_profiles",
                                      CONFIG["stripDVProfiles"])]
    if _v("strip_dv", CONFIG["stripDV"]) and dv in dv_profiles:
        p.strip_dv = True
        p.add("video", f"remove the Dolby Vision layer (keeps HDR10)",
              "TVs without Dolby Vision have to convert this file to play it; "
              "removing that layer leaves normal HDR, and the picture is not "
              "re-encoded so nothing is lost",
              f"remove NAL {CONFIG['dvRemoveNalTypes']}"
              + (", retag hvc1" if CONFIG["dvRetagHvc1"] else ""))

    # ---- video routing ---------------------------------------------------
    target = "h264"
    if (_v("route_hevc", CONFIG["routeToH265OnHevc"])
            and vcodec in ("hevc", "h265")) \
       or (_v("route_10bit", CONFIG["routeToH265On10bit"]) and ten_bit) \
       or (_v("route_hdr", CONFIG["routeToH265OnHDR"]) and hdr) \
       or (_v("route_av1", CONFIG["routeToH265OnAv1"]) and vcodec == "av1"):
        target = "h265"
    p.target = target
    # Carry the source depth through to the executor. h264_nvenc cannot encode
    # 10-bit at all, so this is only meaningful on the h265 path - which is
    # where every 10-bit source is routed by routeToH265On10bit above.
    p.ten_bit = bool(ten_bit and target == "h265")

    # The encoder settings this plan will be executed with. Stamped now so the
    # command that eventually runs cannot disagree with the plan that was shown.
    _tg = CONFIG["videoTargets"][target]
    # WHICH SILICON, RESOLVED NOW rather than at run time. The library may ask
    # for QuickSync on a machine that has no Intel GPU; resolve() answers with
    # something that actually works and says why. Stamping the answer into the
    # plan means the queue shows the encoder that will really be used, and a
    # job queued today still runs the same way if the setting changes tomorrow.
    _fam, _why, _encname = "nvenc", "", _tg["encoder"]
    try:
        from . import encoders as _enc
        _fam, _why = _enc.resolve(str(_v("encoder_family", "auto")))
        _encname = _enc.encoder_for(_fam, "hevc" if target == "h265" else "h264")
    except Exception:                                    # noqa: BLE001
        # encoders.py unavailable or the probe blew up: keep the old constant,
        # which is what every plan used before this existed.
        pass
    p.venc = {
        "family": _fam,
        "family_why": _why,
        "encoder": _encname,
        "preset": str(_v(f"{target}_preset", _tg["preset"])),
        "cq": int(_v(f"{target}_cq", _tg["cq"])),
        # Stored as a percentage in the settings, used as a multiplier here -
        # "150%" reads better in a box than "1.5" and cannot be mistaken for a
        # bitrate.
        "maxrate_factor": float(_v("maxrate_factor",
                                   CONFIG["maxrateFactor"] * 100)) / 100.0,
        "maxrate_floor_mbps": float(_v("maxrate_floor_mbps",
                                       CONFIG["maxrateFloorMbps"])),
        "extra": CONFIG["nvencExtra"].get(target, ""),
    }

    _tiers = {"2160": int(_v("shrink_2160_mbps",
                            CONFIG["spaceSaver"]["maxMbps"]["2160"])),
              "1080": int(_v("shrink_1080_mbps",
                            CONFIG["spaceSaver"]["maxMbps"]["1080"])),
              "720": int(_v("shrink_720_mbps",
                            CONFIG["spaceSaver"]["maxMbps"]["720"])),
              "sd": int(_v("shrink_sd_mbps",
                           CONFIG["spaceSaver"]["maxMbps"]["sd"]))}
    ceiling = _tiers.get(_res_tier(height), float("inf"))
    oversized = (_v("shrink_enabled", CONFIG["spaceSaver"]["enabled"])
                 and overall_mbps > ceiling * CONFIG["spaceSaver"]["triggerFactor"]
                 and (_v("shrink_include_hdr",
                         CONFIG["spaceSaver"]["includeHDR"]) or not hdr)
                 # Drawn animation is never shrunk for size alone - see the
                 # skipKinds note in CONFIG. This does NOT stop anime being
                 # re-encoded for a real reason: a burn-in or an unplayable
                 # codec still goes through the branch above.
                 and kind not in CONFIG["spaceSaver"].get("skipKinds", []))

    # av1 is conditional - see convertAv1. Everything else in this tuple is a
    # format we are happy to leave alone.
    accepted = ["h264", "hevc", "h265"]
    if not _v("convert_av1", CONFIG["convertAv1"]):
        accepted.append("av1")
    needs_codec_change = vcodec not in accepted
    if needs_codec_change:
        why = ("AV1 is not decoded by every client here; the ones that cannot "
               "fall back to a CPU transcode"
               if vcodec == "av1" else
               "codec is not one Plex reliably direct-plays")
        p.encode = True
        # Not a shrink job - see Plan.grow_ok.
        p.grow_ok = True
        p.add("video", f"rebuild the video from {vcodec} to {target}"
                       + (" (10-bit kept)" if p.ten_bit else ""),
              why,
              f"{p.venc['encoder']} cq={p.venc['cq']}")
    elif oversized:          # only reachable if the shrink is re-enabled
        p.encode = True
        # keep SOURCE codec on shrink -> avoids arr h265 re-download loops
        if not _v("shrink_force_h265", CONFIG["spaceSaver"]["forceH265"]) \
                and vcodec == "h264":
            p.target = target = "h264"
            _tg = CONFIG["videoTargets"][target]
            # Same family as the plan already resolved - dropping to h264 is a
            # change of CODEC, not of which silicon does the work.
            _h264 = _tg["encoder"]
            try:
                from . import encoders as _enc2
                _h264 = _enc2.encoder_for(p.venc.get("family") or "nvenc", "h264")
            except Exception:                            # noqa: BLE001
                pass
            p.venc.update(encoder=_h264,
                          preset=str(_v("h264_preset", _tg["preset"])),
                          cq=int(_v("h264_cq", _tg["cq"])),
                          extra=CONFIG["nvencExtra"].get(target, ""))
        p.add("video", f"shrink the video ({overall_mbps:.1f} Mbps is high for this size)",
              f"above the {ceiling} Mbps ceiling for {_res_tier(height)}p; "
              f"source codec kept to avoid an arr re-download loop",
              f"maxrate {max(overall_mbps * p.venc['maxrate_factor'], p.venc['maxrate_floor_mbps']):.1f} Mbps")
    elif hdr:
        # NOTE, not an action. "copy video (HDR preserved)" describes what we are
        # NOT doing, but p.add() marks the file as needing work - so every HDR
        # file with nothing else wrong was queued and rewritten end to end for a
        # measured 0.00% change. Pure I/O for no benefit.
        p.notes.append("video copied untouched — re-encoding HDR would lose "
                       "metadata and break Dolby Vision")

    # ---- audio -----------------------------------------------------------
    # Decide the keep-list for THIS file before looking at any track.
    eng = set(CONFIG["englishLangs"])
    langs_present = {(_lang_guess(a) or "und") for a in audio}
    has_eng = bool(langs_present & eng)
    single = len(audio) <= 1

    # ---- WHICH LANGUAGES SURVIVE ----------------------------------------
    #
    # ASK THE METADATA, NOT THE FOLDER NAME.
    #
    # The rule below used to infer intent from where the file sat: anything
    # under an Anime library kept Japanese + English, everything else kept
    # English and only fell back to the original when no English existed. That
    # is a guess that happens to be right for the libraries it was written for.
    # It has no idea a Korean drama in TV Shows has a Korean performance worth
    # keeping, and it cannot tell a Spanish original from a Spanish dub.
    #
    # orig_lang is the answer from TMDB/TheTVDB, by way of the arrs: the
    # language the thing was MADE in. With it, the policy is one sentence -
    # keep English, keep the original, drop the rest - and it reads the same
    # for anime, for a Danish thriller and for a Hollywood film.
    #
    # UNKNOWN IS NOT "NO ORIGINAL". If the name is missing or is one this
    # module has no ISO codes for, orig_codes is empty and the old logic runs
    # untouched. That distinction is the whole safety of this change: treating
    # "I could not find out" as "there is no original-language track" would
    # delete the performance and leave the dub.
    orig_codes = _orig_codes(orig_lang)

    # THE KEEP-LIST NOW COMES FROM THE POLICY, NOT FROM CONSTANTS.
    #
    # Same shape as before - a set of language codes plus the original - but
    # per content kind and editable at /settings#languages. The defaults are
    # the old constants verbatim, so this reads identically until edited.
    apol = _policy(library, "audio", policy, filename)
    allowed = _expand(apol.get("langs") or [])
    want_orig = bool(apol.get("keep_original"))

    if single:
        keep_langs = langs_present            # only one track - keep it
        policy_note = "single audio track, kept as-is"
    else:
        keep_langs = set(allowed)
        if want_orig and orig_codes:
            keep_langs |= orig_codes
        named = ", ".join(sorted(apol.get("langs") or [])) or "nothing"
        policy_note = f"{library or _KIND_WORD.get(kind, kind)}: keeping {named}"
        if want_orig:
            policy_note += (f" + the original ({orig_lang})" if orig_lang
                       else " + the original language")

        # NEVER GO SILENT. If the policy would remove every audio track in the
        # file - a Norwegian-only film under an English-only policy, or an
        # original nobody could identify - keep what is there instead. A file
        # with no audio is not a tidier file, it is a broken one, and this
        # guard is the reason the policy screen cannot be used to destroy a
        # library by accident.
        if not (keep_langs & langs_present):
            if want_orig and orig_codes and (orig_codes & langs_present):
                keep_langs = orig_codes | {"und"}
                policy_note = (f"{library or _KIND_WORD.get(kind, kind)}: none of "
                           f"the chosen languages are here — keeping "
                           f"{orig_lang}, the language it was made in")
            else:
                keep_langs = langs_present
                policy_note = (f"{library or _KIND_WORD.get(kind, kind)}: none "
                               f"of the chosen languages are here and the "
                               f"original is unknown — keeping every track "
                               f"rather than leaving it silent")

    # THE METADATA MAY ADD A LANGUAGE. IT MAY NEVER TAKE ONE AWAY.
    #
    # Measured across 16,233 multi-audio files, a strict "keep English and the
    # original, drop the rest" would have stripped the Japanese track from 356
    # anime episodes, because TheTVDB records Marvel Anime: Blade with an
    # originalLanguage of English - a co-production credited to the wrong side.
    # 36 more sat under a title marked Chinese while carrying English and
    # Japanese audio. The provider describes the WORK; the tags describe the
    # FILE; where they disagree the file is the thing being played.
    #
    # So when keep-original is on it is a UNION with the chosen languages, and
    # turning it off is the only way to make it subtractive - which is a
    # deliberate choice someone has to make on the settings page.
    if not single and want_orig and orig_codes:
        added = (orig_codes - allowed) & langs_present
        if added:
            policy_note += f"; {orig_lang} is present and kept"
    p.notes.append(f"audio policy — {policy_note}")

    best_per_lang: dict[str, tuple[int, tuple]] = {}
    # TWO PASSES, because "the best track for this language" cannot be known
    # until every track has been seen. The single-pass version emitted a
    # CONVERSION action for a track and then a DROP action for the same track
    # when a better one turned up later, so the plan read
    #     track 0: truehd 6ch -> E-AC3
    #     drop duplicate track 0 (jpn)
    # ffmpeg was always given the right mapping - the dropped tracks were
    # never encoded - but the plan contradicted itself.
    cands: list[tuple] = []      # (i, lang, ch, codec, compatible, rank)
    for i, a in enumerate(audio):
        lang = _lang_guess(a) or "und"
        if lang not in keep_langs:
            p.add("audio", f"remove audio {i} ({lang})",
                  f"{policy}; this language is not kept")
            continue
        if _a("drop_commentary", True) \
                and CONFIG["commentaryPattern"].search(_title(a)):
            p.add("audio", f"remove the commentary track {i}",
                  "director commentary is not something you sit down to "
                  "watch, and it makes the file bigger")
            continue
        ch = int(a.get("channels") or 2)
        codec = (a.get("codec_name") or "").lower()
        compatible = (codec in copy_stereo if ch <= 2
                      else codec in copy_surround)
        # LOSSLESS BEATS BITRATE, and this was a genuine quality bug. ffprobe
        # reports NO bit_rate for TrueHD/DTS-HD/FLAC because they are variable,
        # so a lossless track ranked (6,0,0) and lost to an AC3 at (6,0,640000).
        # Vexille kept its 448k AC3, discarded the TrueHD, and re-encoded
        # lossy -> lossy: a second generation of loss with the lossless master
        # sitting in the same file. Ranked BELOW `compatible` on purpose - an
        # existing E-AC3 is copied untouched, which beats any re-encode.
        prof = str(a.get("profile") or "").lower()
        lossless = 1 if (codec in CONFIG["losslessCodecs"] or "hd ma" in prof
                         or "lossless" in prof) else 0
        rank = (ch, 1 if compatible else 0, lossless,
                int(a.get("bit_rate") or 0))
        cands.append((i, lang, ch, codec, compatible, rank))

    if _a("dedupe_per_lang", CONFIG["dedupeAudioPerLang"]):
        # NEVER DEDUPE TWO UNTAGGED TRACKS. 'und' is not a language, it is the
        # absence of one - two tracks that both failed to say what they are
        # have given no evidence they are the same thing, and the one time this
        # fired in anger the "spare" was the Japanese half of a dual-audio
        # release whose titles the guesser above could not read. Removing a
        # track needs positive evidence; und = und is not it.
        def _tag2(i):
            # 22 was the tightest limit of the lot, on the line that most needs
            # the detail: this is the tag that says WHICH of two same-language
            # tracks is being dropped.
            t = _short(_title(audio[i]), 60)
            return f" ({t})" if t else ""
        for i, lang, ch, codec, compatible, rank in cands:
            if lang == "und" and any(c[1] == "und" and c[0] != i
                                     for c in cands):
                best_per_lang.setdefault(f"und#{i}", (i, rank))
                continue
            prev = best_per_lang.get(lang)
            if prev and prev[1] >= rank:
                p.add("audio", f"remove the spare {lang} audio {i}{_tag2(i)}",
                      "there is already a better version of this language "
                      "in the file")
                continue
            if prev:
                p.add("audio",
                      f"remove the spare {lang} audio {prev[0]}{_tag2(prev[0])}",
                      "a better version of this same language is being kept "
                  "instead")
            best_per_lang[lang] = (i, rank)
        keep_idx = {i for i, _ in best_per_lang.values()}
    else:
        keep_idx = {c[0] for c in cands}

    # NAME THE LANGUAGE ON EVERY TRACK LINE.
    #
    # "audio 0: convert dts surround to E-AC3" told you an index. On a file
    # with four audio tracks the reader's next question is always the same -
    # WHICH one - and answering it meant opening the file in something else.
    # The removal lines already said "(fre, spa)"; the convert lines did not,
    # so the same log was specific about what it threw away and vague about
    # what it kept.
    def _tag(code: str) -> str:
        c = (code or "").strip().lower()
        return f" ({c})" if c and c != "und" else " (untagged)"

    # Pass 2: emit work ONLY for tracks that actually survive.
    for i, lang, ch, codec, compatible, rank in cands:
        if i not in keep_idx:
            continue
        p.keep_audio.append(i)
        a = audio[i]            # pass 2 iterates candidates, not streams

        # Right codec, wrong bitrate. EAE-incompatible E-AC3 (see
        # eac3MaxBitrateK above) is re-encoded to the standard rate rather
        # than copied - the whole point of this system is that Plex can play
        # what comes out of it. A missing bit_rate is treated as fine: E-AC3
        # is CBR and ffprobe reports it reliably, so absence means an odd
        # container, not a high rate.
        br_k = int(a.get("bit_rate") or 0) // 1000
        # THE CHANNEL CEILING APPLIES TO EVERY CODEC, INCLUDING THE GOOD ONES.
        #
        # This test used to live inside the `not compatible` branch below, so a
        # track only got downmixed if its FORMAT was also wrong. An 8-channel
        # E-AC3 is a compatible format, so nothing fired and the file was
        # declared correct - while surroundMaxChannels says, in its own comment,
        # "downmix anything above this".
        #
        # The rule audit is what caught it, and it caught it by disagreeing:
        # audit/channels tests `ch > ceiling` on every audio track regardless of
        # codec, flagged Snow White's eac3 7.1, and the Requeue button answered
        # "the planner considers this file already correct". Two pieces of code
        # reading the same config to opposite conclusions. Confirmed it was not
        # a stale probe - the stored probe and a fresh ffprobe of the file on
        # disk both report eac3/8ch, and decide() returned needed=False for both.
        #
        # The audit is the one that is right: the channel count is what forces
        # Plex to remix on the fly for a 5.1 or stereo client, and it does that
        # whether the eight channels arrived as TrueHD or as E-AC3. Tested
        # first, so it wins over the bitrate branch - which was already doing
        # min(ch, ceiling) and therefore already agreed that >6 must come down.
        if ch > max_ch:
            p.audio_ops.append({"idx": i, "to": "eac3",
                                "br": surr_br, "ch": max_ch})
            p.add("audio", f"audio {i}{_tag(lang)}: {codec} {ch} speakers down "
                           f"to {_ch_name(max_ch)}",
                  f"more than {_ch_name(max_ch)} forces Plex to convert on the "
                  f"fly; {_ch_name(max_ch)} plays as-is",
                  f"{surr_br} kbps")
        elif compatible and codec == "eac3" and ch > 2 \
                and eac3_max > 0 and br_k > eac3_max:
            p.audio_ops.append({"idx": i, "to": "eac3", "br": surr_br,
                                "ch": min(ch, max_ch)})
            p.add("audio", f"audio {i}{_tag(lang)}: lower E-AC3 from {br_k}k to "
                           f"{surr_br}k",
                  "Plex cannot decode E-AC3 at this bitrate and buffers forever; "
                  "the standard rate plays instantly",
                  f"{surr_br} kbps")
        elif not compatible:
            if ch <= 2:
                p.audio_ops.append({"idx": i, "to": "aac",
                                    "br": stereo_br, "ch": ch})
                p.add("audio", f"audio {i}{_tag(lang)}: convert {codec} to AAC stereo",
                      "AAC stereo is the one format every device plays without help",
                      f"{stereo_br} kbps")
            else:
                p.audio_ops.append({"idx": i, "to": "eac3",
                                    "br": surr_br, "ch": ch})
                p.add("audio", f"audio {i}{_tag(lang)}: convert {codec} surround to E-AC3",
                      "this surround format is not played by every device; E-AC3 is",
                      f"{surr_br} kbps")
        else:
            p.audio_ops.append({"idx": i, "to": "copy"})

    if _a("dedupe_per_lang", CONFIG["dedupeAudioPerLang"]):
        p.keep_audio = sorted(i for i, _ in best_per_lang.values())

    # Same guesser as the dedupe: an untagged track titled "Eng" IS English
    # audio, and pretending otherwise made forceEngSubWhenNoEngAudio switch
    # subtitles on over English speech.
    has_eng_audio = any(_lang_guess(audio[i]) in ("eng", "en")
                        for i in p.keep_audio if i < len(audio))

    # ---- subtitles -------------------------------------------------------
    # Same policy machinery as audio, separately configurable - the two
    # questions are genuinely different. You want English audio and nothing
    # else, but a Japanese show with English subtitles needs both, and the old
    # single keepSubLangs constant could not express "keep the original
    # language's subtitles for anime but not for live action".
    spol = _policy(library, "subs", policy, filename)
    sub_keep = _expand(spol.get("langs") or [])
    if spol.get("keep_original") and orig_codes:
        sub_keep |= orig_codes
    keep_untagged = bool(spol.get("keep_untagged", True))

    unwanted: list[str] = []
    commentary: list[int] = []
    for i, s in enumerate(subs):
        lang = _lang(s)
        # Commentary goes before the language test, not after: these are almost
        # always in a language you DO keep, so a language-first check would
        # keep every one of them.
        if CONFIG["dropCommentarySubs"] and \
           CONFIG["commentaryPattern"].search(_title(s)):
            commentary.append(i)
            continue
        keep = lang in sub_keep or (keep_untagged and lang in ("", "und"))
        if not keep:
            unwanted.append(lang or "und")
        if keep:
            p.keep_subs.append(i)
            if CONFIG["neutralizeKeptImageSubFlags"] and \
               (s.get("codec_name") or "").lower() in IMAGE_SUB_CODECS and \
               (_disp(s, "default") or _disp(s, "forced")):
                p.clear_flags.append(i)
                p.add("subtitle",
                      f"stop picture subtitle {i}{_tag(lang)} switching itself on",
                      "picture subtitles set to auto-show make Plex redraw the video "
                      "while you watch; it stays in the list to pick")

    # DROPPING A TRACK IS WORK, AND HAS TO SAY SO.
    #
    # keep_subs already excluded these - the planner had decided - but nothing
    # called it an action, so `needed` stayed False and no job was ever raised.
    # The exclusion therefore only took effect when some OTHER fix happened to
    # rewrite the file. Measured across the library: 658 finished files still
    # carrying Spanish, French, Portuguese, German, Chinese and Nordic tracks
    # the configuration excludes, one of them showing Danish subtitles by
    # default on an English film. A decision that never becomes work is not a
    # decision.
    if commentary:
        p.add("subtitle",
              f"remove {len(commentary)} commentary subtitle track(s)",
              "the commentary AUDIO is already dropped for exactly this reason "
              "- it is not something you sit down to watch - and a transcript "
              "of it is no more use than the audio was",
              ", ".join(_short(_title(subs[i])) or f"track {i}"
                        for i in commentary[:3]))
    if unwanted:
        seen = sorted(set(unwanted))
        p.add("subtitle",
              f"remove {len(unwanted)} subtitle track(s) you do not read"
              f" ({', '.join(seen[:6])}{'…' if len(seen) > 6 else ''})",
              "the subtitle menu should only offer languages you read",
              f"{len(subs) - len(unwanted)} of {len(subs)} track(s) kept")

    # ---- redundant image subs -------------------------------------------
    # BEFORE the burn choice on purpose. Once the PGS signs track is gone, the
    # burn target becomes the ASS signs track, which renders through libass
    # instead of overlaying a bitmap - sharper, correctly scaled, and it is the
    # typesetting the release group actually authored.
    if CONFIG["dropRedundantImageSubs"] and p.keep_subs:
        def _role(s: dict) -> str:
            # THREE roles, not two. The first draft had signs/dialogue only,
            # and the library immediately showed why that is not enough:
            # Aladdin carries a plain 'English' ASS beside an 'SDH' PGS, and
            # 'a text dialogue track exists' would have deleted the SDH one.
            # SDH is not a styling variant of dialogue - it carries speaker
            # labels and sound cues that the plain track does not have, and
            # for a deaf viewer it is the only usable track in the file.
            # Sing-along/karaoke is likewise its own thing.
            t = _title(s)
            if re.search(r"\bsdh\b|hearing.?impaired|\bhi\b", t, re.I):
                return "sdh"
            if re.search(r"sing.?along|karaoke", t, re.I):
                return "singalong"
            return ("signs" if (SIGNS_TITLE_RE.search(t)
                                or _disp(s, "forced")) else "dialogue")

        def _lkey(s: dict) -> str:
            lg = _lang(s)
            return "und" if lg in ("", "und") else lg[:2]

        # OUR OWN OCR OUTPUT DOES NOT COUNT AS COVERAGE.
        #
        # "someone already did it" means a human typeset it - the release's own
        # ASS/SRT, which is better than Tesseract will ever be. A track this
        # system produced by reading the very PGS in question is not
        # independent evidence, and treating it as such would strip the image
        # track from ~1,800 files that were already processed under the
        # decision to KEEP the PGS and demote it. That was a deliberate choice;
        # a new rule should not quietly reverse it on files already committed.
        covered = {(_lkey(subs[i]), _role(subs[i])) for i in p.keep_subs
                   if (subs[i].get("codec_name") or "").lower() in TEXT_SUB_CODECS
                   and not OCR_MADE_RE.search(_title(subs[i]))}

        # ONE EXCEPTION: AN IMAGE SUB WITH NO LANGUAGE TAG AT ALL.
        #
        # The rule above says our own OCR is not evidence, and that stands for
        # a properly tagged track: the PGS is a real English subtitle, it was
        # demoted rather than deleted, and ~1,800 committed files were decided
        # under that promise.
        #
        # An UNTAGGED image sub is a different animal. Its language is unknown,
        # so no language rule can ever match it and no coverage check can ever
        # cover it - it is permanently un-droppable and un-classifiable, and it
        # sits in the file forever alongside the text track nuarr made FROM it.
        # That is the Kaiju No. 8 S01E06 shape the rule audit kept reporting:
        # a PGS titled 'Eng' with no language field, next to a 'Dialogue (OCR)'
        # SRT, which the auditor flagged and the planner said was fine.
        #
        # Since nuarr produced the text track by reading that exact picture
        # track, for an untagged sub the OCR IS the independent evidence -
        # there is nothing else to appeal to. Measured before enabling: 25
        # files in the library carry an untagged image sub, 21 of them also
        # carry an (OCR) text track, so this rewrites 21 files and cannot reach
        # the 1,800.
        ocr_roles = {_role(subs[i]) for i in p.keep_subs
                     if (subs[i].get("codec_name") or "").lower() in TEXT_SUB_CODECS
                     and OCR_MADE_RE.search(_title(subs[i]))}

        for i in list(p.keep_subs):
            s = subs[i]
            if (s.get("codec_name") or "").lower() not in IMAGE_SUB_CODECS:
                continue
            role = _role(s)
            untagged = _lkey(s) == "und"
            by_ocr = untagged and role in ocr_roles
            if (_lkey(s), role) not in covered and not by_ocr:
                continue
            p.keep_subs.remove(i)
            if i in p.clear_flags:
                p.clear_flags.remove(i)          # nothing left to neutralise
            p.add("subtitle",
                  f"remove the picture {role} subtitle {i}{_tag(_lang(s))}"
                  + (f" — {_short(_title(s))}" if _short(_title(s)) else ""),
                  ("this track carries no language tag, and the text version "
                   "beside it was made by reading this very track - keeping "
                   "both leaves an unlabelled picture copy nothing can match"
                   if by_ocr else
                   "the same subtitles are already here as real text, which is "
                   "searchable and easier on playback than a picture"),
                  "text subtitles are searchable, styleable and direct-play")

    if anime and CONFIG["burnEnabled"]:
        bi, variant = pick_burn_target([subs[i] for i in p.keep_subs] if p.keep_subs else subs)
        if bi is not None:
            real = p.keep_subs[bi] if p.keep_subs else bi
            is_image = (subs[real].get("codec_name") or "").lower() in IMAGE_SUB_CODECS
            allowed = (p.encode or (CONFIG["alwaysBurnImageSubs"] and is_image)
                       or not CONFIG["burnOnlyWhenEncoding"])
            if hdr and not CONFIG["burnOnHDR"]:
                p.add("subtitle", f"leave the signs on subtitle {real} as they are",
                      "burning is switched off for HDR files here, so the signs "
                      "stay a separate track")
            elif allowed:
                p.burn_index = real
                p.burn_image = is_image
                p.encode = True
                # A burn is a compatibility encode, not a shrink: its payoff is
                # signs painted into the picture, and an efficient x265 source
                # re-encoded at cq22 routinely lands a few percent LARGER. The
                # first standalone 10-bit burn encoded perfectly and was then
                # discarded by the save-5%-or-die gate; grow_ok judges it
                # against the runaway ceiling instead, like codec conversions.
                p.grow_ok = True
                why = ("these signs would make Plex redraw the video every time you "
                       "watch; painting them in once here fixes that for good"
                       if is_image else
                       "the video is being rebuilt anyway, so adding the signs "
                       "costs nothing extra")
                p.add("subtitle", f"paint the {variant.lower()} into the picture (track {real})", why,
                      "scale PGS to video" if (is_image and CONFIG["scalePgsToVideo"]) else "")
                if CONFIG["removeBurnedSub"]:
                    p.add("subtitle", f"remove subtitle {real}, now part of the picture",
                          "it is part of the picture now, so the separate track is not needed")
            else:
                # A NOTE, NOT AN ACTION. This describes what is deliberately
                # NOT being done, and p.add() sets needed=True - so "leave the
                # signs track alone" was marking the file as having outstanding
                # work, forever. 1,738 files in a 4,000-file sample claimed
                # work whose entire content was a decision to do nothing.
                p.notes.append(f"'{variant}' track {real} left soft — video is "
                               f"copy-eligible and burning would force a "
                               f"needless re-encode")

    if CONFIG["forceEngSubWhenNoEngAudio"] and not has_eng_audio:
        for i in p.keep_subs:
            s = subs[i]
            if (s.get("codec_name") or "").lower() in TEXT_SUB_CODECS \
               and i != p.burn_index \
               and not re.search(r"sign|song", _title(s), re.I):
                p.default_sub = i
                # ONLY IF IT IS NOT ALREADY DEFAULT. Asking for a flag the
                # track already carries is not work, and p.add() would mark
                # the file as needing a full rewrite to achieve nothing: 558
                # of 558 sampled files wanted this and every one was already
                # correct. The assignment above still stands so the flag is
                # re-asserted if some other change rewrites the file.
                if not _disp(s, "default"):
                    p.add("subtitle", f"show subtitle {i} automatically",
                          "the audio is not in English, so this subtitle needs to come "
                          "on by itself")
                break

    # ---- WHICH SUBTITLE APPEARS ON ITS OWN, WHEN YOU CAN HEAR THE FILM ----
    #
    # With English audio, a full-dialogue or SDH track flagged default means
    # subtitles show unasked on every play. 4,057 finished files were in that
    # state - The Mummy Returns opened with English subs over English speech,
    # Animal Farm with Danish. Nothing was broken by the rules; nothing in the
    # rules cleared a flag the release happened to ship, either.
    #
    # The wanted behaviour has two halves, and only one of them is "clear":
    #
    #   FORCED / signs & songs  -> this is exactly the track that SHOULD appear
    #        by itself: alien dialogue in an English film, a sign, a song. It
    #        gets forced+default so Plex shows it without being asked.
    #   dialogue and SDH        -> keepers, but silent until chosen. SDH stays
    #        in the file for a noisy room; it just stops being automatic.
    if CONFIG["autoShowForcedOnly"] and has_eng_audio and p.keep_subs:
        forced_pick = None
        for i in p.keep_subs:
            if i == p.burn_index:
                continue                    # painted in; it has no flags to set
            s = subs[i]
            is_forced = (_disp(s, "forced")
                         or SIGNS_TITLE_RE.search(_title(s)) is not None)
            # Text beats image for the auto-shown track: a flagged image sub is
            # the CPU-burn case this whole area exists to avoid.
            if is_forced and (s.get("codec_name") or "").lower() in TEXT_SUB_CODECS:
                forced_pick = i if forced_pick is None else forced_pick
        for i in p.keep_subs:
            if i == p.burn_index or i == forced_pick:
                continue
            s = subs[i]
            if (_disp(s, "default") or _disp(s, "forced")) and i not in p.clear_flags:
                p.clear_flags.append(i)
                p.add("subtitle",
                      f"stop subtitle {i} switching itself on"
                      + (f" ({_short(_title(s))})" if _title(s) else ""),
                      "this one was set to appear automatically; the audio is "
                      "English, so it stays in the list to pick when you want "
                      "it")
        if forced_pick is not None and not _disp(subs[forced_pick], "forced"):
            p.forced_sub = forced_pick
            p.add("subtitle", f"let subtitle {forced_pick} appear on its own",
                  "signs, songs and foreign lines are the one kind of subtitle "
                  "that should appear on its own")
        elif forced_pick is not None:
            p.forced_sub = forced_pick

    # ---- TEXT SUBTITLES GO BEFORE PICTURE SUBTITLES ------------------------
    #
    # The output track order IS p.keep_subs order - build_ffmpeg maps them in
    # this sequence - so this is the only place the order can be set.
    #
    # WHY IT MATTERS: Plex offers subtitles in file order, and when nothing
    # else distinguishes two candidates it takes the earlier one. A PGS track
    # sitting ahead of the SRT means the picture track is what gets picked,
    # which is the one that forces Plex to burn it in on the CPU at playback -
    # precisely the transcode the rest of these rules exist to avoid.
    #
    # WHY IT IS BEING ADDED NOW: the rule audit checks this invariant and has
    # been reporting subs/order on every run it happened to sample one. Nothing
    # in the rules reordered subtitle tracks, so the finding could never clear
    # - the Requeue button answered "nothing to do" and the row came back the
    # next night. It was the single most-reported finding in the audit's
    # history (6 of the 10 findings ever recorded).
    #
    # IT MOVES THE FEWEST TRACKS THAT SATISFY THE CHECK, AND ONLY WHEN THE
    # CHECK IS ALREADY FAILING.
    #
    # Two earlier versions of this were sorts, and both were far too eager:
    #
    #   "all text ahead of all picture"     218 files, 2.14 TB - it reordered
    #                                       forced and signs tracks the check
    #                                       says nothing about
    #   "all dialogue ahead of everything"  5,741 files, 8.64 TB - worse. It
    #                                       fires on files with NO picture subs
    #                                       at all, demoting the forced signs
    #                                       track that autoShowForcedOnly had
    #                                       just chosen, on 5,477 files whose
    #                                       ONLY planned work was this
    #
    # Both were the same mistake: expressing the fix as a total order over the
    # tracks when the rule is a single pairwise constraint - no dialogue track
    # after the first picture track. So this tests that constraint first, does
    # nothing if it holds, and when it fails lifts exactly the late dialogue
    # tracks to just in front of the first picture track. Everything else keeps
    # its position, which is the difference between 30 files and 5,741.
    #
    # Every index here is an ORIGINAL stream index, so the decisions made above
    # - which track was picked as forced, whose flags are being cleared -
    # travel with the track wherever it lands.
    if CONFIG["orderSubsTextFirst"] and len(p.keep_subs) > 1:
        def _is_img(i: int) -> bool:
            return (subs[i].get("codec_name") or "").lower() in IMAGE_SUB_CODECS

        def _is_dialogue(i: int) -> bool:
            s = subs[i]
            return ((s.get("codec_name") or "").lower() in TEXT_SUB_CODECS
                    and not _disp(s, "forced")
                    and not SIGNS_TITLE_RE.search(_title(s)))

        order = p.keep_subs
        first_img = next((n for n, i in enumerate(order) if _is_img(i)), None)
        # The violation: a dialogue track sitting after the first picture one.
        late = ([i for i in order[first_img + 1:] if _is_dialogue(i)]
                if first_img is not None else [])
        if late:
            rest = [i for i in order if i not in late]
            at = next(n for n, i in enumerate(rest) if _is_img(i))
            p.keep_subs = rest[:at] + late + rest[at:]
            p.add("subtitle",
                  f"move {len(late)} text dialogue subtitle(s) ahead of the "
                  f"picture ones",
                  "Plex picks the first matching subtitle, and picking a "
                  "picture track means it has to paint it into the video on "
                  "every play - putting the real text first stops that")

    # ---- audio with no language on it ------------------------------------
    #
    # A blank language tag is not neutral. Sonarr and Radarr fill in English
    # for it, players do the same, and from there a Japanese episode is
    # described as English audio everywhere downstream - including to the
    # language rules above, which are then working from a false premise.
    #
    # ONLY EVER FILLS IN A BLANK. A track that states a language is never
    # touched, however wrong it looks: an English dub of a foreign film is
    # correctly tagged English, and the audit sweep that preceded this found
    # 573 such files - REC, Drunken Master, Rumble in the Bronx - whose own
    # track titles said "Dub". Overwriting those would have been the bug.
    #
    # The evidence for the original language being right is the presence of
    # full dialogue subtitles: nobody subtitles speech they can understand.
    # Signs and SDH do not count, and a second audio track means the file is
    # dual-audio and the blank one is ambiguous.
    if _a("tag_untagged_audio", True):
        # EVERY untagged track, not just single-track files, and every track
        # position - a dual-audio release with one tagged and one blank is the
        # case most likely to be wrong, because the blank one is whatever the
        # tagged one is not.
        for ai, tr in enumerate(audio):
            if _lang_guess(tr):
                continue                       # already named; never overwrite

            # 1. THE MEASURED ANSWER, when one exists. audiolang listened to
            #    the track. Inference is a proxy for this; when the real thing
            #    is available there is no reason to prefer the proxy.
            heard = None
            if file_id:
                try:
                    from . import audiolang
                    heard = audiolang.for_file(file_id, ai)
                except Exception:              # noqa: BLE001
                    heard = None
            floor = max(0.0, min(0.99, _a("tag_untagged_min_conf", 60) / 100.0))
            if heard and heard.get("code") \
                    and float(heard.get("confidence") or 0) >= floor:
                code = heard["code"]
                p.audio_lang_tags[ai] = code
                p.add("audio", f"name the audio language as {code}",
                      f"the track carries no language tag, so Sonarr and most "
                      f"players call it English — nuarr listened to the audio "
                      f"and it is {code} "
                      f"({heard.get('confidence', 0):.0%} confident)")
                continue

            # 2. INFERENCE, only where it is safe. This is the pre-detection
            #    rule, kept for files the sweep has not reached yet. It needs
            #    dialogue subtitles as the signature of a subtitled original,
            #    which is why it MISSES raws with no subtitle track at all -
            #    the exact gap that detection closes. It also stays limited to
            #    single-track files: on a multi-track file the untagged one is
            #    as likely to be the dub as the original.
            if not orig_codes or len(audio) != 1:
                continue
            dialogue = [s for s in subs
                        if (s.get("codec_name") or "").lower() in TEXT_SUB_CODECS
                        and not _disp(s, "forced")
                        and not SIGNS_TITLE_RE.search(_title(s))
                        and not re.search(r"\bSDH\b|hearing", _title(s), re.I)]
            if dialogue:
                # The three-letter form, and the same one the keep-list uses,
                # so a file tagged by this cannot then fail the language rule
                # that reads it back.
                code = sorted(c for c in orig_codes if len(c) == 3)
                code = code[0] if code else sorted(orig_codes)[0]
                p.audio_lang_tags[ai] = code
                p.add("audio", f"name the audio language as {code}",
                      "the track carries no language tag at all, so Sonarr "
                      "and most players call it English — this is a title "
                      f"made in {orig_lang or code}, with full dialogue "
                      "subtitles, so the audio is almost certainly not")

    # ---- safety ----------------------------------------------------------
    if p.encode and CONFIG["trimToRealDuration"] and duration:
        p.add("metadata", f"keep the runtime at {(duration + CONFIG['trimBufferSec'])/60:.0f} minutes",
              "a stray timestamp at the end of some files can make the result "
              "claim to be hundreds of hours long; this stops that")

    return p
