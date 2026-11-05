"""Which bin a part goes to.

Each bin holds a list of rules, matched against the part's names, most specific first:
  DIN912_M4x16   the part number (class + measured length)
  DIN912_M4      the class (any length)
  DIN912         the family (any size); wildcards work too: DIN912_M*, *_M3, *
Special bins by role: "unknown" (UNKNOWN class), "unsure" (low detection score, or a length that
cannot be measured yet), "overflow" (no rule matched), "all" (everything), "kit" (kit strategy),
"wild" (no fixed contents: the first part of a kind no other bin takes claims a free wild bin,
which then collects that kind - part number, class or family, see Layout.wild_by - until it is
released).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from .layout import Bin, Layout
from .parts import Part

UNKNOWN = "UNKNOWN"
_SIZE_SUFFIX = re.compile(r"_(M\d+(?:\.\d+)?|\d{4})$")


def family(cls: str) -> str:
    """DIN912_M4 -> DIN912, STANDOFF_MF_M3 -> STANDOFF_MF, BRACKET_2020 -> BRACKET."""
    return _SIZE_SUFFIX.sub("", cls)


def part_number(p: Part) -> str | None:
    if p.length_mm is None:
        return None
    return f"{p.cls}x{p.length_mm:g}"


def names(p: Part) -> list[str]:
    out = [n for n in (part_number(p), p.cls, family(p.cls)) if n]
    if p.unknown:
        out.append(UNKNOWN)
    return out


def specificity(rule: str) -> int:
    return sum(ch not in "*?[]" for ch in rule)


def match(rule: str, p: Part) -> str:
    """ "yes", "needs_length" (it would match a part number once the length is known) or "no"."""
    r = rule.strip().upper()
    if not r:
        return "no"
    if any(fnmatch.fnmatchcase(n.upper(), r) for n in names(p)):
        return "yes"
    if p.length_mm is None and r.startswith(p.cls.upper() + "X"):
        return "needs_length"
    return "no"


@dataclass
class Decision:
    bin: Bin | None
    reason: str
    waiting: bool = False  # a better bin needs the part's length, which is not measured yet
    claim: str | None = None  # a free wild bin is claimed for this key when the drop succeeds
    needs: tuple[str, object] | None = None  # ("full", marker) | ("new", key): a bin change


def _role(layout: Layout, role: str) -> Bin | None:
    return next((b for b in layout.bins if b.role == role), None)


def wild_key(p: Part, wild_by: str) -> str | None:
    """What a wild bin collects for this part; None = its length is needed but not measured."""
    if p.unknown:
        return UNKNOWN
    if wild_by == "family":
        return family(p.cls)
    if wild_by == "part" and p.has_length:
        return part_number(p)
    return p.cls


def _wild(
    p: Part,
    layout: Layout,
    claims: Mapping[int, str],
    full: Collection[int] = (),
    ask: bool = False,
) -> Decision | None:
    """A wild bin for the part: the one collecting its kind, else a free one. A full bin is
    skipped (a free one carries on with the same kind); `ask`: with none left, the part waits
    for a bin change instead of going to overflow."""
    wild = [b for b in layout.bins if b.role == "wild"]
    if not wild:
        return None
    key = wild_key(p, layout.wild_by)
    if key is None:
        return Decision(
            None, "waiting for a length measurement (wild bins sort by part number)", waiting=True
        )
    full_one = None
    for b in wild:
        if claims.get(b.marker) == key:
            if b.marker not in full:
                return Decision(b, f"wild bin {b.marker} collects {key}")
            full_one = full_one or b
    free = next((b for b in wild if b.marker not in claims), None)
    if free is not None:
        more = " (the other one is full)" if full_one else ""
        return Decision(free, f"new kind {key}: wild bin {free.marker}{more}", claim=key)
    if full_one is not None:
        return Decision(None, f"bin {full_one.marker} is full", needs=("full", full_one.marker))
    if ask:
        return Decision(None, f"new kind {key}: no free bin", needs=("new", key))
    return None


def destination(
    p: Part,
    layout: Layout,
    min_score: float,
    claims: Mapping[int, str] | None = None,
    full: Collection[int] = (),
    ask: bool = False,
) -> Decision:
    """Where the part goes. `full`: bins too full to take more; `ask`: a new kind without a
    free wild bin waits for a bin change (else it goes to overflow)."""
    claims = claims or {}
    dec = _destination(p, layout, min_score, claims, full, ask)
    if dec.bin is not None and dec.bin.marker in full:
        return Decision(None, f"bin {dec.bin.marker} is full", needs=("full", dec.bin.marker))
    return dec


def _destination(
    p: Part,
    layout: Layout,
    min_score: float,
    claims: Mapping[int, str],
    full: Collection[int],
    ask: bool,
) -> Decision:
    if p.unknown:
        b = _role(layout, "unknown") or _best_rule(p, layout)[0]
        if b is not None:
            return Decision(b, "unknown part")
        return _wild(p, layout, claims, full, ask) or _last_resort(
            layout, "no bin for unknown parts"
        )
    if p.score < min_score:
        b = _role(layout, "unsure")
        if b is not None:
            return Decision(b, f"unsure ({p.score:.2f})")
    best, waiting = _best_rule(p, layout)
    if waiting:
        return Decision(None, "waiting for a length measurement", waiting=True)
    if best is not None:
        return Decision(best, f"rule of bin {best.marker}")
    return _wild(p, layout, claims, full, ask) or _last_resort(layout, "no bin holds it")


def _last_resort(layout: Layout, why: str) -> Decision:
    b = _role(layout, "all") or _role(layout, "overflow")
    return Decision(b, "overflow" if b else why)


def _best_rule(p: Part, layout: Layout) -> tuple[Bin | None, bool]:
    best: tuple[int, Bin] | None = None
    best_pending = -1
    for b in layout.bins:
        if b.role != "sort":
            continue
        for rule in b.holds:
            m = match(rule, p)
            s = specificity(rule)
            if m == "yes" and (best is None or s > best[0]):
                best = (s, b)
            elif m == "needs_length":
                best_pending = max(best_pending, s)
    if best_pending >= 0 and (best is None or best_pending > best[0]):
        return None, True
    return (best[1] if best else None), False
