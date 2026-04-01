# Papercuts: SystemVerilog Code Rewriting Tool

A tool for automated rewriting and equivalence checking of SystemVerilog designs using the [slang](https://github.com/MikePopoloski/slang) SystemVerilog parser.

## Overview

Papercuts applies various code transformations ("papercuts") to SystemVerilog designs and can optionally verify semantic equivalence using formal equivalence checking. The tool supports multiple rewrite strategies including bit-width reduction and conditional removal.

## Features

- **Parameter Concretization**: Automatically resolves parameterized values
- **Expression Reduction**: Simplifies constant expressions
- **Mux Insertion** (`-m`): Creates versions of papercut designs with the microdeletions able to be toggled on and off with select bits
- **Equivalence Checking** (`-e`): Formal verification using JasperGold


## Installation

### Prerequisites

- Python 3.13 or higher
- pybind11-stubgen

Everything else should be installed automatically during the make process

```bash
# Create a virtual environment (recommended)
python -m venv .venv
source venv/bin/activate  # On Linux/Mac
# or
venv\Scripts\activate  # On Windows

# Install pybind11-stubgen
pip install pybind11-stubgen

# Clone the repository
git clone <repository-url>

# Install the package and dependencies
cd papercuts
cmake -B build
cmake --build build -j
```

**Note**: The installation will build pyslang from source, which requires:
- CMake 3.20 or higher
- A C++20 compatible compiler
- Adequate build time (may take several minutes)

## Usage

### Basic Command

```bash
cd python
python -m papercuts <input_files.sv> [options]
```

### Options

- `input_files` - Path to the input SystemVerilog files (required)
- `-m, --mux-rewrites` - Also produce muxed versions of designs
- `-e, --check-equivalence` - Run formal equivalence checks (requires JasperGold)
## Output

The tool generates:
- Modified SystemVerilog files for each module and submodule
- New source file with consolidated passing rewrites (when `-e` is used)

## Acknowledgments

Built on the [slang](https://github.com/MikePopoloski/slang) SystemVerilog parser by Mike Popoloski.