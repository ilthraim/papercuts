#!/usr/bin/env python
"""Repair a papercuts run whose cuts fail together: find the most cuts that can stay on, and prove them.

    repair_cuts.py OUTPUTS [options] -- PAPERCUTS_ARGS...

OUTPUTS is the run's `outputs/` directory. PAPERCUTS_ARGS are the arguments that run was given (input files,
--top, --clock, --reset, ...), with paths relative to --cwd. The cuts that were turned on are the valid cuts in
OUTPUTS/papercuts.results.jsonl, or the list in --cuts ([[module, idx, amount], ...], as in kept_cuts.json).

How it decides
  The cuts are rebuilt with the same papercuts steps (elaborate, prune, one Papercutter per module, cut_index,
  prune). Cut indices only mean what they meant in that run if the rebuilt texts equal
  OUTPUTS/consolidated_sources, so a mismatch is a hard error: use the same papercuts commit and the same options.

  A check is one sequential equivalence check by the run's papercuts backend (--backend; its own options come from
  PAPERCUTS_ARGS), and only the backend's verdict is used. A search check compares the consolidated RTL for a
  subset of the cuts with the ORIGINAL design (as the papercuts elaboration gate does, with its renames), from reset.

  Search (implicit hitting set):
    1. The design with no cuts must be proven.
    2. R = a smallest set of cuts that removes at least one cut from every conflict found so far (z3).
    3. Check all cuts minus R. A counterexample means a new conflict: delta debugging shrinks the failing set
       to one where removing any single cut makes the failure go away. Back to 2.
    4. Proven within --short-s (or --proof-s, if given) ends the search. No verdict: the chain proof below.
  A conflict always comes from a counterexample, never from a check that ran out of time. |R| is the true
  minimum if adding cuts to a failing set keeps it failing; same-node cut pairs can break that, so each
  dropped cut is then added back alone, and the final set's proof is what counts.
  --drop MOD#IDX ... skips the search and proves all cuts minus those.

  Chain proof, when one whole-design check of the final set T runs out of time:
    step 0   original == S0 (no cuts), the check of step 1 above
    step i   S(i-1) == S(i) = S(i-1) + G, a check with S(i-1)'s consolidated RTL as the golden side
    until    S(n) = T
  T == original then follows by transitivity, provided the backend's "proven" means what it does for Jasper's
  check_sec: a full proof from reset, over all inputs and all unpaired free values, with same-named state paired
  with the same initial value in every check, so one assignment meets all the steps' pairings at once. A backend
  that proves less (bounded, or pairing state differently) does not support the chain.
  Order: cuts that papercuts took more than --slow-s to prove alone (OUTPUTS/papercuts.results.jsonl; mostly
  bit-shrinks) go first, one at a time, while the base is still close to the uncut design; their first limit is
  3x papercuts' time (at least the first of --single-s), then each further --single-s. The rest follow in groups
  of --chunk; a group with no verdict in --step-s is split in half. A single cut whose step has a counterexample
  (cuts that only work together) is deferred and retried after the rest; deferred cuts that never go in alone
  are added together, and a counterexample there means T itself differs from the original.

Output, in --work (default OUTPUTS/../repair):
  result.json                       status, kept and dropped cuts, conflicts, the proof (one check or the chain)
  repaired/<top>_consolidated.sv    the repaired design, and repaired/consolidated_sources/<module>.sv
  checks.jsonl                      every check and its verdict. Rerunning resumes from this ledger.
Exit status: 0 proven; 1 anything else (see result.json; on a missing verdict it lists the candidate drop set).

One check at a time. A check is killed above --max-gb, or when free memory falls below 8 GB.
"""
import argparse, asyncio, hashlib, json, os, shutil, signal, subprocess, sys, time

FLOOR_GB, DISK_FLOOR_GB, START_FREE_GB = 8, 6, 48


class Failure(Exception):
    """The repair stopped without a proven set; the message says why."""


