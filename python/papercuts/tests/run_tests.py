#!/usr/bin/env python3
"""Run every papercuts test in this directory with one command.

    python python/papercuts/tests/run_tests.py

Discovers each `test_*.py` module beside this file, calls its `run()`, and
exits non-zero if any test raises (e.g. an assertion failure). The repo's
`python/` dir (this file lives at python/papercuts/tests/) is prepended to
sys.path so the locally built `papercuts` extension is importable without a
separate install.
"""

import importlib
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON_DIR = HERE.parent.parent  # python/papercuts/tests -> python/
sys.path.insert(0, str(PYTHON_DIR))
sys.path.insert(0, str(HERE))


def main():
    modules = sorted(p.stem for p in HERE.glob("test_*.py"))
    if not modules:
        print("no tests found")
        return 1

    failures = []
    for name in modules:
        mod = importlib.import_module(name)
        run = getattr(mod, "run", None)
        if run is None:
            print(f"{name}: SKIP (no run())")
            continue
        try:
            run()
        except Exception:
            failures.append(name)
            print(f"{name}: FAIL")
            traceback.print_exc()

    print("-" * 40)
    if failures:
        print(f"FAILED: {', '.join(failures)} ({len(failures)}/{len(modules)})")
        return 1
    print(f"PASSED: {len(modules)}/{len(modules)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
