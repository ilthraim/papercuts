"""The search variants on small miters: exact-k, core ablation, minimal cores, steering,
farming, tracing, brute-force ground truth and the cone features. No yosys, no Jasper.
"""

import itertools
import json
import os
import tempfile

import numpy as np

from papercuts.cegis.baseline import exact_k_search
from papercuts.cegis.btor2z3 import Design
from papercuts.cegis.comb import CombCegis
from papercuts.cegis.core_min import minimize, parse_levels
from papercuts.cegis.exhaustive import exhaustive_max, fails_under, is_minimal, minimum_core
from papercuts.cegis.features import History, analyze, components, decomposable, select_table, set_features
from papercuts.cegis.steer import AdaptiveSteer
from papercuts.cegis.trace import bits, decode_on, read_trace
from papercuts.manifest import Manifest

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cegis_fixtures")

# out = a ^ (s0 & b) ^ (s1 & b), golden out = a. Each select alone flips the output when
# b is 1, and together they cancel: sizes 0 and 2 are valid, size 1 is not. The hole.
CANCEL = """
1 sort bitvec 1
2 input 1 a
3 input 1 b
4 input 1 pc_sel0
5 input 1 pc_sel1
6 and 1 4 3
7 and 1 5 3
8 xor 1 2 6
9 xor 1 8 7
10 neq 1 9 2
11 bad 10
"""

NO_SELECTS = """
1 sort bitvec 1
2 input 1 a
3 neq 1 2 2
4 bad 3
"""

# Two outputs that share nothing, each with its own cancelling pair: s0 and s1 on x, s2 and
# s3 on y. Exactly 4 sets are valid: each pair is all on or all off.
TWO_OUTPUTS = """
1 sort bitvec 1
2 input 1 x
3 input 1 y
4 input 1 b
5 input 1 pc_sel0
6 input 1 pc_sel1
7 input 1 pc_sel2
8 input 1 pc_sel3
9 and 1 5 4
10 and 1 6 4
11 xor 1 2 9
12 xor 1 11 10
13 neq 1 12 2
14 and 1 7 4
15 and 1 8 4
16 xor 1 3 14
17 xor 1 16 15
18 neq 1 17 3
19 or 1 13 18
20 bad 19
"""


def fixture(name):
    return Design.from_file(os.path.join(FIX, f"{name}.btor"), os.path.join(FIX, f"{name}.info"))


def loop(text=None, name=None, **kw):
    d = Design.from_string(text) if text else fixture(name)
    return CombCegis(d, verbose=False, **kw)


def on_set(res):
    return {n for n, v in res.items() if v}


def test_exact_k_stops_below_maximum_on_a_hole():
    assert on_set(loop(CANCEL).run()) == {"pc_sel0", "pc_sel1"}
    r = exact_k_search(loop(CANCEL))
    assert r.k == 0 and not on_set(r.on), r
    assert r.feasibility == [(1, False)], r.feasibility


def test_exact_k_finds_maximum_without_a_hole():
    full = loop(name="poly").run()
    r = exact_k_search(loop(name="poly"))
    assert r.k == sum(full.values()), (r, on_set(full))
    assert loop(name="poly").verify(r.on) is None


def test_exact_k_answers_on_many_selects():
    # z3.Solver() never answered the first PbEq query at this size
    n = 200
    lines = ["1 sort bitvec 1", "2 input 1 a"]
    lines += [f"{3 + i} input 1 pc_sel{i}" for i in range(n)]
    lines += [f"{3 + n} neq 1 2 2", f"{4 + n} bad {3 + n}"]
    r = exact_k_search(loop("\n".join(lines) + "\n"))
    assert r.k == n and sum(r.on.values()) == n, r.k


def test_exhaustive_agrees_and_sees_the_hole():
    t = exhaustive_max(loop(CANCEL))
    assert t.max_size == 2 and t.holes == [1], t
    assert t.non_monotone_cuts == ["pc_sel0", "pc_sel1"]
    t = exhaustive_max(loop(name="poly"))
    assert t.max_size == sum(loop(name="poly").run().values()), t
    try:
        exhaustive_max(loop(name="poly"), limit=2)
    except ValueError:
        return
    raise AssertionError("the brute-force limit was not enforced")


def test_ablation_same_size_and_no_cores():
    with_cores = loop(name="poly")
    without = loop(name="poly", learn_cores=False)
    a, b = with_cores.run(), without.run()
    assert sum(a.values()) == sum(b.values())
    assert with_cores.cores and not without.cores and not without.learned
    # the hole has an optimum no core is ever needed for either
    assert on_set(loop(CANCEL, learn_cores=False).run()) == {"pc_sel0", "pc_sel1"}
    assert len(with_cores.stats) <= len(without.stats)


def first_counterexample(c, asg):
    cex = c.verify(asg)
    assert cex is not None
    return cex


