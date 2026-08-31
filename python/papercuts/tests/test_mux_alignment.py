"""Select-to-cut alignment for every mux family.

`insert_muxes` must emit `pc_sel<N>` as the control for cut N, for every N that
`Papercutter.cut_info()` reports. This checks that directly, per family:

  ternary / binop  -- forcing the select must yield the same expression the cut
                      produces, once parens and whitespace are normalised out;
  if               -- the guarded predicate must reduce to the constant that
                      selects the branch the cut keeps;
  case             -- forcing the select must make exactly that item's guard
                      constant-false, so the selector falls through to the next
                      matching item (which is what pruning an item does) while
                      every other guard is untouched;
  bitshrink        -- the companion signal must expand, bit for bit, to the same
                      bits the cut's own intermediate wire carries;
  force-const      -- the companion signal must fold to the literal the cut
                      substitutes, and only for the signal that cut targets.

Every family also has to be a no-op with all selects low, and nothing may be
redirected at a write site.
"""

import re

from papercuts import Papercutter, insert_muxes
from papercuts.pypercuts import get_instantiated_modules, wire_mux_hierarchy
from papercuts.utils import print_tree
from pyslang.syntax import SyntaxTree

MUX_KW = dict(caseMux=True, binopMux=True, constForceMux=True)


# ---------------------------------------------------------------- utilities --
def _match_paren(s, i):
    d = 0
    while i < len(s):
        if s[i] == "(":
            d += 1
        elif s[i] == ")":
            d -= 1
            if d == 0:
                return i
        i += 1
    raise ValueError(f"unbalanced parens in {s!r}")


def _split_colon(s):
    """Split `A : B` at bracket depth 0 -- bit-selects carry colons too."""
    d = 0
    for i, c in enumerate(s):
        if c == "[":
            d += 1
        elif c == "]":
            d -= 1
        elif c == ":" and d == 0:
            return s[:i].strip(), s[i + 1:].strip()
    return None


_INNER = re.compile(r"\(([^()]*)\)")


def _reduce(e):
    """Fold away the mux shapes once the selects are constants.

    Bottom-up: a parenthesised group with no applicable rule becomes an opaque
    placeholder so its parent becomes the innermost group in turn. Placeholders
    are expanded again at the end.
    """
    store, n = {}, 0
    e = e.replace("!1'b0", "1'b1").replace("!1'b1", "1'b0")
    while True:
        m = _INNER.search(e)
        if not m:
            break
        b = m.group(1).strip()
        rep = None
        t = re.match(r"^(1'b[01])\s*\?(.*)$", b, re.S)
        if t:
            parts = _split_colon(t.group(2))
            if parts:
                rep = parts[0] if t.group(1) == "1'b1" else parts[1]
        if rep is None:
            t = re.match(r"^(1'b[01])\s*(\|\||\||&&|&)\s*(.*)$", b, re.S)
            if t:
                lit, op, rest = t.group(1), t.group(2), t.group(3)
                if op in ("|", "||"):
                    rep = "1'b1" if lit == "1'b1" else rest
                else:
                    rep = rest if lit == "1'b1" else "1'b0"
        if rep is None:
            key, n = "@%d@" % n, n + 1
            store[key] = "(" + b + ")"
            rep = key
        e = e[:m.start()] + rep.strip() + e[m.end():]
        e = e.replace("!1'b0", "1'b1").replace("!1'b1", "1'b0")

    prev = None
    while prev != e:
        prev = e
        for k, v in store.items():
            e = e.replace(k, v)
    return e


def _fold(e):
    """Reduce a bare expression. Wrapped first so a top-level operator folds too,
    then unwrapped again."""
    r = _reduce("(" + e + ")").strip()
    while r.startswith("(") and r.endswith(")") and _match_paren(r, 0) == len(r) - 1:
        r = r[1:-1].strip()
    return r


