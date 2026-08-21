"""Dead-code pruning for the elaborated design.

Once a design is elaborated, the emitted source often carries signals that are
declared but never read (or never driven) plus the logic that solely drives
them. Papercutter would enumerate cuts on that dead logic, spending an FV check
each to prove that deleting something no output observes changes nothing -- pure
waste. This module removes that dead logic up front (and re-prunes the
consolidated result after cutting), which shrinks both the cut count and every
remaining FV check's cone.

Mechanism: slang's analysis layer (``pyslang.analysis.AnalysisManager`` with
``AnalysisFlags.CheckUnused``) reports the dead symbols; we re-emit the design
through :class:`papercuts.elaborator.Emitter` with those symbols omitted, and
iterate to a fixpoint (removing a dead cone can expose more dead code upstream).

Because only logic that no output can observe is removed, the result is
equivalent to the input by construction. The pipeline's Phase-0
elaboration-vs-original FV gate is the backstop that proves this on every run,
so a hypothetical over-prune fails loudly rather than silently.

Known corner (rare in synthesizable RTL): if a dead signal is assigned from a
side-effecting RHS -- one that increments/decrements another signal or calls a
subroutine with side effects (e.g. ``dead = a++;``) -- removing that assignment
also drops the side effect. If that side effect reaches a live output the design
is no longer equivalent, which the Phase-0 gate catches (the run aborts with a
clear message; re-run with ``--no-prune-dead``). It is not silently miscompiled.
"""
from __future__ import annotations

from dataclasses import dataclass

import pyslang
from pyslang.ast import Compilation, CompilationOptions
from pyslang.syntax import SyntaxTree

from papercuts.elaborator import Emitter, EmitError

# Dead *value* diagnostics: a net/variable that is never read (or never driven).
# Removing such a signal and the logic that solely drives it cannot change any
# output.
DEAD_VALUE_CODES = frozenset({
    "UnusedVariable", "UnusedButSetVariable",
    "UnusedNet", "UnusedButSetNet", "UnusedImplicitNet",
})

# Dead *declaration* diagnostics: declarations that carry no runtime logic and
# are simply unreferenced. Pruned only as fixpoint cleanup (``clean_decls``).
# Port parameters are excluded explicitly below -- dropping one would change a
# module's signature and break its instantiations. Genvars/imports are already
# dropped by the emitter, so they are not listed here.
DEAD_DECL_CODES = frozenset({
    "UnusedParameter", "UnusedTypedef",
})


@dataclass
class PruneStats:
    """Summary of what a :func:`prune_dead_source` run removed."""

    signals: int = 0   # whole dead nets/variables (and their drivers) removed
    decls: int = 0     # unused localparams/typedefs removed
    bits: int = 0      # dead bits of partially-dead signals removed
    iters: int = 0     # fixpoint iterations that changed the source

    def total(self) -> int:
        return self.signals + self.decls + self.bits


def _code_name(diag) -> str:
    """'UnusedNet' from a Diagnostic whose repr is 'DiagCode(UnusedNet)'."""
    return str(diag.code).replace("DiagCode(", "").rstrip(")")


def _const_int(expr):
    """Constant integer value of an index expression, or None if it is not a
    compile-time constant. Folded constants expose ``.constant``; a bare index
    literal carries its value in ``.value``."""
    c = getattr(expr, "constant", None)
    if c is not None:
        try:
            return int(str(c))
        except Exception:
            pass
    if getattr(getattr(expr, "kind", None), "name", None) == "IntegerLiteral":
        try:
            return int(str(expr.value))
        except Exception:
            pass
    return None


def _module_def_name(sym):
    """Definition name of the module that directly contains ``sym`` (or None).

    Used to skip symbols inside protected (verbatim / excluded) modules. Reaches
    the owning instance via the symbol's parent scope."""
    scope = getattr(sym, "parentScope", None)
    inst = getattr(scope, "containingInstance", None) if scope is not None else None
    if inst is None:
        return None
    body = getattr(inst, "body", None)
    defn = getattr(inst, "definition", None) or getattr(body, "definition", None)
    return getattr(defn, "name", None) or getattr(body, "name", None)


def _compile(source: str, top: str):
    """Compile one self-contained source blob with ``top`` pinned as the top."""
    sm = pyslang.SourceManager()
    co = CompilationOptions()
    co.topModules = {top}
    comp = Compilation(pyslang.Bag([co]))
    comp.addSyntaxTree(SyntaxTree.fromText(source, sm))
    return comp, sm


