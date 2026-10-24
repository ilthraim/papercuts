"""cut_index_text neither changes its answer nor keeps a copy per cut.

With --shrink-with-intermediate it re-parses each cut's text once; that
re-parse went into the design tree's SourceManager, which lives as long as the
design, so every call kept one more copy of the module (a 74k-cut design's
no-op scan passed 64 GB). The test runs it in a subprocess and checks both the
text (equal to print_tree(cut_index(...))) and the memory growth.
"""

import subprocess
import sys

CHILD = r'''
import resource
from pyslang.syntax import SyntaxTree
from papercuts.pypercuts import Papercutter
from papercuts.utils import print_tree
N = 2000
body = "\n".join(f"    assign y{i} = s ? a{i} : (b{i} & 8'hF0);" for i in range(N))
ports = ", ".join(f"input logic [7:0] a{i}, input logic [7:0] b{i}, output logic [7:0] y{i}" for i in range(N))
src = f"module m(input logic s, {ports});\n{body}\nendmodule\n"
pc = Papercutter(SyntaxTree.fromText(src), True)
for i in (0, 7, 1001):
    assert pc.cut_index_text([i]) == print_tree(pc.cut_index([i])), i
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
for i in range(400):
    pc.cut_index_text([i])
grown_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
print("GROWN_MB", round(grown_mb, 1), "SRC_KB", len(src) // 1024)
'''


def run():
    p = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=600)
    line = next((l for l in p.stdout.splitlines() if l.startswith("GROWN_MB")), None)
    assert line is not None, (p.stdout[-500:], p.stderr[-1500:])
    grown, src_kb = float(line.split()[1]), int(line.split()[3])
    # one copy per call would be 400 x the source (about 70 MB here)
    assert grown < 400 * src_kb / 1024 / 4, line
    return line
