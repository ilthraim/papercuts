"""A proof that left a black-box input unmatched is not accepted.

Jasper auto-black-boxes large arrays. Their inputs are what the check compares,
so a cut that deletes a memory's only write leaves an input with no partner and
the proof still comes back "proven" (mips_16 data_mem, fv_dataset ISSUES.md 16).
"""

import os
import tempfile

from papercuts.ec import _sec_verdict

PASS = "Interface Check Summary\nauto_bbox_input     - PASSED\nreset_x             - PASSED\n"
BBOX = "Interface Check Summary\nauto_bbox_input     - FAILED - found 3 unmapped signal(s) in imp.\n"
USER = "Interface Check Summary\nuser_bbox_input     - FAILED - found 1 unmapped signal(s) in spec.\n"
RESET = "Interface Check Summary\nauto_bbox_input     - PASSED\nreset_x             - FAILED - found 1 unmapped signal(s) in imp.\n"
OUTPUT = "Interface Check Summary\nauto_bbox_output    - FAILED - found 1 unmapped signal(s) in imp.\n"


def verdict(log, res="proven", rc=0):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jg.log")
        if log is not None:
            with open(path, "w") as f:
                f.write(log)
        return _sec_verdict(f"__PC_VERDICT__:{res}\n", rc, path)


def run():
    assert verdict(PASS) == "proven"
    assert verdict(BBOX) == "unmapped-bbox-input"
    assert verdict(USER) == "unmapped-bbox-input"
    assert verdict(RESET) == "proven"
    assert verdict(OUTPUT) == "proven"
    # The script repeats the interface check after pairing reset_x: the last one counts.
    assert verdict(BBOX + PASS) == "proven"
    assert verdict(PASS + BBOX) == "unmapped-bbox-input"
    # A refutation stays a refutation, and needs no log.
    assert verdict(None, "cex", 1) == "cex"
    assert verdict(BBOX, "cex", 1) == "cex"
    for log in ("no summary here\n", None):
        try:
            verdict(log)
        except (RuntimeError, FileNotFoundError):
            pass
        else:
            raise AssertionError("a proof without an interface summary must not pass")
    return "unmatched black-box input refused; other unmapped signals and refutations unchanged"


if __name__ == "__main__":
    print(run())
