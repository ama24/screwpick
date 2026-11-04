from __future__ import annotations

import numpy as np

from screwpick.parts import Part
from screwpick.rules import destination, family, match, part_number
from screwpick.strategies import Planner


def P(cls, length=None, score=0.9, xy=(110.0, 110.0), gap=10.0, unknown=False, pid=1):  # noqa: N802
    p = Part(pid, cls, score, xy, 0.0, np.zeros((0, 2)), 4.0, length, length is not None, unknown)
    p.bed_xy = xy
    p.gap_mm = gap
    return p


def test_family_and_part_number():
    assert family("DIN912_M4") == "DIN912"
    assert family("STANDOFF_MF_M2.5") == "STANDOFF_MF"
    assert family("BRACKET_2020") == "BRACKET"
    assert family("TNUT_DROP_S6_M4") == "TNUT_DROP_S6"
    assert part_number(P("DIN912_M4", 16.0)) == "DIN912_M4x16"
    assert part_number(P("DIN934_M4")) is None


def test_match():
    assert match("din912_m4", P("DIN912_M4", 16.0)) == "yes"
    assert match("DIN912_M4x16", P("DIN912_M4", 16.0)) == "yes"
    assert match("DIN912_M4x16", P("DIN912_M4", 20.0)) == "no"
    assert match("DIN912_M4x16", P("DIN912_M4")) == "needs_length"
    assert match("DIN912*", P("DIN912_M3", 8.0)) == "yes"
    assert match("DIN912", P("DIN912_M3", 8.0)) == "yes"  # the family
    assert match("UNKNOWN", P("UNKNOWN", unknown=True)) == "yes"


def test_destination_most_specific_rule_wins(layout):
    m = {b.marker: b for b in layout.bins}
    assert destination(P("DIN912_M4", 16.0), layout, 0.5).bin is m[104]  # part number
    assert destination(P("DIN912_M4", 20.0), layout, 0.5).bin is m[105]  # class
    assert destination(P("DIN934_M4"), layout, 0.5).bin is m[106]
    assert destination(P("ISO7380_M3", 8.0), layout, 0.5).bin is m[107]  # "*"
    assert destination(P("UNKNOWN", unknown=True), layout, 0.5).bin is m[100]  # role unknown
    assert destination(P("DIN912_M3", 8.0, score=0.2), layout, 0.5).bin is m[101]  # unsure
    d = destination(P("DIN912_M4"), layout, 0.5)  # M4x16 rule needs the length
    assert d.bin is None and d.waiting


def test_destination_overflow_and_no_bin(layout):
    layout.bins[7].holds = []
    for b in layout.bins:  # no wild bins here (see test_wild_auth for those)
        if b.role == "wild":
            b.role = "sort"
    m = {b.marker: b for b in layout.bins}
    assert destination(P("ISO7380_M3", 8.0), layout, 0.5).bin is m[102]  # overflow
    layout.bins[2].role = "sort"
    d = destination(P("ISO7380_M3", 8.0), layout, 0.5)
    assert d.bin is None and not d.waiting


def test_planner_prefers_free_parts_then_score_then_distance(machine, layout):
    pl = Planner(layout, machine, "sort")
    parts = [
        P("DIN912_M3", 8.0, score=0.99, xy=(100, 100), gap=0.0, pid=1),  # touching
        P("DIN912_M3", 8.0, score=0.80, xy=(150, 150), gap=5.0, pid=2),
        P("DIN912_M3", 8.0, score=0.80, xy=(80, 80), gap=5.0, pid=3),
        P("DIN912_M3", 8.0, score=0.95, xy=(20, 20), gap=9.0, pid=4),  # in a bin: ignored
    ]
    plan = pl.next(parts, here=(60.0, 60.0))
    assert plan.kind == "pick" and plan.part.id == 3 and plan.bin.marker == 103


def test_planner_failed_spot_shake_then_stuck(machine, layout):
    machine.run.max_failed_picks = 2
    machine.run.shake.max_in_a_row = 1
    pl = Planner(layout, machine, "sort")
    part = P("DIN912_M3", 8.0)
    for _ in range(2):
        plan = pl.next([part])
        assert plan.kind == "pick"
        assert pl.report(plan, [part]) is False  # still there
    assert pl.failed_picks == 2
    plan = pl.next([part])
    assert plan.kind == "shake"
    pl.report(plan, [part])  # shaking forgets failed spots
    assert pl.next([part]).kind == "pick"
    assert pl.report(pl.next([part]), []) is True
    assert pl.sorted_count == {103: 1}
    assert pl.next([]).kind == "done"


def test_planner_waiting_parts_go_to_unsure_after_shaking(machine, layout):
    machine.run.shake.max_in_a_row = 1
    pl = Planner(layout, machine, "sort")
    part = P("DIN912_M4")  # its rule needs a length, which is not measured
    assert pl.next([part]).kind == "shake"
    plan = pl.next([part])
    assert plan.kind == "pick" and plan.bin.role == "unsure"


def test_kit_strategy(machine, layout):
    layout.bins[8].role = "kit"
    machine.run.kit = {"DIN912_M3x8": 2}
    pl = Planner(layout, machine, "kit")
    parts = [P("DIN912_M3", 8.0, pid=i, xy=(80 + 10 * i, 100)) for i in range(3)]
    parts.append(P("DIN934_M4", pid=9))
    for _ in range(2):
        plan = pl.next(parts)
        assert plan.kind == "pick" and plan.bin.role == "kit" and plan.part.cls == "DIN912_M3"
        parts.remove(plan.part)
        pl.report(plan, parts)
    assert pl.next(parts).reason == "kit complete"


def test_clear_strategy(machine, layout):
    layout.bins[9].role = "all"
    pl = Planner(layout, machine, "clear")
    plan = pl.next([P("UNKNOWN", unknown=True)])
    assert plan.bin.role == "all"
