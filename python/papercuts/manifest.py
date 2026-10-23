"""The mux manifest: what `-m` inserted, told to whoever consumes the muxed design.

Without it every fact a consumer needs has to be re-derived by convention --
`pc_sel<N>` means cut N, a module is `<name>_muxed`, two cuts on one line are a
pair -- and each of those has already been wrong at least once. The manifest is
written by the pipeline that does the inserting, from the same objects, so it
cannot drift from the muxed source it describes.

`build_manifest` / `check_manifest` / `write_manifest` write it; `Manifest`
reads it back as a consumer does (the CEGIS cut selection, papercuts.cegis).

Shape (version 1), paths relative to the manifest's own directory:

    {"version": 1, "mode": "in-situ"|"elaborated", "top": "...", "mux_suffix": "_muxed",
     "clock": "clk" | null, "reset": "!rst_n" | null,
     "files": {"golden": [...], "muxed": [...]},
     "modules": {
       "<module>": {
         "muxed_name": "<module>_muxed",
         "golden_file": "...", "muxed_file": "...",
         "cuts": [{"index": 0, "type": "ternary(keep-false)", "line": 12,
                   "port": "pc_sel0", "inserted": true}, ...],
         "exclusive": [[0, 1]],
         "forwarded": [{"port": "pc_sel_<child>_<i>", "module": "<child>", "index": i}]}}}

`clock` and `reset` are the `--clock` / `--reset` this run was given, verbatim
(null when absent): the signals papercuts' own formal checks declare. A consumer
that checks the muxed design itself uses these, so both sides reason about the
same clock and the same reset instead of each being told separately.

`inserted: false` marks a select that is RESERVED (the port exists, so cut
indices stay aligned) but drives nothing: a family turned off with
--only-families, or a target its muxer cannot build such as a parameterized
bit-shrink. Such a cut was never exercised by the muxed design, and a consumer
must not treat it as proven.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

MANIFEST_NAME = "mux_manifest.json"
VERSION = 1


def build_manifest(*, mode: str, top: str, suffix: str, modules, muxed_names: dict[str, str],
                   muxed_files: dict[str, str], golden_files: dict[str, str],
                   inserted: dict[str, set[int]], forwarded: dict[str, list[tuple[str, int]]],
                   base_dir: str, clock: str | None = None, reset: str | None = None) -> dict:
    """`modules` is the pipeline's ModuleCuts list; only those that were muxed appear."""
    def rel(p):
        return os.path.relpath(p, base_dir)

    out_modules = {}
    for mod in modules:
        if mod.name not in muxed_files:
            continue  # excluded or not a cut target: never muxed, never cut
        ins = inserted.get(mod.name, set())
        cuts = [{"index": i, "type": ctype, "line": line,
                 "port": f"pc_sel{i}", "inserted": i in ins}
                for i, (ctype, line) in enumerate(mod.cut_infos)]
        exclusive = [list(p) for p in (mod.pc.cut_pairs() if mod.pc is not None else [])]
        fwd = [{"port": f"pc_sel_{child}_{idx}", "module": child, "index": idx}
               for child, idx in forwarded.get(mod.name, [])]
        out_modules[mod.name] = {
            "muxed_name": muxed_names.get(mod.name, mod.name),
            "golden_file": rel(golden_files[mod.name]) if mod.name in golden_files else None,
            "muxed_file": rel(muxed_files[mod.name]),
            "cuts": cuts,
            "exclusive": exclusive,
            "forwarded": fwd,
        }
    return {
        "version": VERSION,
        "generator": "papercuts",
        "mode": mode,
        "top": top,
        "mux_suffix": suffix,
        "clock": clock,
        "reset": reset,
        "files": {
            "golden": sorted({rel(p) for p in golden_files.values()}),
            "muxed": sorted({rel(p) for p in muxed_files.values()}),
        },
        "modules": out_modules,
    }


def write_manifest(manifest: dict, output_dir: str) -> str:
    path = os.path.join(output_dir, MANIFEST_NAME)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=False)
        f.write("\n")
    return path


