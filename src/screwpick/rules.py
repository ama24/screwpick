"""Which bin a part goes to.

Each bin holds a list of rules, matched against the part's names, most specific first:
  DIN912_M4x16   the part number (class + measured length)
  DIN912_M4      the class (any length)
  DIN912         the family (any size); wildcards work too: DIN912_M*, *_M3, *
Special bins by role: "unknown" (UNKNOWN class), "unsure" (low detection score, or a length that
cannot be measured yet), "overflow" (no rule matched), "all" (everything), "kit" (kit strategy).
"""

from __future__ import annotations

import fnmatch
import re
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


def _role(layout: Layout, role: str) -> Bin | None:
    return next((b for b in layout.bins if b.role == role), None)


def destination(p: Part, layout: Layout, min_score: float) -> Decision:
    if p.unknown:
        b = _role(layout, "unknown") or _best_rule(p, layout)[0] or _role(layout, "overflow")
        return Decision(b, "unknown part" if b else "no bin for unknown parts")
    if p.score < min_score:
        b = _role(layout, "unsure")
        if b is not None:
            return Decision(b, f"unsure ({p.score:.2f})")
    best, waiting = _best_rule(p, layout)
    if waiting:
        return Decision(None, "waiting for a length measurement", waiting=True)
    if best is not None:
        return Decision(best, f"rule of bin {best.marker}")
    b = _role(layout, "all") or _role(layout, "overflow")
    return Decision(b, "overflow" if b else "no bin holds it")


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
