"""What to pick next, and where it goes.

Strategies:
  sort   every part to the bin its rules point to (see rules.py); parts without a bin stay
  clear  everything into the "all" bin (or "overflow"), no sorting
  kit    only the parts on the kit list (rule -> count) into the "kit" bin, then stop

Shared rules: only parts inside the working area; which kind (part number, else class) first
depends on run.order:
  census         the most numerous kind first, and finish it before starting the next. Every
                 kind started stays in that order: when one turns up again (dug out from under
                 others), the sorter goes back to it before carrying on.
  largest_group  the most numerous kind on each photo (it can switch back and forth)
  nearest        no order by kind
then parts with free space around them, the most confident, the nearest; a spot that failed too
often is left alone; when nothing can be picked the bed is shaken (a few times at most) to
spread clumps.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from .config import Machine
from .layout import Bin, Layout
from .parts import Part
from .rules import Decision, destination, match, part_number

STRATEGIES = {
    "sort": "Sort: every part to the bin its rules point to",
    "clear": "Clear: everything into one bin, no sorting",
    "kit": "Kit: pick only the parts on the kit list, then stop",
}


@dataclass
class Plan:
    kind: str  # pick | shake | done | stuck | wait_bin (only a bin change helps now)
    part: Part | None = None
    bin: Bin | None = None
    reason: str = ""
    kit_rule: str | None = None
    claim: str | None = None  # wild bin: claimed for this key once the drop succeeds
    note: str = ""  # a change of kind worth telling (census order)


@dataclass
class Spot:
    xy: tuple[float, float]
    cls: str
    fails: int = 1


@dataclass
class Planner:
    layout: Layout
    machine: Machine
    strategy: str = "sort"
    kit_left: dict[str, int] = field(default_factory=dict)
    failed: list[Spot] = field(default_factory=list)
    shakes_in_row: int = 0
    sorted_count: dict[int, int] = field(default_factory=dict)  # bin marker -> parts dropped in
    failed_picks: int = 0
    claims: dict[int, str] = field(default_factory=dict)  # wild bin marker -> what it collects
    started: list[str] = field(default_factory=list)  # census order: kinds begun, in order
    full: set[int] = field(default_factory=set)  # bins too full to take more
    needs: set[tuple[str, object]] = field(default_factory=set)  # bin changes the plate needs
    current: str | None = None  # the kind being picked now

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r} (one of {', '.join(STRATEGIES)})")
        if self.strategy == "kit" and not self.kit_left:
            self.kit_left = {k: int(v) for k, v in self.machine.run.kit.items()}

    # ------------------------------------------------------------------ decisions
    def _decide(self, p: Part) -> tuple[Decision | None, str | None]:
        if self.strategy == "sort":
            run = self.machine.run
            ask = run.when_no_bin == "ask"
            return destination(p, self.layout, run.min_score, self.claims, self.full, ask), None
        if self.strategy == "clear":
            b = next((b for b in self.layout.bins if b.role == "all"), None) or next(
                (b for b in self.layout.bins if b.role == "overflow"), None
            )
            return Decision(b, "clear" if b else "no bin with role all/overflow"), None
        # kit
        kit_bin = next((b for b in self.layout.bins if b.role == "kit"), None)
        for rule, left in self.kit_left.items():
            if left <= 0:
                continue
            m = match(rule, p)
            if m == "yes":
                return Decision(
                    kit_bin, f"kit: {rule}" if kit_bin else "no bin with role kit"
                ), rule
            if m == "needs_length":
                return Decision(None, f"kit: {rule} needs its length", waiting=True), rule
        return None, None

    def _blocked(self, p: Part) -> bool:
        r = self.machine.run.failed_radius_mm
        return any(
            s.cls == p.cls
            and s.fails >= self.machine.run.max_failed_picks
            and _dist(s.xy, p.bed_xy) <= r
            for s in self.failed
        )

    def next(self, parts: list[Part], here: tuple[float, float] = (0.0, 0.0)) -> Plan:
        run = self.machine.run
        if self.strategy == "kit" and all(v <= 0 for v in self.kit_left.values()):
            return Plan("done", reason="kit complete")
        cands = [p for p in parts if self.layout.in_work(p.bed_xy, run.work_margin_mm)]
        if not cands:
            return Plan("done", reason="the working area is empty")
        options: list[tuple[Part, Decision, str | None]] = []
        waiting: list[tuple[Part, str | None]] = []
        no_bin: list[Part] = []
        for_bin: list[Part] = []  # waiting for a bin change
        self.needs = set()
        blocked = 0
        for p in cands:
            if self._blocked(p):
                blocked += 1
                continue
            dec, kit_rule = self._decide(p)
            if dec is None:
                continue
            if dec.needs is not None:
                self.needs.add(dec.needs)
                for_bin.append(p)
            elif dec.waiting:
                waiting.append((p, kit_rule))
            elif dec.bin is None:
                no_bin.append(p)
            else:
                options.append((p, dec, kit_rule))
        clearance = self.machine.picker.clearance_mm
        if options:
            population = Counter(_kind(p) for p in cands)
            order = self.machine.run.order
            note = ""
            if order == "census":
                kind, note = self._census_kind({_kind(o[0]) for o in options}, population)
                options = [o for o in options if _kind(o[0]) == kind]
            by_group = order == "largest_group"
            p, dec, rule = min(
                options,
                key=lambda o: (
                    -population[_kind(o[0])] if by_group else 0,
                    o[0].gap_mm < clearance,
                    -o[0].score,
                    _dist(o[0].bed_xy, here),
                ),
            )
            return Plan("pick", p, dec.bin, dec.reason, rule, dec.claim, note)
        if for_bin:
            return Plan("wait_bin", reason=f"{len(for_bin)} part(s) wait for a bin change")
        stuck_parts = len(waiting) + blocked
        if stuck_parts:
            if run.shake.enabled and self.shakes_in_row < run.shake.max_in_a_row:
                self.shakes_in_row += 1
                return Plan("shake", reason=f"{stuck_parts} part(s) cannot be picked yet: shaking")
            unsure = next((b for b in self.layout.bins if b.role == "unsure"), None)
            if waiting and unsure is not None and self.strategy == "sort":
                p = max((w[0] for w in waiting), key=lambda q: q.gap_mm)
                return Plan("pick", p, unsure, "length still unknown after shaking: unsure bin")
            return Plan(
                "stuck", reason=f"{stuck_parts} part(s) need a hand (moved or spread by hand)"
            )
        if no_bin:
            return Plan("done", reason=f"{len(no_bin)} part(s) left: no bin holds them")
        return Plan("done", reason="nothing left for this strategy")

    def _census_kind(self, pickable: set[str], population: Counter) -> tuple[str, str]:
        """The kind to pick now (census order) and a note when it changes: the first kind
        already started that can be picked again, else the most numerous new kind."""
        kind = next((k for k in self.started if k in pickable), None)
        note = ""
        if kind is None:
            kind = max(pickable, key=lambda k: (population[k], k))
            self.started.append(kind)
            note = f"next: {kind} ({population[kind]} on the plate)"
        elif kind != self.current and self.current is not None:
            if self.started.index(kind) < self.started.index(self.current):
                note = f"back to {kind}: {population[kind]} turned up again"
            else:
                note = f"on with {kind} ({population[kind]} left)"
        self.current = kind
        return kind, note

    def needed_bins(self, parts: list[Part]) -> set[int]:
        """Bins that parts in the working area still go to (or may go to, once a missing length
        is measured). A filled bin outside this set is complete for now."""
        need: set[int] = set()
        margin = self.machine.run.work_margin_mm
        for p in parts:
            if not self.layout.in_work(p.bed_xy, margin):
                continue
            dec, _ = self._decide(p)
            if dec is None:
                continue
            if dec.bin is not None:
                need.add(dec.bin.marker)
            elif dec.waiting:
                cls = p.cls.upper()
                for b in self.layout.bins:
                    key = self.claims.get(b.marker, "").upper()
                    if (
                        key == cls
                        or key.startswith(cls + "X")
                        or b.role == "unsure"
                        or any(r.strip().upper().startswith(cls) for r in b.holds)
                    ):
                        need.add(b.marker)
        return need

    # ------------------------------------------------------------------ feedback
    def report(self, plan: Plan, parts_now: list[Part]) -> bool:
        """After the next photo: did `plan` (a pick) work? Updates the counters."""
        if plan.kind == "shake":
            self.failed.clear()  # the parts have moved
            return True
        if plan.kind != "pick" or plan.part is None:
            return True
        r = self.machine.run.failed_radius_mm
        p = plan.part
        still = any(q.cls == p.cls and _dist(q.bed_xy, p.bed_xy) <= r for q in parts_now)
        if still:
            self.failed_picks += 1
            spot = next(
                (s for s in self.failed if s.cls == p.cls and _dist(s.xy, p.bed_xy) <= r), None
            )
            if spot is None:
                self.failed.append(Spot(p.bed_xy, p.cls))
            else:
                spot.fails += 1
            return False
        self.shakes_in_row = 0
        if plan.bin is not None:
            self.sorted_count[plan.bin.marker] = self.sorted_count.get(plan.bin.marker, 0) + 1
            if plan.claim is not None:
                self.claims[plan.bin.marker] = plan.claim
        if plan.kit_rule is not None and plan.kit_rule in self.kit_left:
            self.kit_left[plan.kit_rule] -= 1
        return True


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _kind(p: Part) -> str:
    """What counts as one group for run.order: the part number, else the class."""
    return part_number(p) or p.cls
