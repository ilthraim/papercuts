"""The Jasper script states its proof-engine load explicitly.

A check runs its whole engine portfolio in parallel (~28 engines, up to ~50
processes), so the machine load of a batch was sessions x that: 16 sessions
drove the load to 165 (runs/fv_dataset/ISSUES.md 2). `--jasper-engines`
writes `set_proofgrid_max_local_jobs` (0 = uncapped) beside one job per
engine; the proof strategy refuses fewer than 4.
"""

from papercuts.ec import generate_jasper_tcl_script


def lines(tcl):
    return [l.strip() for l in tcl.splitlines() if "proofgrid" in l]


def run():
    assert lines(generate_jasper_tcl_script(clock="clk")) == [
        "set_proofgrid_per_engine_max_local_jobs 1", "set_proofgrid_max_local_jobs 0"]
    tcl = generate_jasper_tcl_script(clock="clk", engines=16)
    assert "set_proofgrid_max_local_jobs 16" in lines(tcl)
    assert tcl.index("set_proofgrid_max_local_jobs") < tcl.index("check_sec -prove"), tcl
    try:
        generate_jasper_tcl_script(engines=2)
    except ValueError:
        pass
    else:
        raise AssertionError("a cap below 4 must be refused")
    return "engine settings explicit; 0 uncapped; below 4 refused"
