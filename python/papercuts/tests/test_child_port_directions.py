"""Each module's cutter and muxer get only its children's port directions.

The binding copies the PortDirections map into C++ on every call, so handing
every module the whole design's map cost time in proportion to the design, per
module: opencores pairing (17718 concretized modules) spent ~40 ms a call, twice
per module, before any cut was checked (ISSUES 111). Only an instantiated
module's entry is ever looked up (by the instantiation's type name), so the
child-only map must give the same cuts and the same muxed source as the full one.
"""

from papercuts import Papercutter
from papercuts.__main__ import child_port_directions
from papercuts.pypercuts import insert_muxes_report
from papercuts.utils import print_tree
from pyslang.syntax import SyntaxTree

from test_port_writes import PARENT, _dirs


def run():
    dirs = _dirs()
    full = dict(dirs)
    # unrelated modules, one of them with the name of a signal in PARENT
    full.update({f"other{i}": [("x", "out")] for i in range(2000)})
    full["w"] = [("q", "in")]
    tree = SyntaxTree.fromText(PARENT)
    sub = child_port_directions(tree, full)
    assert sub == {"child": dirs["child"]}, sub

    a, ia = insert_muxes_report(SyntaxTree.fromText(PARENT), True, True, True, caseMux=True, binopMux=True,
                                constForceMux=True, portDirections=full)
    b, ib = insert_muxes_report(SyntaxTree.fromText(PARENT), True, True, True, caseMux=True, binopMux=True,
                                constForceMux=True, portDirections=sub)
    assert print_tree(a) == print_tree(b) and list(ia) == list(ib)

    pa = Papercutter(SyntaxTree.fromText(PARENT), port_directions=full)
    pb = Papercutter(SyntaxTree.fromText(PARENT), port_directions=sub)
    assert list(pa.cut_info()) == list(pb.cut_info())
    for i in range(len(list(pa.cut_info()))):
        assert pa.cut_index_text([i]) == pb.cut_index_text([i]), i

    # a module that instantiates nothing gets an empty map
    leaf = SyntaxTree.fromText("module leaf(input logic a, output logic y); assign y = a; endmodule")
    assert child_port_directions(leaf, full) == {}
    print("  test_child_port_directions: OK (child-only map: same cuts and muxed source as the full map)")
