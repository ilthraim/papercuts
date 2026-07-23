"""Coverage for multi-bit (iterative) bit-shrink support in the C++ core.

Enumeration-level only (no equivalence checking): exercises the new `amounts`
argument to cut_index_text/cut_index and the cut_shrink_widths() accessor, which
together let the Python pipeline greedily narrow a signal by several bits. The
greedy EC loop itself lives in __main__.py and needs a backend, so it is not
tested here.
"""

from papercuts import Papercutter
from pyslang.syntax import SyntaxTree

SRC = """
module shrink_probe (
    input  logic [7:0] a,
    input  logic       s,
    output logic [7:0] y
    );
    logic [7:0] w;
    logic [3:0][7:0] arr;
    always_comb begin
        w = a;
        arr = '0;
        y = s ? w : a;
    end
endmodule
"""


def _norm(s):
    # Collapse whitespace so substring checks don't depend on exact spacing.
    return " ".join(s.split())


def run():
    tree = SyntaxTree.fromText(SRC)
    pc = Papercutter(tree)
    info = pc.cut_info()
    widths = pc.cut_shrink_widths()

    assert len(widths) == len(info), "cut_shrink_widths must align 1:1 with cut_info"

    bitshrink_idxs = [i for i, (t, _) in enumerate(info) if t == "bitshrink"]
    # w (one dim) + arr (two packed dims) = 3 bitshrink cuts. Ports are not collected.
    assert len(bitshrink_idxs) == 3, f"expected 3 bitshrink cuts, got {len(bitshrink_idxs)}"

    # Non-bitshrink cuts report width 0; bitshrink cuts report their dim width (>0).
    for i, (t, _) in enumerate(info):
        if t == "bitshrink":
            assert widths[i] > 0, f"bitshrink idx {i} should have width>0"
        else:
            assert widths[i] == 0, f"non-bitshrink idx {i} ({t}) should have width 0"

    # Regression: amounts absent == amounts={idx:1} == today's 1-bit shrink.
    for bi in bitshrink_idxs:
        assert pc.cut_index_text([bi]) == pc.cut_index_text([bi], {bi: 1}), (
            f"default cut must equal explicit 1-bit shrink at idx {bi}"
        )

    # Classify each bitshrink cut by which declaration its 1-bit form narrows.
    w_idx = arr_d0_idx = arr_d1_idx = None
    for bi in bitshrink_idxs:
        one = _norm(pc.cut_index_text([bi]))
        if "logic [6:0] w;" in one:
            w_idx = bi
        elif "logic [2:0][7:0] arr;" in one:
            arr_d0_idx = bi
        elif "logic [3:0][6:0] arr;" in one:
            arr_d1_idx = bi
    assert None not in (w_idx, arr_d0_idx, arr_d1_idx), (
        f"could not classify bitshrink cuts: w={w_idx} d0={arr_d0_idx} d1={arr_d1_idx}"
    )

    assert widths[w_idx] == 8 and widths[arr_d1_idx] == 8 and widths[arr_d0_idx] == 4

    # Multi-bit shrink on the scalar w: k bits off the top -> [7-k:0], k=1..7.
    for k in range(1, 8):
        out = _norm(pc.cut_index_text([w_idx], {w_idx: k}))
        assert f"logic [{7 - k}:0] w;" in out, f"w shrink by {k} should give [{7-k}:0]"

    # Clamp: asking for more than width-1 bits leaves at least one bit ([0:0]),
    # identical to the max legal shrink.
    assert _norm(pc.cut_index_text([w_idx], {w_idx: 99})) == _norm(
        pc.cut_index_text([w_idx], {w_idx: 7})
    ), "over-cap shrink must clamp to width-1 (one bit left)"

    # Packed dims shrink independently: dim1 by 2 narrows only the inner range.
    out = _norm(pc.cut_index_text([arr_d1_idx], {arr_d1_idx: 2}))
    assert "logic [3:0][5:0] arr;" in out, "arr dim1 shrink by 2 should give [3:0][5:0]"
    out = _norm(pc.cut_index_text([arr_d0_idx], {arr_d0_idx: 2}))
    assert "logic [1:0][7:0] arr;" in out, "arr dim0 shrink by 2 should give [1:0][7:0]"

    print(
        f"test_iterative_bitshrink: OK "
        f"(w=idx{w_idx} w8, arr dims idx{arr_d0_idx}/w4 + idx{arr_d1_idx}/w8)"
    )


if __name__ == "__main__":
    run()
