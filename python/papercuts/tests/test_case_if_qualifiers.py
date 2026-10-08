"""Case flavour and unique/priority qualifiers survive elaborated emission.

The emitter wrote every case statement as a bare `case`, dropping the
casez/casex/inside flavour and the unique/unique0/priority qualifier. Labels are
emitted as folded constants, so casez's `4'b1???` became `4'b1zzz` under plain
`case` -- a literal match on z instead of a wildcard. The FV gate would catch
that, but only by failing the whole run on any design that uses casez.
`case inside` also died outright on a range label (`[0:3]`), which had no
emitter.

`if` lost its qualifier the same way. Restoring it is not enough on its own:
slang records unique/priority only on the head of an else-if chain, and the
emitter wrapped every `else` branch in begin/end, which moves the rest of the
chain into a separate, unqualified statement. An `else if` must stay flat,
while a source `else begin if ... end` must stay wrapped.

The golden assertion is a round-trip through slang: recompile the emitted
design and check that every case and if statement has the original's
qualifier, flavour, evaluated labels and else-chain shape, in order. A textual
check would miss a label whose digits read right but whose value changed.
"""

import contextlib
import io
import tempfile
from pathlib import Path

from papercuts.elaborator import elaborate
from pyslang import ast
from pyslang.ast import Compilation
from pyslang.syntax import SyntaxTree

CASE_SRC = """
module m (
    input  logic [3:0] a,
    output logic [2:0] o,
    output logic [1:0] p,
    output logic [1:0] q,
    output logic [1:0] r
);
  always_comb begin
    casez (a)
      4'b1???: o = 3'd4;
      4'b0z1?: o = 3'd3;
      4'b??01: o = 3'd2;
      default: o = 3'd0;
    endcase
    priority casex (a)
      4'bx0?1: p = 2'd1;
      default: p = 2'd0;
    endcase
    unique0 case (a) inside
      [4'd0:4'd3], 4'd7: q = 2'd1;
      4'b11??: q = 2'd2;
      default: q = 2'd0;
    endcase
    unique case (a)
      4'd1, 4'd2: r = 2'd1;
      default: r = 2'd0;
    endcase
  end
endmodule
"""

IF_SRC = """
module m (
    input  logic a, b, c,
    output logic o, p, q, r, s
);
  always_comb begin
    // One qualified chain: the else-ifs must stay inside it.
    unique if (a) o = 1'b1;
    else if (b) o = 1'b0;
    else if (c) o = 1'b1;
    else o = 1'b0;
    // A block in the else is a separate statement and must stay one. (This is
    // also the only way to qualify an inner chain: `else priority if` is illegal.)
    priority if (a) p = 1'b1;
    else begin
      if (b) p = 1'b0;
      else p = c;
    end
    // unique0 with no final else: the chain may match nothing.
    unique0 if (a) q = 1'b1;
    else if (b) q = 1'b0;
    // Plain chain, unchanged semantics.
    if (a) r = 1'b1;
    else if (b) r = c;
    else r = 1'b0;
    // Nested if in the true branch: no dangling else.
    unique if (a) begin
      if (b) s = 1'b1;
    end
    else s = c;
  end
endmodule
"""


def _elaborate(src):
    # elaborate() reads from files (SyntaxTree.fromFile), so stage the source.
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "design.sv"
        f.write_text(src)
        with contextlib.redirect_stderr(io.StringIO()):
            return elaborate([str(f)])


def _label(e, ctx):
    # Evaluated, not read from .constant: case labels are never folded, so
    # .constant is None and every label would compare equal.
    if isinstance(e, ast.ValueRangeExpression):
        return (_label(e.left, ctx), _label(e.right, ctx))
    v = e.eval(ctx)
    assert v.value is not None, f"label did not evaluate: {e.syntax}"
    return str(v)


def _else_shape(iff):
    """How an if's else branch relates to its chain. The emitter wraps leaf
    branches in begin/end, so a bare statement and a block are the same "leaf";
    what must survive is whether the chain continues or nests in a block."""
    if iff is None:
        return None
    if isinstance(iff, ast.ConditionalStatement):
        return "chain"
    while isinstance(iff, ast.BlockStatement):
        iff = iff.body
    return "nested-if" if isinstance(iff, ast.ConditionalStatement) else "leaf"


def _stmts(text):
    """Shape of each case and if statement, in visit order."""
    comp = Compilation()
    comp.addSyntaxTree(SyntaxTree.fromText(text))
    errs = [d for d in comp.getAllDiagnostics() if d.isError()]
    assert not errs, f"{len(errs)} compile error(s) in:\n{text}"

    found = []
    ctx = ast.EvalContext(comp.getRoot())

    def visit(node):
        if isinstance(node, ast.CaseStatement):
            found.append(("case", node.condition.name, node.check.name,
                          [[_label(e, ctx) for e in it.expressions] for it in node.items]))
        elif isinstance(node, ast.ConditionalStatement):
            found.append(("if", node.check.name, _else_shape(node.ifFalse)))

    comp.getRoot().visit(visit)
    return found


def _round_trip(src):
    want = _stmts(src)
    out = _elaborate(src)
    got = _stmts(out)
    assert got == want, f"statements changed by emission:\n" \
                        f"  want {want}\n  got  {got}\n{out}"
    return want


def run():
    cases = _round_trip(CASE_SRC)
    assert [c[1:3] for c in cases] == [
        ("WildcardJustZ", "None_"), ("WildcardXOrZ", "Priority"),
        ("Inside", "Unique0"), ("Normal", "Unique"),
    ], f"case fixture parsed unexpectedly: {cases}"

    ifs = _round_trip(IF_SRC)
    # Guard the fixture itself: slang must give the shapes the comments claim,
    # or the round-trip proves nothing about chains.
    assert ifs[:3] == [
        ("if", "Unique", "chain"),
        ("if", "None_", "chain"),
        ("if", "None_", "leaf"),
    ], f"unique chain fixture parsed unexpectedly: {ifs}"
    assert ("if", "Priority", "nested-if") in ifs, ifs
    assert ("if", "Unique0", "chain") in ifs, ifs

    print("test_case_if_qualifiers: OK (casez/casex/inside, unique/unique0/priority "
          "on case and if chains)")


if __name__ == "__main__":
    run()