# MARK: papercuts' own options
def papercuts_options(argv):
    """Parse argv with papercuts' own parser (exact flags and defaults), then resolve the options that shape cuts
    the way papercuts' main() does."""
    import papercuts.__main__ as pm

    class Parsed(Exception):
        pass

    real = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        ns = real(self, args, namespace)
        raise Parsed(ns)

    saved = sys.argv
    sys.argv = ["papercuts"] + list(argv)
    argparse.ArgumentParser.parse_args = grab
    try:
        asyncio.run(pm.main())
        raise Failure("papercuts' main() returned without parsing its arguments")
    except Parsed as p:
        a = p.args[0]
    finally:
        argparse.ArgumentParser.parse_args = real
        sys.argv = saved
    if a.method == "cegis-comb":
        a.shrink_with_intermediate = True
        a.const_priority = False
    if a.const_priority is None:
        a.const_priority = True
    a.papercuts_argv = list(argv)
    return a


# MARK: rebuilding the cuts
class Design:
    """The run's modules and cutters, rebuilt as papercuts' main() builds them."""

    def __init__(self, a, orig_dir):
        from pyslang.syntax import SyntaxTree
        from papercuts import chipper
        from papercuts.__main__ import child_port_directions
        from papercuts.backends import get_backend
        from papercuts.deadcode import prune_dead_source
        from papercuts.elaborator import build_compilation_from, elaborate_design, gate_renames, make_parse_env
        from papercuts.pypercuts import Papercutter
        from papercuts.utils import print_tree
        import fnmatch
        self.a, self.print_tree, self.prune = a, print_tree, prune_dead_source
        self.SyntaxTree, self.build_compilation_from, self.gate_renames = SyntaxTree, build_compilation_from, gate_renames

        excl = set(a.exclude_module or [])
        if a.exclude_modules_file:
            for raw in open(a.exclude_modules_file):
                s = raw.split("#", 1)[0].strip()
                if s:
                    excl.add(s)
        if not a.no_default_excludes:
            excl |= get_backend(a.backend).from_args(a).default_excluded_modules()
        incdirs, defines = list(a.incdir or []), list(a.define or [])

        # the original, one module per file: the golden side of every check
        os.makedirs(orig_dir, exist_ok=True)
        sm, opts = make_parse_env(incdirs, defines)
        raws = [SyntaxTree.fromFile(f, sm, opts) for f in a.input_files]
        orig_trees, seen = [], set()
        for raw in raws:
            for name, tree in chipper.split_tree(raw):
                if name in seen:
                    raise Failure(f"module {name} is defined twice")
                seen.add(name)
                orig_trees.append((tree, name))
                with open(os.path.join(orig_dir, f"{name}.sv"), "w") as f:
                    f.write(print_tree(tree))

        symbolic = {}
        self.renames = None
        if a.in_situ:
            conc = orig_trees
            comp = build_compilation_from(raws)
            symbolic = chipper.definition_signal_ranges(comp)
            tops = [t.name for t in comp.getRoot().topInstances]
            self.top = a.top or (tops[0] if len(tops) == 1 else None)
            if self.top is None:
                raise Failure(f"tops {tops}: pass --top")
            verbatim = {n for _, n in conc if any(fnmatch.fnmatch(n, p) for p in excl)}
        else:
            elab = elaborate_design(a.input_files, flatten=True, ignore=excl, fold_constants=a.fold_constants,
                                    incdirs=incdirs, defines=defines, top=a.top)
            if len(elab.tops) != 1:
                raise Failure(f"tops {elab.tops}: pass --top")
            self.top, self.renames = elab.top, elab.renames
            src = elab.source
            if a.prune_dead:
                src, _ = prune_dead_source(src, self.top, protect_modules=elab.verbatim, fold_constants=a.fold_constants)
            conc = [(t, n) for n, t in chipper.split_tree(SyntaxTree.fromText(src))]
            verbatim = elab.verbatim
        dirs = chipper.definition_port_directions(build_compilation_from([t for t, _ in conc]))
        cut_only = set(a.cut_only or [])

        self.order, self.tree, self.pc, self.info = [], {}, {}, {}
        for tree, name in conc:
            self.order.append(name)
            self.tree[name] = tree
            if name in verbatim or (cut_only and not any(fnmatch.fnmatch(name, p) for p in cut_only)):
                continue
            nt = SyntaxTree.fromText(print_tree(tree))
            pc = Papercutter(nt, shrink_with_intermediate=a.shrink_with_intermediate,
                             binops_in_conditions_only=a.binops_in_conditions_only,
                             symbolic_ranges=symbolic.get(name, {}), port_directions=child_port_directions(nt, dirs))
            pc.const_priority = a.const_priority
            self.pc[name] = pc
            self.info[name] = list(pc.cut_info())
        self._cache = {}

    def meta(self, k):
        t, line = self.info[k[0]][k[1]]
        return t, line

    def pairs(self, on):
        """Same-node pairs with both cuts turned on: only one takes effect, so dropping it lets the other act."""
        out = {}
        for m, pc in self.pc.items():
            for x, y in pc.cut_pairs():
                if (m, x) in on and (m, y) in on:
                    out[(m, x)], out[(m, y)] = (m, y), (m, x)
        return out

    def module_text(self, m, amounts):
        key = (m, tuple(sorted(amounts.items())))
        if key not in self._cache:
            if m not in self.pc:
                text = self.print_tree(self.tree[m])
            else:
                text = self.print_tree(self.pc[m].cut_index(sorted(amounts), dict(amounts))) if amounts \
                    else self.print_tree(self.tree[m])
                if self.a.prune_dead and not self.a.in_situ:
                    text = self.prune(text, m, fold_constants=self.a.fold_constants)[0]
            self._cache[key] = text
        return self._cache[key]

    def build(self, chosen):
        """chosen: {(module, idx): amount} -> ({module: text}, the whole design as one file)."""
        by = {m: {} for m in self.order}
        for (m, i), amt in chosen.items():
            by[m][i] = amt
        texts = {m: self.module_text(m, by[m]) for m in self.order}
        return texts, "\n".join(texts[m] for m in self.order)

    def check_renames(self, blob):
        if not self.renames:
            return []
        rs = self.gate_renames(self.renames, self.build_compilation_from([self.SyntaxTree.fromText(blob)]))
        return [[r.kind, r.golden, r.candidate] for r in rs]


