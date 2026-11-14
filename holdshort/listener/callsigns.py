"""Resolve spoken callsigns against live traffic.

find(plain, live) scans a normalised transcript (lowercase, commas as separators) for
callsign-like phrases ("jazz six four six", "truck one and company", "frontier four one
niner five", "united 2384") and resolves each one to a live callsign by its digits,
using the telephony name only to break ties. A phrase that does not resolve is UNKNOWN,
so the caller knows the rules did not understand who was named.
"""

from __future__ import annotations

import re

from holdshort.listener.normalize import _DIGITS, UNKNOWN, normalize_callsign

# spoken telephony -> ICAO designator (plus the short forms used by older callsigns)
TELEPHONY_ICAO: dict[str, tuple[str, ...]] = {
    "jazz": ("JZA",), "frontier": ("FFT",), "united": ("UAL", "UA"),
    "air canada": ("ACA", "AC"), "delta": ("DAL", "DL"), "american": ("AAL",),
    "jetblue": ("JBU",), "jet blue": ("JBU",), "southwest": ("SWA",), "spirit": ("NKS",),
    "endeavor": ("EDV",), "republic": ("RPA",), "envoy": ("ENY",),
}
_TEL_RE = "|".join(sorted((re.escape(k) for k in TELEPHONY_ICAO), key=len, reverse=True))
_TOKEN_RE = re.compile(rf"\b(?:{_TEL_RE})\b|\w+|,")
_STATION_RE = re.compile(r"\b(?:(?:laguardia )?(?:tower|ground)|ops|the jet)\b")
# words that never stand in for a misheard digit
_STOP = {"laguardia", "tower", "ground", "cleared", "runway", "and", "company", "on",
         "at", "stop", "cross", "hold", "short", "to", "for", "the", "say", "request",
         "roger", "just", "there", "please", "holding", "position", "clear", "of", "we",
         "with", "you", "your", "intentions", "line", "up", "wait", "taxi", "via"}


def _digits(tok: str) -> str | None:
    if tok.isdigit():
        return tok
    return _DIGITS.get(tok)


def _is_truck(tok: str) -> bool:
    if tok == "truck":
        return True
    if abs(len(tok) - 5) > 1:
        return False
    # within one edit of "truck"
    a, b = tok, "truck"
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= 1


def _num_part(cs: str) -> str:
    m = re.search(r"\d+$", cs)
    return m.group(0) if m else ""


def _edit1(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    if abs(len(a) - len(b)) != 1:
        return False
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return any(t[:i] + t[i + 1:] == s for i in range(len(t)))


def _wild_eq(pattern: str, digits: str) -> bool:
    return len(pattern) == len(digits) and all(
        p in ("?", d) for p, d in zip(pattern, digits, strict=True))


def match_digits(pattern: str, prefixes: tuple[str, ...] | None, live: dict[str, str],
                 kind: str | None = "aircraft") -> str | None:
    """The unique live callsign whose digits equal ``pattern`` ('?' = misheard digit);
    the telephony prefix breaks ties; one wrong/missing digit only if unique."""
    pool = [cs for cs, k in live.items() if (kind is None or k == kind or k is None)
            and _num_part(cs) and not cs.startswith("TRUCK")]

    def pick(cands: list[str]) -> str | None:
        if prefixes:
            pref = [c for c in cands if c.startswith(prefixes)]
            if len(pref) == 1:
                return pref[0]
            if pref:
                return None
        return cands[0] if len(cands) == 1 else None

    exact = [cs for cs in pool if _wild_eq(pattern, _num_part(cs))]
    if exact:
        return pick(exact)
    if "?" in pattern:
        return None
    near = [cs for cs in pool if _edit1(pattern, _num_part(cs))]
    return pick(near) if near else None


def find(plain: str, live: dict[str, str], aliases: dict[str, str] | None = None
         ) -> list[tuple[int, int, str]]:
    """(start, end, callsign or UNKNOWN) for every callsign-like phrase, in order."""
    toks = [(m.start(), m.end(), m.group(0)) for m in _TOKEN_RE.finditer(plain)]
    spans: list[tuple[int, int, str]] = []
    i = 0
    while i < len(toks):
        s, e, w = toks[i]
        if _is_truck(w) or w == "ops":
            # vehicle: one number, never glued to a following callsign
            j = i + 1
            ds: list[tuple[int, str]] = []
            while j < len(toks) and _digits(toks[j][2]) is not None:
                ds.append((j, _digits(toks[j][2])))
                j += 1
            if not ds:
                i += 1
                continue
            base = "TRUCK" if w != "ops" else "OPS"
            full = base + "".join(d for _, d in ds)
            if full in live:
                take = len(ds)
            else:
                take = 1
            cs = base + "".join(d for _, d in ds[:take])
            end_j = ds[take - 1][0]
            end = toks[end_j][1]
            if plain[end:end + 12].startswith(" and company"):
                end += 12
            if cs not in live and live and normalize_callsign(cs, aliases) != cs:
                cs = UNKNOWN
            spans.append((s, end, cs))
            i = end_j + 1
            continue
        if w in TELEPHONY_ICAO or _digits(w) is not None:
            tel = w if w in TELEPHONY_ICAO else None
            j = i + 1 if tel else i
            pattern = ""
            wild = False
            last = j - 1
            while j < len(toks):
                t = toks[j][2]
                d = _digits(t)
                if d is not None:
                    pattern += d
                    last = j
                    j += 1
                    continue
                if (tel and not wild and pattern and t != "," and t not in _STOP
                        and len(t) <= 6):
                    nxt = toks[j + 1][2] if j + 1 < len(toks) else ","
                    if nxt == "," or _digits(nxt) is not None or nxt in _STOP:
                        pattern += "?"
                        wild = True
                        last = j
                        j += 1
                        continue
                break
            if not pattern:
                i += 1
                continue
            if not tel and len(pattern.replace("?", "")) < 3:
                i = j  # "runway four", "two miles": not a callsign
                continue
            if not tel:
                prev = toks[i - 1][2] if i else ""
                if prev in {"runway", "heading", "wind", "altitude", "flight", "level",
                            "squawk", "contact", "point", "mile", "miles"}:
                    i = j
                    continue
            prefixes = TELEPHONY_ICAO.get(tel) if tel else None
            cs = match_digits(pattern, prefixes, live) if live else None
            if cs is None and tel and "?" not in pattern:
                old = normalize_callsign(f"{tel} {pattern}", aliases)
                if old != UNKNOWN and (not live or old in live or
                                       old in (aliases or {}).values() or
                                       tel in ("air canada", "united", "delta")):
                    cs = old
            spans.append((s, toks[last][1], cs or UNKNOWN))
            i = last + 1
            continue
        i += 1
    for m in _STATION_RE.finditer(plain):
        if not any(a <= m.start() < b for a, b, _ in spans):
            spans.append((m.start(), m.end(), normalize_callsign(m.group(0), aliases)))
    spans.sort()
    return spans
