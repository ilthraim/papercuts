from __future__ import annotations

from papercuts.backends.base import ECBackend
from papercuts.ec import TCL_POWER_UP_SCRIPT_NAME, TCL_SCRIPT_NAME, generate_jasper_tcl_script, run_jasper
from papercuts.utils import Run


class JasperBackend(ECBackend):
    """Default backend: Cadence JasperGold SEC, run directly via ``ec.run_jasper``.

    Requires ``jg``/``csh`` on PATH. Writes its own SEC script (``pcjg.tcl``) in
    :meth:`prepare`, baking in the ``--clock``/``--reset`` declarations; ``jg``
    is invoked from the same working directory the script is written to.
    """

    name = "jg"

    def prepare(self) -> None:
        with open(TCL_SCRIPT_NAME, "w") as f:
            f.write(generate_jasper_tcl_script(clock=self.clock, reset=self.reset))
        # The power-up check: same clock, `reset -none`; every flop is then a
        # reset-X value that check_sec -auto_map_reset_x_values pairs by name.
        with open(TCL_POWER_UP_SCRIPT_NAME, "w") as f:
            f.write(generate_jasper_tcl_script(clock=self.clock, reset=None))

    async def check(self, run: Run) -> bool:
        await run_jasper(run)
        return run.valid

    async def check_from_power_up(self, run: Run) -> bool:
        await run_jasper(run, script=TCL_POWER_UP_SCRIPT_NAME)
        return run.valid
