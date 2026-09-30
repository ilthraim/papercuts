"""A signal connected to an instance's output port is WRITTEN there.

The write-position test used to be purely syntactic (LHS of an assignment), so
`.y(w)` on a child's output looked like a read of `w`. A const-force cut then
wrote `.y(1'b0)`, and the muxer wrote `.y(w_papercuts)`: the child drove the
companion wire, which already had an `assign`, and nothing drove `w` any more
(yosys: "multiple conflicting drivers", "used but has no driver"). Seen on
scalable_arbiter__demo_filters, where it made every checker resolve the netlist
differently. The direction is the child's, so it comes from the compilation
(chipper.definition_port_directions) and goes to the cutter and the muxer alike.
"""

import os
import re
import shutil
import subprocess
import tempfile

from papercuts import Papercutter, chipper
from papercuts.elaborator import build_compilation_from
from papercuts.pypercuts import insert_muxes_report
from papercuts.utils import print_tree
from pyslang.syntax import SyntaxTree

CHILD = """
module child(input logic a, output logic y, inout wire z);
    assign y = ~a;
endmodule
"""

# `w` and `v` are driven by child outputs (named and positional); `u` feeds a
# child input; `z` sits on an inout. Each is a 1-bit scalar, so a const-force target.
PARENT = """
module p(input logic a, output logic o);
    logic w;
    logic v;
    logic u;
    wire  z;
    assign u = a;
    child c1(.a(u), .y(w), .z(z));
    child c2(a, v, z);
    assign o = w ^ v ^ u ^ z;
endmodule
"""


def _dirs():
    comp = build_compilation_from([SyntaxTree.fromText(CHILD), SyntaxTree.fromText(PARENT)])
    return chipper.definition_port_directions(comp)


def _force0(pc, name):
    """The force-const(0) cut on the declaration of `name` in PARENT."""
    line = next(i for i, ln in enumerate(PARENT.splitlines(), 1) if re.search(rf"\b{name};", ln))
    return next(i for i, (t, ln) in enumerate(pc.cut_info()) if t == "force-const(0)" and ln == line)


def _one_line(text):
    return " ".join(text.split())


def _yosys_driver_warnings(sources: dict[str, str], top: str) -> list[str]:
    tmp = tempfile.mkdtemp()
    files = []
    for name, text in sources.items():
        files.append(os.path.join(tmp, name))
        with open(files[-1], "w") as f:
            f.write(text)
    log = os.path.join(tmp, "y.log")
    subprocess.run(["yosys", "-q", "-l", log, "-p",
                    f"plugin -i slang; read_slang {' '.join(files)} --top {top}; prep -top {top}"],
                   capture_output=True, text=True)
    with open(log) as f:
        return [ln.strip() for ln in f if "conflicting drivers" in ln or "has no driver" in ln]


def run():
    dirs = _dirs()
    assert dirs["child"] == [("a", "in"), ("y", "out"), ("z", "inout")], dirs

    # --- the cutter: a const-force cut substitutes reads only -------------------
    pc = Papercutter(SyntaxTree.fromText(PARENT), port_directions=dirs)
    for name in ("w", "v", "z"):
        cut = _one_line(pc.cut_index_text([_force0(pc, name)]))
        assert "1'b0" in cut, (name, cut)
        # the read in `assign o` is forced; the connection the child drives is kept
        assert re.search(rf"assign o = .*1'b0", cut), (name, cut)
        conn = {"w": ".y(w)", "v": "c2(a, v, z)", "z": ".z(z)"}[name]
        assert conn in cut, (name, conn, cut)
    # an input connection is a read: forcing `u` does reach the child's input
    cut = _one_line(pc.cut_index_text([_force0(pc, "u")]))
    assert ".a(1'b0)" in cut, cut

    # --- the muxer: companions carry reads, writes keep the original -------------
    tree, inserted = insert_muxes_report(SyntaxTree.fromText(PARENT), constForceMux=True, portDirections=dirs)
    muxed = print_tree(tree)
    flat = _one_line(muxed)
    assert ".y(w)" in flat and ".y(w_papercuts)" not in flat, flat
    assert "c2(a, v, z)" in flat, flat
    assert ".z(z)" in flat, flat
    assert ".a(u_papercuts)" in flat, flat  # a read: redirected as before
    assert re.search(r"assign o = w_papercuts \^ v_papercuts \^ u_papercuts \^ z_papercuts", flat), flat

    # --- no table: every connection is left alone (never a wrong redirect) -------
    tree, _ = insert_muxes_report(SyntaxTree.fromText(PARENT), constForceMux=True)
    flat = _one_line(print_tree(tree))
    assert ".y(w)" in flat and ".a(u)" in flat, flat

    # --- and the netlist is clean: the defect yosys reported is gone --------------
    if shutil.which("yosys"):
        muxed_named = muxed.replace("module p(", "module p_muxed(")
        warnings = _yosys_driver_warnings({"child.sv": CHILD, "p.sv": muxed_named}, "p_muxed")
        assert warnings == [], warnings

    print("  test_port_writes: OK (output/inout connections kept by cut and mux, input still cut)")
