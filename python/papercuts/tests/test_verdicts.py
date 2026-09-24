"""Normalization of the prover's proof-status return value.

The pass/fail of a run comes from the tool's exit code, but the recorded
*verdict* is what tells you afterwards WHY a cut was rejected -- a real
refutation, a proof that ran out of budget, or a broken environment. Getting
that wrong is quietly expensive: it sends you debugging the setup when the
checker was working fine and simply said no.

The trap is that a proof returns "proven" only when every property shares that
status. A run that completed with a mix -- i.e. at least one refutation -- comes
back as "determined". Matching only "cex" therefore filed every ordinary refuted
cut under "error".
"""

from papercuts.ec import _VERDICT_MARKER, _parse_verdict


def _out(res):
    return f"some tool chatter\n{_VERDICT_MARKER}{res}\nmore chatter\n"


CASES = {
    # Fully decided, all properties agree.
    "proven": "proven",
    "marked_proven": "proven",
    # Fully decided, mixed statuses => something was refuted. "determined" is
    # the regression this test exists for.
    "determined": "cex",
    "ar_determined": "cex",
    "cex": "cex",
    "ar_cex": "cex",
    # Early exit from `check_sec -prove -cex_limit N`. Not every property is
    # decided, but hitting the threshold means an output assertion was refuted.
    "cex_threshold_reached": "cex",
    # Ran without deciding everything: unusable, but not a refutation and not a
    # broken environment.
    "undetermined": "inconclusive",
    "determined_or_skipped": "inconclusive",
    "unprocessed": "inconclusive",
    "time_limit": "inconclusive",
    "per_property_time_limit": "inconclusive",
    "max_trace_length": "inconclusive",
    # Setup problems and aborts.
    "overconstrained": "error",
    "out_of_memory": "error",
    "aborted": "error",
    "failed": "error",
    "no_properties": "error",
    "stopped_by_user": "error",
    "something_new_from_a_future_release": "error",
}


def run():
    for raw, expected in CASES.items():
        # Exit code 1 throughout: a non-proven result is what the caller sees,
        # so the verdict must not be inferred from the exit code when the marker
        # is present.
        got = _parse_verdict(_out(raw), 1)
        assert got == expected, f"{raw!r}: expected {expected!r}, got {got!r}"

    # Case-insensitive, and the *last* marker wins (a retry prints twice).
    assert _parse_verdict(_out("DETERMINED"), 1) == "cex"
    assert _parse_verdict(_out("cex") + _out("proven"), 0) == "proven"

    # No marker: the tool died before printing, so fall back to the exit code
    # rather than losing the run.
    assert _parse_verdict("no marker here\n", 0) == "proven"
    assert _parse_verdict("no marker here\n", 1) == "error"

    print(f"test_verdicts: OK ({len(CASES)} statuses + marker fallbacks)")


if __name__ == "__main__":
    run()
