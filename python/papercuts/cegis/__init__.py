"""Cut selection by CEGIS with a combinational spec: the alternative to checking
each cut with Jasper and consolidating the winners.

    btor2z3.py    BTOR2 -> z3py
    solvers.py    solver strategies (external yices / bitwuzla, z3 tactics), unsat cores
    xpair.py      pairs the two copies' X sources as Jasper does (pyosys pass)
    netlist.py    yosys side: elaboration, driver checks, miter, BTOR2
    registers.py  register pairing across the copies, power-up
    comb.py       CombCegis: synthesizer, verifier, conflict core, base case
    core_min.py   opt-in minimal conflict cores (CaDiCaL deletion)
    steer.py      which tied candidate the synthesizer proposes first
    trace.py      one JSON line per verifier call
    features.py   structural features of the selects, from the miter's BTOR2
    baseline.py   the exact-k binary search MaxSAT replaced
    exhaustive.py brute-force ground truth for small circuits

Copied from the smt_ec research tree (rewriting repo) on 2026-10-23 and
maintained separately from it from then on.
"""
