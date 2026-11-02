"""The baseline the MaxSAT synthesizer replaced: binary search over the cut count.

    r = exact_k_search(CombCegis(design, ...))
    r.on, r.k, r.verify_calls

For each k it runs a CEGIS loop whose synthesizer asks for exactly k selects on,
over the counterexamples so far and with no conflict cores, then keeps the upper
half if k is feasible and the lower half if not. Reimplemented on the current loop
on 2026-11-02 so both searches share a verifier and a spec; the August version
is not in the tree.

The synthesizer is a SAT pipeline rather than z3.Solver(), which gave no answer to the
first exactly-k query in 30 minutes on simple_spi and others (why is not understood: a
toy of the same size with exclusive pairs is instant), and rather than SolverFor("QF_FD"),
whose models broke the PbEq (asked for 47 selects on simple_spi, returned 46). A candidate
of the wrong size is a hard error, since k is what this search reports.

Discarding the upper half on failure assumes that no set of k cuts means no larger
set. Feasibility is not monotone, so that can be wrong and the search can stop
below the true maximum. Whatever it returns did pass the verifier, so the result is
sound but possibly not maximum. Counterexamples hold for every k, so they carry over.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import z3

from papercuts.cegis.comb import CombCegis, MiterMismatch


@dataclass
class ExactK:
    on: dict[str, bool]
    k: int
    verify_calls: int
    feasibility: list[tuple[int, bool]]  # (k, feasible), in search order
    total_s: float


def _synthesizer() -> z3.Solver:
    return z3.Then("simplify", "propagate-values", "card2bv", "bit-blast", "aig", "tseitin-cnf", "sat").solver()


def exact_k_search(c: CombCegis) -> ExactK:
    t_start = time.time()
    names = c.sel_names
    sels = [c.sels[n] for n in names]
    s = _synthesizer()
    for a, b in c.exclusive:
        s.add(z3.Not(z3.And(c.sels[a], c.sels[b])))

    calls = 0

    def check(asg: dict[str, bool], k: int) -> dict | None:
        nonlocal calls
        calls += 1
        c.stats.append({"iter": len(c.stats), "phase": "exact-k", "k": k,
                        "assignment": [n for n in names if asg[n]]})
        cex = c.verify(asg)
        c.stats[-1]["verify_result"], c.stats[-1]["verify_s"] = c.last_verify
        return cex

    off = dict.fromkeys(names, False)
    cex = check(off, 0)
    if cex is not None:
        raise MiterMismatch(cex, "the copies differ with all cuts off")
    best, lo, hi = off, 0, len(names)
    feasibility = []
    while lo < hi:
        k = (lo + hi + 1) // 2
        found = None
        s.push()
        s.add(z3.PbEq([(b, 1) for b in sels], k))
        while True:
            t0 = time.time()
            r = s.check()
            if c.stats:
                c.stats[-1]["synth_s"] = time.time() - t0
                c.flush_trace()
            if r != z3.sat:
                if r != z3.unsat:
                    raise RuntimeError(f"exact-{k} synthesizer returned {r}")
                break
            m = s.model()
            asg = {n: z3.is_true(m.eval(c.sels[n], model_completion=True)) for n in names}
            if sum(asg.values()) != k:
                raise RuntimeError(f"exact-{k} synthesizer returned {sum(asg.values())} selects")
            cex = check(asg, k)
            if cex is None:
                found = asg
                break
            c.cex_traces.append(cex)
            s.pop()
            s.add(c._cex_formula(cex, c.sel_bv))
            s.push()
            s.add(z3.PbEq([(b, 1) for b in sels], k))
        s.pop()
        feasibility.append((k, found is not None))
        if found is not None:
            best, lo = found, k
        else:
            hi = k - 1
    c.flush_trace()
    return ExactK(on=best, k=lo, verify_calls=calls, feasibility=feasibility, total_s=time.time() - t_start)
