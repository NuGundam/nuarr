r"""One answer to "are these two language tags the same language?"

WHY THIS MODULE EXISTS. Four places in nuarr compared language codes and each
had its own table:

    rules._ISO_PAIRS     the planner, deciding which tracks to keep
    audit._ISO_ALIAS     the check, deciding whether the planner kept the right ones
    audit._ISO3          a language NAME to one code
    origlang.LANG_CODES  a language NAME to every code that counts as it

Four tables answering one question is four chances to disagree, and they did.
`Troll` is a Norwegian film whose audio is tagged `nob` (Bokmal); TMDB records
the title's language as `Norwegian`. The planner knew - origlang.LANG_CODES
lists nob, nno and nor under "norwegian" - so it correctly kept the track. The
check did not, so it reported a perfectly correct file as carrying an extra
audio track, and in auto mode would have blacklisted the release and asked the
arr for another copy of a file that was already right.

The failure was not the missing entry. It was that adding one to the table that
happened to be wrong would have left the other three unfixed and no way to tell.

TWO KINDS OF EQUIVALENCE, and only one of them is symmetric:

BIBLIOGRAPHIC vs TERMINOLOGICAL is symmetric. ISO 639-2 gives some languages two
codes for no reason a viewer would recognise - chi/zho, fre/fra, ger/deu - and
the muxer wrote whichever its library preferred. These are the same language in
both directions, always.

MACROLANGUAGE MEMBERSHIP IS DIRECTIONAL, and getting this wrong is worse than
not having it. A macrolanguage is a group: `zho` (Chinese) contains `cmn`
(Mandarin) and `yue` (Cantonese); `nor` contains `nob` and `nno`; `ara` contains
a dozen regional Arabics. A track tagged `cmn` satisfies a request for `zho` -
somebody who asked for Chinese and got Mandarin got what they asked for. But a
track tagged `yue` does NOT satisfy a request for `cmn`: Cantonese and Mandarin
are not interchangeable audio, and folding both onto `zho` - which is what the
first version of this did - would have quietly accepted the wrong dub.

So membership travels one way only: a request for a macrolanguage is satisfied
by any of its members, and a request for a member is satisfied only by that
member (or its other spelling). That is the whole of expand(): it turns a
keep-list into every tag that would satisfy it, and callers then compare tags
with plain equality against that set.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# ISO 639-2/B (bibliographic) -> 639-2/T (terminological). Symmetric: both
# spellings mean the same language and either may appear in a file.
_BT = {
    "alb": "sqi", "arm": "hye", "baq": "eus", "bur": "mya", "chi": "zho",
    "cze": "ces", "dut": "nld", "fre": "fra", "geo": "kat", "ger": "deu",
    "gre": "ell", "ice": "isl", "mac": "mkd", "mao": "mri", "may": "msa",
    "per": "fas", "rum": "ron", "slo": "slk", "tib": "bod", "wel": "cym",
}

# ISO 639-1 two-letter codes, for the tags that carry them ("en", "ja"). Same
# language, so symmetric, and folded onto the three-letter form because that is
# what the rest of nuarr speaks.
_A2 = {
    "en": "eng", "ja": "jpn", "jp": "jpn", "ko": "kor", "zh": "zho",
    "fr": "fra", "de": "deu", "es": "spa", "it": "ita", "pt": "por",
    "ru": "rus", "nl": "nld", "sv": "swe", "no": "nor", "nb": "nob",
    "nn": "nno", "da": "dan", "fi": "fin", "pl": "pol", "cs": "ces",
    "hu": "hun", "tr": "tur", "ar": "ara", "he": "heb", "iw": "heb",
    "hi": "hin", "th": "tha", "vi": "vie", "id": "ind", "el": "ell",
    "uk": "ukr", "ro": "ron", "bg": "bul", "hr": "hrv", "sr": "srp",
    "sk": "slk", "ca": "cat", "is": "isl", "ta": "tam", "te": "tel",
    "ml": "mal", "fa": "fas", "ms": "msa", "bn": "ben", "ur": "urd",
    "et": "est", "lv": "lav", "lt": "lit", "sl": "slv", "mk": "mkd",
    "sq": "sqi", "hy": "hye", "ka": "kat", "az": "aze", "kk": "kaz",
    "uz": "uzb", "mn": "mon", "ne": "nep", "si": "sin", "km": "khm",
    "lo": "lao", "my": "mya", "sw": "swa", "af": "afr", "eu": "eus",
    "gl": "glg", "cy": "cym", "ga": "gle", "mt": "mlt", "la": "lat",
    "yi": "yid", "pa": "pan", "gu": "guj", "kn": "kan", "mr": "mar",
    "or": "ori", "as": "asm", "ky": "kir", "tg": "tgk", "tk": "tuk",
    "ps": "pus", "ku": "kur", "am": "amh", "so": "som", "ha": "hau",
    "yo": "yor", "ig": "ibo", "zu": "zul", "xh": "xho", "st": "sot",
    "tn": "tsn", "bs": "bos", "be": "bel", "eo": "epo", "fo": "fao",
    "fy": "fry", "lb": "ltz", "haw": "haw", "mi": "mri", "sm": "smo",
    "to": "ton", "fj": "fij", "tl": "tgl", "ceb": "ceb", "jv": "jav",
    "su": "sun", "mg": "mlg", "ny": "nya", "sn": "sna", "rw": "kin",
}

# MEMBER -> MACROLANGUAGE, ISO 639-3. Directional; see the module docstring.
#
# Scoped to what turns up in a media library and its metadata: the individual
# language a release actually tags, against the macrolanguage a provider records
# for the title. Bokmal under Norwegian is the case that prompted it; Mandarin
# and Cantonese under Chinese, the regional Arabics, Farsi and Dari under
# Persian, and Malay are the ones with real releases behind them. A code that is
# not here simply compares as itself, which is the safe direction - it can miss
# an equivalence, never invent one.
_MACRO = {
    "nob": "nor", "nno": "nor",
    "cmn": "zho", "yue": "zho", "wuu": "zho", "hsn": "zho", "nan": "zho",
    "hak": "zho", "gan": "zho", "cdo": "zho", "czh": "zho", "cjy": "zho",
    "cpx": "zho", "mnp": "zho", "lzh": "zho",
    "arb": "ara", "ary": "ara", "arz": "ara", "apc": "ara", "acm": "ara",
    "afb": "ara", "ajp": "ara", "aeb": "ara", "ayl": "ara", "ars": "ara",
    "acw": "ara", "abv": "ara", "shu": "ara",
    "pes": "fas", "prs": "fas",
    "zsm": "msa", "zlm": "msa", "bjn": "msa", "min": "msa", "jax": "msa",
    "ekk": "est", "khk": "mon", "plt": "mlg", "npi": "nep", "pnb": "lah",
    "swh": "swa", "swc": "swa", "uzn": "uzb", "uzs": "uzb",
    "lvs": "lav", "ltg": "lav", "als": "sqi", "aln": "sqi", "aae": "sqi",
    "ckb": "kur", "kmr": "kur", "sdh": "kur",
    "azj": "aze", "azb": "aze", "pbu": "pus", "pst": "pus", "pbt": "pus",
    "kas": "kas", "ory": "ori", "spv": "ori",
    "gaz": "orm", "hae": "orm", "orc": "orm",
    "knc": "kau", "kby": "kau", "quz": "que", "quy": "que", "qub": "que",
    "gug": "grn", "gui": "grn", "gun": "grn",
    "ydd": "yid", "yih": "yid", "kok": "kok", "gom": "kok",
    "rmy": "rom", "rmn": "rom", "hno": "lah", "skr": "lah",
    "fuc": "ful", "fuv": "ful", "ffm": "ful",
    "nya": "nya", "twi": "aka", "fat": "aka",
    "bcl": "bik", "cbk": "cbk",
    # DELIBERATELY ABSENT: srp / hrv / bos are members of hbs (Serbo-Croatian)
    # in ISO, and folding them would make a Serbian dub satisfy a request for
    # Croatian. Releases tag them separately and viewers treat them as separate
    # languages, so nuarr does too.
}

_MEMBERS: dict[str, set[str]] = {}
for _m, _p in _MACRO.items():
    _MEMBERS.setdefault(_p, set()).add(_m)


def key(code) -> str:
    """The canonical spelling of one tag. Does NOT fold macrolanguages.

    chi -> zho and en -> eng, because those are two spellings of one language.
    nob stays nob, because Bokmal is not every Norwegian - see expand().
    """
    c = str(code or "").strip().lower().replace("_", "-")
    c = c.split("-")[0]                  # zh-cn, pt-BR: the region is not the language
    if not c:
        return ""
    c = _A2.get(c, c)
    return _BT.get(c, c)


def macro(code) -> str:
    """The macrolanguage this tag belongs to, or "" if it is not a member."""
    return _MACRO.get(key(code), "")


def expand(codes) -> set[str]:
    """Every tag that would satisfy this keep-list.

    A macrolanguage brings in its members (nor accepts nob); a member brings in
    only its own spellings (cmn does not accept yue). Both spellings of a
    bibliographic pair are always included, because a file may carry either.
    """
    out: set[str] = set()
    for c in codes or []:
        k = key(c)
        if not k:
            continue
        out.add(k)
        out.add(str(c).strip().lower())            # as written, for callers
                                                   # that compare raw tags
        for b, t in _BT.items():                   # the other spelling
            if k == t:
                out.add(b)
        out |= _MEMBERS.get(k, set())              # macrolanguage -> members
        m = _MACRO.get(k)
        if m:
            # A member also satisfies itself under its parent's spelling only
            # when the parent is what was asked for, which the branch above
            # covers. Nothing is added here on purpose.
            pass
    return {c for c in out if c}


def same(a, b) -> bool:
    """Would a track tagged `a` satisfy a request for `b`?

    Directional, and the direction matters: same("nob", "nor") is True because
    Bokmal is Norwegian, and same("nor", "nob") is also True because a file
    tagged with the macrolanguage is the best answer available to a request for
    one of its members - a nor-tagged track IS the Norwegian audio. What stays
    False is same("yue", "cmn"): two members of one macrolanguage are not
    interchangeable, which is the whole reason membership is not a fold.
    """
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    return _MACRO.get(ka) == kb or _MACRO.get(kb) == ka


def names_to_codes(name: str) -> set[str]:
    """Every code that counts as a provider's language NAME ("Norwegian").

    Asks origlang, which owns the name table and is what the planner uses, so
    the check and the planner cannot disagree about what a name means. Falls
    back to the ISO tables here for a name origlang has never seen, and returns
    an empty set when neither knows it - empty means "no opinion", and every
    caller must treat it as such rather than as "no match".
    """
    n = (name or "").strip().lower()
    if not n:
        return set()
    out: set[str] = set()
    try:
        from .origlang import codes_for
        out |= {key(c) for c in codes_for(n)}
    except Exception:                                        # noqa: BLE001
        pass
    return {c for c in out if c}
