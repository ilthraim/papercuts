# Papercuts: SystemVerilog Code Rewriting Tool

A tool for automated rewriting and equivalence checking of SystemVerilog designs using the [slang](https://github.com/MikePopoloski/slang) SystemVerilog parser.

## Overview

Papercuts applies various code transformations ("papercuts") to SystemVerilog designs and can optionally verify semantic equivalence using formal equivalence checking. The tool supports multiple rewrite strategies including bit-width reduction, conditional removal, and case branch deletion.

## Features

- **Parameter Concretization**: Automatically resolves parameterized values
- **Expression Reduction**: Simplifies constant expressions
- **Bit-Width Shrinking** (`-s`): Reduces signal bit widths where possible
- **Case Branch Deletion** (`-c`): Removes case statement branches
- **If-Conditional Removal** (`-i`): Eliminates if-statement conditionals
- **Ternary Removal** (`-t`): Removes ternary operators
- **Equivalence Checking** (`-e`): Formal verification using JasperGold
- **Run All** (`--all`): Run all papercuts and check equivalence
- **Run All Without EC** (`--all-no-ec`): Run all papercuts without equivalence checking

## Installation

### Prerequisites

- Python 3.12 or higher
- Git

### Using uv (Recommended)

[uv](https://github.com/astral-sh/uv) is a fast Python package installer and resolver.

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone the repository
git clone <repository-url>
cd rewriting

# Install the package and dependencies
cd papercuts
uv pip install -e .
```

### Using pip

```bash
# Clone the repository
git clone <repository-url>
cd rewriting

# Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Linux/Mac
# or
venv\Scripts\activate  # On Windows

# Install the package and dependencies
cd papercuts
pip install -e .
```

**Note**: The installation will build pyslang from source, which requires:
- CMake 3.15 or higher
- A C++20 compatible compiler
- Adequate build time (may take several minutes)

## Usage

### Basic Command

```bash
python -m papercuts.main <input_file.sv> [options]
```

### Options

- `input_file` - Path to the input SystemVerilog file (required)
- `-s, --shrink-bits` - Apply bit-width shrinking transformations
- `-c, --delete-case-branch` - Delete case statement branches
- `-i, --remove-if-conditionals` - Remove if-statement conditionals
- `-t, --remove-ternary-conditionals` - Remove ternary operators
- `-e, --check-equivalence` - Run formal equivalence checks (requires JasperGold)

### Examples

#### Using uv run (Recommended)

With uv, you can run the tool without explicitly activating a virtual environment:

```bash
# From the papercuts directory
cd papercuts
uv run python main.py your_design.sv -s

# Or run pc_core.py directly
uv run python pc_core.py your_design.sv -s -c

# Apply multiple transformations
uv run python main.py your_design.sv -s -c -i

# With equivalence checking
uv run python main.py your_design.sv -s -c -e
```

#### Using Python directly

After installing with pip or activating your virtual environment:

```bash
# Apply bit-width shrinking
python -m papercuts.main your_design.sv -s

# Apply multiple transformations
python -m papercuts.main your_design.sv -s -c -i

# Apply transformations with equivalence checking
python -m papercuts.main your_design.sv -s -c -e

# Direct script execution
cd papercuts
python pc_core.py your_design.sv -s -c
```

## Output

The tool generates:
- Modified SystemVerilog files for each transformation
- Wrapper files for equivalence checking (when `-e` is used)
- Log files with equivalence check results
- `equivalence_results.txt` summarizing PASS/FAIL status

## Modifying FV Flow

To modify the specific formal verification flow used, replace the marked command in ec.py with the command used in your specific FV flow. By default, papercuts generates the concretized and optimized .sv files in an `outputs` directory, and the FV command will be executed in this directory.

## Project Structure

```
rewriting/
├── papercuts/          # Main Python package
│   ├── pc_core.py     # Core rewriting engine
│   ├── main.py        # CLI entry point
│   ├── concretizer.py # Parameter and expression resolution
│   ├── ec.py          # Equivalence checking interface
│   └── pyproject.toml # Package configuration
├── formal/            # Formal verification scripts
├── verilog_examples/  # Example SystemVerilog files
└── testing/           # Test scripts and results
```

## Dependencies

- **pyslang**: SystemVerilog parser and rewriter (built from source)
- **Python 3.12+**: Required for modern type hints and features

## Acknowledgments

Built on the [slang](https://github.com/MikePopoloski/slang) SystemVerilog parser by Mike Popoloski.
