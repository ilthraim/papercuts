"""Every tree the bindings return owns its memory.

A rewritten tree shares nodes and token data with the tree it was made from,
and nothing ties their lifetimes, so main's `tree_ = rename_module(tree_, ...)`
chain (each step drops the last tree) left the result dangling: a 106-bit
literal printed as other text of the file (hls seidel_2d), invalid UTF-8
(opencores p16c5x). It shows only now and then, so the chain runs in a
subprocess under MALLOC_PERTURB_, which fills freed memory with garbage: a
dangling read then fails every time.
"""

import os
import subprocess
import sys

CHILD = r'''
import gc
from pyslang.syntax import SyntaxTree
from papercuts import insert_muxes, rename_instance_types, rename_module
from papercuts.pypercuts import wire_mux_hierarchy
from papercuts.utils import print_tree

PARENT = """
module parent(input logic clk, input logic [3:0] a, output logic [105:0] y, output logic [3:0] q);
    assign y = 106'h38e38e38e38e39;
    child u (.clk(clk), .a(a ? a : 4'h0), .q(q));
endmodule
"""
tree = insert_muxes(SyntaxTree.fromText(PARENT), ternaryMux=True)
tree = wire_mux_hierarchy(tree, ["pc_sel_child_0"], {"child": [("pc_sel0", "pc_sel_child_0")]})
tree = rename_module(tree, "parent_muxed")
tree = rename_instance_types(tree, {"child": "child_muxed"})
gc.collect()
junk = [SyntaxTree.fromText("module j%d; wire ap_block_pp0_stage%d; endmodule" % (k, k)) for k in range(300)]
text = print_tree(tree)
assert "106'h38e38e38e38e39" in text, text
assert "module parent_muxed" in text and "child_muxed" in text, text
print("OK")
'''


def run():
    env = dict(os.environ, MALLOC_PERTURB_="165")
    p = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, errors="replace", env=env,
                       timeout=300)
    assert p.returncode == 0 and "OK" in p.stdout, (p.returncode, p.stdout[-800:], p.stderr[-1500:])
    return "rewrite chain owns its memory under MALLOC_PERTURB_"
