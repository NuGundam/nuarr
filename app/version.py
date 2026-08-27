r"""The one place nuarr's version is written down.

WHY A MODULE AND NOT A STRING IN THREE FILES. The version has to appear in the
UI header, in the settings panel, in the installer's bundle.json, in the
filename of the built exe, and in the comparison against whatever GitHub is
offering. Five readers is exactly the shape that drifts: someone bumps the one
in web.py, the installer keeps shipping the old number, and the update check
starts offering an upgrade to a version already installed. So it lives here,
and the build reads it from here rather than being told.

SEMVER, and the comparison is done on the parsed tuple rather than the string.
"1.10.0" < "1.9.0" is true as strings and false as versions, which is the
classic way an update checker goes quiet for months at exactly the point the
project gets busy enough to reach a double-digit minor.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- the version
# major - a change that needs a human to do something (config format, a
#         setting that moved, anything that could surprise an existing install)
# minor - a new capability
# patch - a fix to something that was already meant to work
VERSION = "1.0.1"

# Filled in when a bundle is built, so a running install can say not just what
# it is but when it was made. Left empty in a source checkout on purpose - an
# invented date would be worse than a blank.
BUILD_DATE = ""

# Where to look for newer versions. Owner/name only, no URL, because the API
# path and the browse path differ and building both from one field is less to
# get wrong than storing two.
#
# The OFFICIAL repository is the default: a fresh install should learn about
# releases without anyone finding a settings page first, or the update
# machinery only ever works for the person who built it. Overridable in
# Settings for anyone following a fork, and "off" there disables checks
# entirely - the default must not remove the ability to say no.
DEFAULT_REPO = "NuGundam/nuarr"

_SEMVER = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?\s*$")


def parse(s: str) -> tuple[int, int, int] | None:
    r"""'v1.2.3' -> (1, 2, 3). None when it is not a version at all.

    Tolerates the leading v because GitHub tags almost always carry one and
    releases almost always do not, and a checker that treats "v1.0.1" and
    "1.0.1" as different versions will offer you an update to what you are
    already running, forever.
    """
    if not s:
        return None
    m = _SEMVER.match(str(s))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(candidate: str, than: str = VERSION) -> bool:
    """True only when `candidate` parses AND is strictly greater."""
    a, b = parse(candidate), parse(than)
    if a is None or b is None:
        # UNPARSEABLE IS NOT NEWER. A malformed tag - someone pushes "latest"
        # or "release-final" - must not read as an upgrade, or the badge lights
        # up permanently against something that can never be installed.
        return False
    return a > b


def info() -> dict:
    """Everything a caller needs to describe this build."""
    return {
        "version": VERSION,
        "build_date": BUILD_DATE,
        "parsed": parse(VERSION),
    }
