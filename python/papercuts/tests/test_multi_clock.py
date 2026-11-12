"""Every clock of a multi-clock design is declared to the formal tool.

With one `--clock`, a second clock was an ordinary input to Jasper: it changes at
most once per declared cycle, so logic that needs it faster was never exercised
and wrong cuts proved (rs_decoder's clock2, ssp_uart's SSP_SCK). `--clock` now
repeats, the backend declares them all in one `clock c1 c2` (same rate, in
phase), and the manifest records the list (version 2) so a consumer declares
the same clocks.
"""

import argparse
import os
import tempfile

from papercuts.backends.jasper import JasperBackend
from papercuts.ec import TCL_POWER_UP_SCRIPT_NAME, TCL_SCRIPT_NAME, generate_jasper_tcl_script
from papercuts.manifest import Manifest, ManifestError
from papercuts.utils import clock_list


def _clock_lines(tcl):
    return [l.strip() for l in tcl.splitlines() if l.strip().startswith("clock ")]


def _raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return
    raise AssertionError(f"{fn.__name__}{a}{k} did not raise {exc.__name__}")


def _manifest(version, clock):
    data = {"version": version, "top": "t", "mux_suffix": "_m", "files": {"golden": [], "muxed": []},
            "modules": {"t": {"muxed_name": "t_m", "golden_file": None, "muxed_file": "t_m.sv",
                              "cuts": [], "exclusive": [], "forwarded": []}}}
    if clock is not ...:
        data["clock"] = clock
    return data


def run():
    # --- CLI: --clock repeats, in order; absent means clock-free -----------------
    p = argparse.ArgumentParser()
    JasperBackend.add_cli_args(p)
    a = p.parse_args(["--clock", "clock1", "--clock", "clock2", "--reset", "!reset"])
    b = JasperBackend.from_args(a)
    assert b.clock == ["clock1", "clock2"], b.clock
    assert JasperBackend.from_args(p.parse_args([])).clock == []

    # --- both generated scripts declare every clock in one command ---------------
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        os.chdir(tmp)
        b.prepare()
        for name in (TCL_SCRIPT_NAME, TCL_POWER_UP_SCRIPT_NAME):
            assert _clock_lines(open(name).read()) == ["clock clock1 clock2"], name
    finally:
        os.chdir(cwd)
    assert _clock_lines(generate_jasper_tcl_script(clock=["clk"])) == ["clock clk"]
    assert _clock_lines(generate_jasper_tcl_script()) == ["clock -none"]
    assert _clock_lines(generate_jasper_tcl_script(clock=[])) == ["clock -none"]

    # --- a single string is one clock, never split into characters ---------------
    # (runs/ scripts and saved repair-job configs pass one)
    assert clock_list("clk") == ["clk"]
    assert JasperBackend(clock="clk").clock == ["clk"]
    assert _clock_lines(generate_jasper_tcl_script(clock="clk")) == ["clock clk"]
    _raises(ValueError, clock_list, "clk1 clk2")
    _raises(TypeError, clock_list, {"clk"})
    _raises(ValueError, clock_list, ["clk", "clk"])
    _raises(ValueError, clock_list, ["clk a"])
    _raises(ValueError, clock_list, [""])

    # --- manifest: version 2 holds the list; version 1's single name still reads --
    tmp_path = os.path.join(tmp, "m.json")
    for version, clock, want in ((2, ["c1", "c2"], ["c1", "c2"]), (2, [], []), (1, "clk", ["clk"]),
                                 (1, None, []), (1, ..., [])):
        assert Manifest(_manifest(version, clock), tmp_path).clock == want, (version, clock)
    _raises(ManifestError, Manifest, _manifest(2, "clk"), tmp_path)        # v2 string: not a list
    _raises(ManifestError, Manifest, _manifest(1, ["clk"]), tmp_path)      # v1 list: never written
    _raises(ManifestError, Manifest, _manifest(3, []), tmp_path)

    print("  test_multi_clock: OK (--clock repeats; `clock c1 c2` in both scripts; "
          "one string is one clock; manifest v2 list, v1 name read as one clock)")


if __name__ == "__main__":
    run()
