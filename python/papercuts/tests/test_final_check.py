"""The final check of all proven cuts, its repair and chain proofs (repair.py).

Two layers. The search itself (`finalize`, `chain`, `ddmin`, `hitting_set`) runs
against a model checker: a design is a set of cuts, its behaviour is the set of
planted conflicts it contains (empty = the original), a check is proven when both
sides behave the same, and checks too big for their limit come back with no
verdict. The pipeline plumbing then runs `--method fv` end to end with a stub
backend that proves, refutes or times out the final check, and hands every Run
to `ec.jasper_args`, so each shape the final phase builds is one Jasper accepts.
"""

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile

import papercuts.__main__ as pm
from papercuts.backends.base import ECBackend
from papercuts.ec import jasper_args
from papercuts.repair import ChainLimits, Check, chain, ddmin, finalize, hitting_set
from papercuts.tests.test_cegis_pipeline import DESIGN


# MARK: the search, on a model
class Model:
    """A checker over sets of cuts.

    conflicts: sets of cuts that misbehave together. wide: a check from the
    elaborated design (empty base) of more cuts than this has no verdict unless
    its limit is None. hard: {cut: seconds} a step adding that cut needs."""

    def __init__(self, conflicts=(), wide=10**9, hard=None):
        self.conflicts = [frozenset(c) for c in conflicts]
        self.wide, self.hard = wide, hard or {}
        self.log = []          # (base, cuts, limit, why) of every check run
        self._tasks = {}
        self.n_run = 0

    def behaviour(self, s):
        return frozenset(c for c in self.conflicts if c <= set(s))

    def verdict(self, base, cuts, limit):
        added = set(cuts) - set(base)
        need = max([self.hard.get(k, 0) for k in added] + [0])
        if not base and len(cuts) > self.wide:
            need = max(need, 10**6)
        if need and limit is not None and limit < need:
            return "inconclusive"
        return "proven" if self.behaviour(base) == self.behaviour(cuts) else "cex"

    def launch(self, base, cuts, limit, why):
        key = (frozenset(base), frozenset(cuts))
        for have, t in self._tasks.get(key, []):
            if not t.done() or t.result().verdict != "inconclusive" or \
                    have is None or (limit is not None and have >= limit):
                return t

        async def go():
            await asyncio.sleep(0)
            self.log.append((tuple(base), tuple(cuts), limit, why))
            self.n_run += 1
            return Check(self.verdict(base, cuts, limit), 1.0, limit, f"{len(self.log):016x}",
                         tuple(base), tuple(cuts), why)

        t = asyncio.ensure_future(go())
        self._tasks.setdefault(key, []).append((limit, t))
        return t

    async def check(self, base, cuts, limit, why):
        return await self.launch(base, cuts, limit, why)

    async def drain(self):
        pending = [t for ts in self._tasks.values() for _, t in ts if not t.done()]
        if pending:
            await asyncio.gather(*pending)


def cuts(n, mod="m"):
    return [(mod, i) for i in range(n)]


def fin(model, cs, own=None, limit=600, lim=None):
    return asyncio.run(finalize(model, cs, own or {}, limit, lim or ChainLimits()))


def test_proven_at_once():
    m = Model()
    r = fin(m, cuts(5))
    assert r.first_verdict == "proven" and r.proof["kind"] == "single"
    assert r.kept == cuts(5) and not r.dropped and len(m.log) == 1


def test_conflict_repaired_by_one_drop():
    a, c = ("m", 1), ("m", 4)
    m = Model(conflicts=[{a, c}])
    r = fin(m, cuts(8))
    assert r.first_verdict == "cex" and r.conflicts == [[a, c]], r.conflicts
    assert len(r.dropped) == 1 and r.dropped[0][0] in (a, c) and r.dropped[0][1] == "conflict"
    assert len(r.kept) == 7 and r.proof["kind"] == "single"
    # the dropped cut, added back alone to the repaired set, brings the failure back
    assert list(r.added_back.values()) == ["cex"], r.added_back
    base, cand, *_ = [x for x in m.log if x[3].startswith("repair: add back")][0]
    assert set(cand) - set(base) == {r.dropped[0][0]} and set(base) == set(r.kept)


def test_two_conflicts_share_a_cut():
    # {1,2} and {2,3}: dropping 2 alone meets both
    m = Model(conflicts=[{("m", 1), ("m", 2)}, {("m", 2), ("m", 3)}])
    r = fin(m, cuts(6))
    assert [k for k, _ in r.dropped] == [("m", 2)], r.dropped
    assert len(r.conflicts) >= 1


