"""One JSON line per verifier call, written as the loop goes, so a run that is killed
still leaves its history. This is the raw data for the iteration tables and the
training set.

The first line is the header: {"kind": "header", "selects": [...], "exclusive": [[i, j], ...], "two_frames": false}.
Then one record per candidate:

    {"kind": "iter", "iter": 0, "phase": "search" | "farm" | "exact-k",
     "on": "<hex>",            bit i set = selects[i] on in the candidate
     "n_on": 231, "verify": "sat" | "unsat", "verify_s": 0.1, "synth_s": 0.2,
     "core": [[i, 1], ...],    the learned core as (select index, value); absent if none
     "core_s": 0.1, "core_first": 12, "extra_cores": 2, "k": 57}

`verify` is the label: "unsat" means the candidate meets the spec. `synth_s` is the
time taken to produce the NEXT candidate, so it is absent on the last record.
"""

from __future__ import annotations

import json


def encode_on(on: list[str], index: dict[str, int]) -> str:
    mask = 0
    for n in on:
        mask |= 1 << index[n]
    return format(mask, "x")


def decode_on(text: str) -> int:
    return int(text, 16)


def bits(mask: int) -> list[int]:
    out = []
    while mask:
        low = mask & -mask
        out.append(low.bit_length() - 1)
        mask ^= low
    return out


class TraceWriter:
    def __init__(self, path: str, selects: list[str], exclusive: list[tuple[str, str]], **meta):
        self.index = {n: i for i, n in enumerate(selects)}
        self.f = open(path, "w")
        self._write({"kind": "header", "selects": list(selects),
                     "exclusive": [[self.index[a], self.index[b]] for a, b in exclusive], **meta})

    def _write(self, rec: dict) -> None:
        self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.f.flush()

    def iteration(self, st: dict) -> None:
        rec = {"kind": "iter", "iter": st["iter"], "phase": st.get("phase", "search"),
               "on": encode_on(st["assignment"], self.index), "n_on": len(st["assignment"]),
               "verify": st.get("verify_result"), "verify_s": st.get("verify_s")}
        for key in ("synth_s", "core_s", "core_first", "extra_cores", "k"):
            if key in st:
                rec[key] = st[key]
        if "core_lits" in st:
            rec["core"] = [[self.index[n], int(v)] for n, v in st["core_lits"]]
        self._write(rec)

    def close(self) -> None:
        self.f.close()


def read_trace(path: str) -> tuple[dict, list[dict]]:
    """(header, iteration records). A last line cut short by a kill is dropped."""
    header, recs = None, []
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                break
            if r["kind"] == "header":
                header = r
            else:
                recs.append(r)
    if header is None:
        raise ValueError(f"{path}: no header")
    return header, recs
