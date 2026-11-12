from __future__ import annotations

import argparse

from papercuts.backends.base import ECBackend
from papercuts.ec import TCL_POWER_UP_SCRIPT_NAME, TCL_SCRIPT_NAME, generate_jasper_tcl_script, run_jasper
from papercuts.utils import Run


class JasperBackend(ECBackend):
    """Default backend: Cadence JasperGold SEC, run directly via ``ec.run_jasper``.

    Requires ``jg``/``csh`` on PATH. Writes its own SEC script (``pcjg.tcl``) in
    :meth:`prepare`, baking in the ``--clock`` (every one)/``--reset`` declarations; ``jg``
    is invoked from the same working directory the script is written to.
    """

    name = "jg"

    def __init__(self, clock: "list[str] | None" = None, reset: "str | None" = None,
                 bbox_array: "int | None" = None, engines: int = 0) -> None:
        super().__init__(clock=clock, reset=reset)
        self.bbox_array = bbox_array
        self.engines = engines

    @classmethod
    def add_cli_args(cls, parser: argparse.ArgumentParser) -> None:
        super().add_cli_args(parser)
        parser.add_argument(
            "--jasper-bbox-array",
            type=int,
            default=None,
            metavar="BITS",
            help="Jasper: black-box only arrays larger than BITS (elaborate -bbox_a). "
            "Jasper's default black-boxes much smaller arrays and then compares every "
            "write into them, read or not, which can fail a check on a valid cut "
            "(hls covariance). Raising it models the arrays instead, at a large memory "
            "cost on array-heavy designs; off by default.",
        )
        parser.add_argument(
            "--jasper-engines",
            type=int,
            default=0,
            metavar="N",
            help="Jasper: proof-engine jobs one check may run at once "
            "(set_proofgrid_max_local_jobs; one job per engine). A check runs its whole "
            "engine portfolio in parallel, ~28 engines and up to ~50 processes, so the "
            "machine load is about -j times that. 0 (default) leaves the portfolio "
            "whole; the proof strategy needs at least 4, and a cap drops engines, which "
            "can make a hard check far slower (a deep cex: 81 s uncapped, >15 min at 16).",
        )

    @classmethod
    def from_args(cls, args) -> "JasperBackend":
        return cls(clock=args.clock, reset=args.reset, bbox_array=args.jasper_bbox_array,
                   engines=args.jasper_engines)

    def prepare(self) -> None:
        with open(TCL_SCRIPT_NAME, "w") as f:
            f.write(generate_jasper_tcl_script(clock=self.clock, reset=self.reset, bbox_array=self.bbox_array,
                                               engines=self.engines))
        # The power-up check: same clock, `reset -none`; every flop is then a
        # reset-X value that check_sec -auto_map_reset_x_values pairs by name.
        with open(TCL_POWER_UP_SCRIPT_NAME, "w") as f:
            f.write(generate_jasper_tcl_script(clock=self.clock, reset=None, bbox_array=self.bbox_array,
                                               engines=self.engines))

    async def _check(self, run: Run) -> str:
        return await run_jasper(run)

    async def _check_from_power_up(self, run: Run) -> str:
        return await run_jasper(run, script=TCL_POWER_UP_SCRIPT_NAME)
