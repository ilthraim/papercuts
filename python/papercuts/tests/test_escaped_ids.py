"""Escaped identifiers through elaboration, cutting and muxing (EPFL netlists).

`\\opcode[0] ` is one scalar port named `opcode[0]`. Emitted bare it parses as an
array select (Jasper: "ANSI port 'opcode' cannot be redeclared", epfl__ctrl).
Emitted escaped it breaks the cutter and muxer, which rebuild expressions from
tokens: an escaped name ends at whitespace that belongs to the next token, so a
rebuilt `(\\a[0])` swallows the parenthesis, and the muxer crashed. So every
escaped name becomes a plain one, except the top's ports, which keep their names
(checks match ports by name) behind plain aliases. Checked here: the elaborated
design has the original top port names; nothing but the top's port list and
its aliases is escaped; and every cut and the muxed design still compile.
"""

import os
import tempfile

from papercuts import Papercutter
from papercuts.elaborator import build_compilation_from, elaborate_design, report_diagnostics
from papercuts.pypercuts import insert_muxes_report
from papercuts.utils import print_tree
from pyslang.syntax import SyntaxTree

SRC = r"""
module leaf(input \in.x , output \reg );
    assign \reg = ~\in.x ;
endmodule
module top(\a[0] , \a[1] , \y$out , z);
    input \a[0] , \a[1] ;
    output \y$out , z;
    wire \n[3] ;
    assign \n[3] = \a[0] & \a[1] ;
    assign \y$out = \n[3] ;
    leaf \u[0] (.\in.x (\n[3] ), .\reg (z));
endmodule
"""


def ports(text):
    comp = build_compilation_from([SyntaxTree.fromText(text)])
    assert not report_diagnostics(comp), text
    top = comp.getRoot().topInstances[0]
    return sorted(p.name for p in top.body.portList)


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "e.v")
        with open(path, "w") as f:
            f.write(SRC)
        out = elaborate_design([path]).source
        assert ports(out) == ports(SRC) == sorted(["a[0]", "a[1]", "y$out", "z"]), (ports(out), out)
        top = out[out.index("module top"):]
        body = top[top.index(");") + 2:]
        escaped = [ln.strip() for ln in body.splitlines() if "\\" in ln]
        assert escaped == ["wire logic a_0_ = \\a[0] ;", "wire logic a_1_ = \\a[1] ;"], escaped
        leaf = out[out.index("module top_u_0"):out.index("module top ")] if "module top_u_0" in out else out
        assert "\\" not in leaf, leaf  # the child is all plain
        for text in [out] + [Papercutter(SyntaxTree.fromText(out)).cut_index_text([i])
                             for i in range(len(list(Papercutter(SyntaxTree.fromText(out)).cut_info())))]:
            ports(text)
        muxed, _ = insert_muxes_report(SyntaxTree.fromText(out), True, True, True, caseMux=True,
                                       binopMux=True, constForceMux=True)
        ports(print_tree(muxed))
    print("test_escaped_ids: OK (top ports keep their names behind aliases; cuts and muxes compile)")


if __name__ == "__main__":
    run()
