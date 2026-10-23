"""Files with and without a `timescale elaborate together (opencores brisc, ptc,
datetime, asram_if). slang refuses the mix unless a default time scale is set;
timing is irrelevant to the formal checks, so the elaborator sets one."""

import os
import tempfile

from papercuts.elaborator import elaborate_design


def run():
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.v"), os.path.join(tmp, "b.v")
        with open(a, "w") as f:
            f.write("`timescale 1ns/10ps\nmodule leaf(input x, output y); assign y = ~x; endmodule\n")
        with open(b, "w") as f:
            f.write("module top(input x, output y); leaf u(.x(x), .y(y)); endmodule\n")
        e = elaborate_design([a, b])
        assert e.tops == ["top"], e.tops
    print("test_timescale: OK (mixed timescale elaborates)")


if __name__ == "__main__":
    run()
