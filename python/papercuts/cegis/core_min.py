"""Minimal conflict cores with CaDiCaL (opt-in: CombCegis(core_levels=...)).

The deletion algorithm is smt_ec/src/sat_core.py's (handoff/09), copied on
2026-11-02: `parse_levels`, `_delete` and `_extra` are unchanged. The encoder is
not. smt_ec bit-blasts the design's `bad` by hand, which cannot express this
loop's spec (it also compares next states and reads registers through
registers.RegPair) and would be a second copy of the semantics. Here the CNF
comes from the formula the synthesizer itself gets, `CombCegis._cex_formula`:
the spec on the counterexample, folded so only the selects are left.

Exactness is the same argument as smt_ec's. The clauses are equisatisfiable with
that formula with every select free, so a set of select literals that is UNSAT
under assumptions fails every assignment agreeing with it. A select the folded
formula does not mention cannot be in a core and is left out of the candidate.

    r = minimize(formula, [("pc_sel0", True), ...], levels="10,50,250,1000")
    r.lits   # the core: [(select, value in the candidate)]
    r.extra  # further exact cores found on the way, to learn as well

With an `inf` top level the core ends minimal; otherwise a literal still deferred
at the top level stays in (`unresolved`), so it stays exact but perhaps not minimal.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import z3

SOLVER = "cadical195"
CAP = 3  # an attempt at a level may spend CAP times its per-round budget over all its rounds
EXTRA_MAX = 10  # extra cores have at most this many literals


class NoCore(Exception):
    """The formula fails whatever the selects are."""


@dataclass
class MinCore:
    lits: list[tuple[str, bool]]
    first_core: int  # size of failed() before deletion
    extra: list[list[tuple[str, bool]]] = field(default_factory=list)
    checks: int = 0
    unresolved: int = 0
    encode_s: float = 0.0
    deletion_s: float = 0.0
    vars: int = 0
    clauses: int = 0


def parse_levels(spec) -> tuple:
    """Deletion's conflict levels from text ("10,50,250,1000", "10,50,inf") or a
    sequence of numbers: each level's per-round conflict budget, positive integers
    strictly increasing, the last of which may be inf (no budget). There is no
    default; anything else is a ValueError."""
    if spec is None:
        raise ValueError("core levels are required (e.g. 10,50,250,1000)")
    items = spec.split(",") if isinstance(spec, str) else list(spec)
    out = []
    for x in items:
        if isinstance(x, str):
            v = math.inf if x.strip() == "inf" else int(x.strip()) if x.strip().isdigit() else None
        else:
            v = x if x == math.inf or (isinstance(x, int) and not isinstance(x, bool)) else None
        if v is None or v <= 0:
            raise ValueError(f"core levels {spec!r}: {x!r} is not a positive integer or inf")
        out.append(v)
    if not out:
        raise ValueError(f"core levels {spec!r}: none given")
    if any(b <= a for a, b in zip(out, out[1:])):
        raise ValueError(f"core levels {spec!r}: must be strictly increasing, with inf only last")
    return tuple(out)


def _delete(s, core, cand, levels):
    """Deletion over the core by progressive unfixing, scheduled breadth first over
    conflict levels; smt_ec/src/sat_core.py has the full account. `cand` is the
    candidate's select literals; `core` (K) a subset of them that fails.

    An attempt to drop l from K starts with the selects fixed at the candidate's
    values except l and frees only the ones a refutation uses. Each round is one
    solve with a conflict budget:
      SAT: l is needed (a proof).
      UNSAT with failed() smaller than K: failed() is an exact core without l; K = it.
      UNSAT otherwise: free failed() minus K and solve again.
      Out of budget: deferred to the next level.

    Returns (K, checks, unresolved, pool), pool being every failed() set seen, each
    itself an exact core."""
    keep = list(core)
    checks = 0
    deferred: dict[int, int] = {}  # literal -> the level its last attempt ran out at, against the current K
    necessary: set[int] = set()
    pool: set[frozenset] = set()
    top = len(levels) - 1

    def level(l):
        return deferred[l] + 1 if l in deferred else 0

    while len(keep) > 1:
        todo = [l for l in keep if l not in necessary and level(l) <= top]
        if not todo:
            break
        lit = min(todo, key=lambda l: (level(l), l > 0, abs(l)))
        lv = level(lit)
        budget = levels[lv]
        checks += 1
        assume = [c for c in cand if c != lit]
        used = 0
        r = None
        while True:
            b = min(budget, CAP * budget - used)
            if b <= 0:  # the cap is spent
                r = None
                break
            before = s.accum_stats()["conflicts"]
            s.conf_budget(-1 if b == math.inf else b)  # -1: no limit
            r = s.solve_limited(assumptions=assume)
            used += s.accum_stats()["conflicts"] - before
            if r is None and b == math.inf:
                raise RuntimeError("CaDiCaL stopped without a conflict budget")
            if r is not False:
                break
            got = set(s.get_core() or [])
            if not got:
                raise NoCore("deletion: the formula fails whatever the selects are")
            pool.add(frozenset(got))
            if len(got) < len(keep):
                break
            inside = set(keep)
            assume = [a for a in assume if a not in got or a in inside]
        if r is False:
            new = [c for c in cand if c in got]
            if not set(new) <= set(keep):  # a swap: a new core gets its cheap pass first
                deferred.clear()
                necessary.clear()
            keep = new
        elif r:
            necessary.add(lit)
        else:
            deferred[lit] = lv
    unresolved = sum(1 for l in keep if l not in necessary) if len(keep) > 1 else 0
    return keep, checks, unresolved, pool


def _extra(pool, final):
    """The sets of `pool` to learn besides the final core: at most EXTRA_MAX
    literals, not the final core, and no other pool set inside them, so none implies
    another's clause. Sorted, so the same pool gives the same list."""
    final = frozenset(final)
    out = [f for f in pool if f != final and len(f) <= EXTRA_MAX and not any(g < f for g in pool)]
    return sorted(out, key=lambda f: (len(f), sorted((abs(l), l) for l in f)))