def turned_on(outputs, cuts_file, D):
    """{(module, idx): amount}, checked against the rebuilt cut plan."""
    if cuts_file:
        rows = [(m, i, amt, None, None) for m, i, amt in json.load(open(cuts_file))]
    else:
        last = {}
        for ln in open(os.path.join(outputs, "papercuts.results.jsonl")):
            r = json.loads(ln)
            if r.get("phase") == "cuts":
                last[(r["module"], r["idx"])] = r
        rows = [(m, i, r.get("shrink_amount") or 1, r["type"], r["line"]) for (m, i), r in sorted(last.items())
                if r["valid"]]
    on = {}
    for m, i, amt, typ, line in rows:
        if m not in D.pc or not 0 <= i < len(D.info[m]):
            raise Failure(f"cut {m}#{i} does not exist in the rebuilt design")
        if typ is not None and (typ, line) != tuple(D.info[m][i]):
            raise Failure(f"cut {m}#{i} was {typ} at line {line} in the run but is {tuple(D.info[m][i])} now: "
                          f"the cut plan changed (different papercuts commit or options)")
        on[(m, i)] = amt
    return on


def reproduce(outputs, D, on):
    texts, _ = D.build(on)
    bad = []
    for m in D.order:
        p = os.path.join(outputs, "consolidated_sources", f"{m}.sv")
        if not os.path.exists(p) or open(p).read() != texts[m]:
            bad.append(m)
    if bad:
        raise Failure(f"the rebuilt cuts do not reproduce {outputs}/consolidated_sources ({', '.join(bad[:5])}"
                      f"{' ...' if len(bad) > 5 else ''}). Use the papercuts commit and options of that run.")