def _split_header(text: str) -> tuple[str, str]:
    """(port list, body) of a module's source. Ports are declared in the header,
    so a select that appears only there drives nothing."""
    i = text.find(");")
    return (text, "") if i < 0 else (text[:i], text[i:])


def check_manifest(manifest: dict, muxed_text: dict[str, str] | None = None) -> list[str]:
    """Consistency problems, as human-readable lines. Empty means sound.

    Structural checks always run: cut lists in index order, exclusive pairs and
    forwarded entries naming cuts that exist.

    Given `muxed_text` (module -> the source actually written), it also checks the
    manifest against that source: one select port per cut, every cut marked
    `inserted` referenced in the body, and every inert one referenced nowhere but
    the port list. That is the cheap half of "pc_sel<N> really is cut N" -- it
    catches a band that slipped, a mux that silently failed to build, and a count
    that drifted, without elaborating anything.
    """
    problems = []
    for name, m in manifest["modules"].items():
        n = len(m["cuts"])
        for i, cut in enumerate(m["cuts"]):
            if cut["index"] != i:
                problems.append(f"{name}: cut list is not in index order at {i}")
        for a, b in m["exclusive"]:
            if not (0 <= a < n and 0 <= b < n):
                problems.append(f"{name}: exclusive pair ({a}, {b}) outside 0..{n - 1}")
            elif a == b:
                problems.append(f"{name}: cut {a} paired with itself")
        if muxed_text is not None and name in muxed_text:
            header, body = _split_header(muxed_text[name])
            declared = {int(x) for x in re.findall(r"pc_sel(\d+)\b", header)}
            used = {int(x) for x in re.findall(r"pc_sel(\d+)\b", body)}
            if declared != set(range(n)):
                missing = sorted(set(range(n)) - declared)
                extra = sorted(declared - set(range(n)))
                problems.append(
                    f"{name}: {n} cuts but select ports {sorted(declared)[:8]}..."
                    + (f" missing {missing}" if missing else "")
                    + (f" unexpected {extra}" if extra else ""))
            for cut in m["cuts"]:
                i = cut["index"]
                if cut["inserted"] and i not in used:
                    problems.append(f"{name}: cut {i} is marked inserted but pc_sel{i} drives nothing")
                if not cut["inserted"] and i in used:
                    problems.append(f"{name}: cut {i} is marked reserved but pc_sel{i} is used")
        for f in m["forwarded"]:
            child = manifest["modules"].get(f["module"])
            if child is None:
                problems.append(f"{name}: forwards {f['port']} for unknown module {f['module']}")
            elif not (0 <= f["index"] < len(child["cuts"])):
                problems.append(f"{name}: forwarded {f['port']} is outside {f['module']}'s cuts")
    return problems


# MARK: Reading
# The consumer's half of the contract, copied from smt_ec/src/manifest.py
# (rewriting repo 12a5a7f, 2026-10-23). Everything a consumer needs is read
# from the manifest, nothing inferred from names or line numbers.

class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Cut:
    module: str
    index: int
    type: str
    line: int
    inserted: bool  # False: the select is reserved but drives nothing
    port: str | None  # top-level port that toggles it, None if not reachable at the top

    @property
    def id(self) -> str:
        return f"{self.module}#{self.index}"

    @property
    def family(self) -> str:
        return self.type.split("(", 1)[0]


