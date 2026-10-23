"""
btor2z3: build z3py expressions directly from a Yosys-generated BTOR2 file.

Why BTOR2 rather than SMT-LIB: every line is `id op sort args [symbol]`, all
values are bit-vectors, the graph is a DAG in topological order, and sequential
designs are explicit (`state` / `next` / `init`).  Each op maps onto exactly one
z3py call, so the translator is a table, not a parser.

Usage sketch:

    d = Design.from_file("tests/fixtures/poly.btor")
    f = d.frame(name="t0")               # fresh z3 consts for inputs and states
    solver.add(f.bad[0] == 1)            # `bad` is 1 when an assert fails
    m = solver.model(); f.inputs["x"], f.states["mod.q"], f.outputs["eq"]

    # sequential: unrolled trace of k frames
    tr = d.unroll(k, shared={"pc_sel0": sel0, ...})

Generate the input with `write_btor -s -x` after `async2sync; dffunmap` (see
netlist.build_btor).  Arrays (memories) are not supported; run `memory_map` first.

Copied from smt_ec/src/btor2z3.py (rewriting repo, 12a5a7f + working tree, 2026-10-23); the two
copies are maintained separately.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import z3

BV = z3.BitVecRef


def _bool2bv(b: z3.BoolRef) -> BV:
    return z3.If(b, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))


def _bv2bool(v: BV) -> z3.BoolRef:
    return v == z3.BitVecVal(1, v.size())


def _conj(xs: list) -> z3.BoolRef:
    """And without 1-ary/0-ary forms (external SMT-LIB parsers reject `(and x)`)."""
    xs = [x for x in xs if not z3.is_true(x)]
    if not xs:
        return z3.BoolVal(True)
    return xs[0] if len(xs) == 1 else z3.And(*xs)


# Binary ops: BTOR name -> z3 builder on two BitVecs (result BitVec).
_BINOPS: dict[str, Callable[[BV, BV], BV]] = {
    "and": lambda a, b: a & b,
    "or": lambda a, b: a | b,
    "xor": lambda a, b: a ^ b,
    "nand": lambda a, b: ~(a & b),
    "nor": lambda a, b: ~(a | b),
    "xnor": lambda a, b: ~(a ^ b),
    "implies": lambda a, b: ~a | b,
    "iff": lambda a, b: ~(a ^ b),
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "udiv": z3.UDiv,
    "sdiv": lambda a, b: a / b,
    "urem": z3.URem,
    "srem": z3.SRem,
    "smod": lambda a, b: a % b,
    "sll": lambda a, b: a << b,
    "srl": z3.LShR,
    "sra": lambda a, b: a >> b,
    "rol": z3.RotateLeft,
    "ror": z3.RotateRight,
    "concat": z3.Concat,
    # comparisons return 1-bit vectors
    "eq": lambda a, b: _bool2bv(a == b),
    "neq": lambda a, b: _bool2bv(a != b),
    "ult": lambda a, b: _bool2bv(z3.ULT(a, b)),
    "ulte": lambda a, b: _bool2bv(z3.ULE(a, b)),
    "ugt": lambda a, b: _bool2bv(z3.UGT(a, b)),
    "ugte": lambda a, b: _bool2bv(z3.UGE(a, b)),
    "slt": lambda a, b: _bool2bv(a < b),
    "slte": lambda a, b: _bool2bv(a <= b),
    "sgt": lambda a, b: _bool2bv(a > b),
    "sgte": lambda a, b: _bool2bv(a >= b),
}

_UNOPS: dict[str, Callable[[BV], BV]] = {
    "not": lambda a: ~a,
    "neg": lambda a: -a,
    "inc": lambda a: a + 1,
    "dec": lambda a: a - 1,
    "redor": lambda a: _bool2bv(a != 0),
    "redand": lambda a: _bool2bv(a == z3.BitVecVal(-1, a.size())),
    "redxor": lambda a: _redxor(a),
}


def _redxor(a: BV) -> BV:
    r = z3.Extract(0, 0, a)
    for i in range(1, a.size()):
        r = r ^ z3.Extract(i, i, a)
    return r


@dataclass
class Node:
    id: int
    op: str
    sort: int  # width (sort id resolved to width)
    args: list
    sym: str | None = None


@dataclass
class Frame:
    """One time step: z3 consts for inputs/states plus every derived node."""

    name: str
    inputs: dict[str, BV]
    states: dict[str, BV]
    outputs: dict[str, BV] = field(default_factory=dict)
    next: dict[str, BV] = field(default_factory=dict)  # state name -> next-value expr
    bad: list[BV] = field(default_factory=list)  # 1-bit, 1 == assertion failed
    constraints: list[BV] = field(default_factory=list)  # 1-bit assumptions
    values: dict[int, BV] = field(default_factory=dict)  # node id -> expr

    @property
    def ok(self) -> z3.BoolRef:
        """All assertions hold in this frame."""
        return _conj([b == 0 for b in self.bad])

    @property
    def assumed(self) -> z3.BoolRef:
        return _conj([c == 1 for c in self.constraints])


@dataclass
class Trace:
    frames: list[Frame]
    shared: dict[str, BV]

    @property
    def ok(self) -> z3.BoolRef:
        return _conj([f.ok for f in self.frames])

    @property
    def assumed(self) -> z3.BoolRef:
        return _conj([f.assumed for f in self.frames])


class Design:
    def __init__(self):
        self.nodes: dict[int, Node] = {}
        self.order: list[int] = []
        self.sorts: dict[int, int] = {}
        self.inputs: dict[str, tuple[int, int]] = {}  # name -> (node id, width)
        self.states: dict[str, tuple[int, int]] = {}
        self.outputs: dict[str, int] = {}  # name -> node id
        self.next: dict[str, int] = {}  # state name -> node id of next expr
        self.init: dict[str, int] = {}  # state name -> node id of init expr
        self.bad: list[int] = []
        self.constraints: list[int] = []
        self.clocks: set[str] = set()
        self.module: str | None = None
        # Symbol -> node id, for the named RTL signals `write_btor -s` labels.
        # `async2sync` renames the state elements beyond recognition, but the
        # signal a register drives keeps its name here, so this is how a register
        # is identified on an asynchronous-reset design. First occurrence wins.
        self.symbols: dict[str, int] = {}
        self.lines: list[str] = []  # raw BTOR2 text, kept so emit_pinned can rewrite it

    # ------------------------------------------------------------ parsing
    @classmethod
    def from_file(cls, path: str, info_path: str | None = None) -> "Design":
        with open(path) as f:
            d = cls.from_string(f.read())
        if info_path:
            with open(info_path) as f:
                for line in f:
                    parts = line.split()
                    if not parts:
                        continue
                    if parts[0] == "name":
                        d.module = parts[1]
                    elif parts[0] in ("posedge", "negedge"):
                        d.clocks.add(int(parts[1]))
        return d

    @classmethod
    def from_string(cls, text: str) -> "Design":
        d = cls()
        d.lines = text.splitlines()
        for raw in d.lines:
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            d._parse_line(line)
        # clocks were recorded as node ids from the info file; resolve names lazily
        return d

    def _parse_line(self, line: str):
        toks = line.split()
        nid, op = int(toks[0]), toks[1]
        if op == "sort":
            if toks[2] != "bitvec":
                raise NotImplementedError(f"line {nid}: sort {toks[2]} unsupported (run memory_map)")
            self.sorts[nid] = int(toks[3])
            return
        # ops whose first arg is a sort id
        if op in ("input", "state", "const", "constd", "consth", "zero", "one", "ones"):
            width = self.sorts[int(toks[2])]
            rest = toks[3:]
            sym = None
            if op in ("input", "state"):
                sym = rest[0] if rest else f"_anon{nid}"
                node = Node(nid, op, width, [], sym)
                (self.inputs if op == "input" else self.states)[sym] = (nid, width)
            elif op == "const":
                node = Node(nid, op, width, [int(rest[0], 2)])
            elif op == "constd":
                node = Node(nid, op, width, [int(rest[0], 10)])
            elif op == "consth":
                node = Node(nid, op, width, [int(rest[0], 16)])
            elif op == "zero":
                node = Node(nid, "const", width, [0])
            elif op == "one":
                node = Node(nid, "const", width, [1])
            else:  # ones
                node = Node(nid, "const", width, [(1 << width) - 1])
        elif op in ("output",):
            name = toks[3] if len(toks) > 3 else f"_out{nid}"
            self.outputs[name] = int(toks[2])
            return
        elif op in ("bad", "constraint", "fair", "justice"):
            if op == "bad":
                self.bad.append(int(toks[2]))
            elif op == "constraint":
                self.constraints.append(int(toks[2]))
            return
        elif op in ("next", "init"):
            state_id, val_id = int(toks[3]), int(toks[4])
            sname = self.nodes[state_id].sym
            (self.next if op == "next" else self.init)[sname] = val_id
            return
        else:
            width = self.sorts[int(toks[2])]
            nargs = {"slice": 3, "uext": 2, "sext": 2, "ite": 3}.get(op, 2 if op in _BINOPS else 1)
            args = [int(t) for t in toks[3 : 3 + nargs]]
            sym = toks[3 + nargs] if len(toks) > 3 + nargs else None
            node = Node(nid, op, width, args, sym)
            if sym:
                self.symbols.setdefault(sym, nid)
            if op not in _BINOPS and op not in _UNOPS and op not in ("slice", "uext", "sext", "ite"):
                raise NotImplementedError(f"line {nid}: op {op} unsupported")
        self.nodes[nid] = node
        self.order.append(nid)

    # ------------------------------------------------------------ helpers
    @property
    def is_sequential(self) -> bool:
        return bool(self.states)

    def input_names(self, pattern: str | None = None, exclude_clocks: bool = True) -> list[str]:
        names = []
        for n, (nid, _) in self.inputs.items():
            if exclude_clocks and nid in self.clocks:
                continue
            if pattern and not re.match(pattern, n):
                continue
            names.append(n)
        return names

    def width(self, name: str) -> int:
        if name in self.inputs:
            return self.inputs[name][1]
        if name in self.states:
            return self.states[name][1]
        return self.nodes[self.symbols[name]].sort

    # ------------------------------------------------------------ depth analysis
    def state_deps(self) -> dict[str, set[str]]:
        """Register dependency graph: state -> states its `next` expression reads."""
        deps: dict[int, frozenset] = {}
        for nid in self.order:
            nd = self.nodes[nid]
            if nd.op == "state":
                deps[nid] = frozenset([nd.sym])
            elif nd.op in ("input", "const"):
                deps[nid] = frozenset()
            else:
                acc: set[str] = set()
                for a in nd.args:
                    if a in deps:  # slice/uext widths are ints, not node ids
                        acc |= deps[a]
                deps[nid] = frozenset(acc)
        return {s: set(deps[n]) for s, n in self.next.items()}

    def bmc_complete_depth(self) -> int | None:
        """Depth at which BMC-unsat is a full proof, or None if the register
        graph has a cycle. Combinational: 1. Acyclic (pure pipeline): longest
        register chain + 1, since after that many steps no state depends on the
        initial state any more."""
        if not self.is_sequential:
            return 1
        g = self.state_deps()
        for s in self.states:
            g.setdefault(s, set())  # state without `next` holds its value: no deps
        longest: dict[str, int] = {}
        on_path: set[str] = set()

        def chain(s: str) -> int | None:
            if s in longest:
                return longest[s]
            if s in on_path:
                return None
            on_path.add(s)
            best = 0
            for t in g[s]:
                c = chain(t)
                if c is None:
                    return None
                best = max(best, c)
            on_path.discard(s)
            longest[s] = best + 1
            return longest[s]

        import sys
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 10 * len(self.states) + 100))
        depth = 0
        for s in self.states:
            c = chain(s)
            if c is None:
                return None
            depth = max(depth, c)
        return depth + 1

    # ------------------------------------------------------------ pinned BTOR2
    _INPUT_LINE = re.compile(r"^(\s*)(\d+)\s+input\s+(\d+)\b")

    def emit_pinned(self, assignment: dict[str, int | bool], path: str) -> str:
        """Write a copy of the BTOR2 with the given inputs turned into constants.

        Each `<id> input <sort> [<name>]` whose input is in `assignment` becomes
        `<id> const <sort> <bits>` with the same node id and sort, so every
        other line is untouched and external engines (pono, btormc, sby) see
        the selects as constants. Values may be bool or int and are truncated
        to the input's width. Anonymous `_anon<id>` inputs are pinned the same
        way (pass value 0 for `--anon zero`). Names that are not inputs raise
        ValueError. Returns `path`.
        """
        pending = dict(assignment)
        for n in pending:
            if n not in self.inputs:
                raise ValueError(f"emit_pinned: {n!r} is not an input of this design")
        by_id = {nid: n for n, (nid, _) in self.inputs.items()}
        out = []
        for raw in self.lines:
            m = self._INPUT_LINE.match(raw.split(";", 1)[0])
            name = by_id.get(int(m.group(2))) if m else None
            if name is not None and name in pending:
                indent, nid, sort_id = m.groups()
                width = self.sorts[int(sort_id)]
                val = int(pending.pop(name)) & ((1 << width) - 1)
                out.append(f"{indent}{nid} const {sort_id} {val:0{width}b} ; pinned {name}")
            else:
                out.append(raw)
        if pending:  # cannot happen when `lines` and `inputs` came from the same text
            raise ValueError(f"emit_pinned: inputs not found in text: {sorted(pending)}")
        with open(path, "w") as f:
            f.write("\n".join(out) + "\n")
        return path

    # ------------------------------------------------------------ building
    def frame(
        self,
        name: str = "f",
        inputs: dict[str, BV] | None = None,
        states: dict[str, BV] | None = None,
    ) -> Frame:
        """Instantiate the combinational cone once.

        `inputs` / `states` may supply pre-made z3 exprs (fresh consts, shared
        consts, or literal BitVecVals for constant folding); anything missing
        gets a fresh const named `<name>.<sym>`.
        """
        inputs = dict(inputs or {})
        states = dict(states or {})
        for n, (_, w) in self.inputs.items():
            inputs.setdefault(n, z3.BitVec(f"{name}.{n}", w))
        for n, (_, w) in self.states.items():
            states.setdefault(n, z3.BitVec(f"{name}.{n}", w))
        fr = Frame(name, inputs, states)
        v = fr.values
        for nid in self.order:
            nd = self.nodes[nid]
            op = nd.op
            if op == "input":
                v[nid] = inputs[nd.sym]
            elif op == "state":
                v[nid] = states[nd.sym]
            elif op == "const":
                v[nid] = z3.BitVecVal(nd.args[0], nd.sort)
            elif op == "slice":
                v[nid] = z3.Extract(nd.args[1], nd.args[2], v[nd.args[0]])
            elif op == "uext":
                v[nid] = z3.ZeroExt(nd.args[1], v[nd.args[0]]) if nd.args[1] else v[nd.args[0]]
            elif op == "sext":
                v[nid] = z3.SignExt(nd.args[1], v[nd.args[0]]) if nd.args[1] else v[nd.args[0]]
            elif op == "ite":
                v[nid] = z3.If(_bv2bool(v[nd.args[0]]), v[nd.args[1]], v[nd.args[2]])
            elif op in _BINOPS:
                v[nid] = _BINOPS[op](v[nd.args[0]], v[nd.args[1]])
            else:
                v[nid] = _UNOPS[op](v[nd.args[0]])
            assert v[nid].size() == nd.sort, f"node {nid} {op}: width {v[nid].size()} != {nd.sort}"
        fr.outputs = {n: v[i] for n, i in self.outputs.items()}
        fr.next = {s: v[i] for s, i in self.next.items()}
        fr.bad = [v[i] for i in self.bad]
        fr.constraints = [v[i] for i in self.constraints]
        return fr

    def cone_states(self, nid: int) -> set[str]:
        """States the value of node `nid` depends on. Used to find the state a
        named register lives in when the pass that created it renamed it: the
        register's read value is a function of exactly one state (its own), so a
        singleton here identifies it."""
        memo: dict[int, frozenset] = {}

        def walk(n: int) -> frozenset:
            if n in memo:
                return memo[n]
            nd = self.nodes[n]
            if nd.op == "state":
                out = frozenset([nd.sym])
            elif nd.op in ("input", "const"):
                out = frozenset()
            else:
                acc: set[str] = set()
                for a in nd.args:
                    if a in self.nodes:
                        acc |= walk(a)
                out = frozenset(acc)
            memo[n] = out
            return out

        import sys
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 10 * len(self.nodes) + 100))
        return set(walk(nid))

    def cone_inputs(self, nid: int) -> set[str]:
        """Inputs the value of node `nid` depends on within one frame: the walk
        stops at states, whose value in a frame is given rather than computed."""
        seen, out, stack = set(), set(), [nid]
        while stack:
            n = stack.pop()
            if n in seen or n not in self.nodes:
                continue
            seen.add(n)
            nd = self.nodes[n]
            if nd.op == "input":
                out.add(nd.sym)
            elif nd.op not in ("state", "const"):
                stack.extend(nd.args)
        return out

    def init_values(self, fr: Frame, skip=()) -> list[z3.BoolRef]:
        """Constraints pinning frame-0 states to their `init` expressions, except
        the states in `skip` (a trace that already gives their values)."""
        return [fr.states[s] == fr.values[i] for s, i in self.init.items() if s not in skip]

    def unroll(
        self,
        k: int,
        shared: dict[str, BV] | None = None,
        prefix: str = "t",
        inputs_per_frame: list[dict[str, BV]] | None = None,
        init_states: dict[str, BV] | None = None,
    ) -> Trace:
        """k frames chained through `next`. `shared` inputs are the same const in
        every frame (the papercuts selects). Frame-0 states come from
        `init_states` if given, else fresh consts."""
        shared = dict(shared or {})
        frames: list[Frame] = []
        states = dict(init_states or {})
        for i in range(k):
            ins = dict(shared)
            if inputs_per_frame and i < len(inputs_per_frame):
                ins.update(inputs_per_frame[i])
            fr = self.frame(f"{prefix}{i}", inputs=ins, states=states)
            frames.append(fr)
            states = dict(fr.next)
        return Trace(frames, shared)


def model_value(m: z3.ModelRef, e: BV) -> int:
    v = m.eval(e, model_completion=True)
    return v.as_long()