# MARK: one check
def child(cwd):
    """Runs one check in its own session, so the parent can kill the backend's whole process tree."""
    from papercuts.backends import get_backend
    from papercuts.utils import Design as Side, Rename, Run
    os.chdir(cwd)
    c = json.load(open("check.json"))
    a = papercuts_options(c["papercuts_args"])
    backend = get_backend(a.backend).from_args(a)
    backend.prepare()
    golden = Side(c["top"], c["orig"])
    run = Run(golden=golden, candidate=golden.override(c["top"], c["blob"]), work_dir=cwd, index=0,
              renames=tuple(Rename(*r) for r in c["renames"]), time_limit_s=c["limit"], stop_at_first_cex=True)
    asyncio.run(backend.check(run))
    json.dump({"verdict": run.verdict}, open("verdict.json", "w"))


def proc_tree(sid):
    """pids and resident GB of every process in session `sid` or below it."""
    info = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            s = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            info[int(pid)] = (int(s[1]), int(s[3]), int(s[21]) * os.sysconf("SC_PAGE_SIZE") / 2**30)
        except (OSError, IndexError, ValueError):
            pass
    pids = {p for p, (_, ses, _) in info.items() if ses == sid}
    grew = True
    while grew:
        more = {p for p, (pp, _, _) in info.items() if pp in pids} - pids
        grew = bool(more)
        pids |= more
    return pids, sum(info[p][2] for p in pids)


def free_gb():
    for ln in open("/proc/meminfo"):
        if ln.startswith("MemAvailable:"):
            return int(ln.split()[1]) / 2**20