class Manifest:
    """A written manifest, as its consumer sees it.

        m = Manifest.load("outputs/mux_manifest.json")
        m.top, m.muxed_top          # module to miter, and its muxed twin
        m.clock, m.reset            # the run's --clock / --reset (None if not given)
        m.golden_files, m.muxed_files
        m.testable                  # cuts reachable from a top-level port
        m.untested                  # reserved selects that drive nothing
        m.exclusive_ports           # [(port, port)] at the top
        m.cut_of("pc_sel_stage_3")  # -> Cut(module="stage", index=3, ...)
    """

    def __init__(self, data: dict, path: str):
        self.path = os.path.abspath(path)
        self.dir = os.path.dirname(self.path)
        self.data = data
        v = data.get("version")
        if v != VERSION:
            raise ManifestError(f"{path}: manifest version {v!r}, this reader supports {VERSION}")
        self.mode = data.get("mode", "?")
        self.top = data["top"]
        self.suffix = data.get("mux_suffix", "")
        # Optional fields: manifests written before papercuts recorded them read as None.
        self.clock = data.get("clock")
        self.reset = data.get("reset")
        mods = data["modules"]
        if self.top not in mods:
            raise ManifestError(f"{path}: top module {self.top!r} has no entry (modules: {sorted(mods)})")
        self.muxed_top = mods[self.top]["muxed_name"]

        # A module's own cuts are pc_sel<N> on that module; everything below the top
        # reaches the top through a forwarded port. The manifest states both, so no
        # name is parsed here.
        port_of: dict[tuple[str, int], str] = {}
        for f in mods[self.top]["forwarded"]:
            port_of[(f["module"], f["index"])] = f["port"]
        for c in mods[self.top]["cuts"]:
            port_of[(self.top, c["index"])] = c["port"]

        self.cuts: list[Cut] = []
        for name, m in mods.items():
            for c in m["cuts"]:
                self.cuts.append(Cut(module=name, index=c["index"], type=c["type"], line=c["line"],
                                     inserted=bool(c["inserted"]), port=port_of.get((name, c["index"]))))
        self.by_port = {c.port: c for c in self.cuts if c.port}
        self.by_id = {c.id: c for c in self.cuts}

        # Exclusivity is declared per module, in that module's own cut indices;
        # translate to the top-level ports the synthesizer actually sees.
        self.exclusive_ports: list[tuple[str, str]] = []
        for name, m in mods.items():
            for a, b in m["exclusive"]:
                pa, pb = port_of.get((name, a)), port_of.get((name, b))
                if pa and pb and self.by_port[pa].inserted and self.by_port[pb].inserted:
                    self.exclusive_ports.append((pa, pb))

        self.golden_files = [self._resolve(p) for p in data["files"]["golden"]]
        self.muxed_files = [self._resolve(p) for p in data["files"]["muxed"]]
        missing = [p for p in self.golden_files + self.muxed_files if not os.path.exists(p)]
        if missing:
            raise ManifestError(f"{path}: source file(s) listed but not found: {missing}")

    def _resolve(self, p: str) -> str:
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.dir, p))

    @classmethod
    def load(cls, path: str) -> "Manifest":
        with open(path) as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise ManifestError(f"{path}: not valid JSON ({e})") from e
        return cls(data, path)

    @property
    def testable(self) -> list[Cut]:
        """Cuts a top-level port actually toggles: the only ones worth certifying."""
        return [c for c in self.cuts if c.inserted and c.port]

    @property
    def untested(self) -> list[Cut]:
        """Cuts the muxed design cannot exercise: the select is reserved (so indices
        stay aligned) but drives nothing, or it never reached the top. Never report
        one of these as proven."""
        return [c for c in self.cuts if not c.inserted or not c.port]

    def cut_of(self, port: str) -> Cut:
        if port not in self.by_port:
            raise ManifestError(f"{self.path}: no cut for port {port!r}")
        return self.by_port[port]

    def check_against(self, ports: dict[str, tuple[str, int]]) -> list[str]:
        """Compare the manifest with the muxed top's actual ports (name -> (dir, width)).
        Returns human-readable problems; empty means they agree."""
        problems = []
        declared = {p for p in ports if p.startswith("pc_sel")}
        for c in self.cuts:
            if c.port and c.port not in declared:
                problems.append(f"{c.id}: manifest names port {c.port} but {self.muxed_top} has no such port")
            elif c.port and ports[c.port] != ("input", 1):
                problems.append(f"{c.port}: expected a 1-bit input, design has {ports[c.port]}")
        for p in sorted(declared - set(self.by_port)):
            problems.append(f"{self.muxed_top} has select port {p} that the manifest does not describe")
        return problems
