"""--top in the elaborated front end.

Sources often carry a helper module nothing instantiates (an unused RAM model, a
debug block), which makes it a second top. Without a choice the pipeline has to
stop; with --top only the chosen hierarchy is elaborated, so the helper is
neither emitted nor cut.
"""

import os
import tempfile

from papercuts.elaborator import elaborate_design

SRC = """
module leaf(input logic a, output logic y);
    assign y = ~a;
endmodule
module real_top(input logic a, output logic y);
    leaf u(.a(a), .y(y));
endmodule
module unused_helper(input logic [3:0] d, output logic [3:0] q);
    assign q = d;
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "two_tops.sv")
        with open(path, "w") as f:
            f.write(SRC)
        both = elaborate_design([path])
        assert sorted(both.tops) == ["real_top", "unused_helper"], both.tops
        one = elaborate_design([path], top="real_top")
        assert one.tops == ["real_top"] and one.top == "real_top", one.tops
        assert "unused_helper" not in one.source and "real_top" in one.source, one.source
    print("test_elaborate_top: OK (two tops, --top picks one)")


if __name__ == "__main__":
    run()
