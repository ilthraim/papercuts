"""Cut selection by CEGIS with a combinational spec: the alternative to checking
each cut with Jasper and consolidating the winners.

    btor2z3.py    BTOR2 -> z3py
    solvers.py    solver strategies (external yices / bitwuzla, z3 tactics), unsat cores
    xpair.py      pairs the two copies' X sources as Jasper does (pyosys pass)
    netlist.py    yosys side: elaboration, driver checks, miter, BTOR2
    registers.py  register pairing across the copies, power-up
    comb.py       CombCegis: synthesizer, verifier, conflict core, base case

Copied from the smt_ec research tree (rewriting repo) on 2026-10-23 and
maintained separately from it from then on.
"""
