"""The final check of the combined design, its repair, and chain proofs (--method fv).

Every proven cut is applied at once and the whole design is checked against the
elaborated one, which the gate proved equal to the original (`finalize`).

- Proven: done.
- Counterexample: the cuts conflict, and the set is REPAIRED. The failing set is
  reduced by delta debugging (`ddmin`) to a minimal conflict, one where removing
  any cut makes the failure go away. The next candidate drops a smallest set of
  cuts that meets every conflict found so far (`hitting_set`, z3's optimizer) and
  is checked whole again, until a candidate is proven.
- No verdict within --fv-time-limit (repaired or not): the candidate is proven by
  a CHAIN of small checks (`chain`). The cuts are applied in groups; each group's
  check compares the design before the group with the design after it, the first
  starting from the elaborated design, so the last design equals the original by
  transitivity. Every group's start and end are known in advance, so all chain
  checks run at once. Cuts whose own check took more than --chain-slow-cut
  seconds go first, one per group; the rest go in groups of --chain-group-size.
  A group that is not proven is halved (keeping its start and end). A single cut
  with no verdict is retried with a longer limit, then DROPPED, which changes the
  start of every later group, so those are checked again. A single cut with a
  counterexample conflicts with the cuts before it: that is a conflict for the
  repair, and the loop goes on with a new candidate.

Afterwards each cut the repair dropped is added back alone to the final set and
checked against it, which shows whether dropping it was needed.

A conflict always comes from a counterexample, never from a check that ran out
of time, so the drops are only as few as the checks that reached a verdict can
show. A check whose verdict is "error" or "unmapped" stops the run: neither says
anything about the cuts.

Checks are memoized by the text of both sides, so a chain repeated after a new
conflict only reruns the groups whose start or end changed, and a check that is
already running is joined, not started twice. Nothing is cancelled: a check made
stale by a drop runs out and is ignored. Every check is written to
papercuts.results.jsonl (phase "final"), and the result to final_check.json.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from papercuts.utils import Design, Run, print_tree

Cut = tuple  # (module name, cut index)


class FinalCheckError(Exception):
    """A final, repair or chain check gave a verdict that says nothing about the cuts."""


def name(k: Cut) -> str:
    return f"{k[0]}#{k[1]}"


# MARK: building a design with some of the cuts
class CutSet:
    """The texts of the design with a chosen set of proven cuts applied, exactly
    as consolidation builds them: each module cut with `cut_index`, then pruned
    (unless --in-situ or --no-prune-dead), excluded modules untouched."""

    def __init__(self, modules, *, prune: bool, fold_constants: bool, prune_fn=None):
        self.modules = modules
        self.prune = prune
        self.fold_constants = fold_constants
        self._prune_fn = prune_fn
        self._cache: dict = {}

    def module_text(self, mod, amounts: dict[int, int]) -> str:
        key = (mod.name, tuple(sorted(amounts.items())))
        if key not in self._cache:
            if mod.excluded:
                text = print_tree(mod.tree)
            else:
                text = print_tree(mod.pc.cut_index(sorted(amounts), dict(amounts))) if amounts \
                    else print_tree(mod.tree)
                if self.prune:
                    if self._prune_fn is None:
                        from papercuts.deadcode import prune_dead_source
                        self._prune_fn = prune_dead_source
                    text, _ = self._prune_fn(text, mod.name, fold_constants=self.fold_constants)
            self._cache[key] = text
        return self._cache[key]

    def texts(self, chosen: dict[Cut, int]) -> dict[str, str]:
        """{module: text} in module order, for {cut: amount}."""
        by: dict[str, dict[int, int]] = {m.name: {} for m in self.modules}
        for (m, i), amt in chosen.items():
            by[m][i] = amt
        return {m.name: self.module_text(m, by[m.name]) for m in self.modules}

    def blob(self, chosen: dict[Cut, int]) -> str:
        return "\n".join(self.texts(chosen).values())


# MARK: running checks
@dataclass
class Check:
    """One finished check: is the design with `cuts` equal to the design with `base`?"""
    verdict: str
    elapsed: float
    limit: "int | None"
    key: str
    base: tuple
    cuts: tuple
    why: str


def _covers(limit_have, limit_want) -> bool:
    """A run with limit_have answers a request with limit_want (None = no limit)."""
    if limit_have is None:
        return True
    return limit_want is not None and limit_have >= limit_want


class Checker:
    """Runs the final, repair and chain checks through the backend, under the
    pipeline's job semaphore.

    A check compares the design with `cuts` applied against the design with
    `base` applied. An empty base is the elaborated design itself (the golden
    library every cut was checked against); any other base is written out as a
    library of its own. Checks are keyed by the two texts."""

    def __init__(self, cutset: CutSet, amounts: dict[Cut, int], *, backend, semaphore, work_dir: str,
                 golden_dir: str, top: str, results_path: "str | None" = None, tracker=None):
        self.cs, self.amounts = cutset, amounts
        self.backend, self.sem = backend, semaphore
        self.work, self.golden_dir, self.top = work_dir, golden_dir, top
        self.results_path, self.tracker = results_path, tracker
        self._tasks: dict[str, list[tuple["int | None", asyncio.Task]]] = {}
        self.n_run = 0

    def _sel(self, cuts) -> dict[Cut, int]:
        return {k: self.amounts[k] for k in cuts}

    def launch(self, base, cuts, limit: "int | None", why: str) -> "asyncio.Task[Check]":
        """The check of `cuts` against `base`: a running or finished task that
        answers it, or a new one."""
        base, cuts = tuple(base), tuple(cuts)
        gold = self.cs.blob(self._sel(base)) if base else None
        cand = self.cs.blob(self._sel(cuts))
        key = hashlib.sha256(((gold or "<elaborated>") + "\0" + cand).encode()).hexdigest()
        for have, task in self._tasks.get(key, []):
            if not task.done():
                if _covers(have, limit):
                    return task
                continue
            c = task.result()
            if c.verdict != "inconclusive" or _covers(have, limit):
                return task
        task = asyncio.ensure_future(self._run(key, base, cuts, gold, cand, limit, why))
        self._tasks.setdefault(key, []).append((limit, task))
        return task

    async def check(self, base, cuts, limit, why) -> Check:
        return await self.launch(base, cuts, limit, why)

    async def drain(self) -> None:
        """Wait for every check still running (stale chain checks included)."""
        pending = [t for ts in self._tasks.values() for _, t in ts if not t.done()]
        if pending:
            await asyncio.gather(*pending)

    def _write_lib(self, key: str, base) -> str:
        lib = os.path.join(self.work, "libs", key[:16])
        os.makedirs(lib, exist_ok=True)
        for m, text in self.cs.texts(self._sel(base)).items():
            with open(os.path.join(lib, f"{m}.sv"), "w") as f:
                f.write(text)
        return lib

    async def _run(self, key, base, cuts, gold, cand, limit, why) -> Check:
        self.n_run += 1
        async with self.sem:
            wd = os.path.join(self.work, "checks", f"{key[:16]}_{limit or 'nolimit'}")
            os.makedirs(wd, exist_ok=True)
            lib = self._write_lib(key, base) if base else self.golden_dir
            path = os.path.join(wd, f"{self.top}_candidate.sv")
            with open(path, "w") as f:
                f.write(cand)
            golden = Design(self.top, lib)
            run = Run(golden=golden, candidate=golden.override(self.top, path), work_dir=wd, index=-1,
                      rewrite_path=path, time_limit_s=limit, stop_at_first_cex=True)
            tid = ("final", key, limit)
            if self.tracker is not None:
                self.tracker.start(tid, "final", why)
            t0 = time.time()
            try:
                await self.backend.check(run)
            finally:
                if self.tracker is not None:
                    self.tracker.finish(tid, run.verdict, run.valid)
            c = Check(run.verdict, round(time.time() - t0, 3), limit, key, base, cuts, why)
            if self.results_path:
                with open(self.results_path, "a") as f:
                    f.write(json.dumps({
                        "ts": time.time(), "phase": "final", "module": self.top, "idx": -1, "type": why,
                        "line": None, "valid": run.valid, "verdict": run.verdict, "elapsed": c.elapsed,
                        "limit": limit, "base": len(base), "applied": len(cuts),
                        "added": [name(k) for k in cuts if k not in set(base)] if base else None,
                        "key": key[:16],
                    }) + "\n")
            return c


def definite(c: Check) -> Check:
    if c.verdict not in ("proven", "cex", "inconclusive"):
        raise FinalCheckError(f"{c.why}: the check {c.key[:16]} ({len(c.base)} -> {len(c.cuts)} cuts) "
                              f"gave {c.verdict!r}")
    return c


# MARK: delta debugging and the hitting set
async def ddmin(items: list, fails: Callable[[list], Awaitable[bool]]) -> list:
    """A subset of the failing `items` that still fails, and passes when any one
    item is removed (Zeller's ddmin). A round's subsets are checked at once and
    the first failing one in order is taken, so the result is the sequential
    algorithm's. Only sets seen to fail are kept: no monotonicity is assumed."""
    n = 2
    while len(items) >= 2:
        size = len(items) // n
        chunks = [items[i:i + size] for i in range(0, len(items), size)] if size else [[x] for x in items]
        res = await asyncio.gather(*(fails(c) for c in chunks))
        hit = next((c for c, r in zip(chunks, res) if r), None)
        if hit is not None:
            items, n = hit, 2
            continue
        rests = [[x for x in items if x not in c] for c in chunks] if len(chunks) > 2 else []
        res = await asyncio.gather(*(fails(r) for r in rests))
        hit = next((r for r, ok in zip(rests, res) if ok and r), None)
        if hit is not None:
            items, n = hit, max(n - 1, 2)
            continue
        if n >= len(items):
            break
        n = min(len(items), 2 * n)
    return items


def hitting_set(conflicts: list[list[Cut]], prefer: set = frozenset()) -> set:
    """A smallest set of cuts meeting every conflict (z3's optimizer). Among the
    smallest, cuts in `prefer` are taken first."""
    import z3
    if not conflicts:
        return set()
    cuts = sorted({k for c in conflicts for k in c})
    v = {k: z3.Bool(f"c{i}") for i, k in enumerate(cuts)}
    o = z3.Optimize()
    for c in conflicts:
        o.add(z3.Or([v[k] for k in c]))
    for k in cuts:
        o.add_soft(z3.Not(v[k]), weight=1000 + (0 if k in prefer else 1))
    if o.check() != z3.sat:
        raise FinalCheckError("no set of cuts meets every conflict")
    m = o.model()
    return {k for k in cuts if z3.is_true(m.eval(v[k]))}


# MARK: chain proofs
@dataclass
class ChainLimits:
    group_s: int = 300           # a group not proven within it is halved
    single_s: int = 900          # first limit for one cut: max(single_s, 3x its own check)
    retry_s: int = 3600          # retry limit for one cut: max(retry_s, 2x its first limit)
    group_size: int = 16
    slow_s: float = 60           # cuts whose own check took longer go first, one per group


@dataclass
class _Seg:
    cuts: list
    limit: int
    retried: bool = False


@dataclass
class ChainResult:
    proven: bool
    steps: list = field(default_factory=list)     # proven: one dict per group, in order
    dropped: list = field(default_factory=list)   # single cuts with no verdict
    cex_cut: "Cut | None" = None                  # not proven: the single cut with a counterexample
    cex_base: list = field(default_factory=list)  # and the cuts applied before it


async def chain(checker, target: list[Cut], own_s: dict[Cut, float], lim: ChainLimits) -> ChainResult:
    """Prove the design with `target` equal to the elaborated design by a chain."""
    def seg(cuts: list) -> _Seg:
        """A group's limit is group_s; a single cut's first limit scales with its own check."""
        if len(cuts) > 1:
            return _Seg(cuts, lim.group_s)
        return _Seg(cuts, max(lim.single_s, int(3 * own_s.get(cuts[0], 0))))

    slow = sorted((k for k in target if own_s.get(k, 0) > lim.slow_s), key=lambda k: (own_s[k], k))
    slow_set = set(slow)
    fast = [k for k in target if k not in slow_set]
    segs = [seg([k]) for k in slow] + [seg(fast[i:i + lim.group_size])
                                       for i in range(0, len(fast), lim.group_size)]
    dropped: list[Cut] = []

    def why(s: _Seg) -> str:
        return f"chain: single {name(s.cuts[0])}" if len(s.cuts) == 1 else f"chain: group of {len(s.cuts)}"

    while True:
        start: list[Cut] = []
        pending, settled, changed = [], True, False
        for j, s in enumerate(segs):
            t = checker.launch(start, start + s.cuts, s.limit, why(s))
            if not t.done():
                pending.append(t)
                settled = False
                start = start + s.cuts
                continue
            c = definite(t.result())
            if c.verdict == "proven":
                start = start + s.cuts
                continue
            if len(s.cuts) > 1:
                h = len(s.cuts) // 2
                segs[j:j + 1] = [seg(s.cuts[:h]), seg(s.cuts[h:])]
                changed = True
                break
            k = s.cuts[0]
            if c.verdict == "inconclusive":
                if not s.retried:
                    s.limit, s.retried = max(lim.retry_s, 2 * s.limit), True
                else:
                    dropped.append(k)
                    del segs[j]
                changed = True
                break
            # a counterexample: it conflicts with the cuts before it, once every
            # group before it is proven from the start it has now
            if settled:
                return ChainResult(False, dropped=dropped, cex_cut=k, cex_base=list(start))
            settled = False
            start = start + s.cuts
        if changed:
            continue
        if settled:
            steps, start = [], []
            for s in segs:
                c = checker.launch(start, start + s.cuts, s.limit, why(s)).result()
                steps.append({"from": len(start), "added": [name(k) for k in s.cuts], "key": c.key[:16],
                              "elapsed": c.elapsed, "limit": c.limit})
                start = start + s.cuts
            return ChainResult(True, steps=steps, dropped=dropped)
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)


