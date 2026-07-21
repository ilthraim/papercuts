"""Nested / overlapping cut composition.

Consolidation applies every proven cut for a module in one cut_index() call.
When cuts nest inside a single expression -- an operand of an operand, a
ternary in another ternary's kept branch, an `if` in a kept `else` -- all of
them must be applied, not just the outermost. slang's CST rewriter used to
splice a replacement subtree verbatim, so any change nested inside (or on) a
replaced node was silently dropped and only the outermost cut in each chain
survived. This exercises the CloneVisitor fix that resolves a replacement --
following replacement chains and recursing into the kept subtree -- instead of
splicing it verbatim.

Each expression below is a three-deep chain. We apply the same keep-one-side
cut at every level in a single cut_index() and require the whole chain to
collapse:
  out  : keep-left of all three binops         -> a|x|b|c  collapses to `a`
  out2 : keep-false (node.right) of all ternaries -> collapses to 2'b11
  out3 : keep-else of all three ifs            -> collapses to out3 = 2'b11
Under the pre-fix behaviour each of `|`, `?`, and `if` would still be present.
"""

from papercuts import Papercutter
from pyslang.syntax import SyntaxTree

SRC = """
module nested (
    input  logic x,
    output logic out,
    output logic [1:0] out2,
    output logic [1:0] out3
    );
    logic a, b, c;

    always_comb begin
        a = 1'b0;
        b = 1'b0;
        c = 1'b0;
    end

    assign out = a | x | b | c;
    assign out2 = (a ? 2'b00 : (x ? 2'b01 : (b ? 2'b10 : 2'b11)));

    always_comb begin
        if (a) out3 = 2'b00;
        else if (x) out3 = 2'b01;
        else if (b) out3 = 2'b10;
        else out3 = 2'b11;
    end
endmodule
"""


def _norm(text):
    return " ".join(text.split())


def _rhs(norm, lhs):
    # RHS of `assign <lhs> = ...;` from whitespace-normalized text.
    return norm.split(f"{lhs} =", 1)[1].split(";", 1)[0].strip()


def _by_type(info, prefix):
    # cut_index() indices for a family, in enumeration order: two variants per
    # node (keep-left/right, keep-false/true, keep-true/else), nodes pre-order.
    return [i for i, (t, _) in enumerate(info) if t.startswith(prefix)]


def run():
    tree = SyntaxTree.fromText(SRC)
    pc = Papercutter(tree)
    info = pc.cut_info()

    binop = _by_type(info, "binop")    # 3 nodes x {keep-left, keep-right}
    tern = _by_type(info, "ternary")   # 3 nodes x {keep-false, keep-true}
    ifs = _by_type(info, "if")         # 3 nodes x {keep-else, keep-true}

    assert len(binop) == 6, f"expected 6 binop cuts, got {len(binop)}"
    assert len(tern) == 6, f"expected 6 ternary cuts, got {len(tern)}"
    assert len(ifs) == 6, f"expected 6 if cuts, got {len(ifs)}"

    # First variant of each node (indices 0, 2, 4): keep-left / keep-false /
    # keep-else. Applying all three levels of a chain in ONE cut_index() is the
    # whole point -- every level must collapse, not just the outermost.
    layer = [binop[0], binop[2], binop[4],
             tern[0], tern[2], tern[4],
             ifs[0], ifs[2], ifs[4]]

    text = pc.cut_index(layer).root.__str__()
    norm = _norm(text)

    # out: the binop chain must fully collapse -- no `|` left, survivor is `a`.
    out_rhs = _rhs(norm, "out")
    assert "|" not in out_rhs, f"binop chain not fully collapsed: out = {out_rhs!r}"
    assert out_rhs.replace("(", "").replace(")", "").strip() == "a", \
        f"binop chain collapsed to wrong operand: out = {out_rhs!r}"

    # out2: the ternary chain must fully collapse -- no `?` left, survivor 2'b11.
    out2_rhs = _rhs(norm, "out2")
    assert "?" not in out2_rhs, f"ternary chain not fully collapsed: out2 = {out2_rhs!r}"
    assert "2'b11" in out2_rhs, f"ternary chain collapsed to wrong arm: out2 = {out2_rhs!r}"

    # out3: the if/else chain must fully collapse -- no `if` left, survivor 2'b11.
    assert "if" not in norm, f"if chain not fully collapsed (an `if` survives): {norm!r}"
    assert "out3 = 2'b11" in norm, f"if chain collapsed to wrong arm: {norm!r}"

    print("test_nested: OK (binop/ternary/if chains fully composed)")


if __name__ == "__main__":
    run()
