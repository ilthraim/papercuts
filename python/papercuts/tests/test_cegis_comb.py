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
    assert cex["init"][p.mod.state] >> 7 & 1
    assert cex["init"][p.mod.state] == cex["init"][p.orig.state]


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
    # paired flip-flop to flip-flop, slice by slice
    assert unmatched == [] and [(p.kind, p.orig.state) for p in pairs] == [("state", "orig.r_lo"), ("state", "orig.r_hi")], pairs
    c = CombCegis(d, verbose=False, selects=[], registers=[("r", 4)])
    assert c.verify({}) is None  # the copies agree with R in place


# Register `w` is one net whose high half is a flip-flop and whose low half is
# combinational: a read of register `a`, which the muxed copy reads through a
# bit-shrink of a[1] (opencores video_systems qnr: s_pipe[0] = z = iz). a[1] is
# always 0 after the first cycle but free at power-up, so the cut is invalid
# from power-up (Jasper's cex). Equating the whole of `w` put a[1] == 0 into R
# and the cut came out valid.
COMB_WORD = """
1 sort bitvec 1
2 sort bitvec 2
3 sort bitvec 4
4 input 2 d
5 input 1 pc_sel0
6 state 2 mod.a
7 state 2 orig.a
8 state 2 mod.wff
9 state 2 orig.wff
10 slice 1 4 0 0
11 uext 2 10 1
12 next 2 6 11
13 next 2 7 11
14 slice 1 6 1 1
15 zero 1
16 ite 1 5 15 14
17 slice 1 6 0 0
18 concat 2 16 17
19 concat 3 8 18 mod.w
20 concat 3 9 7 orig.w
21 next 2 8 18
22 next 2 9 7
23 neq 1 8 9
24 bad 23
"""


def test_combinational_word_of_a_register_is_not_in_r():
    from papercuts.cegis.registers import match_registers
    d = Design.from_string(COMB_WORD)
    pairs, unmatched = match_registers(d, ["a", "w"], selects={"pc_sel0"})
    assert sorted(p.name for p in pairs) == ["a", "w[3:2]"] and unmatched == [], (pairs, unmatched)
    c = CombCegis(d, verbose=False, selects=["pc_sel0"], registers=[("a", 2), ("w", 4)])
    assert c.run() == {"pc_sel0": False}


# An asynchronous register whose reset expression carries a cut: async2sync's
# read mux is steered by a select, but its arm is still async2sync's own state
# (opencores cop, ptc: every async register went unmatched without this)
ASYNC_CUT = """
1 sort bitvec 1
2 sort bitvec 2
3 input 1 rst
4 input 1 pc_sel0
5 input 2 d
6 state 2 $auto$async2sync.cc:1$m
7 state 2 $auto$async2sync.cc:1$o
8 zero 1
9 ite 1 4 8 3
10 zero 2
11 ite 2 9 10 6 mod.q
12 ite 2 3 10 7 orig.q
13 next 2 6 5
14 next 2 7 5
15 neq 1 11 12
16 bad 15
"""


def test_async_register_behind_a_cut_reset():
    from papercuts.cegis.registers import match_registers
    d = Design.from_string(ASYNC_CUT)
    pairs, unmatched = match_registers(d, ["q"], selects={"pc_sel0"})
    assert unmatched == [] and [(p.mod.state, p.orig.state) for p in pairs] == [
        ("$auto$async2sync.cc:1$m", "$auto$async2sync.cc:1$o")], (pairs, unmatched)


# `w` is a wire around an anonymous flip-flop q; in the muxed copy w[1] reads
# q[1] through a bit-shrink's select, so q[1] is reachable through `w` in the
# golden copy only (hls nussinov sub_ln21). q[0] pairs, which identifies the
# flip-flop, and then q[1] pairs with it.
SHRUNK_READ = """
1 sort bitvec 1
2 sort bitvec 2
3 input 2 d
4 input 1 pc_sel0
5 state 2 qm
6 state 2 qo
7 next 2 5 3
8 next 2 6 3
9 slice 1 5 1 1
10 zero 1
11 ite 1 4 10 9
12 slice 1 5 0 0
13 concat 2 11 12 mod.w
14 uext 2 6 0 orig.w
15 neq 1 13 14
16 bad 15
"""


def test_flip_flop_paired_whole_once_identified():
    from papercuts.cegis.registers import match_registers
    d = Design.from_string(SHRUNK_READ)
    pairs, unmatched = match_registers(d, ["w"], selects={"pc_sel0"})
    assert unmatched == [] and any(p.mod.state == "qm" and p.orig.state == "qo" and p.mod.width == 2
                                   for p in pairs), (pairs, unmatched)


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