def test_minimized_core_is_exact_and_minimal():
    c = loop(name="poly")
    asg = dict.fromkeys(c.sel_names, True)
    cex = first_counterexample(c, asg)
    r = minimize(c._cex_formula(cex, c.sel_bv), [(n, True) for n in c.sel_names], "10,50,inf")
    assert r.unresolved == 0
    assert r.lits and is_minimal(c, cex, r.lits), r
    # minimal is not minimum, but a minimal core can never beat the true minimum
    assert len(r.lits) >= len(minimum_core(c, cex, asg)[0])
    formula = c._cex_formula(cex, c.sel_bv)
    for extra in r.extra:
        assert fails_under(formula, c, extra), extra  # exact, though maybe not minimal


def test_minimized_cores_change_no_answer():
    plain, shrunk = loop(name="poly"), loop(name="poly", core_levels="10,50,250,1000")
    assert sum(plain.run().values()) == sum(shrunk.run().values())
    assert shrunk.cores
    assert on_set(loop(CANCEL, core_levels="10,inf").run()) == {"pc_sel0", "pc_sel1"}


def test_minimized_core_keeps_off_literals():
    # the candidate {s0} fails because s1 is off: the core must say so
    c = loop(CANCEL)
    asg = {"pc_sel0": True, "pc_sel1": False}
    cex = first_counterexample(c, asg)
    r = minimize(c._cex_formula(cex, c.sel_bv), list(asg.items()), "10,inf")
    assert sorted(r.lits) == [("pc_sel0", True), ("pc_sel1", False)], r


