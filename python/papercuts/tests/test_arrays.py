"""Multi-packed-dimension bit-shrink (arrays).

Narrow mode shrinks each packed dimension of a vector independently: a
`logic [3:0][7:0] x;` yields two cuts, one narrowing the outer dimension
([3:0] -> [2:0]) and one the inner ([7:0] -> [6:0]). Nets behave the same.
Unpacked dimensions (memory depth, e.g. `mem [0:3]`) are left untouched and
ride along on the declarator; only the packed element width is shrunk.
"""

from papercuts import Papercutter
from pyslang.syntax import SyntaxTree

# Inline comments are intentionally avoided here: narrowing preserves any trailing
# comment on a declaration's line, which would defeat the exact-match assertions.
SRC = """
module arrays #(parameter W=8) (
    input  logic [7:0] a,
    output logic [7:0] y
    );
    logic [3:0][7:0] pk;
    wire [1:0][7:0] wpk;
    logic [7:0] mem [0:3];
    logic [7:0] vec;
    logic [3:0][7:0] pk2, pk3;

    assign wpk = {a, a};
    always_comb begin
        pk = '0;
        mem[0] = a; mem[1] = a; mem[2] = a; mem[3] = a;
        vec = a;
        pk2 = '0; pk3 = '0;
        y = pk[0] ^ wpk[0] ^ mem[0] ^ vec ^ pk2[0] ^ pk3[0];
    end
endmodule
"""


def _decl_lines(text):
    return [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith(("logic", "reg", "bit", "wire"))]


def run():
    tree = SyntaxTree.fromText(SRC)
    pc = Papercutter(tree)
    info = pc.cut_info()
    shrink_idxs = [i for i, (t, _) in enumerate(info) if t == "bitshrink"]

    # pk(2) + wpk(2) + mem(1) + vec(1) + pk2(2) + pk3(2) = 10.
    assert len(shrink_idxs) == 10, f"expected 10 bitshrink cuts, got {len(shrink_idxs)}"

    produced = set()
    for idx in shrink_idxs:
        out = pc.cut_index([idx])
        produced.update(_decl_lines(out.root.__str__()))

    # Each packed dimension is shrinkable independently, outer and inner.
    expected = {
        "logic [2:0][7:0] pk;",     # pk outer dim narrowed
        "logic [3:0][6:0] pk;",     # pk inner dim narrowed
        "wire [0:0][7:0] wpk;",     # net outer dim narrowed
        "wire [1:0][6:0] wpk;",     # net inner dim narrowed
        "logic [6:0] mem [0:3];",   # unpacked depth preserved, element width shrunk
        "logic [6:0] vec;",         # single-dim regression still works
        "logic [2:0][7:0] pk2;",    # shared decl: pk2 outer narrowed
        "logic [3:0][6:0] pk2;",    # shared decl: pk2 inner narrowed
    }
    missing = expected - produced
    assert not missing, f"missing narrowed forms: {missing}"

    # When pk2 is cut in the shared `pk2, pk3` declaration, pk3 must be split out
    # untouched at its full width (and vice-versa) -- no accidental co-narrowing.
    assert "logic [3:0][7:0] pk3;" in produced, "pk3 should remain full-width when pk2 is cut"
    assert "logic [3:0][7:0] pk2;" in produced, "pk2 should remain full-width when pk3 is cut"

    # mem's unpacked dimension must never be shrunk (out of scope): no [0:2].
    for line in produced:
        if "mem" in line:
            assert "[0:3]" in line, f"mem unpacked depth must be preserved: {line!r}"

    print(f"test_arrays: OK ({len(shrink_idxs)} bitshrink cuts)")


if __name__ == "__main__":
    run()