def analyze_dead(comp, *, protect_modules=(), clean_decls=True):
    """Return ``(dead_value_hpaths, dead_decl_hpaths)`` for a compiled design.

    Excludes ports, port parameters, and any symbol inside a protected
    (verbatim / excluded) module. The compilation must already be elaborated
    (call ``getAllDiagnostics()`` first)."""
    from pyslang import analysis as an

    protect = set(protect_modules or ())
    opts = an.AnalysisOptions()
    opts.flags = an.AnalysisFlags.CheckUnused
    mgr = an.AnalysisManager(opts)
    mgr.analyze(comp)

    values, decls = set(), set()
    for d in mgr.getDiagnostics():
        code = _code_name(d)
        is_value = code in DEAD_VALUE_CODES
        is_decl = clean_decls and code in DEAD_DECL_CODES
        if not (is_value or is_decl):
            continue
        sym = d.symbol
        if sym is None:
            continue
        # Never prune ports or port parameters (module interface identity).
        if getattr(getattr(sym, "kind", None), "name", None) == "Port":
            continue
        if getattr(sym, "isPortParam", False):
            continue
        hp = getattr(sym, "hierarchicalPath", None)
        if not hp:
            continue
        if _module_def_name(sym) in protect:
            continue
        (values if is_value else decls).add(hp)
    return values, decls


def compute_dead_bits(comp, *, protect_modules=(), exclude_hps=()):
    """Return ``{signal hp: set of dead bit indices}`` for PARTIALLY-dead signals
    -- ones with some bits driven-but-never-read while other bits are live, which
    slang's whole-symbol :func:`analyze_dead` cannot see.

    A bit is dead if it is driven (``getDrivers`` bit-ranges) but never read. The
    read set is built by walking every read-position expression; anything that
    can't be pinned to constant bits (whole-signal read, variable index, part
    select) marks the signal fully live, and output/inout-port-backed signals are
    never candidates (their bits are observed at the boundary). Signals in
    ``exclude_hps`` (already whole-dead) or inside ``protect_modules`` are
    skipped. Conservative throughout: when in doubt a bit is treated as live, so
    a bit is only ever pruned when provably unread."""
    from pyslang import analysis as an

    protect = set(protect_modules or ())
    exclude = set(exclude_hps or ())
    mgr = an.AnalysisManager()
    mgr.analyze(comp)

    # Read accumulation, keyed by signal hp. read[hp] is a set of read bits, or
    # None meaning "fully live" (a whole/variable/part-select read was seen). The
    # (hp, source-location) key correlates a select's base NamedValue with the
    # same NamedValue when it is visited on its own, so a bit-select base is not
    # mistaken for a whole-signal read.
    sel_base = {}   # (hp, loc) -> set of bits, or None
    plain = []      # (hp, loc) for every NamedValue reference

    def collect(expr):
        def cb(n):
            k = n.kind.name
            if k == "ElementSelect" and n.value.kind.name == "NamedValue":
                hp = n.value.symbol.hierarchicalPath
                loc = str(n.value.sourceRange.start)
                idx = _const_int(n.selector)
                if idx is None:
                    sel_base[(hp, loc)] = None
                else:
                    cur = sel_base.get((hp, loc), set())
                    if cur is not None:
                        sel_base[(hp, loc)] = cur | {idx}
            elif k == "RangeSelect" and n.value.kind.name == "NamedValue":
                hp = n.value.symbol.hierarchicalPath
                loc = str(n.value.sourceRange.start)
                lo = _const_int(n.left)
                hi = _const_int(n.right)
                if lo is not None and hi is not None and n.selectionKind.name == "Simple":
                    cur = sel_base.get((hp, loc), set())
                    if cur is not None:
                        sel_base[(hp, loc)] = cur | set(range(min(lo, hi), max(lo, hi) + 1))
                else:
                    sel_base[(hp, loc)] = None
            elif k == "NamedValue":
                hp = getattr(n.symbol, "hierarchicalPath", None)
                if hp:
                    plain.append((hp, str(n.sourceRange.start)))
        expr.visit(cb)

    # Per-scope pass: gather output-port names (never prune those) and feed only
    # genuine read-position expressions into collect(). A continuous assign's LHS
    # base is a write (skipped); everything else -- RHS, instance port hookups,
    # and any other member (procedural blocks, asserts) -- is treated as a read,
    # so nothing live is ever missed.
    scopes = []  # (scope, def_name, output_port_names)

    def gather(scope, def_name):
        outp = {m.name for m in scope
                if m.kind.name == "Port" and m.direction.name != "In"}
        scopes.append((scope, def_name, outp))
        for m in scope:
            k = m.kind.name
            if k == "ContinuousAssign":
                collect(m.assignment.right)
                left = m.assignment.left
                if left.kind.name == "ElementSelect" and _const_int(left.selector) is None:
                    collect(left.selector)  # variable LHS index is itself a read
            elif k == "Instance":
                for conn in m.portConnections:
                    if conn.expression is not None:
                        collect(conn.expression)
                gather(m.body, m.body.definition.name)
            elif k in ("GenerateBlockArray", "GenerateBlock"):
                gather(m, def_name)
            else:
                try:
                    collect(m)  # conservative: any other member is read context
                except Exception:
                    pass

    for inst in comp.getRoot().topInstances:
        gather(inst.body, inst.body.definition.name)

    read = {}
    for hp, loc in plain:
        bits = sel_base[(hp, loc)] if (hp, loc) in sel_base else None
        if hp not in read:
            read[hp] = set()
        if bits is None:
            read[hp] = None
        elif read[hp] is not None:
            read[hp] |= bits

    dead = {}
    for scope, def_name, outp in scopes:
        if def_name in protect:
            continue
        for m in scope:
            if m.kind.name not in ("Net", "Variable"):
                continue
            if m.name in outp:  # output/inout-port-backed: observed externally
                continue
            hp = m.hierarchicalPath
            if hp in exclude:
                continue
            driven = set()
            try:
                for _drv, rng in mgr.getDrivers(m):
                    lo, hi = rng
                    driven.update(range(min(lo, hi), max(lo, hi) + 1))
            except Exception:
                continue
            if not driven:
                continue
            rd = read.get(hp, set())
            if rd is None:  # fully live
                continue
            d = driven - rd
            if d:
                dead[hp] = d
    return dead


