"""The combinational CEGIS loop (papercuts.cegis.comb) on prepared BTOR2 miters.

Each fixture's answer is known. Where a sequential checker would accept more
(ctr, ctrfree, seqenc), the extra cut is valid only through reachability or
cancellation, which the combinational spec rejects by design -- the loop
returning it would mean the spec had quietly weakened.
"""

import os

import z3

from papercuts.cegis.btor2z3 import Design
from papercuts.cegis.comb import AnonymousX, CombCegis, MiterMismatch

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cegis_fixtures")


def load(name):
    return Design.from_file(os.path.join(FIX, f"{name}.btor"), os.path.join(FIX, f"{name}.info"))


def on(res):
    return sorted((n for n, v in res.items() if v), key=lambda n: int(n[6:]))


def run_loop(name, **kw):
    c = CombCegis(load(name), verbose=False, **kw)
    res = c.run()
    assert c.base_ok, (name, c.base_evidence)
    return c, res


# Two copies of a ROM whose contents differ: R holds after every step, but not at power-up.
ROM_MISMATCH = """
1 sort bitvec 1
2 input 1 d
3 state 1 mod.rom
4 state 1 orig.rom
5 one 1
6 zero 1
7 init 1 3 5
8 init 1 4 6
9 next 1 3 3
10 next 1 4 4
11 neq 1 3 4
12 bad 11
"""

# An X yosys left anonymous: independent in the two copies, so refused.
ANON = """
1 sort bitvec 1
2 input 1 a
3 input 1
4 eq 1 2 3
5 not 1 4
6 bad 5
"""


def test_fixture_answers():
    for name, expect in [("poly", ["pc_sel0", "pc_sel2", "pc_sel3", "pc_sel4"]), ("seq", ["pc_sel3"]),
                         ("seqpipe", ["pc_sel3"]), ("noinit", ["pc_sel1"])]:
        assert on(run_loop(name)[1]) == expect, name


def test_reachability_and_cancellation_cuts_are_off():
    # ctr / ctrfree pc_sel0: q[7] is never set, but from a state with it set the
    # next q differs. seqenc pc_sel4: r forced to 0 cancels out of q, but r
    # itself differs.
    for name in ("ctr", "ctrfree", "seqenc"):
        assert on(run_loop(name)[1]) == [], name


def test_deep_failures_found_in_one_cycle():
    # the shared time counter is free, so failures at t = 30, 200 and 499 each
    # show in one frame: c2 is masked by c5, c4 and c8 not together, c7 off
    c, res = run_loop("deep")
    got = on(res)
    assert len(got) == 8, got
    assert "pc_sel7" not in got and "pc_sel2" in got and "pc_sel5" in got, got
    assert not ("pc_sel4" in got and "pc_sel8" in got), got
    assert all(len(cx["inputs"]) == 1 for cx in c.cex_traces)


def test_two_frame_query_agrees():
    # the async2sync path (R read off named nodes in a second frame), forced on
    # a synchronous design: same answers, counterexamples carry two frames
    for name, expect in [("seq", ["pc_sel3"]), ("seqpipe", ["pc_sel3"]), ("ctr", [])]:
        c = CombCegis(load(name), verbose=False)
        c.two_frames = True
        assert on(c.run()) == expect, name
        assert all(len(cx["inputs"]) == 2 for cx in c.cex_traces), name


def test_in_process_solver_agrees():
    c = CombCegis(load("seq"), verbose=False, solver="qfbv")
    assert on(c.run()) == ["pc_sel3"]


def test_next_state_is_part_of_the_spec():
    # seqpipe's output is registered: ok in frame 0 reads only states, which R
    # makes equal. Every refutation is therefore R after the step failing.
    c = CombCegis(load("seqpipe"), verbose=False)
    asg = {n: n == "pc_sel0" for n in c.sel_names}
    cex = c.verify(asg)
    assert cex is not None
    tr = c._cex_trace(cex, c._sel_lits(asg))
    assert z3.is_true(z3.simplify(tr.frames[0].ok))
    assert z3.is_false(z3.simplify(c._r_next(tr)))
    assert c.conflict_core(cex, asg)[0] == ["pc_sel0"]


def test_counterexample_need_not_be_reachable():
    # ctr pc_sel0's refutation starts with q's top bit set, which no run from reset reaches
    c = CombCegis(load("ctr"), verbose=False)
    cex = c.verify({"pc_sel0": True, "pc_sel1": False})
    assert cex is not None and len(cex["inputs"]) == 1
    p = c.pairs[0]
    assert cex["init"][p.mod] >> 7 & 1
    assert cex["init"][p.mod] == cex["init"][p.orig]


def test_base_case_catches_rom_mismatch():
    c = CombCegis(Design.from_string(ROM_MISMATCH), verbose=False)
    assert c.run() == {}
    assert not c.base_ok and "base sat" in c.base_evidence


def test_unpaired_register_is_a_mismatch():
    # q never compared: free in each copy, so the copies differ with every cut off
    c = CombCegis(load("noinit"), verbose=False, registers=[("nothing", 4)])
    assert c.pairs == []
    try:
        c.run()
    except MiterMismatch:
        return
    raise AssertionError("expected MiterMismatch")


def test_exclusive_pair():
    c = CombCegis(load("poly"), verbose=False, exclusive=[("pc_sel0", "pc_sel2")])
    got = on(c.run())
    assert not ("pc_sel0" in got and "pc_sel2" in got), got
    assert len(got) == 3, got


def test_individual_validity_matches_direct_verify():
    # the conflict-core shortcut must answer exactly what a verifier call would
    for name in ("poly", "seq", "deep", "ctr"):
        c = CombCegis(load(name), verbose=False)
        c.run()
        got = c.individually_valid(c.sel_names)
        assert any(h == "core" for _, h in got.values()), (name, "shortcut never used")
        for n, (valid, _) in got.items():
            direct = c.verify({m: m == n for m in c.sel_names}) is None
            assert valid == direct, (name, n, got[n])


# mod.r is one 4-bit state; orig.r is a named concatenation of two 2-bit states
# (yosys split the golden register; koios test's pipe_3)
SPLIT = """
1 sort bitvec 1
2 sort bitvec 2
3 sort bitvec 4
4 input 3 d
5 state 3 mod.r
6 state 2 orig.r_hi
7 state 2 orig.r_lo
8 concat 3 6 7 orig.r
9 next 3 5 4
10 slice 2 4 3 2
11 slice 2 4 1 0
12 next 2 6 10
13 next 2 7 11
14 neq 1 5 8
15 bad 14
"""


def test_state_paired_with_split_register():
    from papercuts.cegis.registers import match_registers
    d = Design.from_string(SPLIT)
    pairs, unmatched = match_registers(d, ["r"])
    assert unmatched == [] and len(pairs) == 1 and pairs[0].kind == "symbol", (pairs, unmatched)
    c = CombCegis(d, verbose=False, selects=[], registers=[("r", 4)])
    assert c.two_frames and c.verify({}) is None  # the copies agree with R in place


def test_anonymous_x_refused():
    try:
        CombCegis(Design.from_string(ANON), verbose=False)
    except AnonymousX:
        return
    raise AssertionError("expected AnonymousX")


def run():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_cegis_comb: OK ({len(tests)} tests)")


if __name__ == "__main__":
    run()
