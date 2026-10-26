"""Non-ANSI port lists re-emit faithfully.

- `output [55:0] y;` beside `wire [55:0] y = expr;` is one net with a driver.
  Merged into the ANSI header the driver was dropped, the output left undriven,
  and the dead-code prune then removed everything behind it (opencores
  double_fpu, openmsp430, irda). The driver is now an assign.
- A trailing comma (`module c(a, y, );`, opencores pipelined_fft_256) is an
  empty port. It came out as `input void _`, which does not parse. It is dropped
  from the header and from every connection.
"""

import os
import tempfile

from papercuts.elaborator import elaborate

DESIGN = """
module c(a, y, );
    input a;
    output [3:0] y;
    wire [3:0] y = {4{a}} ^ 4'b0101;
endmodule
module t(input a, output [3:0] y);
    c u(a, y, );
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.v")
        with open(path, "w") as f:
            f.write(DESIGN)
        out = elaborate([path])
        again = os.path.join(tmp, "again.sv")
        with open(again, "w") as f:
            f.write(out)
        assert elaborate([again]) == out, out
    assert "void" not in out and "._(" not in out, out
    assert "assign y = ({4{a}} ^ 4'b101);" in out, out
    return "net-declaration driver of a port kept; empty port dropped"