def _norm(t):
    t = t.replace("(", " ").replace(")", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"\s+([;,])", r"\1", t)


def _body(text):
    """Module body, minus generated companion decls, with each assignment RHS
    parenthesised so a top-level ternary reduces too. Applied to both sides of
    every comparison, so it cannot mask a real difference."""
    # Drop the module header outright -- it may wrap across lines, and the select
    # ports are appended at its end.
    head = text.find(");")
    text = text[head + 2:] if head >= 0 else text
    keep = []
    for l in text.splitlines():
        if "endmodule" in l:
            continue
        # Drop the companion declaration and its driver, but keep lines that merely
        # read a companion -- those are the ones the expression families rewrite.
        if re.match(r"\s*(?:logic|reg|bit|wire|tri)\b.*_papercuts\s*;", l):
            continue
        if re.match(r"\s*assign \w+_papercuts\s*=", l):
            continue
        keep.append(l)
    # Undo read redirection so the body lines up with the cutter's, which has no
    # companion signals when the cut under test is an expression cut.
    t = "\n".join(keep).replace("_papercuts", "")
    return re.sub(r"=\s*(.+?);", lambda m: "= (" + m.group(1) + ");", t)


def _force(text, i, nsel):
    """Drive pc_sel<i> high and every other select low."""
    for k in range(nsel):
        text = re.sub(r"\bpc_sel%d\b" % k, "1'b1" if k == i else "1'b0", text)
    return text


def _guards(text):
    out, i = [], 0
    while True:
        i = text.find("if (", i)
        if i < 0:
            return out
        j = _match_paren(text, i + 3)
        # Wrapped so a top-level `LIT && ...` is reducible like any other group.
        out.append("(" + re.sub(r"\s+", " ", text[i + 4:j]).strip() + ")")
        i = j


def _bits(e, dims_by_sig):
    """Ordered list of the bits (most significant first) that `e` denotes."""
    e = e.strip()
    if re.fullmatch(r"\d*'b[01]", e):
        return [e[-1]]
    if e.startswith("{") and e.endswith("}"):
        inner = e[1:-1].strip()
        m = re.fullmatch(r"(\d+)\{(.*)\}", inner, re.S)
        if m:
            return _bits(m.group(2), dims_by_sig) * int(m.group(1))
        parts, d, cur = [], 0, ""
        for ch in inner:
            if ch in "({":
                d += 1
            elif ch in ")}":
                d -= 1
            if ch == "," and d == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        out = []
        for p in parts:
            out += _bits(p, dims_by_sig)
        return out
    m = re.fullmatch(r"(\w+)((?:\[[^\]]*\])*)", e)
    sig, sels = m.group(1), re.findall(r"\[([^\]]*)\]", m.group(2))
    dims = dims_by_sig[sig]

    def walk(level, path, si):
        if si < len(sels):
            sel = sels[si]
            if ":" in sel:
                a, b = (int(x) for x in sel.split(":"))
                out = []
                for i in range(max(a, b), min(a, b) - 1, -1):
                    out += walk(level + 1, path + [i], si + 1)
                return out
            return walk(level + 1, path + [int(sel)], si + 1)
        if level == len(dims):
            return [sig + "".join("[%d]" % i for i in path)]
        l, r = dims[level]
        out = []
        for i in range(max(l, r), min(l, r) - 1, -1):
            out += walk(level + 1, path + [i], si)
        return out

    return walk(0, [], 0)


def _dims_of(src):
    out = {}
    for line in src.splitlines():
        m = re.match(r"\s*(?:logic|reg|bit|wire|tri)\b(?:\s+(?:logic|signed|unsigned))*\s*"
                     r"((?:\[\s*\d+\s*:\s*\d+\s*\])*)\s*(\w+)\s*;", line)
        if m and m.group(1):
            out[m.group(2)] = [tuple(int(x) for x in d.split(":"))
                               for d in re.findall(r"\[\s*(\d+\s*:\s*\d+)\s*\]", m.group(1))]
    return out


