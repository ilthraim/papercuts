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

# Lifecycle a tracked equivalence check moves through.
PENDING = "pending"
RUNNING = "running"
DONE = "done"


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
        self._pid = os.getpid()
        self._started = time.time()
        self._phase = ""
        self._tasks: dict = {}  # task_id -> row dict (insertion-ordered)
        self._flush()

    def set_phase(self, phase: str) -> None:
        """Record the pipeline's current high-level phase (for the header)."""
        self._phase = phase
        self._flush()

    def register(self, task_id, phase: str, label: str, ctype: str = "") -> None:
        """Add a check as ``pending`` so the backlog is visible before dispatch.

        ``ctype`` is the kind of cut being checked (e.g. ``bitshrink``,
        ``ternary(keep-true)``); empty for non-cut checks (self-check, gate).
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
        self._flush()

    def finish(self, task_id, verdict, valid: bool) -> None:
        """Mark a check ``done`` with its verdict and validity."""
        t = self._tasks.get(task_id)
        if t is None:
            return
        t["state"] = DONE
        t["end"] = time.time()
        t["verdict"] = verdict
        t["valid"] = bool(valid)
        self._flush()

    def _flush(self) -> None:
        snapshot = {
            "pid": self._pid,
            "started": self._started,
            "updated_at": time.time(),
            "phase": self._phase,
            "tasks": list(self._tasks.values()),
        }
        with open(self._tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(self._tmp, self._path)  # atomic on POSIX


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
