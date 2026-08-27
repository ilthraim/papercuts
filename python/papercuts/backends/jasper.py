from __future__ import annotations

from papercuts.backends.base import ECBackend
from papercuts.ec import TCL_SCRIPT_NAME, generate_jasper_tcl_script, run_jasper
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

    async def check(self, run: Run) -> bool:
        await run_jasper(run)
        return run.valid
