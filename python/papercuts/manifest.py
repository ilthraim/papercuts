"""The mux manifest: what `-m` inserted, told to whoever consumes the muxed design.

Without it every fact a consumer needs has to be re-derived by convention --
`pc_sel<N>` means cut N, a module is `<name>_muxed`, two cuts on one line are a
pair -- and each of those has already been wrong at least once. The manifest is
written by the pipeline that does the inserting, from the same objects, so it
cannot drift from the muxed source it describes.

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
