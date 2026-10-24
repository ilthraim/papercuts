"""The dead-code prune emits from an unfrozen compilation.

analyze_dead freezes its compilation, and the prune used to re-emit from that
same one; constant folding then hit slang's `!isFrozen()` assertion when a fold
needed a new constant (opencores cavlc, consolidating the selected set). The
fixture is that module's cut source.
"""

import os

from papercuts.deadcode import prune_dead_source

HERE = os.path.dirname(os.path.abspath(__file__))


def run():
    with open(os.path.join(HERE, "prune_fixtures", "cavlc_read_levels_cut.sv")) as f:
        src = f.read()
    out, stats = prune_dead_source(src, "cavlc_top_cavlc_read_levels")
    assert out.strip(), out
    return f"pruned without the frozen-fold assertion ({stats})"