class Checker:
    def __init__(self, D, a, work, orig_dir, max_checks, max_gb):
        self.D, self.a, self.work, self.orig = D, a, work, orig_dir
        self.max_checks, self.max_gb, self.ran = max_checks, max_gb, 0
        self.ledger = os.path.join(work, "checks.jsonl")
        self.memo = {}
        if os.path.exists(self.ledger):
            for ln in open(self.ledger):
                r = json.loads(ln)
                self.memo.setdefault(r["sha256"], []).append(r)

    def known(self, h, limit):
        for r in self.memo.get(h, []):
            if r["verdict"] in ("cex", "proven") or (r["verdict"] == "inconclusive" and r["limit"] >= limit):
                return r
        return None

    def __call__(self, chosen, limit, why):
        """Check the design with `chosen` cuts against the original."""
        texts, blob = self.D.build(chosen)
        h = hashlib.sha256(blob.encode()).hexdigest()
        r = self.known(h, limit)
        if r:
            return r
        self._spend()
        r = self._run(h, blob, limit)
        return self._log(r, h, limit, why, n=len(chosen))

    def step(self, base, chosen, limit, why):
        """Check the design with `chosen` cuts against the design with `base` cuts (a chain step)."""
        _, blob = self.D.build(chosen)
        _, gold = self.D.build(base)
        h = hashlib.sha256(("chain\0" + gold + "\0" + blob).encode()).hexdigest()
        r = self.known(h, limit)
        if r:
            return r
        self._spend()
        lib = os.path.join(self.work, "libs", h[:16])
        os.makedirs(lib, exist_ok=True)
        with open(os.path.join(lib, f"{self.D.top}.sv"), "w") as f:
            f.write(gold)
        r = self._run(h, blob, limit, orig=lib, renames=[])
        return self._log(r, h, limit, why, n=len(chosen), base_n=len(base),
                         added=[f"{m}#{i}" for m, i in sorted(set(chosen) - set(base))])

    def _spend(self):
        if self.ran >= self.max_checks:
            raise Failure(f"used --max-checks {self.max_checks} checks; rerun with a larger budget to continue")
        self.ran += 1

    def _log(self, r, h, limit, why, **extra):
        r.update(sha256=h, limit=limit, why=why, **extra, ts=time.strftime("%Y-%m-%d %H:%M:%S"))
        with open(self.ledger, "a") as f:
            f.write(json.dumps(r) + "\n")
        self.memo.setdefault(h, []).append(r)
        size = f"{extra['base_n']} -> {extra['n']} cuts" if "base_n" in extra else f"{extra['n']} cuts"
        print(f"[{time.strftime('%H:%M:%S')}] {why}: {size} -> {r['verdict']} {r['elapsed']}s {r.get('peak_gb')} GB",
              flush=True)
        if r["verdict"] in ("error", "unmapped"):
            raise Failure(f"check {h[:16]} ({why}) gave {r['verdict']}: see {self.ledger}")
        return r

    def _run(self, h, blob, limit, orig=None, renames=None):
        """One check of `blob` against `orig` (default: the original design, with the elaboration renames)."""
        cwd = os.path.join(self.work, "checks", h[:16])
        shutil.rmtree(cwd, ignore_errors=True)
        os.makedirs(cwd)
        bpath = os.path.join(cwd, f"{self.D.top}_consolidated.sv")
        open(bpath, "w").write(blob)
        a = self.a
        json.dump({"papercuts_args": a.papercuts_argv, "top": self.D.top, "orig": orig or self.orig, "blob": bpath,
                   "limit": limit, "renames": self.D.check_renames(blob) if renames is None else renames},
                  open(os.path.join(cwd, "check.json"), "w"))
        while free_gb() < START_FREE_GB or shutil.disk_usage(self.work).free / 2**30 < DISK_FLOOR_GB:
            print(f"  waiting: {free_gb():.0f} GB memory free, {shutil.disk_usage(self.work).free / 2**30:.0f} GB disk",
                  flush=True)
            time.sleep(120)
        t0, forced, peak = time.time(), None, 0.0
        with open(os.path.join(cwd, "child.log"), "w") as out:
            p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--child", cwd], stdout=out,
                                 stderr=subprocess.STDOUT, start_new_session=True)
            while p.poll() is None:
                time.sleep(2)
                gb = proc_tree(p.pid)[1]
                peak = max(peak, gb)
                forced = ("wall-limit" if time.time() - t0 > limit + 1800 else "memory-limit" if gb > self.max_gb
                          else "memory-pressure" if free_gb() < FLOOR_GB else None)
                if forced:
                    for q in proc_tree(p.pid)[0]:
                        try:
                            os.kill(q, signal.SIGKILL)
                        except OSError:
                            pass
                    p.wait()
                    break
        r = {"elapsed": round(time.time() - t0, 1), "peak_gb": round(peak, 1)}
        vp = os.path.join(cwd, "verdict.json")
        if forced:
            r["verdict"], r["forced"] = "inconclusive", forced
        elif os.path.exists(vp):
            r["verdict"] = json.load(open(vp))["verdict"]
        else:
            r["verdict"] = "error"
            r["error"] = open(os.path.join(cwd, "child.log"), errors="replace").read()[-800:]
        shutil.rmtree(cwd, ignore_errors=True)
        return r


# MARK: search
def ddmin(items, fails):
    """A subset of the failing `items` that still fails and stops failing if any one item is removed.
    Only sets that were actually seen to fail are kept, so no monotonicity is assumed."""
    n = 2
    while len(items) >= 2:
        size = len(items) // n
        chunks = [items[i:i + size] for i in range(0, len(items), size)] if size else [[x] for x in items]
        for c in chunks:
            if fails(c):
                items, n = c, 2
                break
        else:
            for c in chunks:
                rest = [x for x in items if x not in c]
                if rest and fails(rest):
                    items, n = rest, max(n - 1, 2)
                    break
            else:
                if n >= len(items):
                    break
                n = min(len(items), 2 * n)
    return items


