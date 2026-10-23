"""Gate primitives in the elaborated front end (ISCAS85-style netlists).

Each primitive becomes the continuous assign it is equivalent to, so the gates
are cuttable binops. Checked here by evaluating the emitted design on every
input against the gate's truth table with slang's own constant evaluation (the
elaboration gate does the same with Jasper on every run).
"""

import itertools
import os
import tempfile

from papercuts.elaborator import EmitError, elaborate_design

SRC = """
module g(input a, input b, input c, output o_and, output o_nand, output o_or, output o_nor,
         output o_xor, output o_xnor, output o_buf1, output o_buf2, output o_not);
    and  g0(o_and, a, b, c);
    nand g1(o_nand, a, b);
    or   g2(o_or, a, b, c);
    nor  g3(o_nor, a, b);
    xor  g4(o_xor, a, b, c);
    xnor g5(o_xnor, a, b);
    buf  g6(o_buf1, o_buf2, a);
    not  g7(o_not, b);
endmodule
"""

EXPECT = {
    "o_and": lambda a, b, c: a & b & c, "o_nand": lambda a, b, c: 1 - (a & b),
    "o_or": lambda a, b, c: a | b | c, "o_nor": lambda a, b, c: 1 - (a | b),
    "o_xor": lambda a, b, c: a ^ b ^ c, "o_xnor": lambda a, b, c: 1 - (a ^ b),
    "o_buf1": lambda a, b, c: a, "o_buf2": lambda a, b, c: a, "o_not": lambda a, b, c: 1 - b,
}


def _eval(src: str, a: int, b: int, c: int) -> dict[str, int]:
    """Evaluate every output of `g` for one input vector by substituting the
    inputs as constants into the emitted assigns."""
    out = {}
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("assign "):
            lhs, rhs = line[len("assign "):].rstrip(";").split(" = ", 1)
            env = {"a": a, "b": b, "c": c, **out}
            expr = rhs.replace("~", "1^")  # 1-bit operands: ~x == 1^x
            out[lhs] = eval(expr, {}, env) & 1
    return out


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "g.v")
        with open(path, "w") as f:
            f.write(SRC)
        src = elaborate_design([path], fold_constants=False).source
        assert "nand g1" not in src and "assign o_nand = ~((a) & (b));" in src, src
        for a, b, c in itertools.product((0, 1), repeat=3):
            got = _eval(src, a, b, c)
            for out, f in EXPECT.items():
                assert got[out] == f(a, b, c), (out, a, b, c, got[out])

        tri = os.path.join(tmp, "t.v")
        with open(tri, "w") as f:
            f.write("module t(input a, input e, output y); bufif1 g(y, a, e); endmodule\n")
        try:
            elaborate_design([tri])
        except EmitError as e:
            assert "bufif1" in str(e), e
        else:
            raise AssertionError("bufif1 should be refused")
    print("test_primitives: OK (and/nand/or/nor/xor/xnor/buf/not truth tables; bufif refused)")


if __name__ == "__main__":
    run()