def test_core_levels_required_shape():
    assert parse_levels("10,50,inf")[-1] == float("inf")
    for bad in (None, "", "50,10", "0", "inf,10", "x"):
        try:
            parse_levels(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted core levels {bad!r}")
    try:
        loop(name="poly", core_levels="10", learn_cores=False)
    except ValueError:
        return
    raise AssertionError("core_levels with learn_cores=False was accepted")


def test_steering_keeps_the_optimum():
    best = sum(loop(name="poly").run().values())
    for mode in ("hint", "lex"):
        c = loop(name="poly")
        c.steer = AdaptiveSteer(c.sel_names, mode=mode)
        assert sum(c.run().values()) == best, mode
        assert any(c.steer.score.values()), mode


def test_adaptive_scores():
    s = AdaptiveSteer(["a", "b", "c"], decay=0.5)
    s.observe([("a", True)])
    s.observe([("a", True), ("b", False)])
    assert s.score["a"] > 0 > s.score["b"] and s.score["c"] == 0
    assert s.phases() == {"a": False, "b": True, "c": True}
    w = s.weights()
    assert w["a"] == -1000 and w["b"] > 0 and "c" not in w
    # a later bump outweighs an equal count of earlier ones
    s = AdaptiveSteer(["a", "b"], decay=0.5)
    s.observe([("a", True)])
    s.observe([("b", True)])
    assert s.score["b"] > s.score["a"]


def test_hint_is_honoured_and_can_change():
    # either select alone is legal, both are not: the tie is what steering decides
    c = loop(CANCEL, exclusive=[("pc_sel0", "pc_sel1")])
    picks = []
    for pick in ("pc_sel0", "pc_sel1", "pc_sel0"):
        class Fixed:
            mode = "hint"

            @staticmethod
            def phases():
                return {n: n == pick for n in c.sel_names}

        c.steer = Fixed
        picks.append(on_set(c.synthesize()))
    assert picks == [{"pc_sel0"}, {"pc_sel1"}, {"pc_sel0"}], picks


def test_lex_breaks_the_tie_exactly():
    c = loop(CANCEL, exclusive=[("pc_sel0", "pc_sel1")])
    for pick in ("pc_sel0", "pc_sel1"):
        class Fixed:
            mode = "lex"

            @staticmethod
            def weights():
                return {pick: 5}

        c.steer = Fixed
        assert on_set(c.synthesize()) == {pick}


def test_farm_enumerates_valid_sets():
    c = loop(TWO_OUTPUTS)
    best = c.run()
    assert sum(best.values()) == 4
    found = c.farm(best, max_calls=64)
    # every pair is either both on or both off, per output: 4 valid sets in all
    want = {frozenset(), frozenset({"pc_sel0", "pc_sel1"}), frozenset({"pc_sel2", "pc_sel3"})}
    assert {frozenset(on_set(a)) for a in found} == want, found
    for a in found:
        assert loop(TWO_OUTPUTS).verify(a) is None
    assert any(s.get("phase") == "farm" and s["verify_result"] == "sat" for s in c.stats)
    assert c.synthesize() is None


def test_farm_distance_and_budget():
    c = loop(TWO_OUTPUTS)
    best = c.run()
    found = c.farm(best, max_calls=64, distance=3)
    assert all(sum(best[n] != a[n] for n in best) >= 3 for a in found), found
    c = loop(TWO_OUTPUTS)
    best = c.run()
    before = len(c.stats)
    c.farm(best, max_calls=2)
    assert len(c.stats) == before + 2


def test_farm_with_no_selects_has_nothing_to_do():
    # vtr__single_wire and vtr__single_ff: no cuts, so there is nothing to block
    for distance in (1, 8):
        c = loop(NO_SELECTS)
        assert c.run() == {}
        before = len(c.stats)
        assert c.farm({}, max_calls=8, distance=distance) == []
        assert len(c.stats) == before


def test_trace_records_every_call():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.jsonl")
        c = loop(name="poly", trace_path=path)
        res = c.run()
        c.farm(res, max_calls=3)
        c.trace.close()
        head, recs = read_trace(path)
        assert head["selects"] == c.sel_names
        assert len(recs) == len(c.stats)
        search = [r for r in recs if r["phase"] == "search"]
        assert [r["verify"] for r in search[:-1]] == ["sat"] * (len(search) - 1) and search[-1]["verify"] == "unsat"
        assert {head["selects"][i] for i in bits(decode_on(search[-1]["on"]))} == on_set(res)
        assert all("core" in r and "synth_s" in r for r in search[:-1])
        # a kill mid-line loses only that line
        with open(path, "a") as f:
            f.write('{"kind":"iter","iter":')
        assert len(read_trace(path)[1]) == len(recs)
        with open(path) as f:
            assert all(json.loads(line) for line in f.read().splitlines()[:-1])


def test_exact_k_trace():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.jsonl")
        c = loop(name="poly", trace_path=path)
        r = exact_k_search(c)
        c.trace.close()
        _, recs = read_trace(path)
        assert len(recs) == r.verify_calls and all(x["phase"] == "exact-k" for x in recs)
        assert recs[0]["k"] == 0 and recs[0]["verify"] == "unsat"


def test_cones_and_components():
    d = Design.from_string(TWO_OUTPUTS)
    sel = [f"pc_sel{i}" for i in range(4)]
    cones = analyze(d, sel)
    assert len(cones.sink_names) == 2, cones.sink_names
    assert components(cones) == [[0, 1], [2, 3]]
    assert decomposable(cones, two_frames=False) and not decomposable(cones, two_frames=True)
    assert components(cones, [("pc_sel1", "pc_sel2")]) == [[0, 1, 2, 3]]
    assert cones.fanout == [1, 1, 1, 1]
    assert cones.sink_dist == [4, 3, 4, 3], cones.sink_dist
    assert cones.cone_nodes[0] == 5  # and, xor, xor, neq, or


def test_independence_holds_by_brute_force():
    """The claim the components rest on: F(S) is the AND of F over each component."""
    for text, name in ((TWO_OUTPUTS, None), (None, "poly"), (None, "seqpipe"), (None, "seq")):
        c = loop(text, name)
        cones = analyze(c.d, c.sel_names)
        assert decomposable(cones, c.two_frames), name
        comps = components(cones)
        n = len(c.sel_names)
        if n > 8:
            continue

        def F(on):
            return c.verify({m: i in on for i, m in enumerate(c.sel_names)}) is None

        for size in range(n + 1):
            for sub in itertools.combinations(range(n), size):
                s = set(sub)
                assert F(s) == all(F(s & set(g)) for g in comps), (name, s, comps)


class _Man:
    """As much of a Manifest as select_table reads."""

    def __init__(self, names):
        cuts = [{"index": i, "type": "ternary(keep-false)" if i % 2 else "binop(land)(keep-left)", "line": i + 1,
                 "port": n, "inserted": True} for i, n in enumerate(names)]
        self.m = Manifest({"version": 1, "top": "t", "mux_suffix": "_m", "files": {"golden": [], "muxed": []},
                           "modules": {"t": {"muxed_name": "t_m", "cuts": cuts, "exclusive": [], "forwarded": []}}},
                          "/nowhere/mux_manifest.json")


def test_select_and_set_features():
    d = Design.from_string(TWO_OUTPUTS)
    sel = [f"pc_sel{i}" for i in range(4)]
    t = select_table(analyze(d, sel), _Man(sel).m, [], two_frames=False)
    assert t.X.shape[0] == 4 and t.decomposable and t.n_components == 2
    col = {c: i for i, c in enumerate(t.columns)}
    assert list(t.X[:, col["family_ternary"]]) == [0, 1, 0, 1]
    assert list(t.X[:, col["variant_keep-false"]]) == [0, 1, 0, 1]
    assert list(t.X[:, col["component"]]) == [2, 2, 2, 2]

    h = History(4)
    empty = np.zeros(4, dtype=bool)
    f = set_features(t, empty, h, None)
    assert f["n_on"] == 0 and f["hamming_prev"] == -1 and f["core_closest"] == 0
    on = np.array([True, True, False, False])
    f = set_features(t, on, h, empty)
    assert f["n_on"] == 2 and f["sinks_shared"] == 1 and f["sink_pairs"] == 1 and f["hamming_prev"] == 2
    assert f["components_touched"] == 1 and f["frac_family_ternary"] == 0.5
    h.add([(0, 1), (2, 0)])  # s0 on and s2 off
    f = set_features(t, on, h, None)
    assert f["n_cores"] == 1 and f["core_on_sum"] == 1 and f["core_off_sum"] == 1 and f["core_closest"] == 1.0
    f = set_features(t, np.array([True, False, True, False]), h, None)
    assert f["core_closest"] == 0.5 and f["core_near_misses"] == 1


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
