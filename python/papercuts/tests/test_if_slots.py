"""An if(keep-false) cut on an `if` with no else, wherever the `if` sits.

keep-false on an `if` with no else means "nothing runs". The cutter used to
remove() the statement, which only works when the `if` is a list element (a
statement inside begin/end). An `if` can also fill a single-statement slot --
the clause of an `else` (`else if`), a bare always body, a bare then-branch --
and removing it there leaves a null child: slang's not_null asserts
("Assertion 'ptr' failed") and the whole run dies at cut generation. The
elaborator always produced that shape once it started emitting `else if` flat
(to keep unique/priority on the head of the chain), so 15 of 26 OpenCores
designs crashed in -m and -e alike. The cut is now an empty `begin end`, which
fits every statement slot and keeps a label (a label on a bare `;` is illegal).

Checks: every slot shape cuts and recompiles cleanly with exactly that `if`
gone; a label survives; the cut composes with an outer keep-else cut in one
cut_index() call; and the real path -- an elaborated flat `else if` chain --
cuts at every index of every family.
"""

import contextlib
import io
import tempfile
from pathlib import Path

from papercuts import Papercutter
from papercuts.elaborator import elaborate
from pyslang import ast
from pyslang.ast import Compilation
from pyslang.syntax import SyntaxTree

# One no-else `if` per slot shape, each predicate a distinct signal so a cut's
# target can be told apart in the output.
SLOTS_SRC = """
module m (
    input  logic clk,
    input  logic a, b, c, d, e, f,
    output logic q, r, s, t, u
);
  always @(posedge clk) begin
    if (a) q <= 1'b1;
    else if (b) q <= 1'b0;
  end
  always @(posedge clk)
    if (c) r <= 1'b1;
  always @(posedge clk) begin
    if (a)
      if (d) s <= 1'b1;
  end
  always @(posedge clk) begin
    if (e) t <= 1'b1;
    blk: if (f) u <= 1'b1;
  end
endmodule
"""

# The crashing designs' shape: a flat else-if chain with no final else, under an
# async reset (ima_adpcm_enc L161).
ELAB_SRC = """
module m (
    input  logic       clock,
    input  logic       reset,
    input  logic [2:0] sq,
    input  logic [7:0] pre,
    output logic [6:0] idx
);
  always @(posedge clock or posedge reset) begin
    if (reset) idx <= 7'b0;
    else if (sq == 3'b101) begin
      if (pre[7]) idx <= 7'b0;
      else if (pre[6:0] > 7'd88) idx <= 7'd88;
      else idx <= pre[6:0];
    end
  end
endmodule
"""


def _compile(text):
    comp = Compilation()
    comp.addSyntaxTree(SyntaxTree.fromText(text))
    errs = [d for d in comp.getAllDiagnostics() if d.isError()]
    assert not errs, f"{len(errs)} compile error(s) in:\n{text}"
    return comp


def _if_predicates(text):
    """Predicate source text of every if statement, in visit order."""
    found = []

    def visit(node):
        if isinstance(node, ast.ConditionalStatement):
            found.append(" ".join(str(node.conditions[0].expr.syntax).split()))

    _compile(text).getRoot().visit(visit)
    return found


def _keep_false(info, line):
    hits = [i for i, (t, ln) in enumerate(info) if t == "if(keep-false)" and ln == line]
    assert len(hits) == 1, f"expected one if(keep-false) on line {line}, got {hits}"
    return hits[0]


def _line_of(src, needle):
    hits = [n for n, text in enumerate(src.splitlines(), 1) if needle in text]
    assert len(hits) == 1, f"{needle!r} appears on lines {hits}"
    return hits[0]


def _elaborate(src):
    # elaborate() reads from files (SyntaxTree.fromFile), so stage the source.
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "design.sv"
        f.write_text(src)
        with contextlib.redirect_stderr(io.StringIO()):
            return elaborate([str(f)])


def _check_slots():
    pc = Papercutter(SyntaxTree.fromText(SLOTS_SRC))
    info = pc.cut_info()
    before = _if_predicates(SLOTS_SRC)
    assert before == ["a", "b", "c", "a", "d", "e", "f"], f"fixture parsed unexpectedly: {before}"

    # (predicate, line marker): else-if slot, bare always body, bare then-branch,
    # list element, labelled list element.
    for pred, marker in [("b", "else if (b)"), ("c", "if (c)"), ("d", "if (d)"),
                         ("e", "if (e)"), ("f", "if (f)")]:
        idx = _keep_false(info, _line_of(SLOTS_SRC, marker))
        text = pc.cut_index_text([idx])  # raised "Assertion 'ptr' failed" before the fix
        after = _if_predicates(text)
        want = list(before)
        want.remove(pred)
        assert after == want, f"keep-false on `if ({pred})` left ifs {after}, want {want}:\n{text}"
        if pred == "f":
            assert "blk:" in text, f"label dropped by keep-false:\n{text}"

    # Composition: keep-else on the outer `if (a)` lifts `if (b)` into its place,
    # and keep-false on `if (b)` must still apply there, in the same call.
    outer = _keep_false(info, _line_of(SLOTS_SRC, "if (a) q <= 1'b1;"))
    inner = _keep_false(info, _line_of(SLOTS_SRC, "else if (b)"))
    text = pc.cut_index_text([outer, inner])
    after = _if_predicates(text)
    assert after == ["c", "a", "d", "e", "f"], f"composed cut left ifs {after}:\n{text}"
    assert "q <=" not in text, f"composed cut left an assignment to q:\n{text}"


def _check_elaborated():
    out = _elaborate(ELAB_SRC)
    # Guard the fixture: the emitter must give the flat `else if` that crashed.
    flat = [ln for ln in out.splitlines() if ln.strip().startswith("else if")]
    assert len(flat) == 2, f"expected two flat else-ifs in elaborated output:\n{out}"

    pc = Papercutter(SyntaxTree.fromText(out))
    info = pc.cut_info()
    assert any(t == "if(keep-false)" for t, _ in info), info
    for i, (ctype, line) in enumerate(info):
        try:
            text = pc.cut_index_text([i])
        except RuntimeError as e:
            raise AssertionError(f"cut {i} {ctype} L{line} raised: {e}") from e
        _compile(text)
    return len(info)


def run():
    _check_slots()
    n = _check_elaborated()
    print(f"test_if_slots: OK (else-if / bare body / bare then / list / labelled; "
          f"composed with keep-else; {n} elaborated cuts)")


if __name__ == "__main__":
    run()
