r"""Sonarr / Radarr "Import Using Script" -> nuarr chooses the disk.

The arr runs this with the source and destination paths. It asks nuarr to copy
the file onto the emptiest pool disk nobody is watching (see app/arrimport.py).
Whatever happens, the arr gets an answer it can act on:

  [MoveStatus]MoveComplete  nuarr put the file at the destination
  [MoveStatus]DeferMove     nuarr stepped aside - the arr imports it itself

It always exits 0: a non-zero exit fails the import outright, and nothing here
is worth failing an import over. Sidecar files next to the source (subtitles
and the like) are listed as [ExtraFile] either way, because the arr takes its
extras list from this script's output once script import is on.
"""
import json
import os
import sys
import urllib.request

NUARR = "http://127.0.0.1:8770/api/arrimport"
EXTRA = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".nfo"}


def env(name: str) -> str:
    for p in ("Sonarr_", "Radarr_", "sonarr_", "radarr_"):
        v = os.environ.get(p + name)
        if v:
            return v
    return ""


def main() -> None:
    if env("EventType").lower() == "test":
        return
    src = sys.argv[1] if len(sys.argv) > 1 else env("SourcePath")
    dst = sys.argv[2] if len(sys.argv) > 2 else env("DestinationPath")
    arr = "Sonarr" if os.environ.get("Sonarr_SourcePath") or os.environ.get("sonarr_sourcepath") \
        else "Radarr"
    stem = os.path.splitext(os.path.basename(src))[0].lower()
    try:
        for f in os.listdir(os.path.dirname(src)):
            p = os.path.join(os.path.dirname(src), f)
            if (os.path.isfile(p) and os.path.splitext(f)[1].lower() in EXTRA
                    and f.lower().startswith(stem)):
                print(f"[ExtraFile]{p}")
    except OSError:
        pass
    status = "DeferMove"
    try:
        body = json.dumps({"src": src, "dst": dst, "mode": env("TransferMode"),
                           "arr": arr}).encode()
        req = urllib.request.Request(NUARR, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=4 * 3600))
        if r.get("ok") and os.path.exists(dst):
            status = "MoveComplete"
    except Exception:
        status = "DeferMove"
    print(f"[MoveStatus]{status}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("[MoveStatus]DeferMove")
    sys.exit(0)
