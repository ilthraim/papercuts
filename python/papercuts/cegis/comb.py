"""
CEGIS over a papercuts-muxed miter with a combinational spec: the largest set
of cuts that, together, leave the logic between the registers unchanged.

    c = CombCegis(design, selects=[...], registers=[...], exclusive=[...])
    on = c.run()             # {select: bool}; c.base_ok says whether it holds from power-up
    c.verify({sel: ...})     # None if that one assignment meets the spec, else a counterexample

Spec. Let R be "every matched register pair (mod.X, orig.X) holds equal values"
(registers.match_registers). For every state s and input i:

    R(s)  =>  ok(s, i)  and  R(next(s, i))

i.e. from any shared state the two copies give equal outputs now and load equal
registers at the clock edge. The verifier asks for a violation with the
selects pinned:

  unsat  the candidate is accepted. R is inductive and implies `eq`; with R at
         power-up (base(), run once on the result) the miter is equivalent for
         all time.
  sat    the model is a genuine counterexample to the spec -- one state pair
         and one input vector -- and goes to the conflict core and the
         synthesizer. It need not be reachable: a sat answer says the cut
         changes the logic, not that the design misbehaves.

The result is therefore sound but conservative next to a sequential checker:
cuts that are valid only through reachability (a counter bit that is never
set) or through cancellation (a register that differs but never reaches an
output) come out off.

Reset plays no part. Power-up X values are mapped equal across the copies
(Jasper's auto-mapping, registers.PowerUp), and R, being preserved by every
step, still holds when reset ends. The base case therefore only checks what
power-up fixes regardless of reset: ROM contents.

X sources are inputs the BTOR2 already pairs across the copies
(xpair.pair_x); an anonymous X input (yosys's own `_anon<N>`) means the netlist
skipped that pass and is refused.

Counterexample format: {"inputs": [frame0, ...], "init": {state: value}}. It
has one frame, or two when some paired state has no next function: R after the
step is then read in a second frame.

Copied from smt_ec/src_comb/cegis_comb.py (rewriting repo 12a5a7f + working
tree, 2026-10-23); the two copies are maintained separately.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable

import z3

from papercuts.cegis import core_min
from papercuts.cegis.btor2z3 import Design, Trace, _conj, model_value
from papercuts.cegis.registers import correspondence, match_registers, power_up
from papercuts.cegis.solvers import check, make_solver, resolve_solver, run_external, smt2_text
from papercuts.cegis.solvers import core as solver_core
from papercuts.cegis.trace import TraceWriter

# Papercuts names a module's own selects pc_sel<N> and the ones it forwards for a
# child pc_sel_<module>_<N>, so a hierarchical top carries both shapes. Only the
# fallback when no manifest names the selects.
SEL_RE = r"^pc_sel(?:_\w+_)?\d+$"


def sel_key(n: str) -> int:
    m = re.search(r"(\d+)$", n)
    return int(m.group(1)) if m else 0


class MiterMismatch(RuntimeError):
    """A counterexample the copies fail with every cut off: they differ on their
    own, or frame 0 lets them start apart (a register left unpaired)."""

    def __init__(self, cex: dict, how: str = "counterexample independent of every select (empty conflict core)"):
        self.cex = cex
        super().__init__(
            f"{how}: the original and the muxed copy differ with all cuts off, or start in different "
            f"states; trace {cex['inputs']} init={cex['init']}"
        )


class AnonymousX(ValueError):
    """The BTOR2 carries yosys's own anonymous X inputs, which are independent in
    the two copies; build it with netlist.build_btor, which pairs them."""


class _DictModel:
    """Minimal stand-in for z3.ModelRef when values come from an external solver."""

    def __init__(self, vals):
        self.vals = vals

    def eval(self, e, model_completion=True):
        return z3.BitVecVal(self.vals.get(e.decl().name(), 0), e.size())


def _no_events(kind: str, **fields) -> None:
    pass


class CombCegis:
    def __init__(
        self,
        design: Design,
        sel_re: str = SEL_RE,
        verbose: bool = True,
        solver: str = "auto",
        exclusive: list[tuple[str, str]] | None = None,
        selects: list[str] | None = None,
        pin_zero: list[str] | None = None,
        registers: list[tuple[str, int]] | None = None,
        on_event: Callable[..., None] | None = None,
        timeout: float | None = None,
        core_levels=None,
        learn_cores: bool = True,
        steer=None,
        trace_path: str | None = None,
    ):
        """`core_levels`: minimize each conflict core with CaDiCaL (core_min), at these
        conflict levels; None uses the solver's raw core. `learn_cores=False` learns
        counterexamples only, the ablation baseline. `steer`: see steer.py.
        `trace_path`: write one JSON line per verifier call (trace.py)."""
        if core_levels is not None and not learn_cores:
            raise ValueError("core_levels minimizes learned cores, and learn_cores=False learns none")
        self.d = design
        self.core_levels = None if core_levels is None else core_min.parse_levels(core_levels)
        self.learn_cores = learn_cores
        self.steer = steer
        anon = design.input_names(r"^_anon\d+$")
        if anon:
            raise AnonymousX(f"{len(anon)} anonymous X input(s), e.g. {anon[:5]}")
        self.verbose = verbose
        self.on_event = on_event or _no_events
        self.solver = resolve_solver(solver)
        self.timeout = timeout  # seconds per solver call; None = no limit
        # (RTL path, width) per register (registers.matched_registers); empty
        # means pair by BTOR2 state name, the synchronous case
        self.registers = list(registers or [])
        self.sel_names = list(selects) if selects is not None else design.input_names(sel_re)
        # a mux steered by a select is a cut, never read through to a flip-flop
        self.pairs, self.unmatched = match_registers(design, [p for p, _ in self.registers] or None,
                                                     selects=set(self.sel_names))
        # power-up without a reset: ROM contents fixed, every other pair equal (module docstring)
        self.power = power_up(design, self.registers or None, selects=set(self.sel_names))
        # R after the step: read off `next` directly when every paired state has a
        # next function, else from a second frame
        self.two_frames = any(s not in design.next for p in self.pairs for s in p.states())
        # inert selects pinned to 0 so they are never reported as valid cuts
        self.zero_names = [n for n in (pin_zero or []) if n in design.inputs]
        self.pinned = {n: z3.BitVecVal(0, design.width(n)) for n in self.zero_names}
        # Clock inputs are ordinary inputs here, recorded in counterexamples like
        # the rest: a design that also reads its clock as data (opencores cop,
        # wb_clk_i) gave counterexamples the conflict core could not reproduce.
        self.free_names = [n for n in design.input_names(exclude_clocks=False)
                           if n not in self.sel_names and n not in self.zero_names]
        self.sels: dict[str, z3.BoolRef] = {n: z3.Bool(n) for n in self.sel_names}
        self.sel_bv = {n: z3.If(b, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1)) for n, b in self.sels.items()}
        self.synth = z3.Optimize()
        for b in self.sels.values():
            self.synth.add_soft(b, 1)
        self.hard: list[z3.BoolRef] = []  # all the synthesizer's constraints: lex steering rebuilds from these
        self.exclusive = [(a, b) for a, b in (exclusive or []) if a in self.sels and b in self.sels]
        for a, b in self.exclusive:
            self._add_hard(z3.Not(z3.And(self.sels[a], self.sels[b])))
        self.cex_traces: list[dict] = []
        self.learned: list[z3.BoolRef] = []
        self.cores: list[list[tuple[str, bool]]] = []  # each learned core, as (select, value) literals
        self._learned_keys: set[frozenset] = set()
        self.stats: list[dict] = []
        self.base_ok: bool | None = None  # set by run(): R at power-up on the result
        self.base_evidence = ""
        self.total_s: float | None = None
        self.last_extra: list = []  # further exact cores from the last conflict_core
        self.trace = (TraceWriter(trace_path, self.sel_names, self.exclusive, two_frames=self.two_frames)
                      if trace_path else None)
        self._traced = 0  # stats records already written

    def _add_hard(self, c: z3.BoolRef) -> None:
        self.hard.append(c)
        self.synth.add(c)

    def flush_trace(self) -> None:
        """Write the records not yet written. A record is complete once the next
        candidate exists, since it carries the time spent producing that candidate."""
        if self.trace is None:
            return
        for st in self.stats[self._traced:]:
            self.trace.iteration(st)
        self._traced = len(self.stats)

    def log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    # ------------------------------------------------------------ spec
    def _trace(self, sel_exprs, prefix: str, inputs: list[dict] | None = None, init: dict | None = None) -> Trace:
        shared = dict(self.pinned)
        shared.update(sel_exprs)
        return self.d.unroll(2 if self.two_frames else 1, shared=shared, prefix=prefix,
                             inputs_per_frame=inputs, init_states=init)

    def _r_next(self, tr: Trace) -> z3.BoolRef:
        f0 = tr.frames[0]
        if self.two_frames:
            return correspondence(tr.frames[1], self.pairs, self.d)
        return _conj([x == y for x, y in (p.next_value(f0) for p in self.pairs)])

    def _spec(self, tr: Trace) -> tuple[list, z3.BoolRef]:
        """(hypotheses, goal): constraints and R in frame 0  =>  ok(f0) and R after the step."""
        f0 = tr.frames[0]
        return [tr.assumed, correspondence(f0, self.pairs, self.d)], z3.And(f0.ok, self._r_next(tr))

    def _sel_lits(self, assignment):
        return {n: z3.BitVecVal(1 if assignment[n] else 0, 1) for n in self.sel_names}

    def _cex_trace(self, cex: dict, sel_exprs) -> Trace:
        inputs = [{n: z3.BitVecVal(v, self.d.width(n)) for n, v in fr.items()} for fr in cex["inputs"]]
        init = {n: z3.BitVecVal(v, self.d.width(n)) for n, v in cex["init"].items()}
        return self._trace(sel_exprs, "cx", inputs, init)

    def _cex_formula(self, cex: dict, sel_exprs) -> z3.BoolRef:
        """The spec on this counterexample, folded: everything but the selects is
        a literal, so simplify() leaves a formula over the selects alone."""
        tr = self._cex_trace(cex, sel_exprs)
        hyp, goal = self._spec(tr)
        return z3.simplify(z3.And(*hyp, goal))

    # ------------------------------------------------------------ verify
    def verify(self, assignment: dict[str, bool]) -> dict | None:
        """The spec with the selects pinned. None if it holds, else a counterexample."""
        t0 = time.time()
        tr = self._trace(self._sel_lits(assignment), "v")
        hyp, goal = self._spec(tr)
        r, m = self._solve(hyp + [z3.Not(goal)], tr)
        dt = time.time() - t0
        self.log(f"  verify[{self.solver}]: {r} in {dt:.2f}s")
        self.on_event("verify", result=str(r), s=round(dt, 3))
        self.last_verify = (str(r), dt)
        if r == z3.unsat:
            return None
        if r != z3.sat:
            raise RuntimeError(f"verifier returned {r} (timeout or solver error): no answer is not an answer")
        frames = [{n: model_value(m, f.inputs[n]) for n in self.free_names} for f in tr.frames]
        init = {n: model_value(m, tr.frames[0].states[n]) for n in self.d.states}
        return {"inputs": frames, "init": init}

    def _solve(self, assertions: list, tr: Trace):
        if not self.solver.startswith("ext:"):
            s = make_solver(self.solver)
            if self.timeout is not None:
                s.set("timeout", int(self.timeout * 1000))
            s.add(*assertions)
            r = s.check()
            return r, (s.model() if r == z3.sat else None)
        s = z3.SolverFor("QF_BV")
        s.add(*assertions)
        names = {v.decl().name() for v in tr.frames[0].states.values() if z3.is_const(v)}
        for f in tr.frames:
            names.update(f.inputs[n].decl().name() for n in self.free_names)
        body = smt2_text(s)
        # z3 only declares what the formula mentions; anything else is a don't-care
        declared = set(re.findall(r"\(declare-fun \|?([^|\s()]+)\|? \(\)", body))
        quoted = " ".join(f"|{n}|" for n in sorted(names & declared))
        try:
            out = run_external(self.solver[4:], body + (f"(get-value ({quoted}))\n" if quoted else ""), self.timeout)
        except subprocess.TimeoutExpired:
            return z3.unknown, None
        first = out.strip().splitlines()[0] if out.strip() else "unknown"
        if first == "unsat":
            return z3.unsat, None
        if first != "sat":
            return z3.unknown, None
        vals = {}
        for mm in re.finditer(r"\(\|?([^|()\s]+)\|?\s+(#b[01]+|#x[0-9a-fA-F]+|true|false)\)", out):
            nm, v = mm.group(1), mm.group(2)
            vals[nm] = int(v[2:], 2) if v.startswith("#b") else int(v[2:], 16) if v.startswith("#x") else int(v == "true")
        return z3.sat, _DictModel(vals)

    # ------------------------------------------------------------ learn
    def conflict_core(self, cex: dict, assignment: dict[str, bool]):
        """Subset of the assignment that already violates the spec on this cex."""
        formula = self._cex_formula(cex, self.sel_bv)
        literals = [(n, bool(assignment[n])) for n in self.sel_names]
        self.last_extra = []
        if self.core_levels is not None:
            m = core_min.minimize(formula, literals, self.core_levels)
            lits = m.lits
            self.last_extra = [self._core_clause(e) for e in m.extra]
            self.cores.extend(list(e) for e in m.extra)
            if self.stats:
                self.stats[-1].update(core_first=m.first_core, extra_cores=len(m.extra))
        else:
            r, lits = solver_core(self.solver, [formula], literals, timeout_s=self.timeout)
            if r != "unsat":
                raise RuntimeError(f"counterexample did not reproduce with selects as assumptions ({r})")
        self.cores.append(list(lits))
        if self.stats:
            self.stats[-1]["core_lits"] = list(lits)
        return [n for n, _ in lits], self._core_clause(lits)

    def _core_clause(self, lits) -> list:
        return [self.sels[n] if v else z3.Not(self.sels[n]) for n, v in lits]

    def add_cex(self, cex: dict):
        self._add_hard(self._cex_formula(cex, self.sel_bv))
        self.cex_traces.append(cex)

    def add_conflict(self, core) -> bool:
        key = frozenset(str(lit) for lit in core)
        if key in self._learned_keys:
            return False
        self._learned_keys.add(key)
        clause = z3.Not(z3.And(*core))
        self.learned.append(clause)
        self._add_hard(clause)
        return True

    def synthesize(self) -> dict[str, bool] | None:
        t0 = time.time()
        asg = None
        opt = self._steered()
        if opt.check() == z3.sat:
            m = opt.model()
            asg = {nm: z3.is_true(m.eval(b, model_completion=True)) for nm, b in self.sels.items()}
        dt = time.time() - t0
        if self.stats:
            self.stats[-1]["synth_s"] = dt
            self.flush_trace()
        if asg is not None:
            self.log(f"  synthesize: {sum(asg.values())}/{len(self.sel_names)} selects on in {dt:.2f}s")
        self.on_event("synth", on=None if asg is None else sum(asg.values()), s=round(dt, 3))
        return asg

    def _steered(self) -> z3.Optimize:
        """The optimizer for this call. A hint goes on the running one. Lex needs a
        second objective whose weights change every call, and soft constraints cannot
        be retracted, so it builds a fresh optimizer from the hard constraints."""
        if self.steer is None:
            return self.synth
        if self.steer.mode == "hint":
            for n, v in self.steer.phases().items():
                self.synth.set_initial_value(self.sels[n], z3.BoolVal(v))
            return self.synth
        opt = z3.Optimize()
        opt.add(*self.hard)
        for b in self.sels.values():
            opt.add_soft(b, 1, "count")
        for n, w in self.steer.weights().items():
            opt.add_soft(self.sels[n] if w > 0 else z3.Not(self.sels[n]), abs(w), "tie")
        return opt

    def _learn(self, cex: dict, asg: dict[str, bool]) -> None:
        """Everything a refuted candidate teaches the synthesizer."""
        if self.learn_cores:
            t0 = time.time()
            names, core = self.conflict_core(cex, asg)
            self.stats[-1]["core_s"] = time.time() - t0
            self.stats[-1]["core"] = names
            if not names:
                self.on_event("error", message="empty conflict core: the copies differ with all cuts off")
                raise MiterMismatch(cex, "counterexample independent of every select (empty conflict core); "
                                         f"unmatched registers: {self.unmatched}")
            self.on_event("cex", frames=len(cex["inputs"]), core=names)
            self.log(f"  cex {cex['inputs']} init={cex['init']}")
            self.log(f"  conflict core: {names}")
            self.add_cex(cex)
            self.add_conflict(core)
            for extra in self.last_extra:
                self.add_conflict(extra)
            if self.steer is not None:
                self.steer.observe(self.stats[-1]["core_lits"])
        else:
            self.on_event("cex", frames=len(cex["inputs"]), core=None)
            self.log(f"  cex {cex['inputs']} init={cex['init']}")
            self.add_cex(cex)

    # ------------------------------------------------------------ farming
    def farm(self, accepted: dict[str, bool], max_calls: int, max_s: float | None = None,
             distance: int = 1) -> list[dict[str, bool]]:
        """After the search: more assignments that meet the spec, as training data.
        Blocks each accepted assignment and carries on, so the synthesizer proposes
        the next best thing it cannot yet refute. Records are marked phase "farm".
        Refuted proposals teach it as usual and are the near-boundary negatives.

        `distance` > 1 also keeps later candidates at least that Hamming distance from
        every accepted one. Blocking only the exact assignment tends to return its
        one-flip neighbours, which are nearly the same training example.

        This changes the synthesizer, so `accepted`, not anything after, is the
        result of the search. Stops after `max_calls` verifier calls, `max_s`
        seconds, or when nothing is left. Returns the newly accepted assignments."""
        if distance < 1:
            raise ValueError(f"farm distance {distance}: expected >= 1")
        t0 = time.time()
        found: list[dict[str, bool]] = []
        self._block(accepted, distance)
        for _ in range(max_calls):
            if max_s is not None and time.time() - t0 > max_s:
                break
            asg = self.synthesize()
            if asg is None:
                break
            self.stats.append({"iter": len(self.stats), "phase": "farm",
                               "assignment": [n for n in self.sel_names if asg[n]]})
            cex = self.verify(asg)
            self.stats[-1]["verify_result"], self.stats[-1]["verify_s"] = self.last_verify
            if cex is None:
                found.append(asg)
                self._block(asg, distance)
            else:
                self._learn(cex, asg)
        self.flush_trace()
        return found

    def _block(self, asg: dict[str, bool], distance: int) -> None:
        flips = [z3.Not(b) if asg[n] else b for n, b in self.sels.items()]
        self._add_hard(z3.Or(*flips) if distance == 1 else z3.PbGe([(f, 1) for f in flips], distance))

    # ------------------------------------------------------------ base
    def base(self, assignment: dict[str, bool]) -> tuple[bool, str]:
        """R at power-up, without a reset (module docstring). Fails only on what
        power-up fixes, i.e. ROM contents that differ between the copies."""
        tr = self.d.unroll(1, shared={**self.pinned, **self._sel_lits(assignment)}, prefix="base")
        b0 = tr.frames[0]
        r = check(self.solver, self.power.constraints(b0, self.d)
                  + [tr.assumed, z3.Not(correspondence(b0, self.pairs, self.d))], timeout_s=self.timeout)
        if r == z3.unsat:
            return True, f"R holds at power-up ({self.power.describe()})"
        why = "timeout" if r == z3.unknown else "matched registers can differ at power-up (ROM contents?)"
        return False, f"base {r}: {why}"

    # ------------------------------------------------------------ one at a time
    def individually_valid(self, names: list[str]) -> dict[str, tuple[bool, str]]:
        """Each select on alone, every other one off: does the spec hold?
        -> {select: (valid, how)}, how being "core" or "verify".

        Not implied by the maximum set either way (two cuts can be valid only
        together), so every select is asked. A learned conflict core whose only
        positive literal is the select already answers no: the core fails every
        assignment agreeing with it, and select-alone does. The rest cost one
        verifier call each."""
        refuted = set()
        for core in self.cores:
            pos = [n for n, v in core if v]
            if len(pos) == 1:
                refuted.add(pos[0])
        out = {}
        for n in names:
            if n in refuted:
                out[n] = (False, "core")
            else:
                out[n] = (self.verify({m: m == n for m in self.sel_names}) is None, "verify")
            self.on_event("individual", select=n, valid=out[n][0], how=out[n][1])
        return out

    # ------------------------------------------------------------ loop
    def _no_assignment_left(self):
        off = {n: z3.BitVecVal(0, 1) for n in self.sel_names}
        for cex in self.cex_traces:
            if z3.is_false(self._cex_formula(cex, off)):
                self.on_event("error", message="a counterexample fails with every cut off")
                raise MiterMismatch(cex, "a stored counterexample fails with every cut off")
        self.log("no assignment satisfies all counterexamples")
        self.on_event("error", message="no assignment satisfies all counterexamples")
        return None

    def run(self, max_iter: int = 10000) -> dict[str, bool] | None:
        t_start = time.time()
        if self.unmatched:
            self.log(f"unmatched registers (free in both copies, never compared): {self.unmatched}")
        asg = {n: True for n in self.sel_names}
        if self.exclusive or self.steer is not None:  # all-on may be illegal, and steering owns the first pick too
            asg = self.synthesize()
            if asg is None:
                self.on_event("error", message="no assignment satisfies the exclusivity constraints")
                return None
        self.on_event("run_start", selects=len(self.sel_names), sequential=self.d.is_sequential,
                      pairs=len(self.pairs))
        for it in range(max_iter):
            self.stats.append({"iter": it, "assignment": [n for n in self.sel_names if asg[n]]})
            self.log(f"iteration {it}: {sum(asg.values())}/{len(asg)} on")
            self.on_event("iteration", it=it, on=sum(asg.values()))
            cex = self.verify(asg)
            self.stats[-1]["verify_result"], self.stats[-1]["verify_s"] = self.last_verify
            if cex is None:
                self.base_ok, self.base_evidence = self.base(asg)
                self.stats[-1]["base"] = {"ok": self.base_ok, "evidence": self.base_evidence}
                self.total_s = time.time() - t_start
                self.log(f"  {'proved' if self.base_ok else 'NOT proved sequentially'}: {self.base_evidence}")
                self.flush_trace()
                self.log(f"done in {self.total_s:.2f}s, {it} counterexamples, {len(self.learned)} learned clauses")
                self.on_event("done", on=sum(asg.values()), base=self.base_ok, total_s=round(self.total_s, 3))
                return asg
            self._learn(cex, asg)
            asg = self.synthesize()
            if asg is None:
                self.flush_trace()
                return self._no_assignment_left()
        self.on_event("error", message=f"iteration cap {max_iter} hit")
        raise RuntimeError("iteration cap hit")
