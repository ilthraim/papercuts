# Papercuts: SystemVerilog Code Rewriting Tool

A tool for automated rewriting and equivalence checking of SystemVerilog designs using the [slang](https://github.com/MikePopoloski/slang) SystemVerilog parser.

## Overview

Papercuts applies various code transformations ("papercuts") to SystemVerilog designs and can optionally verify semantic equivalence using formal equivalence checking. The tool supports multiple rewrite strategies including bit-width reduction and conditional removal.

## Features

- **Parameter Concretization**: Automatically resolves parameterized values
- **Expression Reduction**: Simplifies constant expressions
- **Mux Insertion** (`-m`): Creates versions of papercut designs with the microdeletions able to be toggled on and off with select bits
- **Equivalence Checking** (`-e`): Formal verification using JasperGold
- **CEGIS cut selection** (`--method cegis-comb`): Picks the largest jointly valid set of cuts with an SMT-based CEGIS loop instead of checking each cut with JasperGold


## Installation

### Prerequisites

- Linux
- Python 3.12 or higher
- CMake 3.20 or higher
- A C++20 compiler (tested with clang 21)
- JasperGold on `PATH`, for `-e`
- For `--method cegis-comb` only: a `yosys` binary on `PATH` that can load the
  [yosys-slang](https://github.com/povik/yosys-slang) plugin (`plugin -i slang`).
  [oss-cad-suite](https://github.com/YosysHQ/oss-cad-suite-build) ships both.
  Optional but much faster: `yices-smt2` or `bitwuzla` on `PATH` (oss-cad-suite
  ships both too); without them the loop falls back to an in-process z3 solver.

The build fetches a pinned fork of slang and compiles it together with its
Python bindings (pyslang), so the first build takes several minutes.

```bash
git clone <repository-url> papercuts
cd papercuts

# The CMake presets expect the virtual environment at .venv in the repo
python3 -m venv .venv
source .venv/bin/activate
pip install nanobind nanobind-backend z3-solver pyosys

# Configure and build (set CXX first if your default compiler is too old)
cmake --preset Release
cmake --build build -j 8

# Make `papercuts` and `pyslang` importable from the source tree
echo "$PWD/python" > "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/papercuts-dev.pth"

# Check the install
python python/papercuts/tests/run_tests.py   # expect PASSED: N/N (every test)
```

The build writes the compiled modules into `python/` itself, so after a C++
change `cmake --build build` is all it takes; Python edits are live.

**Do not `pip install -e .`** Its editable-install import hook shadows the
modules the build writes into `python/` and breaks `import pyslang` after the
next rebuild. If it happens: `pip uninstall -y papercuts` and redo the
`papercuts-dev.pth` line above.

## Usage

### Basic Command

```bash
python -m papercuts <input_files.sv> [options]
```

Results are written to `./outputs` in the current directory (replacing any
previous run's), so run from a scratch directory.

### Options

- `input_files` - Path to the input SystemVerilog files (required)
- `-m, --mux-rewrites` - Also produce muxed versions of designs
- `-e, --check-equivalence` - Run formal equivalence checks (requires JasperGold)
- `--in-situ` - Cut the original source in place (see below)
- `--top NAME` - Name the design top, so `--in-situ` needs no elaboration at all
- `--method {fv,cegis-comb}` - How passing cuts are chosen (see below); both need `-e`

### CEGIS cut selection

```bash
python -m papercuts design.sv -e --method cegis-comb
```

The default method (`fv`) checks every cut with JasperGold, then consolidates
each module's winners. `cegis-comb` instead muxes every cut in (it implies `-m`)
and runs a CEGIS loop over a golden-vs-muxed miter to find the largest set of
cuts that, together, leave the logic between the registers unchanged: if all
matched registers are equal, the outputs and next states are equal. That is
sound but stricter than sequential equivalence, so cuts that are valid only
through reachability or cancellation are left out.

JasperGold is still required: it runs the same elaboration gates as `fv`, and
checks the whole consolidated design once at the end. That check is joint
because the selected set is only valid jointly, and a failure is fatal.

The netlists come from Yosys: the `yosys` binary runs the yosys-slang front end
and writes RTLIL, and pyosys (`pip install pyosys`) turns that into BTOR2.

In `papercuts.log`, `set` marks membership of the selected set and `valid` marks
whether the cut holds on its own. Neither implies the other.

Options:

- `--cegis-solver STRATEGY` - `auto` (default: `yices-smt2`, else `bitwuzla`,
  else a z3 tactic pipeline), `ext:BINARY`, `tactic:a,b,c`, or `qfbv`
- `--cegis-timeout SECONDS` - Limit per solver call; running out is an error.
  Default: no limit
- `--cegis-ignore-initial` - Build the netlists with `read_slang --ignore-initial`,
  for designs whose initial blocks it rejects
- `--no-cegis-individual` - Skip checking each cut on its own (so `valid` is not
  reported)

`--iterative-bitshrink` is not available under this method.

### In-situ mode

By default papercuts elaborates the design first: parameters are concretized,
generate loops unrolled, and a module instantiated N times becomes N specialized
copies, each cut independently. `--in-situ` skips all of that and cuts the
original parameterized source, so the outputs are edits you can apply to the
design as written.

Because a definition stays shared by all its instances, every cut must hold for
each instantiation. The equivalence check enforces that for free -- it elaborates
at the design top either way -- so in-situ produces fewer but directly
source-actionable cuts.

Bit-shrink still works on parameterized ranges (`logic [WIDTH-1:0] x;` becomes
`logic [(WIDTH-1)-1:0] x;`). Syntax alone does not say how wide such a range is
or which way it runs, and both matter -- a reversed range is legal SV, so
narrowing the wrong end widens the signal instead of erroring. A read-only
elaboration supplies the bounds each range really evaluates to; it only reads the
design and never rewrites it. Passing `--top` makes it optional, so a design that
cannot be elaborated at all is still cuttable -- parameterized bit-shrinks are
then skipped and the other cut families are unaffected.

## Output

The tool generates:
- Modified SystemVerilog files for each module and submodule
- New source file with consolidated passing rewrites (when `-e` is used)

## Acknowledgments

Built on the [slang](https://github.com/MikePopoloski/slang) SystemVerilog parser by Mike Popoloski.