"""Coverage for the force-const cut family.

Forces reads of a 1-bit scalar (logic/reg/bit/wire) to a constant 1'b0/1'b1,
leaving the declaration and its driver intact. Only internal 1-bit scalars are
targeted -- multi-bit signals and ports are not collected -- and write positions
(assignment LHS, including inside an LHS concatenation) are never substituted.
"""

from papercuts import Papercutter
from pyslang.syntax import SyntaxTree

SRC = """
module fc (
    input  logic       a,
    input  logic [7:0] din,
    output logic       y,
    output logic [7:0] dout
    );
    logic s0;
    reg   r0;
    bit   b0;
    wire  w0;
    logic [7:0] wide;
    logic lhs0;

    assign w0 = a;
    assign y  = s0 & r0 & b0 & w0;

    always_comb begin
        s0 = a;
        r0 = a;
        b0 = a;
        wide = din;
        {lhs0, dout[6:0]} = din;
        dout[7] = lhs0;
    end
endmodule
"""


def _line_of(needle):
    for i, ln in enumerate(SRC.splitlines(), start=1):
        if needle in ln:
            return i
    raise AssertionError(f"substring not found in SRC: {needle!r}")


def _force_idxs(pc, line, polarity):
    tag = f"force-const({polarity})"
    return [i for i, (t, ln) in enumerate(pc.cut_info()) if t == tag and ln == line]


def run():
    # Both bit-shrink modes: cegis-comb always uses the intermediate-wire one, and
    # the substitution once ran only in narrow mode, so every force-const cut there
    # was identical to the source (runs/cegis_comb_dataset/ISSUES.md 103).
    for wire_mode in (False, True):
        n = _check(Papercutter(SyntaxTree.fromText(SRC), wire_mode))
    print(f"test_force_const: OK ({n} force-const cuts, narrow and intermediate-wire modes)")


def _check(pc):
    info = pc.cut_info()
    fc = [i for i, (t, _) in enumerate(info) if t.startswith("force-const")]

    # Five 1-bit scalars (s0, r0, b0, w0, lhs0) x 2 polarities = 10 cuts. The
    # multi-bit `wide` and every port (a, y, din, dout) are not collected.
    assert len(fc) == 10, f"expected 10 force-const cuts, got {len(fc)}"

    wide_line = _line_of("logic [7:0] wide;")
    assert not any(info[i][1] == wide_line for i in fc), "multi-bit signal must not be forced"

    # Force s0 -> 0: reads become 1'b0, but the declaration and its LHS write stay.
    (idx0,) = _force_idxs(pc, _line_of("logic s0;"), 0)
    out0 = pc.cut_index([idx0]).root.__str__()
    assert "y  = 1'b0 & r0 & b0 & w0;" in out0, f"read of s0 not forced to 0:\n{out0}"
    assert "logic s0;" in out0, "s0 declaration must be preserved"
    assert "s0 = a;" in out0, "s0 LHS write must not be substituted"

    # Force s0 -> 1: reads become 1'b1.
    (idx1,) = _force_idxs(pc, _line_of("logic s0;"), 1)
    out1 = pc.cut_index([idx1]).root.__str__()
    assert "y  = 1'b1 & r0 & b0 & w0;" in out1, f"read of s0 not forced to 1:\n{out1}"

    # Force lhs0 -> 0: the read is substituted, but its LHS concat occurrence is not.
    (idxc,) = _force_idxs(pc, _line_of("logic lhs0;"), 0)
    outc = pc.cut_index([idxc]).root.__str__()
    assert "dout[7] = 1'b0;" in outc, f"read of lhs0 not forced:\n{outc}"
    assert "{lhs0, dout[6:0]} = din;" in outc, "lhs0 in LHS concat must not be substituted"

    return len(fc)


if __name__ == "__main__":
    run()
