"""
Solver selection for the CEGIS loop (comb.py).

A "strategy" string picks how a z3py formula is decided:

    auto          ext:yices-smt2 if on PATH, else ext:bitwuzla, else AIG_TACTIC
    ext:BIN       dump SMT-LIB2 (QF_BV) and run an external solver binary
    tactic:a,b,c  z3 tactic pipeline
    qfbv          z3.SolverFor("QF_BV")

`check(strategy, assertions)` is the model-free entry point (the base case).
`core(...)` returns an unsat core over assumption literals (the conflict core).
The loop's model-extracting external path is comb.CombCegis._solve.

Copied from smt_ec/src/solvers.py (rewriting repo, 12a5a7f + working tree, 2026-10-23); the two
copies are maintained separately.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import z3

AIG_TACTIC = "tactic:simplify,propagate-values,solve-eqs,elim-uncnstr,reduce-bv-size,bit-blast,aig,tseitin-cnf,sat"


def resolve_solver(strategy: str) -> str:
    if strategy != "auto":
        return strategy
    for b in ("yices-smt2", "bitwuzla"):
        if shutil.which(b):
            return f"ext:{b}"
    return AIG_TACTIC


def make_solver(strategy: str) -> z3.Solver:
    """A fresh in-process z3 solver for a non-external strategy."""
    if strategy == "qfbv" or strategy.startswith("ext:"):
        return z3.SolverFor("QF_BV")  # ext: callers dump this solver to SMT-LIB
    if strategy.startswith("tactic:"):
        names = strategy[7:].split(",")
        return (z3.Tactic(names[0]) if len(names) == 1 else z3.Then(*names)).solver()
    raise ValueError(f"unknown solver strategy {strategy}")


def smt2_text(s: z3.Solver, extra_tail: str = "") -> str:
    """SMT-LIB2 for the solver's assertions, one (check-sat) at the end."""
    text = s.to_smt2().replace("(check-sat)", "").replace("(set-logic QF_BV)", "")
    return "(set-option :produce-models true)\n(set-logic QF_BV)\n" + text + "(check-sat)\n" + extra_tail


def run_external(binary: str, text: str, timeout_s: float | None = None) -> str:
    """Run an SMT-LIB2 solver binary on `text`; returns raw stdout.
    Raises subprocess.TimeoutExpired on timeout."""
    with tempfile.NamedTemporaryFile("w", suffix=".smt2", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return subprocess.run([binary, path], capture_output=True, text=True, timeout=timeout_s).stdout
    finally:
        os.unlink(path)


# bitwuzla needs -i for check-sat-assuming; yices-smt2 answers one query without flags
_CORE_FLAGS = {"bitwuzla": ["-i"]}
_CORE_LIT = re.compile(r"\((?:not|bvnot)\s+\|?([^|()\s]+)\|?\)|\|?([^|()\s]+)\|?")


def core(strategy: str, assertions: list, literals: list[tuple[str, bool]],
         timeout_s: float | None = None) -> tuple[str, list[tuple[str, bool]] | None]:
    """Decide `assertions` under the assumption literals (Boolean constant name,
    polarity) and return ("unsat", core literals) | ("sat", None) | ("unknown", None).

    External strategies write the formula once as SMT-LIB and run one
    `check-sat-assuming` + `get-unsat-assumptions`. Every assumed constant is
    declared explicitly: simplification can drop one from the formula, and an
    undeclared name is an error. Solvers report errors on stdout with exit 0, so
    the answer is read from the output. Other strategies use z3 in-process."""
    strategy = resolve_solver(strategy)
    names = [n for n, _ in literals]
    if not strategy.startswith("ext:"):
        s = z3.SolverFor("QF_BV")
        if timeout_s is not None:
            s.set("timeout", int(timeout_s * 1000))
        s.add(*assertions)
        lits = {n: (z3.Bool(n) if v else z3.Not(z3.Bool(n))) for n, v in literals}
        r = s.check(*lits.values())
        if r == z3.sat:
            return "sat", None
        if r != z3.unsat:
            return "unknown", None
        got = {str(c) for c in s.unsat_core()}
        return "unsat", [(n, v) for n, v in literals if str(lits[n]) in got]
    binary = strategy[4:]
    s = z3.SolverFor("QF_BV")
    s.add(*assertions)
    text = s.to_smt2().replace("(check-sat)", "").replace("(set-logic QF_BV)", "")
    declared = set(re.findall(r"\(declare-fun \|?([^|\s()]+)\|? \(\)", text))
    decls = "".join(f"(declare-fun {n} () Bool)\n" for n in names if n not in declared)
    assume = " ".join(n if v else f"(not {n})" for n, v in literals)
    text = ("(set-option :produce-unsat-assumptions true)\n(set-logic QF_BV)\n" + decls + text
            + f"(check-sat-assuming ({assume}))\n(get-unsat-assumptions)\n")
    with tempfile.NamedTemporaryFile("w", suffix=".smt2", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        flags = _CORE_FLAGS.get(os.path.basename(binary), [])
        out = subprocess.run([binary, *flags, path], capture_output=True, text=True, timeout=timeout_s).stdout
    except subprocess.TimeoutExpired:
        return "unknown", None
    finally:
        os.unlink(path)
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    if not lines or lines[0] not in ("sat", "unsat"):
        return "unknown", None
    if lines[0] == "sat":
        return "sat", None
    body = " ".join(lines[1:]).strip()
    if not body.startswith("(") or body.startswith("(error"):
        return "unknown", None
    pol = dict(literals)
    got = []
    for neg, pos in _CORE_LIT.findall(body[1:-1]):
        n = neg or pos
        if n in pol:
            got.append((n, not neg))
    return "unsat", got


def check(strategy: str, assertions: list, timeout_s: float | None = None) -> z3.CheckSatResult:
    """Decide the conjunction of `assertions`. Returns z3.sat / z3.unsat / z3.unknown
    (unknown on timeout or an unparseable external answer)."""
    strategy = resolve_solver(strategy)
    if strategy.startswith("ext:"):
        s = z3.SolverFor("QF_BV")
        s.add(*assertions)
        try:
            out = run_external(strategy[4:], smt2_text(s), timeout_s)
        except subprocess.TimeoutExpired:
            return z3.unknown
        first = out.strip().splitlines()[0] if out.strip() else "unknown"
        return z3.sat if first == "sat" else z3.unsat if first == "unsat" else z3.unknown
    s = make_solver(strategy)
    if timeout_s is not None:
        s.set("timeout", int(timeout_s * 1000))
    s.add(*assertions)
    return s.check()
