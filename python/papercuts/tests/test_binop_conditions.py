"""Coverage for the `binops_in_conditions_only` Papercutter flag.

The binop cut family normally targets every reducible binary expression. With
`binops_in_conditions_only=True`, it must target only binops inside the
condition of an `if` statement or a ternary (`?:`) -- e.g. the `a & b` in
`if (a & b)` or the `g ^ h` in `(g ^ h) ? .. : ..` -- and leave binops in
branch bodies / assignment RHSs alone. Other cut families are unaffected.
"""

from papercuts import Papercutter
from pyslang.syntax import SyntaxTree

SRC = """
module cond_binops (
    input  logic [7:0] a, b, c, d, e, f, g, h,
    output logic [7:0] y,
    output logic       z
    );
    always_comb begin
        if (a & b) begin
            y = c + d;
        end
        else begin
            y = e - f;
        end
        z = (g ^ h) ? a[0] : b[0];
    end
endmodule
"""

# 1-based line numbers of each binop within SRC, located by a unique substring
# so the test stays correct if the source above is edited.
def _line_of(needle):
    for i, ln in enumerate(SRC.splitlines(), start=1):
        if needle in ln:
            return i
    raise AssertionError(f"substring not found in SRC: {needle!r}")


def _binop_lines(pc):
    return {line for (t, line) in pc.cut_info() if t.startswith("binop")}


def _nonbinop_count(pc):
    return sum(1 for (t, _) in pc.cut_info() if not t.startswith("binop"))


def run():
    cond_lines = {_line_of("if (a & b)"), _line_of("(g ^ h) ?")}  # if- + ternary-predicate
    body_lines = {_line_of("y = c + d;"), _line_of("y = e - f;")}  # branch-body RHS binops

    # Default: the binop family sees condition binops AND body binops.
    tree = SyntaxTree.fromText(SRC)
    pc_all = Papercutter(tree)
    all_lines = _binop_lines(pc_all)
    assert cond_lines <= all_lines, f"default should include condition binops: {cond_lines - all_lines}"
    assert body_lines <= all_lines, f"default should include body binops: {body_lines - all_lines}"

    # Flag on: only condition binops remain; body binops are gone.
    tree2 = SyntaxTree.fromText(SRC)
    pc_cond = Papercutter(tree2, binops_in_conditions_only=True)
    cond_only = _binop_lines(pc_cond)
    assert cond_only == cond_lines, (
        f"conditions-only should be exactly {sorted(cond_lines)}, got {sorted(cond_only)}"
    )
    assert not (body_lines & cond_only), f"body binops leaked into conditions-only: {body_lines & cond_only}"

    # Non-binop cut families (if, ternary, bitshrink, ...) are untouched by the flag.
    assert _nonbinop_count(pc_all) == _nonbinop_count(pc_cond), (
        "flag must not change non-binop cut families"
    )

    print(
        f"test_binop_conditions: OK "
        f"(all={len(all_lines)} lines, conditions-only={sorted(cond_only)})"
    )


if __name__ == "__main__":
    run()