# MARK: the final check, repaired and chained as needed
@dataclass
class FinalResult:
    kept: list
    dropped: list            # [(cut, reason)], reason "conflict" or "no verdict"
    conflicts: list
    proof: dict
    added_back: dict
    first_verdict: str       # the final check of every proven cut, before any repair
    checks: int = 0


async def finalize(checker, cuts: list[Cut], own_s: dict[Cut, float], limit: "int | None",
                   lim: ChainLimits, pairs: "dict[Cut, Cut] | None" = None,
                   log: Callable[[str], None] = lambda s: None) -> FinalResult:
    """Prove a subset of `cuts` (all proven alone) equal to the elaborated design,
    dropping the fewest the checks show must go."""
    pairs = pairs or {}
    conflicts: list[list[Cut]] = []
    culprits: set = set()        # single cuts a chain step refuted
    no_verdict: list[Cut] = []   # dropped by a chain
    R: set = set()
    first = None

    async def fails(s):
        return definite(await checker.check([], s, limit, "repair: shrink")).verdict == "cex"

    while True:
        cand = [k for k in cuts if k not in R and k not in no_verdict]
        why = "final" if first is None else f"repair: candidate (dropping {len(R) + len(no_verdict)})"
        c = definite(await checker.check([], cand, limit, why))
        if first is None:
            first = c.verdict
        log(f"{why}: {len(cand)} cuts -> {c.verdict} ({c.elapsed:.0f} s)")
        if c.verdict == "proven":
            proof = {"kind": "single", "key": c.key[:16], "elapsed": c.elapsed, "limit": c.limit}
            break
        if c.verdict == "cex":
            conflict = await ddmin(cand, fails)
        else:
            log(f"no verdict in {limit} s: proving the {len(cand)} cuts by a chain")
            ch = await chain(checker, cand, own_s, lim)
            no_verdict += [k for k in ch.dropped if k not in no_verdict]
            if ch.dropped:
                log(f"chain dropped (no verdict): {[name(k) for k in ch.dropped]}")
            if ch.proven:
                proof = {"kind": "chain", "steps": ch.steps,
                         "elapsed": round(sum(s["elapsed"] for s in ch.steps), 1)}
                break
            k = ch.cex_cut
            log(f"chain: adding {name(k)} after {len(ch.cex_base)} cuts gives a counterexample")
            culprits.add(k)
            # the cuts before k were just proven together, so k is in the conflict
            rest = await ddmin(ch.cex_base, lambda s: fails(s + [k])) if ch.cex_base else []
            conflict = sorted(rest + [k])
        conflicts.append(conflict)
        log(f"conflict {len(conflicts)}: {[name(k) for k in conflict]}")
        open_ = [cf for cf in conflicts if not set(cf) & set(no_verdict)]
        R = hitting_set(open_, prefer=set(pairs) | culprits)

    kept = [k for k in cuts if k not in R and k not in no_verdict]
    added_back = {}
    if R:
        res = await asyncio.gather(*(checker.check(kept, sorted(kept + [k]), limit, f"repair: add back {name(k)}")
                                     for k in sorted(R)))
        added_back = {name(k): definite(r).verdict for k, r in zip(sorted(R), res)}
    await checker.drain()
    dropped = [(k, "conflict") for k in sorted(R)] + [(k, "no verdict") for k in no_verdict]
    return FinalResult(kept, dropped, conflicts, proof, added_back, first, checker.n_run)