def test_chain_order_and_groups():
    # no verdict above 10 cuts at once; one slow cut goes first, alone
    cs = cuts(40)
    slow = ("m", 33)
    m = Model(wide=10)
    lim = ChainLimits(group_size=16)
    r = fin(m, cs, own={slow: 100.0}, lim=lim)
    assert r.first_verdict == "inconclusive" and r.proof["kind"] == "chain"
    steps = r.proof["steps"]
    assert steps[0]["added"] == ["m#33"] and steps[0]["from"] == 0
    assert [len(s["added"]) for s in steps] == [1, 16, 16, 7], steps
    assert [s["from"] for s in steps] == [0, 1, 17, 33]
    assert r.kept == cs and not r.dropped
    first = [x for x in m.log if x[3] == "chain: single m#33"][0]
    assert first[2] == 900  # max(900, 3 x 100 s)
    # every step was launched before any finished: the chain runs at once
    assert all(x[3].startswith("chain") for x in m.log[1:1 + len(steps)])


def test_chain_drops_a_cut_with_no_verdict():
    cs = cuts(20)
    h = ("m", 5)
    m = Model(wide=10, hard={h: 10**5})
    r = fin(m, cs, lim=ChainLimits(group_size=8))
    assert r.proof["kind"] == "chain"
    assert r.dropped == [(h, "no verdict")] and h not in r.kept and len(r.kept) == 19
    tries = [x[2] for x in m.log if x[3] == "chain: single m#5"]
    assert tries == [900, 3600], tries  # first limit, one retry, then dropped
    # the groups after it start from the set without it
    steps = r.proof["steps"]
    added = [k for s in steps for k in s["added"]]
    assert "m#5" not in added and len(added) == 19
    assert [s["from"] for s in steps] == [sum(len(t["added"]) for t in steps[:i]) for i in range(len(steps))]


def test_chain_conflict_goes_to_repair():
    # a conflict across two groups, in a set too big to check at once
    x, y = ("m", 2), ("m", 13)
    cs = cuts(24)
    m = Model(conflicts=[{x, y}], wide=10)
    r = fin(m, cs, lim=ChainLimits(group_size=8))
    assert r.first_verdict == "inconclusive"
    assert r.conflicts == [[x, y]], r.conflicts
    # the cut whose step was refuted is the one dropped
    assert r.dropped == [(y, "conflict")] and r.proof["kind"] == "chain"
    assert len(r.kept) == 23 and r.added_back == {"m#13": "cex"}


def test_chain_without_limit_never_runs():
    m = Model(wide=3)
    r = fin(m, cuts(8), limit=None)
    assert r.proof["kind"] == "single" and len(m.log) == 1


def test_ddmin_minimal():
    bad = {3, 7, 11}

    async def fails(s):
        await asyncio.sleep(0)
        return bad <= set(s)

    assert sorted(asyncio.run(ddmin(list(range(16)), fails))) == [3, 7, 11]


def test_hitting_set_prefers():
    cf = [[("m", 1), ("m", 2)]]
    assert hitting_set(cf, prefer={("m", 2)}) == {("m", 2)}
    assert hitting_set(cf, prefer={("m", 1)}) == {("m", 1)}
    assert hitting_set([[("m", 1), ("m", 2)], [("m", 2), ("m", 3)]]) == {("m", 2)}


# MARK: the pipeline
def make_stub(calls, mode):
    """mode: "proven", "cex" (the first final check of all cuts is refuted) or
    "timeout" (it, and any later check of the same text, has no verdict)."""
    state = {}

    class Stub(ECBackend):
        name = "stub"

        async def _check(self, run):
            jasper_args(run, None)  # every shape must be one Jasper takes
            calls.append(run)
            if "/final/" not in run.work_dir or mode == "proven":
                return "proven"
            text = open(os.path.join(run.candidate.overrides[0][1])).read()
            from_elab = run.golden.lib_dir.endswith("concrete_sources")
            state.setdefault("all", text)
            if from_elab and text == state["all"]:
                return "cex" if mode == "cex" else "inconclusive"
            return "proven"

    return Stub


def rd(tmp, path):
    """Read a file the run named relative to its working directory."""
    with open(os.path.join(tmp, path)) as f:
        return f.read()


def exists(tmp, path):
    return os.path.exists(os.path.join(tmp, path))


def pipeline(tmp, mode, extra=()):
    with open(os.path.join(tmp, "design.sv"), "w") as f:
        f.write(DESIGN)
    calls = []
    saved = (os.getcwd(), sys.argv, pm.get_backend)
    os.chdir(tmp)
    sys.argv = ["papercuts", "design.sv", "-e", "--method", "fv", *extra]
    pm.get_backend = lambda name: make_stub(calls, mode)
    exit_ = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(pm.main())
    except SystemExit as e:
        exit_ = e
    finally:
        os.chdir(saved[0])
        sys.argv, pm.get_backend = saved[1], saved[2]
    out = os.path.join(tmp, "outputs")
    rows = [json.loads(ln) for ln in open(os.path.join(out, "papercuts.results.jsonl"))]
    return calls, exit_, out, rows


