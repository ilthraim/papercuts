"""Subroutine lifetimes in the elaborated source.

Emitted modules carry no lifetime, so a function or task in them is static by
default: the elaborator writes `automatic` when it is needed and nothing
otherwise. An explicit `function static` is legal SV but Fusion Compiler's reader
rejects it (VER-721), which failed every elaborated/consolidated source that had a
function in it.
"""

import os
import re
import tempfile

from papercuts.elaborator import elaborate_design

SRC = """
module top(input logic [2:0] a, output logic [6:0] y, output logic [3:0] z);
    function logic [6:0] onehot(input logic [2:0] n);
        onehot = 7'b1 << n;
    endfunction
    function static logic [3:0] inc_s(input logic [3:0] v);
        inc_s = v + 4'd1;
    endfunction
    function automatic logic [3:0] inc_a(input logic [3:0] v);
        inc_a = v + 4'd1;
    endfunction
    assign y = onehot(a);
    assign z = inc_s({1'b0, a}) ^ inc_a({a, 1'b0});
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "funcs.sv")
        with open(path, "w") as f:
            f.write(SRC)
        out = elaborate_design([path], top="top").source
    heads = re.findall(r"^\s*function\b[^;]*;", out, re.M)
    assert len(heads) == 3, out
    assert not any(re.search(r"\bstatic\b", h) for h in heads), heads
    assert sum(bool(re.search(r"\bfunction automatic\b", h)) for h in heads) == 1, heads
    print("test_subroutine_lifetime: OK (static implicit, automatic explicit)")


if __name__ == "__main__":
    run()
