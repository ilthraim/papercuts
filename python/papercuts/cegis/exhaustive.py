"""Ground truth by brute force, for circuits small enough to afford it. It checks the
search against every assignment, using nothing the search learned.

    t = exhaustive_max(c)                  one verifier call per legal assignment
    t.max_size, t.maxima                   the true optimum and every set attaining it
    t.valid_by_size                        {size: valid sets}; a size with none, below the
                                           maximum, is a hole no exact-k search can see past
    m = minimum_core(c, cex, assignment)   every smallest core of this counterexample
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import z3

from papercuts.cegis.comb import CombCegis

LIMIT = 14  # 2**14 verifier calls is minutes; much past this is not a small circuit


@dataclass
class Exhaustive:
    max_size: int
    maxima: list[frozenset]
    valid_by_size: dict[int, int]
    tested_by_size: dict[int, int]
    individually_valid: list[str]
    verify_calls: int

    @property
    def holes(self) -> list[int]:
        """Sizes below the maximum with no valid set."""
        return [k for k in range(self.max_size) if not self.valid_by_size.get(k)]

    @property
    def non_monotone_cuts(self) -> list[str]:
        """Cuts in some maximum set that fail on their own: they only work in composition."""
        alone = set(self.individually_valid)
        return sorted({n for s in self.maxima for n in s} - alone)


def _too_big(n: int, limit: int) -> None:
    if n > limit:
        raise ValueError(f"{n} selects: over the limit of {limit} for brute force")


def exhaustive_max(c: CombCegis, limit: int = LIMIT) -> Exhaustive:
    names = c.sel_names
    _too_big(len(names), limit)
    max_size, maxima, calls = -1, [], 0
    valid: dict[int, int] = {}
    tested: dict[int, int] = {}
    alone = []
    for mask in range(1 << len(names)):
        on = frozenset(n for i, n in enumerate(names) if mask >> i & 1)
        if any(a in on and b in on for a, b in c.exclusive):
            continue
        calls += 1
        tested[len(on)] = tested.get(len(on), 0) + 1
        if c.verify({n: n in on for n in names}) is not None:
            continue
        valid[len(on)] = valid.get(len(on), 0) + 1
        if len(on) == 1:
            alone.extend(on)
        if len(on) > max_size:
            max_size, maxima = len(on), []
        if len(on) == max_size:
            maxima.append(on)
    return Exhaustive(max_size, maxima, valid, tested, sorted(alone), calls)


def fails_under(formula: z3.BoolRef, c: CombCegis, lits: list[tuple[str, bool]]) -> bool:
    """Does every assignment agreeing with `lits` fail this counterexample's formula?"""
    s = z3.Solver()
    s.add(formula)
    return s.check(*[c.sels[n] if v else z3.Not(c.sels[n]) for n, v in lits]) == z3.unsat


def minimum_core(c: CombCegis, cex: dict, assignment: dict[str, bool], limit: int = LIMIT) -> list[list[tuple[str, bool]]]:
    """Every smallest subset of the assignment's literals that fails `cex`."""
    _too_big(len(c.sel_names), limit)
    formula = c._cex_formula(cex, c.sel_bv)
    lits = [(n, bool(assignment[n])) for n in c.sel_names]
    for size in range(len(lits) + 1):
        found = [list(sub) for sub in itertools.combinations(lits, size) if fails_under(formula, c, list(sub))]
        if found:
            return found
    raise RuntimeError("the candidate passes its own counterexample")


def is_minimal(c: CombCegis, cex: dict, lits: list[tuple[str, bool]]) -> bool:
    """`lits` fails `cex`, and dropping any one literal stops it failing."""
    formula = c._cex_formula(cex, c.sel_bv)
    return fails_under(formula, c, lits) and not any(fails_under(formula, c, lits[:i] + lits[i + 1:])
                                                     for i in range(len(lits)))