def to_cnf(formula: z3.BoolRef) -> tuple[list[list[int]], dict[str, int]]:
    """CNF of `formula`, every Boolean constant keeping its name: (clauses, name -> variable).
    No value propagation or equality solving: either could remove a select the
    core must be able to name."""
    goals = z3.Then("simplify", "bit-blast", "tseitin-cnf")(formula)
    if len(goals) != 1:
        raise RuntimeError(f"expected one CNF goal, got {len(goals)}")
    var: dict[str, int] = {}

    def lit(e) -> int:
        neg = z3.is_not(e)
        a = e.arg(0) if neg else e
        if not z3.is_const(a) or a.decl().kind() != z3.Z3_OP_UNINTERPRETED:
            raise RuntimeError(f"not a CNF literal: {e}")
        v = var.setdefault(a.decl().name(), len(var) + 1)
        return -v if neg else v

    clauses: list[list[int]] = []
    for c in goals[0]:
        if z3.is_false(c):
            clauses.append([])
        elif z3.is_or(c):
            clauses.append([lit(x) for x in c.children()])
        elif not z3.is_true(c):
            clauses.append([lit(c)])
    return clauses, var


def minimize(formula: z3.BoolRef, literals: list[tuple[str, bool]], levels) -> MinCore:
    """A minimal subset of `literals` (select, value) under which `formula` is
    UNSAT. The whole list must be UNSAT, or RuntimeError. Empty `lits` means the
    formula is false whatever the selects are."""
    from pysat.solvers import Solver

    levels = parse_levels(levels)
    t0 = time.time()
    clauses, var = to_cnf(formula)
    encode_s = time.time() - t0
    if any(not c for c in clauses):
        return MinCore(lits=[], first_core=0, encode_s=encode_s)

    cand = [var[n] if v else -var[n] for n, v in literals if n in var]
    order = {n: i for i, (n, _) in enumerate(literals)}
    name_of = {v: n for n, v in var.items()}

    def named(core):
        return sorted(((name_of[abs(l)], l > 0) for l in core), key=lambda x: order[x[0]])

    with Solver(name=SOLVER, bootstrap_with=clauses) as s:
        if s.solve(assumptions=cand) is not False:
            raise RuntimeError("the candidate satisfies the counterexample's formula: nothing to minimize")
        first = list(s.get_core() or [])
        if not first:
            return MinCore(lits=[], first_core=0, encode_s=encode_s, vars=len(var), clauses=len(clauses))
        t0 = time.time()
        keep, checks, unresolved, pool = _delete(s, first, cand, levels)
        deletion_s = time.time() - t0
    pool |= {frozenset(first), frozenset(keep)}
    return MinCore(lits=named(keep), first_core=len(first), extra=[named(f) for f in _extra(pool, keep)],
                   checks=checks, unresolved=unresolved, encode_s=encode_s, deletion_s=deletion_s,
                   vars=len(var), clauses=len(clauses))