def test_pipeline_proven():
    # every cut, then ONE final check of the whole design; no per-module checks
    with tempfile.TemporaryDirectory() as tmp:
        calls, exit_, out, rows = pipeline(tmp, "proven")
        assert exit_ is None, exit_
        finals = [c for c in calls if "/final/" in c.work_dir]
        assert len(finals) == 1 and all(c.index != -1 or c in finals for c in calls)
        f = finals[0]
        assert f.golden.lib_dir.endswith("concrete_sources") and not f.renames
        assert f.time_limit_s is None and not getattr(f, "power_up", False)
        text = rd(tmp, f.candidate.overrides[0][1])
        assert all(f"module {m}" in text for m in ("top", "top_c0", "top_c1")), text
        valid = sum(1 for r in rows if r["phase"] == "cuts" and r["valid"])
        fr = [r for r in rows if r["phase"] == "final"]
        assert len(fr) == 1 and fr[0]["type"] == "final" and fr[0]["applied"] == valid and fr[0]["valid"]
        # the returned design is the text the final check proved
        joint = open(os.path.join(out, "consolidated_sources", "joint", "top_consolidated.sv")).read()
        assert joint == text
        res = json.load(open(os.path.join(out, "final_check.json")))
        assert res["kept_count"] == valid and res["proof"]["kind"] == "single" and not res["dropped"]
        log = open(os.path.join(out, "papercuts.log")).read()
        assert f"final check of all {valid} proven cuts: proven" in log
        assert f"final design ({valid} cuts kept): PROVEN by one check" in log
        assert "Y(-" in log and "valid  set" not in log


def test_pipeline_repair():
    with tempfile.TemporaryDirectory() as tmp:
        calls, exit_, out, rows = pipeline(tmp, "cex")
        assert exit_ is None, exit_
        valid = sum(1 for r in rows if r["phase"] == "cuts" and r["valid"])
        res = json.load(open(os.path.join(out, "final_check.json")))
        assert res["first_verdict"] == "cex" and res["kept_count"] == valid - 1
        assert len(res["dropped"]) == 1 and res["dropped"][0]["reason"] == "conflict"
        # the stub refutes only the text of all cuts; cuts that leave no trace once the
        # others are applied are not needed to reproduce it, so ddmin leaves them out
        assert len(res["conflicts"]) == 1 and 1 <= len(res["conflicts"][0]) <= valid
        d = res["dropped"][0]
        assert res["added_back"] == {f"{d['module']}#{d['idx']}": "proven"}
        add = [c for c in calls if "/final/libs/" in c.golden.lib_dir]
        assert len(add) == 1 and exists(tmp, os.path.join(add[0].golden.lib_dir, "top.sv"))
        joint = open(os.path.join(out, "consolidated_sources", "joint", "top_consolidated.sv")).read()
        first = rd(tmp, [c for c in calls if "/final/" in c.work_dir][0].candidate.overrides[0][1])
        assert joint != first
        log = open(os.path.join(out, "papercuts.log")).read()
        assert "final check of all" in log and "repaired: 1 conflict(s)" in log
        assert f"dropped {d['module']}#{d['idx']} (conflict; added back alone: proven)" in log


def test_pipeline_chain():
    with tempfile.TemporaryDirectory() as tmp:
        calls, exit_, out, rows = pipeline(tmp, "timeout", ["--fv-time-limit", "1000",
                                                              "--chain-group-size", "3"])
        assert exit_ is None, exit_
        valid = sum(1 for r in rows if r["phase"] == "cuts" and r["valid"])
        res = json.load(open(os.path.join(out, "final_check.json")))
        assert res["first_verdict"] == "inconclusive" and res["kept_count"] == valid
        steps = res["proof"]["steps"]
        assert res["proof"]["kind"] == "chain" and len(steps) >= 2
        assert sum(len(s["added"]) for s in steps) == valid
        chain = [c for c in calls if "/final/" in c.work_dir][1:]
        assert {c.time_limit_s for c in chain} <= {300, 900}
        later = [c for c in chain if "/final/libs/" in c.golden.lib_dir]
        assert later and all(exists(tmp, os.path.join(c.golden.lib_dir, "top.sv")) for c in later)
        assert "PROVEN by a chain of" in open(os.path.join(out, "papercuts.log")).read()


def run():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_final_check: OK ({len(tests)} tests)")
