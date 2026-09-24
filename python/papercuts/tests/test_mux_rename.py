"""Renaming the muxed copy so it can sit beside the original in one miter.

`-m` writes every muxed module under `<name><--mux-suffix>` (default
`_muxed`). Two definitions of one name cannot be elaborated together, so
without this an external equivalence checker cannot build a miter of the
original against the muxed copy at all.

What has to hold, and what this checks:

  * the module header is renamed, and only the header -- ports, selects and
    body text are untouched;
  * an instantiation of a module that was ALSO muxed follows the rename, so
    the muxed hierarchy stays internally consistent;
  * an instantiation of a module that was NOT muxed (excluded, or not a cut
    target) keeps its original name: it holds no cuts, so both hierarchies
    share that one definition;
  * instance names, `#(...)` overrides and port connections survive, including
    a multi-instance declaration (`child u1 (...), u2 (...);`), which the
    per-instance `rename_submodules` would have split apart;
  * the rename composes with `wire_mux_hierarchy`, which must run first
    because its connection map is keyed by the original child names.
"""

import re

from papercuts import insert_muxes, rename_instance_types, rename_module
from papercuts.pypercuts import wire_mux_hierarchy
from papercuts.utils import print_tree
from pyslang.syntax import SyntaxTree

PARENT = """
module parent (input logic a, input logic b, output logic y);
    logic m0, m1, m2;
    child #(.W(2)) u0 (.a(a), .y(m0));
    child u1 (.a(m0), .y(m1)), u2 (.a(m1), .y(m2));
    leaf u3 (.a(b), .y(y));
    assign y = (a ? m2 : b) & y;
endmodule
"""

CHILD = """
module child #(parameter W = 1) (input logic a, output logic y);
    assign y = a ? 1'b1 : 1'b0;
endmodule
"""


def _muxed(src):
    return insert_muxes(SyntaxTree.fromText(src), True, True, True,
                        caseMux=True, binopMux=True, constForceMux=True)


def run():
    parent = _muxed(PARENT)
    child = _muxed(CHILD)

    # `leaf` is not in the rename map: it was never muxed, so both the original
    # and the muxed hierarchy instantiate the same definition.
    renames = {"parent": "parent_muxed", "child": "child_muxed"}

    out = print_tree(rename_instance_types(rename_module(parent, "parent_muxed"),
                                           {"child": "child_muxed"}))

    assert re.search(r"\bmodule\s+parent_muxed\b", out), "module header not renamed"
    assert not re.search(r"\bmodule\s+parent\b", out), f"old module name still present:\n{out}"
    assert out.count("child_muxed") == 2, f"expected both child instantiations renamed:\n{out}"
    assert re.search(r"\bchild_muxed\s*#\(\s*\.W\(2\)\s*\)\s*u0\b", out), \
        f"parameter override or instance name lost:\n{out}"
    assert re.search(r"\bchild_muxed\s+u1\b.*\bu2\b", out, re.S), \
        f"multi-instance declaration not preserved:\n{out}"
    assert re.search(r"\bleaf\s+u3\b", out), f"un-muxed module must keep its name:\n{out}"
    # Nothing but the two names may change: undoing the rename textually has to
    # give back the muxed source byte for byte (connections included -- mux
    # insertion rewires them to `*_papercuts` companions, and that must survive).
    undone = out.replace("parent_muxed", "parent").replace("child_muxed", "child")
    assert undone == print_tree(parent), "the rename changed more than the module names"

    # The child's own rename is independent of the parent's.
    child_out = print_tree(rename_module(child, renames["child"]))
    assert re.search(r"\bmodule\s+child_muxed\b", child_out)

    # Selects survive the rename: the ports and every pc_sel reference are the
    # same before and after, so cut index N still means pc_sel<N>.
    before = sorted(set(re.findall(r"\bpc_sel\d+\b", print_tree(parent))))
    after = sorted(set(re.findall(r"\bpc_sel\d+\b", out)))
    assert before == after, f"selects changed: {before} -> {after}"
    assert before, "fixture produced no selects"

    # Order matters: wire_mux_hierarchy is keyed by the ORIGINAL child name, so
    # wiring first and renaming second is what the pipeline must do.
    wired = wire_mux_hierarchy(parent, ["pc_sel_child_0"], {"child": [("pc_sel0", "pc_sel_child_0")]})
    wired_out = print_tree(rename_instance_types(rename_module(wired, "parent_muxed"),
                                                 {"child": "child_muxed"}))
    assert "pc_sel_child_0" in wired_out, f"forwarded select lost:\n{wired_out}"
    assert wired_out.count("child_muxed") == 2, f"wiring then renaming disagreed:\n{wired_out}"

    # Renaming nothing is a no-op (what --mux-suffix '' asks for).
    assert print_tree(rename_instance_types(parent, {})) == print_tree(parent)

    print("test_mux_rename: OK")


if __name__ == "__main__":
    run()
