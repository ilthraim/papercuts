"""The Jasper script states its proof-engine load explicitly.

A check runs its whole engine portfolio in parallel (~28 engines, up to ~50
processes), so the machine load of a batch was sessions x that: 16 sessions
drove the load to 165 (runs/fv_dataset/ISSUES.md 2). `--jasper-engines`
writes `set_proofgrid_max_local_jobs` (0 = uncapped) beside one job per
engine; the proof strategy refuses fewer than 4.

Per-cut checks pass prove limits as script arguments 10 and 11 ("-" = none):
`-cex_limit 1` from Run.stop_at_first_cex (one refuted output settles the cut;
Jasper returns cex_threshold_reached, a cex to papercuts) and `-time_limit` from
Run.time_limit_s, i.e. --fv-time-limit (time_limit, inconclusive). Gates pass
neither; test_jasper_args pins the mapping.
"""

from papercuts.ec import generate_jasper_tcl_script


def lines(tcl):
    return [l.strip() for l in tcl.splitlines() if "proofgrid" in l]


def run():
    assert lines(generate_jasper_tcl_script(clock=["clk"])) == [
        "set_proofgrid_per_engine_max_local_jobs 1", "set_proofgrid_max_local_jobs 0"]
    tcl = generate_jasper_tcl_script(clock=["clk"], engines=16)
    assert "set_proofgrid_max_local_jobs 16" in lines(tcl)
    assert tcl.index("set_proofgrid_max_local_jobs") < tcl.index("set res [check_sec -prove"), tcl
    try:
        generate_jasper_tcl_script(engines=2)
    except ValueError:
        pass
    else:
        raise AssertionError("a cap below 4 must be refused")
    assert "set res [check_sec -prove -strategy proof {*}$prove_limits]" in tcl, tcl
    assert 'lappend prove_limits -time_limit [lindex $argv 10]' in tcl, tcl
    assert 'lappend prove_limits -cex_limit [lindex $argv 11]' in tcl, tcl
    # both contexts name the top; left to itself Jasper elaborates the first root
    assert "set top [lindex $argv 12]" in tcl, tcl
    elab = [l.strip() for l in tcl.splitlines() if l.strip().startswith("elaborate ")]
    assert len(elab) == 2 and all(l.startswith("elaborate -top $top ") for l in elab), elab
    from papercuts.ec import _parse_verdict
    assert _parse_verdict("__PC_VERDICT__:cex_threshold_reached", 1) == "cex"
    assert _parse_verdict("__PC_VERDICT__:time_limit", 1) == "inconclusive"
    return "engine settings explicit; 0 uncapped; below 4 refused; prove limits as arguments"