def prune_dead_source(source, top, *, protect_modules=(), clean_decls=True,
                      flatten=True, fold_constants=True, max_iters=16):
    """Re-emit ``source`` with dead signals/logic (and unused decls) removed,
    iterating to a fixpoint. Returns ``(pruned_source, PruneStats)``.

    ``protect_modules`` are module definition names to leave untouched -- they
    are re-emitted verbatim (via the emitter's ``ignore`` set) and never pruned,
    matching how ``--exclude-module`` boundaries are handled elsewhere.

    Best-effort: if analysis or re-emission fails, the best result so far is
    returned unchanged rather than aborting the run. The FV gate still validates
    whatever is produced, so a skipped prune only costs speed, never soundness."""
    protect = set(protect_modules or ())
    best = source
    stats = PruneStats()
    for _ in range(max_iters):
        try:
            comp, _sm = _compile(best, top)
            if any(d.isError() for d in comp.getAllDiagnostics()):
                # Won't elaborate -> analysis can't be trusted; keep last good.
                break
            values, decls = analyze_dead(
                comp, protect_modules=protect, clean_decls=clean_decls)
            # Bit-level pass: partially-dead signals slang's whole-symbol
            # analysis can't see. Excludes signals already whole-dead (values).
            dead_bits = compute_dead_bits(
                comp, protect_modules=protect, exclude_hps=values)
            if not values and not decls and not dead_bits:
                break
            emitter = Emitter()
            emitter.flatten = flatten
            emitter.fold_constants = fold_constants
            # Keep verbatim/excluded modules verbatim on re-emit; this also means
            # their bodies are never walked, so pruning naturally skips them.
            emitter.ignore = protect
            emitter.prune_paths = values | decls
            emitter.prune_bits = dead_bits
            new_source = emitter.run(comp)
        except EmitError:
            break
        if new_source == best:
            # Nothing actually changed (e.g. only unremovable/concat targets were
            # flagged) -- converged.
            break
        stats.iters += 1
        stats.signals += len(values)
        stats.decls += len(decls)
        stats.bits += sum(len(b) for b in dead_bits.values())
        best = new_source
    return best, stats
