"""Coverage for `Papercutter.const_priority`.

Off, a binop or case cut is applied as selected, even when the part it decides reads
a signal a force-const cut forces: `y = a & x` with keep-left and x forced to 0 comes out `y = a`.
On (the default), the force-const cut wins there: the binop cut whose dropped operand reads x and the case cut
whose selector reads x are not applied, so the result is `y = a & 1'b0`. Ternary and if cuts keep
priority either way, and a cut that only deletes code containing a forced read (an if keeping its
true branch, whose dropped else branch reads x) still applies.
"""

from papercuts import Papercutter
from pyslang.syntax import SyntaxTree

SRC = """
module cp (
    input  logic       clk, a, b, c, s0, s1,
    input  logic [1:0] sel,
    output logic       y0, y1, y2, y3, y4
    );
    logic x;
    assign x = a ^ b;
    assign y0 = a & x;
    assign y1 = x ? b : c;
    always_comb begin
        if (x) y2 = b;
        else   y2 = c;
    end
    always_comb begin
        case ({x, s0})
            2'b00: y3 = a;
            2'b01: y3 = b;
            default: y3 = c;
        endcase
    end
    always_comb begin
        if (s1) y4 = a;
        else    y4 = x;
    end
endmodule
"""


def _index(pc, typ, needle):
    """Cut index of type `typ` on the (unique) source line containing `needle`."""
    lines = SRC.splitlines()
    want = next(i for i, ln in enumerate(lines, start=1) if needle in ln)
    hits = [i for i, (t, ln) in enumerate(pc.cut_info()) if t == typ and ln == want]
    assert len(hits) == 1, (typ, needle, hits)
    return hits[0]


def _text(on, idxs):
    pc = Papercutter(SyntaxTree.fromText(SRC))
    pc.const_priority = on
    return " ".join(pc.cut_index_text(idxs).split())


def run():
    pc = Papercutter(SyntaxTree.fromText(SRC))
    assert pc.const_priority is True
    x0 = _index(pc, "force-const(0)", "logic x;")
    keep_a = _index(pc, "binop(and,keep-left)", "y0 = a & x")
    tern = _index(pc, "ternary(keep-true)", "y1 = x ?")
    if_x = _index(pc, "if(keep-true)", "if (x)")
    if_s1 = _index(pc, "if(keep-true)", "if (s1)")
    case = [i for i, (t, ln) in enumerate(pc.cut_info()) if t == "case(prune-item)"][0]

    # Off: order-based selection, unchanged.
    off = _text(False, [x0, keep_a, tern, if_x, case, if_s1])
    assert "assign y0 = a;" in off, off
    assert "assign y1 = b;" in off, off

    # On: the forced signal wins over the binop and case cuts that would decide its read.
    on = _text(True, [x0, keep_a, tern, if_x, case, if_s1])
    assert "assign y0 = a & 1'b0;" in on, on
    # ternary and if cuts keep priority
    assert "assign y1 = b;" in on, on
    assert "if (1'b0)" not in on and "y2 = b;" in on, on
    assert "case ({1'b0, s0})" in on and "2'b00: y3 = a;" in on, on
    # ... but a deletion that only contains a forced read still applies.
    assert "y4 = 1'b0" not in on and "y4 = a;" in on, on

    # On, with no force-const cut active: nothing changes.
    rest = [keep_a, tern, if_x, case, if_s1]
    assert _text(True, rest) == _text(False, rest)
    print("const_priority: ok")