def _rhs(text, sig):
    m = re.search(r"assign %s_papercuts = (.*?);" % re.escape(sig), text, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


# -------------------------------------------------------------------- check --
def _check(name, src):
    pc = Papercutter(SyntaxTree.fromText(src), True)  # intermediate-wire bit shrink
    info = pc.cut_info()
    muxed = print_tree(insert_muxes(SyntaxTree.fromText(src), True, True, True, **MUX_KW))
    sel_nums = [int(m) for m in re.findall(r"pc_sel(\d+)", muxed)]
    nsel = max(sel_nums, default=-1) + 1

    assert nsel == len(info), (
        f"{name}: {len(info)} cuts but {nsel} selects -- families are misaligned")

    dims = _dims_of(src)
    cf_owner = {}  # signal -> (force-0 select, force-1 select)
    for sig, s1, s0 in re.findall(
            r"assign (\w+)_papercuts = pc_sel(\d+) \? 1'b1 : \(pc_sel(\d+) \?", muxed):
        cf_owner[sig] = (int(s0), int(s1))
    bs_sigs = [s for s in re.findall(r"assign (\w+)_papercuts =", muxed) if s not in cf_owner]

    case_idx = [i for i, (l, _) in enumerate(info) if l.startswith("case")]
    off = _force(muxed, -1, nsel)
    base_guards = [_reduce(g) for g in _guards(off)]

    # All selects low must be a no-op for every family. Case rewrites the statement
    # into an if-chain, and bit-shrink/force-const add companion signals and redirect
    # reads, so for those the no-op property is checked per family just below rather
    # than by comparing the whole body.
    families = {l.split("(")[0] for l, _ in info}
    if families <= {"ternary", "if", "binop"}:
        assert _norm(_reduce(_body(off))) == _norm(_body(src)), \
            f"{name}: all-off changed the design"
    for g in base_guards:
        assert "1'b" not in g, f"{name}: all-off left guard {g} constant"
    for sig in cf_owner:
        assert _fold(_rhs(off, sig)) == sig, f"{name}: all-off forced {sig}"
    for sig in bs_sigs:
        assert _bits(_fold(_rhs(off, sig)), dims) == _bits(sig, dims), \
            f"{name}: all-off shrank {sig}"

    for i, (label, _) in enumerate(info):
        fam = label.split("(")[0]
        forced = _force(muxed, i, nsel)

        if fam in ("ternary", "binop"):
            got, want = _norm(_reduce(_body(forced))), _norm(_body(pc.cut_index_text([i])))
            assert got == want, f"{name} cut[{i}] {label}:\n  mux: {got}\n  cut: {want}"

        elif fam == "if":
            want = "1'b1" if label.endswith("(keep-true)") else "1'b0"
            preds = [_reduce(g).strip("()") for g in _guards(_body(forced))]
            assert want in preds, f"{name} cut[{i}] {label}: predicates {preds} want {want}"

        elif fam == "case":
            n = case_idx.index(i)
            g = [_reduce(x) for x in _guards(forced)]
            assert g[n] == "1'b0", f"{name} cut[{i}]: item {n} guard is {g[n]}, want 1'b0"
            for m in range(len(g)):
                if m != n:
                    assert g[m] == base_guards[m], f"{name} cut[{i}]: guard {m} changed"

        elif fam == "bitshrink":
            want_src = pc.cut_index_text([i])
            for sig in bs_sigs:
                got = _bits(_fold(_rhs(forced, sig)), dims)
                want = _bits(_rhs(want_src, sig) or sig, dims)
                assert got == want, f"{name} cut[{i}] {sig}:\n  mux: {got}\n  cut: {want}"

        elif fam == "force-const":
            lit = "1'b1" if label.endswith("(1)") else "1'b0"
            for sig, sels in cf_owner.items():
                got = _fold(_rhs(forced, sig))
                want = lit if i in sels else sig
                assert got == want, f"{name} cut[{i}] {sig}: got {got}, want {want}"

    return len(info)


CASES = {
    "ternary": """module m (input logic p, input logic a, input logic b, output logic y);
  assign y = p ? a : b;
endmodule
""",
    "nested-ternary": """module m (input logic a, input logic b, input logic c,
          input logic d, input logic e, output logic y);
  assign y = (a ? b : c) ? d : e;
endmodule
""",
    "if-else": """module m (input logic p, input logic a, input logic b, output logic y);
  always_comb begin
    if (p) y = a;
    else y = b;
  end
endmodule
""",
    "if-no-else": """module m (input logic p, input logic a, output logic y);
  always_comb begin
    y = 1'b0;
    if (p) y = a;
  end
endmodule
""",
    "binop": """module m (input logic a, input logic b, output logic y);
  assign y = a & b;
endmodule
""",
    "nested-binop": """module m (input logic a, input logic b, input logic c, output logic y);
  assign y = (a & b) | c;
endmodule
""",
    "shift-keep-left-only": """module m (input logic [7:0] a, input logic [2:0] s, output logic [7:0] y);
  assign y = a << s;
endmodule
""",
    "case": """module m (input logic [1:0] a, output logic [1:0] o);
  always_comb begin
    case (a)
      2'd0: o = 2'd3;
      2'd1: o = 2'd2;
      default: o = 2'd0;
    endcase
  end
endmodule
""",
    "case-overlapping-items": """module m (input logic [1:0] a, output logic [1:0] o);
  always_comb begin
    case (a)
      2'd1: o = 2'd3;
      2'd1: o = 2'd2;
      default: o = 2'd0;
    endcase
  end
endmodule
""",
    "bitshrink-signed-and-net": """module m (input logic [7:0] a, output logic [7:0] o1, output logic [7:0] o2);
  logic signed [7:0] sgn;
  wire [7:0] w;
  assign sgn = a;
  assign w = a;
  assign o1 = sgn;
  assign o2 = w;
endmodule
""",
    "bitshrink-multidim": """module m (input logic [3:0][7:0] a, output logic [31:0] o);
  logic [3:0][7:0] m2;
  assign m2 = a;
  assign o = m2;
endmodule
""",
    "force-const": """module m (input logic c, input logic d, output logic z);
  wire en;
  logic flag;
  assign en = c;
  always_comb flag = d;
  assign z = en & flag;
endmodule
""",
}


# ------------------------------------------------------------------ hierarchy --
_LEAF = """module leaf (input logic p, input logic a, input logic b, output logic y);
  assign y = p ? a : b;
endmodule
"""
_MID = """module mid (input logic p, input logic a, input logic b, output logic y, output logic z);
  leaf u_leaf (.p(p), .a(a), .b(b), .y(y));
  assign z = a & b;
endmodule
"""
_TOP_NAMED = """module top (input logic p, input logic a, input logic b,
            output logic y, output logic z, output logic w);
  mid u_mid (.p(p), .a(a), .b(b), .y(y), .z(z));
  assign w = p ? a : b;
endmodule
"""
_TOP_ORDERED = _TOP_NAMED.replace("mid u_mid (.p(p), .a(a), .b(b), .y(y), .z(z));",
                                  "mid u_mid (p, a, b, y, z);")


def _check_hierarchy(label, top_src):
    """A child's selects must reach the top as distinct ports, whichever connection
    style the parent instantiates it with, and the whole design must still
    elaborate."""
    import pyslang

    srcs = {"leaf": _LEAF, "mid": _MID, "top": top_src}
    muxed, own = {}, {}
    for n, src in srcs.items():
        t = insert_muxes(SyntaxTree.fromText(src), True, True, True, **MUX_KW)
        muxed[n] = t
        nums = [int(m) for m in re.findall(r"\bpc_sel(\d+)\b", print_tree(t))]
        own[n] = max(nums) + 1 if nums else 0
        assert own[n] == len(Papercutter(SyntaxTree.fromText(src)).cut_info()), \
            f"{label}/{n}: select count does not match cut count"

    kids = {n: [c for c in get_instantiated_modules(t) if c in muxed] for n, t in muxed.items()}
    fwd = {}

    def forwarded(n):
        if n in fwd:
            return fwd[n]
        out = []
        for c in kids.get(n, []):
            for e in [(c, i) for i in range(own[c])] + forwarded(c):
                if e not in out:
                    out.append(e)
        fwd[n] = out
        return out

    for n in muxed:
        forwarded(n)
    assert set(fwd["top"]) == {("mid", 0), ("mid", 1), ("leaf", 0), ("leaf", 1)}, \
        f"{label}: top must forward both descendants' selects, got {fwd['top']}"

    out = {}
    for n, t in muxed.items():
        extra = [f"pc_sel_{m}_{i}" for m, i in fwd[n]]
        conns = {}
        for c in kids.get(n, []):
            pairs = [(f"pc_sel{i}", f"pc_sel_{c}_{i}") for i in range(own[c])]
            pairs += [(f"pc_sel_{m}_{i}", f"pc_sel_{m}_{i}") for m, i in fwd[c]]
            if pairs:
                conns[c] = pairs
        out[n] = print_tree(wire_mux_hierarchy(t, extra, conns) if (extra or conns) else t)

    got = set(re.findall(r"pc_sel\w*", out["top"].split(");")[0]))
    want = {f"pc_sel{i}" for i in range(own["top"])} | {f"pc_sel_{m}_{i}" for m, i in fwd["top"]}
    assert got == want, f"{label}: top ports {sorted(got)} != {sorted(want)}"

    # The leaf is only reachable through mid, so mid must pass its selects down.
    assert ".pc_sel0(pc_sel_leaf_0)" in out["mid"], f"{label}: mid does not drive leaf's selects"

    comp = pyslang.ast.Compilation()
    comp.addSyntaxTree(SyntaxTree.fromText("\n".join(out.values())))
    rep = pyslang.DiagnosticEngine.reportAll(comp.sourceManager, comp.getAllDiagnostics())
    errs = [l for l in rep.splitlines() if " error:" in l]
    assert not errs, f"{label}: wired design does not elaborate:\n" + "\n".join(errs[:5])


def run():
    total = 0
    for name, src in CASES.items():
        total += _check(name, src)

    # casez/casex cannot be muxed faithfully and must be refused, not silently
    # left inert -- while remaining perfectly cuttable.
    wild = """module m (input logic [3:0] a, output logic [1:0] o);
  always_comb begin
    casez (a)
      4'b1???: o = 2'd3;
      default: o = 2'd0;
    endcase
  end
endmodule
"""
    try:
        insert_muxes(SyntaxTree.fromText(wild), True, True, True, **MUX_KW)
        raise AssertionError("casez was muxed instead of rejected")
    except RuntimeError as e:
        assert "casez" in str(e), e
    assert len(Papercutter(SyntaxTree.fromText(wild)).cut_info()) == 1, \
        "casez must still be cuttable"
    # ... and the escape hatch reserves its selects rather than dropping them.
    out = print_tree(insert_muxes(SyntaxTree.fromText(wild), True, True, True,
                                  caseMux=False, binopMux=True, constForceMux=True))
    assert "pc_sel0" in out, "caseMux=False must still reserve the select"

    _check_hierarchy("named", _TOP_NAMED)
    _check_hierarchy("positional", _TOP_ORDERED)

    print(f"test_mux_alignment: OK ({len(CASES)} designs, {total} cuts, all selects "
          f"aligned; hierarchy wired for named + positional instantiation)")


if __name__ == "__main__":
    run()