def hitting_set(conflicts, all_cuts, pairs):
    """Fewest cuts meeting every conflict; ties prefer a cut whose same-node partner can take over."""
    import z3
    if not conflicts:
        return set()
    o = z3.Optimize()
    v = {k: z3.Bool(f"c{i}") for i, k in enumerate(all_cuts)}
    for c in conflicts:
        o.add(z3.Or([v[k] for k in c]))
    for k in {k for c in conflicts for k in c}:
        o.add_soft(z3.Not(v[k]), weight=1000 + (0 if k in pairs else 1))
    if o.check() != z3.sat:
        raise Failure("no hitting set")
    m = o.model()
    return {k for k in v if z3.is_true(m.eval(v[k]))}


def hints(outputs):
    """Seconds papercuts took to prove each cut alone: how hard its chain step is likely to be."""
    h = {}
    for ln in open(os.path.join(outputs, "papercuts.results.jsonl")):
        r = json.loads(ln)
        if r.get("phase") == "cuts":
            h[(r["module"], r["idx"])] = r.get("elapsed") or 0
    return h


def chain_prove(on, target, check, hint, o):
    """Prove `target` (a list of cuts) equivalent to the original step by step; returns the steps."""
    singles = [int(x) for x in o.single_s.split(",")]
    sel = lambda s: {k: on[k] for k in s}
    name = lambda k: f"{k[0]}#{k[1]}"
    steps = []

    def record(r, cur, add):
        steps.append({"from": len(cur) if cur is not None else "original", "added": [name(k) for k in add],
                      "sha256": r["sha256"], "elapsed": r["elapsed"], "limit": r["limit"]})

    r = check(sel([]), o.short_s, "no-cuts")
    if r["verdict"] != "proven":
        raise Failure(f"no cuts vs original: {r['verdict']}")
    record(r, None, [])
    fast = [k for k in target if hint.get(k, 0) <= o.slow_s]
    slow = sorted((k for k in target if hint.get(k, 0) > o.slow_s), key=lambda k: hint[k])
    cur = []
    todo = [[k] for k in slow] + [fast[i:i + o.chunk] for i in range(0, len(fast), o.chunk)]
    while todo:
        deferred, progress = [], False
        while todo:
            g = todo.pop(0)
            if len(g) > 1:
                r = check.step(sel(cur), sel(cur + g), o.step_s, f"chain: group of {len(g)}")
                if r["verdict"] == "proven":
                    record(r, cur, g)
                    cur, progress = cur + g, True
                else:
                    todo[:0] = [g[:len(g) // 2], g[len(g) // 2:]]
                continue
            first = max(singles[0], int(3 * hint.get(g[0], 0)))
            for lim in [first] + [x for x in singles if x > first]:
                r = check.step(sel(cur), sel(cur + g), lim, f"chain: single {name(g[0])}")
                if r["verdict"] != "inconclusive":
                    break
            if r["verdict"] == "proven":
                record(r, cur, g)
                cur, progress = cur + g, True
            elif r["verdict"] == "cex":
                deferred += g
            else:
                raise Failure(f"chain: no verdict adding {name(g[0])} at {singles[-1]} s ({r.get('forced')})")
        if not deferred:
            break
        if progress:
            print(f"chain: deferred {[name(k) for k in deferred]}: another round", flush=True)
            todo = [[k] for k in deferred]
            continue
        for lim in [o.step_s] + singles:
            r = check.step(sel(cur), sel(cur + deferred), lim, f"chain: deferred together ({len(deferred)})")
            if r["verdict"] != "inconclusive":
                break
        if r["verdict"] == "cex":
            raise Failure(f"chain: the set is not equivalent to the original: {len(cur)} cuts are proven, adding "
                          f"{[name(k) for k in deferred]} gives a cex; rerun with --proof-s "
                          f"for a direct counterexample to shrink")
        if r["verdict"] != "proven":
            raise Failure(f"chain: no verdict adding the deferred cuts {[name(k) for k in deferred]}")
        record(r, cur, deferred)
        cur = cur + deferred
        break
    if sorted(cur) != sorted(target):
        raise Failure("chain: internal error, the chain did not reach the target set")
    return steps


def repair(D, on, check, o, hint, drop):
    cuts = sorted(on)
    pairs = D.pairs(on)
    sel = lambda s: {k: on[k] for k in s}

    def proved(s, why):
        r = check(sel(s), o.short_s, why)
        if r["verdict"] == "inconclusive" and o.proof_s:
            r = check(sel(s), o.proof_s, why + "-proof")
        return r

    base = check(sel([]), o.short_s, "no-cuts")
    if base["verdict"] == "inconclusive":
        base = check(sel([]), max(o.proof_s, 3600), "no-cuts-proof")
    if base["verdict"] != "proven":
        raise Failure(f"the design with no cuts is {base['verdict']} against the original, so no cut set can be")
    conflicts = []
    while True:
        R = set(drop) if drop is not None else hitting_set(conflicts, cuts, pairs)
        cand = [k for k in cuts if k not in R]
        r = proved(cand, f"candidate (dropping {len(R)})")
        if r["verdict"] == "proven":
            proof = {"kind": "single", **{x: r.get(x) for x in ("sha256", "elapsed", "limit")}}
            break
        if r["verdict"] == "cex" and drop is not None:
            raise Failure("the --drop set gives a counterexample")
        if r["verdict"] != "cex":
            if o.no_chain:
                e = Failure(f"no verdict for the set dropping {len(R)} cuts ({r['verdict']}, {r.get('forced')}); "
                            f"rerun without --no-chain, or with a larger --proof-s")
                e.candidate_drop, e.conflicts = sorted(R), conflicts
                raise e
            print(f"no verdict in one check: proving the {len(cand)} cuts by a chain", flush=True)
            try:
                steps = chain_prove(on, cand, check, hint, o)
            except Failure as e:
                e.candidate_drop, e.conflicts = sorted(R), conflicts
                raise
            proof = {"kind": "chain", "steps": steps, "elapsed": round(sum(s["elapsed"] for s in steps), 1)}
            break
        limit = max(o.short_s, min(o.proof_s or o.short_s, int(2 * r["elapsed"])))
        c = ddmin(cand, lambda s: check(sel(s), limit, "shrink")["verdict"] == "cex")
        conflicts.append(c)
        print(f"conflict {len(conflicts)}: {[f'{m}#{i}' for m, i in c]}", flush=True)
    added_back = {}
    if not o.no_controls:
        for k in sorted(R):
            added_back[f"{k[0]}#{k[1]}"] = check(sel(cand + [k]), o.short_s, "add-back")["verdict"]
    return cand, R, conflicts, proof, added_back


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outputs", nargs="?")
    ap.add_argument("--cuts", help="JSON [[module, idx, amount], ...] instead of the run's valid cuts")
    ap.add_argument("--drop", nargs="+", help="skip the search: prove all turned-on cuts minus these (module#idx)")
    ap.add_argument("--cwd", default=".", help="directory papercuts was run from (input paths are relative to it)")
    ap.add_argument("--work", help="output and ledger directory (default OUTPUTS/../repair)")
    ap.add_argument("--short-s", type=int, default=120, help="search check limit, seconds (default 120)")
    ap.add_argument("--proof-s", type=int, default=0,
                    help="one longer whole-design check before falling back to the chain (default 0: none)")
    ap.add_argument("--no-chain", action="store_true", help="stop instead of proving the final set by a chain")
    ap.add_argument("--chunk", type=int, default=16, help="chain: cuts per group (default 16)")
    ap.add_argument("--step-s", type=int, default=300, help="chain: group limit before it is split (default 300)")
    ap.add_argument("--single-s", default="900,3600,14400", help="chain: limits tried in turn for one cut")
    ap.add_argument("--slow-s", type=int, default=60,
                    help="chain: cuts papercuts took longer than this to prove alone go first, one at a time")
    ap.add_argument("--max-checks", type=int, default=400, help="checks allowed this invocation (default 400)")
    ap.add_argument("--max-gb", type=float, default=64, help="kill a check above this resident memory (default 64)")
    ap.add_argument("--no-controls", action="store_true", help="skip adding each dropped cut back alone at the end")
    ap.add_argument("--dry-run", action="store_true", help="rebuild the cuts and check they reproduce the run; no checks")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    argv = sys.argv[1:]
    pc_argv = argv[argv.index("--") + 1:] if "--" in argv else []
    o = ap.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    if o.child:
        return child(o.child)
    if not o.outputs or not pc_argv:
        ap.error("need OUTPUTS and, after --, the papercuts arguments of that run")
    outputs = os.path.abspath(o.outputs)
    cuts_file = o.cuts and os.path.abspath(o.cuts)
    work = os.path.abspath(o.work or os.path.join(outputs, "..", "repair"))
    os.makedirs(work, exist_ok=True)
    os.chdir(os.path.abspath(o.cwd))
    a = papercuts_options(pc_argv)
    orig = os.path.join(work, "orig")
    D = Design(a, orig)
    on = turned_on(outputs, cuts_file, D)
    res = {"outputs": outputs, "papercuts_args": pc_argv, "top": D.top, "turned_on": len(on)}
    try:
        drop = None
        if o.drop:
            drop = set()
            for x in o.drop:
                m, i = x.rsplit("#", 1)
                if (m, int(i)) not in on:
                    raise Failure(f"--drop {x} is not a turned-on cut")
                drop.add((m, int(i)))
        reproduce(outputs, D, on)
        if o.dry_run:
            print(f"reproduces: {len(on)} cuts turned on, {sum(len(v) for v in D.info.values())} cuts in the plan, "
                  f"top {D.top}")
            return 0
        check = Checker(D, a, work, orig, o.max_checks, o.max_gb)
        kept, dropped, conflicts, proof, added_back = repair(D, on, check, o, hints(outputs), drop)
    except Failure as e:
        res.update(status="failed", reason=str(e))
        if hasattr(e, "candidate_drop"):
            res.update(candidate_drop=[f"{m}#{i}" for m, i in e.candidate_drop],
                       conflicts=[[f"{m}#{i}" for m, i in c] for c in e.conflicts])
        json.dump(res, open(os.path.join(work, "result.json"), "w"), indent=1)
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    texts, blob = D.build({k: on[k] for k in kept})
    out = os.path.join(work, "repaired")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(os.path.join(out, "consolidated_sources"))
    for m, t in texts.items():
        open(os.path.join(out, "consolidated_sources", f"{m}.sv"), "w").write(t)
    open(os.path.join(out, f"{D.top}_consolidated.sv"), "w").write(blob)
    desc = lambda k: {"module": k[0], "idx": k[1], "type": D.meta(k)[0], "line": D.meta(k)[1], "amount": on[k]}
    res.update(status="proven", kept_count=len(kept), kept=[[m, i, on[(m, i)]] for m, i in kept],
               dropped=[desc(k) for k in sorted(dropped)], conflicts=[[desc(k) for k in c] for c in conflicts],
               added_back=added_back, proof=proof, checks=sum(len(v) for v in check.memo.values()))
    json.dump(res, open(os.path.join(work, "result.json"), "w"), indent=1)
    how = f"a chain of {len(proof['steps'])} steps" if proof["kind"] == "chain" else "one check"
    print(f"proven by {how}: kept {len(kept)} of {len(on)}; dropped {[f'{m}#{i}' for m, i in sorted(dropped)]}")
    print(f"result: {work}/result.json   design: {out}/{D.top}_consolidated.sv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
