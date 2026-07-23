"""Live status file + viewer for an in-progress equivalence-checking run.

Backend-agnostic: the pipeline is the only layer common to every
equivalence-checking backend, so it -- not any individual backend -- records
each check's lifecycle (pending -> running -> done) into
``<output_dir>/status.json`` as checks are dispatched. A second process renders
that file so the user can watch which formal checks are in flight *right now*
and how long each has been running::

    # terminal 1: the run
    python -m papercuts design.sv -e

    # terminal 2: watch the checks currently running
    python -m papercuts.status --watch

The viewer is read-only -- it only reads status.json and never affects the run,
so it can be opened late, closed, reopened, or not run at all with no effect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

#: File the pipeline writes and the viewer reads, inside the output directory.
STATUS_FILENAME = "status.json"

#: Human-readable, continuously-rewritten per-type stats (crash-survivable).
STATS_FILENAME = "papercuts.stats.log"

# Lifecycle a tracked equivalence check moves through.
PENDING = "pending"
RUNNING = "running"
DONE = "done"


def _family(ctype: str) -> str:
    """Coarse cut family for roll-ups: the part before the first '('.

    ``binop(mul,keep-left)`` -> ``binop``; ``ternary(keep-true)`` -> ``ternary``;
    ``bitshrink`` -> ``bitshrink``.
    """
    return ctype.split("(", 1)[0] if ctype else "-"


# MARK: Emitter
class StatusWriter:
    """Records each equivalence check's lifecycle to ``<out_dir>/status.json``.

    Used by the pipeline. Every state transition rewrites the JSON snapshot
    atomically (temp file + :func:`os.replace`) so a concurrent reader never
    sees a half-written file. All calls happen on the pipeline's single asyncio
    thread, in the synchronous code around ``await backend.check(...)``, so no
    locking is needed.

    Tasks are keyed by an opaque id (the pipeline uses ``id(run)``); the key is
    never written to the file, only used in-process to correlate
    register/start/finish for the same check.
    """

    def __init__(self, out_dir: str) -> None:
        self._path = os.path.join(out_dir, STATUS_FILENAME)
        self._tmp = self._path + ".tmp"
        self._stats_path = os.path.join(out_dir, STATS_FILENAME)
        self._stats_tmp = self._stats_path + ".tmp"
        self._pid = os.getpid()
        self._started = time.time()
        self._phase = ""
        self._tasks: dict = {}  # task_id -> row dict (insertion-ordered)
        # Per-type running aggregate, keyed by ctype (empty ctypes -- self-check,
        # gate, consolidate -- are not counted here). Updated on register/finish
        # and flushed with every snapshot so a killed run leaves current per-type
        # success counts on disk. "pending" is derived (planned - proven - failed).
        self._by_type: dict[str, dict] = {}  # ctype -> {planned, proven, failed, verdicts}
        # Load-bearing gate verdicts, surfaced in the stats file for context.
        self._gate: "tuple[str | None, bool] | None" = None
        self._selfcheck: "tuple[str | None, bool] | None" = None
        # status.json/stats.log feed a viewer that polls ~every 2s, so writing on
        # every lifecycle transition -- each rewrite re-serializes ALL tasks, so
        # the cost is O(checks^2) -- does not scale past a few thousand cuts (an
        # 11k-cut register loop alone took >2 min). Coalesce: an unforced _flush()
        # writes at most once per _min_flush_interval; forced flushes (phase
        # changes, the batch register() below, terminal verdicts) always write.
        self._min_flush_interval = 0.25
        self._last_flush = 0.0
        self._flush(force=True)

    def _bump_type(self, ctype: str) -> dict:
        return self._by_type.setdefault(
            ctype, {"planned": 0, "proven": 0, "failed": 0, "verdicts": {}}
        )

    def set_phase(self, phase: str) -> None:
        """Record the pipeline's current high-level phase (for the header)."""
        self._phase = phase
        self._flush(force=True)

    def flush(self) -> None:
        """Force a snapshot write now.

        Public counterpart to the internal throttled flush: call after a batch of
        ``register(..., flush=False)`` to publish the whole backlog in one write.
        """
        self._flush(force=True)

    def register(
        self, task_id, phase: str, label: str, ctype: str = "", flush: bool = True
    ) -> None:
        """Add a check as ``pending`` so the backlog is visible before dispatch.

        ``ctype`` is the kind of cut being checked (e.g. ``bitshrink``,
        ``ternary(keep-true)``); empty for non-cut checks (self-check, gate).

        Pass ``flush=False`` when registering many checks in a loop, then call
        :meth:`flush` once -- otherwise each register re-serializes the growing
        task list to disk (O(n^2), the 11k-cut startup stall).
        """
        self._tasks[task_id] = {
            "phase": phase,
            "label": label,
            "ctype": ctype,
            "state": PENDING,
            "start": None,
            "end": None,
            "verdict": None,
            "valid": False,
        }
        if ctype:
            self._bump_type(ctype)["planned"] += 1
        if flush:
            self._flush()

    def start(self, task_id, phase: str = "", label: str = "", ctype: str = "") -> None:
        """Mark a check ``running`` and stamp its start time.

        If the check was not pre-registered (e.g. the one-off self-check / gate),
        it is registered on the fly from ``phase``/``label``/``ctype``.
        """
        t = self._tasks.get(task_id)
        if t is None:
            self.register(task_id, phase, label, ctype)
            t = self._tasks[task_id]
        t["state"] = RUNNING
        t["start"] = time.time()
        # Force for the one-off load-bearing checks; throttle the many cut checks.
        self._flush(force=t.get("phase") in ("selfcheck", "gate"))

    def finish(self, task_id, verdict, valid: bool) -> None:
        """Mark a check ``done`` with its verdict and validity."""
        t = self._tasks.get(task_id)
        if t is None:
            return
        t["state"] = DONE
        t["end"] = time.time()
        t["verdict"] = verdict
        t["valid"] = bool(valid)

        # Fold into the per-type aggregate (cut checks only) and remember the
        # load-bearing gate/self-check verdicts for the stats header.
        ctype = t.get("ctype")
        if ctype:
            agg = self._bump_type(ctype)
            if valid:
                agg["proven"] += 1
            else:
                agg["failed"] += 1
                # Bucket only failures by verdict, so "failed by verdict" (cex vs
                # inconclusive vs error) stays a clean breakdown of the failures.
                key = verdict or "unknown"
                agg["verdicts"][key] = agg["verdicts"].get(key, 0) + 1
        is_load_bearing = t.get("phase") in ("gate", "selfcheck")
        if t.get("phase") == "gate":
            self._gate = (verdict, bool(valid))
        elif t.get("phase") == "selfcheck":
            self._selfcheck = (verdict, bool(valid))

        # Force for the one-off load-bearing checks; throttle the many cut checks.
        self._flush(force=is_load_bearing)

    def _totals(self) -> dict:
        """Grand totals across all counted (cut) types."""
        planned = sum(a["planned"] for a in self._by_type.values())
        proven = sum(a["proven"] for a in self._by_type.values())
        failed = sum(a["failed"] for a in self._by_type.values())
        verdicts: dict[str, int] = {}
        for a in self._by_type.values():
            for k, v in a["verdicts"].items():
                verdicts[k] = verdicts.get(k, 0) + v
        return {
            "planned": planned,
            "proven": proven,
            "failed": failed,
            "pending": planned - proven - failed,
            "verdicts": verdicts,
        }

    def _flush(self, force: bool = False) -> None:
        # Coalesce high-frequency unforced flushes (start/finish of many cuts) so
        # the write rate -- and the O(n) per-write serialization -- stays bounded
        # regardless of cut count. Forced flushes always write.
        now = time.time()
        if not force and (now - self._last_flush) < self._min_flush_interval:
            return
        self._last_flush = now
        snapshot = {
            "pid": self._pid,
            "started": self._started,
            "updated_at": time.time(),
            "phase": self._phase,
            "tasks": list(self._tasks.values()),
            "by_type": self._by_type,
            "totals": self._totals(),
            "gate": self._gate,
            "selfcheck": self._selfcheck,
        }
        with open(self._tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(self._tmp, self._path)  # atomic on POSIX
        self._write_stats_log()

    def _write_stats_log(self) -> None:
        """Rewrite the human-readable per-type stats file, atomically.

        Bounded (one row per cut sub-type plus family/total roll-ups), so
        rewriting it on every transition is cheap -- unlike the full per-cut log.
        Always current on disk, so a run that is killed mid-check still leaves an
        up-to-date success-per-type breakdown behind.
        """
        totals = self._totals()
        complete = self._phase == "done"
        updated = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

        def pending(a: dict) -> int:
            return a["planned"] - a["proven"] - a["failed"]

        # Family roll-up (binop/ternary/if/case/bitshrink).
        fam: dict[str, dict] = {}
        for ctype, a in self._by_type.items():
            f = fam.setdefault(_family(ctype), {"planned": 0, "proven": 0, "failed": 0})
            f["planned"] += a["planned"]
            f["proven"] += a["proven"]
            f["failed"] += a["failed"]

        subtypes = sorted(self._by_type.items())
        families = sorted(fam.items())
        w_type = max([len("TYPE")] + [len(t) for t in self._by_type] + [len("TOTAL")])
        w_fam = max([len("FAMILY")] + [len(f) for f in fam], default=len("FAMILY"))

        def verdict_str(vd: dict) -> str:
            return "  ".join(f"{k} {v}" for k, v in sorted(vd.items())) or "-"

        lines = [
            f"# papercuts stats — phase {self._phase or '-'} · pid {self._pid} · "
            f"updated {updated} · STATUS: {'complete' if complete else 'running'}",
        ]
        gate = self._gate[0] if self._gate else "-"
        selfc = self._selfcheck[0] if self._selfcheck else "-"
        lines.append(f"# self-check: {selfc}   gate: {gate}")
        lines.append(
            f"# {'TYPE':<{w_type}}  {'planned':>7}  {'proven':>6}  "
            f"{'failed':>6}  {'pending':>7}"
        )
        for ctype, a in subtypes:
            lines.append(
                f"  {ctype:<{w_type}}  {a['planned']:>7}  {a['proven']:>6}  "
                f"{a['failed']:>6}  {pending(a):>7}"
            )
        lines.append("#")
        lines.append(
            f"# by family: {'FAMILY':<{w_fam}}  {'planned':>7}  {'proven':>6}  "
            f"{'failed':>6}  {'pending':>7}"
        )
        for f, a in families:
            lines.append(
                f"  {f:<{w_fam}}  {a['planned']:>7}  {a['proven']:>6}  "
                f"{a['failed']:>6}  {a['planned'] - a['proven'] - a['failed']:>7}"
            )
        lines.append("#")
        lines.append(
            f"  {'TOTAL':<{w_type}}  {totals['planned']:>7}  {totals['proven']:>6}  "
            f"{totals['failed']:>6}  {totals['pending']:>7}"
        )
        lines.append(f"# failed by verdict: {verdict_str(totals['verdicts'])}")

        with open(self._stats_tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(self._stats_tmp, self._stats_path)  # atomic on POSIX


# MARK: Viewer
def _load(path: str):
    """Read status.json, tolerating absence or a mid-write partial read.

    On NFS the writer's atomic ``os.replace`` swaps the inode out from under a
    reader that has already opened the path, so a read can fail with
    ``ESTALE`` ("Stale file handle"); ``json.load`` can also raise
    ``ValueError`` on a truncated snapshot. All of these are transient — the
    next poll re-opens the freshly replaced file — so treat them as "no
    snapshot this tick" rather than crashing the viewer.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _fmt_elapsed(seconds: float) -> str:
    """Human-friendly duration: ``45s`` / ``3m41s`` / ``1h07m``."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _pid_alive(pid: int) -> bool:
    """Whether the orchestrator process is still alive (same-host check).

    Used only to warn that ``running`` rows are frozen because papercuts died;
    a live-but-quiet run (a check legitimately in flight, no transitions) is not
    treated as stale.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def render(snap: dict, show_all: bool, now: "float | None" = None) -> str:
    """Format a status snapshot into the table shown to the user."""
    if now is None:
        now = time.time()
    tasks = snap.get("tasks", [])
    running = [t for t in tasks if t["state"] == RUNNING]
    pending = [t for t in tasks if t["state"] == PENDING]
    done = [t for t in tasks if t["state"] == DONE]
    proven = sum(1 for t in done if t["valid"])
    failed = len(done) - proven

    phase = snap.get("phase") or "-"
    lines = [
        f"papercuts status — phase {phase} · running {len(running)} · "
        f"pending {len(pending)} · done {len(done)} "
        f"(proven {proven} · failed {failed})"
    ]

    age = now - snap.get("updated_at", now)
    pid = snap.get("pid")
    sub = f"last update {_fmt_elapsed(age)} ago"
    if pid and not _pid_alive(pid) and running:
        sub += "   ⚠ papercuts not running (crashed?) — elapsed times frozen"
    lines += [sub, ""]

    rows = running + (pending + done if show_all else [])
    if not rows:
        lines.append("  (no FV checks running)")
        return "\n".join(lines)

    # Running first, then pending, then done; within running, longest-first.
    order = {RUNNING: 0, PENDING: 1, DONE: 2}

    def sort_key(t):
        if t["state"] == RUNNING and t["start"]:
            return (order[t["state"]], -(now - t["start"]))
        return (order[t["state"]], 0)

    rows = sorted(rows, key=sort_key)

    def type_of(t):
        return t.get("ctype") or "-"

    w_phase = max([len("PHASE")] + [len(t["phase"]) for t in rows])
    w_label = max([len("LABEL")] + [len(t["label"]) for t in rows])
    w_type = max([len("TYPE")] + [len(type_of(t)) for t in rows])
    lines.append(
        f"  {'PHASE':<{w_phase}}  {'LABEL':<{w_label}}  "
        f"{'TYPE':<{w_type}}  {'STATE':<7}  {'ELAPSED':>8}"
    )
    for t in rows:
        if t["state"] == RUNNING and t["start"]:
            elapsed = _fmt_elapsed(now - t["start"])
        elif t["state"] == DONE and t["start"] and t["end"]:
            elapsed = _fmt_elapsed(t["end"] - t["start"])
        else:
            elapsed = "-"
        lines.append(
            f"  {t['phase']:<{w_phase}}  {t['label']:<{w_label}}  "
            f"{type_of(t):<{w_type}}  {t['state']:<7}  {elapsed:>8}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m papercuts.status",
        description="Show the equivalence-check runs currently in flight for an "
        "in-progress papercuts run.",
    )
    p.add_argument(
        "--dir",
        default="./outputs",
        help="papercuts output directory to read (default: ./outputs)",
    )
    p.add_argument(
        "-w",
        "--watch",
        action="store_true",
        help="refresh continuously until interrupted (Ctrl-C to quit)",
    )
    p.add_argument(
        "-n",
        "--interval",
        type=float,
        default=2.0,
        help="watch refresh interval in seconds (default: 2)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="also show pending and completed checks, not just running",
    )
    args = p.parse_args(argv)

    path = os.path.join(args.dir, STATUS_FILENAME)

    def frame() -> str:
        snap = _load(path)
        if snap is None:
            return (
                f"papercuts status — no status file at {path}\n"
                "  (waiting for a run with -e to start…)"
            )
        return render(snap, show_all=args.all)

    if not args.watch:
        print(frame())
        return 0

    # Watch: repaint in place (home + clear) until Ctrl-C, leaving the last
    # frame on screen on exit.
    try:
        while True:
            sys.stdout.write("\033[H\033[2J")
            sys.stdout.write(frame() + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
