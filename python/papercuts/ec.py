from __future__ import annotations
import asyncio
import re
from typing import Dict, List, Tuple, TYPE_CHECKING
import pyslang

import papercuts.pc_core as pc_core
from papercuts.pc_utils import print_tree


# MARK: Jasper Wrapper
def generate_jasper_wrapper(
    module1_str: str, module2_str: str, wrapper_name: str = "wrapper"
) -> str:
    """
    Generate a SystemVerilog wrapper module for formal verification of two modules.

    Args:
        module1_str: String containing the first SystemVerilog module
        module2_str: String containing the second SystemVerilog module
        wrapper_name: Optional name for the wrapper module (default: "wrapper")

    Returns:
        String containing the wrapper module
    """

    def parse_module(module_str: str) -> Tuple[str, List[Dict], List[Dict], List[Dict]]:
        """Parse a SystemVerilog module to extract name, inputs, outputs, and parameters."""

        # Remove single-line comments
        module_str_no_comments = re.sub(r"//.*?$", "", module_str, flags=re.MULTILINE)
        # Remove multi-line comments
        module_str_no_comments = re.sub(r"/\*.*?\*/", "", module_str_no_comments, flags=re.DOTALL)

        # Extract module name
        module_match = re.search(r"module\s+(\w+)", module_str_no_comments)
        if not module_match:
            raise ValueError("Could not find module name")
        module_name = module_match.group(1)

        # Extract parameter declarations
        # Matches: parameter [type] NAME=value
        param_pattern = r"parameter\s+(?:\w+\s+)?(\w+)\s*=\s*([^,\)]+)"
        parameters = []

        for match in re.finditer(param_pattern, module_str_no_comments):
            param_name = match.group(1)
            param_value = match.group(2).strip()

            parameters.append({"name": param_name, "value": param_value})

        # Extract port declarations
        # This regex handles both ANSI-style and traditional port declarations
        # Matches: input/output [wire] [logic] [signed] [width] name
        port_pattern = (
            r"(input|output)\s+(?:(wire)\s+)?(?:(logic)\s+)?(?:(signed)\s+)?(\[.*?\])?\s*(\w+)"
        )

        inputs = []
        outputs = []

        for match in re.finditer(port_pattern, module_str_no_comments):
            direction = match.group(1)
            wire_keyword = match.group(2) or ""
            logic_keyword = match.group(3) or ""
            signed_keyword = match.group(4) or ""
            width = match.group(5) or ""
            port_name = match.group(6)

            # Build type string from wire/logic/signed keywords
            type_parts = []
            if wire_keyword:
                type_parts.append(wire_keyword)
            if logic_keyword:
                type_parts.append(logic_keyword)
            if signed_keyword:
                type_parts.append(signed_keyword)
            if width:
                type_parts.append(width)

            port_info = {
                "name": port_name,
                "type": " ".join(type_parts) if type_parts else "logic",
                "width": width,
            }

            if direction == "input":
                inputs.append(port_info)
            else:
                outputs.append(port_info)

        return module_name, inputs, outputs, parameters

    # Parse both modules
    module1_name, module1_inputs, module1_outputs, module1_params = parse_module(module1_str)
    module2_name, module2_inputs, module2_outputs, module2_params = parse_module(module2_str)

    # Generate wrapper module
    wrapper = []

    # Add parameters to wrapper if they exist
    if module1_params:
        wrapper.append(f"module {wrapper_name} #(")
        param_lines = []
        for param in module1_params:
            param_lines.append(f"    parameter {param['name']} = {param['value']}")
        wrapper.append(",\n".join(param_lines))
        wrapper.append(") (")
    else:
        wrapper.append(f"module {wrapper_name} (")

    # Generate port list
    port_lines = []

    # Add all inputs (assuming both modules have the same inputs)
    for inp in module1_inputs:
        port_lines.append(f"    input {inp['type']} {inp['name']}")

    # Add outputs from both modules with prefixes
    for out in module1_outputs:
        port_lines.append(f"    output {out['type']} {module1_name}_{out['name']}")

    for out in module2_outputs:
        port_lines.append(f"    output {out['type']} {module2_name}_{out['name']}")

    # Add equiv output
    port_lines.append("    output logic equiv")

    # Join ports with commas
    wrapper.append(",\n".join(port_lines))
    wrapper.append(");\n")

    # Instantiate first module
    if module1_params:
        wrapper.append(f"\n{module1_name} #(")
        param_conn_lines = []
        for param in module1_params:
            param_conn_lines.append(f"    .{param['name']}({param['name']})")
        wrapper.append(",\n".join(param_conn_lines))
        wrapper.append(f") {module1_name}_inst (")
    else:
        wrapper.append(f"\n{module1_name} {module1_name}_inst (")

    conn_lines = []
    for inp in module1_inputs:
        conn_lines.append(f"    .{inp['name']}({inp['name']})")
    for out in module1_outputs:
        conn_lines.append(f"    .{out['name']}({module1_name}_{out['name']})")
    wrapper.append(",\n".join(conn_lines))
    wrapper.append(");\n")

    # Instantiate second module
    if module2_params:
        wrapper.append(f"\n{module2_name} #(")
        param_conn_lines = []
        for param in module2_params:
            param_conn_lines.append(f"    .{param['name']}({param['name']})")
        wrapper.append(",\n".join(param_conn_lines))
        wrapper.append(f") {module2_name}_inst (")
    else:
        wrapper.append(f"\n{module2_name} {module2_name}_inst (")
    conn_lines = []
    for inp in module2_inputs:
        conn_lines.append(f"    .{inp['name']}({inp['name']})")
    for out in module2_outputs:
        conn_lines.append(f"    .{out['name']}({module2_name}_{out['name']})")
    wrapper.append(",\n".join(conn_lines))
    wrapper.append(");\n")

    # Generate equivalence check
    if module1_outputs and module2_outputs:
        equiv_checks = []

        # Match outputs by name and create equality checks
        for out1 in module1_outputs:
            for out2 in module2_outputs:
                if out1["name"] == out2["name"]:
                    equiv_checks.append(
                        f"({module1_name}_{out1['name']} == {module2_name}_{out2['name']})"
                    )
                    break

        if equiv_checks:
            wrapper.append(f"\nassign equiv = {' & '.join(equiv_checks)};")
        else:
            wrapper.append("\nassign equiv = 1'b1; // No matching outputs found")
    else:
        wrapper.append("\nassign equiv = 1'b1; // No outputs to compare")

    # Add formal verification block
    wrapper.append("\n\n`ifdef FORMAL")
    wrapper.append("  // Assertion for formal verification")
    wrapper.append("  always @(*) begin")
    wrapper.append("      assert(equiv);")
    wrapper.append("  end")
    wrapper.append("`endif\n")

    wrapper.append("\nendmodule")

    return "\n".join(wrapper)


# MARK: Jasper Files
def generate_jasper_files(run: pc_core.Run, output_dir: str = ".") -> None:
    """
    Generate SystemVerilog wrapper and TCL script files for formal verification.

    Args:
        run: pc_core.Run object containing module information
    """

    wrapper_str = generate_jasper_wrapper(
        module1_str=print_tree(run.input_tree),
        module2_str=print_tree(run.output_tree),
    )

    try:
        tcl_script = generate_jasper_tcl_script(f"{run.mod_fname}_wrapper")
        run.wrapper_fname = f"{run.mod_fname}_wrapper"
        with open(f"{output_dir}/{run.wrapper_fname}.tcl", "w") as fout:
            fout.write(tcl_script)
        with open(f"{output_dir}/{run.wrapper_fname}.sv", "w") as fout:
            fout.write(wrapper_str)
    except Exception as e:
        print(f"Error generating files for {run.mod_fname}: {e}")


# MARK: Jasper TCL
def generate_jasper_tcl_script(wrapper_name: str) -> str:
    """
    Generate a TCL script for formal verification of the wrapper module.

    Args:
        wrapper_name: Name of the wrapper module
    Returns:
        String containing the TCL script
    """

    tcl_script = f"# TCL script for formal verification of {wrapper_name}\n"
    tcl_script += "if {[catch {\n"
    tcl_script += f"    analyze -sv -y . {wrapper_name}.sv +libext+.sv +define+FORMAL\n"
    tcl_script += """
    elaborate -top wrapper  -bbox_mul 64 -bbox_div 64 -bbox_mod 64
    clock -none
    reset -none

    set res [autoprove -all -silent]

    if {$res eq "proven"} {
        exit 0
    } else {
        exit 1
    }

} err]} {
    puts "Error during formal verification: $err"
    exit 1
}"""

    # autoprove -all -dump_trace -dump_trace_type vcd -dump_trace_dir ../traces -silent

    return tcl_script


# MARK: Jasper Runner
async def run_jasper(run: pc_core.Run, print_output: bool = True):
    name = run.wrapper_fname.split("_wrapper")[0]
    process = await asyncio.create_subprocess_shell(
        f"csh -c 'jg -no_gui -tcl {run.wrapper_fname}.tcl -proj ./{name}_jgproject'",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    output = ""

    if process.stdout is not None:
        async for line in process.stdout:
            if print_output:
                print(line.decode(), end="")
            output += line.decode()

    await process.wait()

    run.valid = process.returncode == 0
    run.output = output
    return


# MARK: DPV TCL
def generate_dpv_tcl_script(module1_name: str, module2_name: str) -> str:
    """
    Generate a TCL script for formal verification of the wrapper module.

    Args:
        wrapper_name: Name of the wrapper module
    Returns:
        String containing the TCL script
    """

    tcl_script = f"# set SPEC_TOP      \"{module1_name}\"\n"
    tcl_script = f"# set IMPL_TOP      \"{module2_name}\"\n"
    tcl_script = f"# set SPEC_FILE      \"{module1_name}.sv\"\n"
    tcl_script = f"# set IMPL_FILE      \"{module2_name}.sv\"\n"
    tcl_script += """
# -----------------------------------------------------------------------------
# Compile Specification Design (C++ - Pure Combinational)
# -----------------------------------------------------------------------------
proc compile_spec {} {
    global SPEC_TOP SPEC_FILE
    
    # Create C++ design - no clock/reset for combinational
    create_design -name spec -top $SPEC_TOP
    
    # Analyze the C++ file using cppan
    vcs -sverilog $SPEC_FILE
    
    # Compile the design to generate DFG
    compile_design spec
}

# -----------------------------------------------------------------------------
# Compile Implementation Design (RTL - Pure Combinational, no clock/reset)
# -----------------------------------------------------------------------------
proc compile_impl {} {
    global IMPL_TOP IMPL_FILE
    
    # Create RTL design without clock/reset for pure combinational logic
    create_design -name impl -top $IMPL_TOP
    
    # Analyze the SystemVerilog file
    vcs -sverilog $IMPL_FILE
    
    # Compile the design to generate DFG
    compile_design impl
}

# -----------------------------------------------------------------------------
# User Assumes and Lemmas Procedure
# Defines the mapping between C++ spec and RTL impl designs
# -----------------------------------------------------------------------------
proc setup_equivalence {} {
    # For combinational designs:
    # - C++ model: outputs computed instantly (phase 0 or 1)
    # - RTL combinational: outputs computed instantly (phase 0 or 1)
    # 
    # Using phase 1 formulation (recommended for safety, see UG section 5.1.1)
    
    # Map all inputs by name between spec and impl at phase 1
    map_by_name -inputs -specphase 1 -implphase 1
    
    # Map all outputs by name between spec and impl at phase 1
    # For combinational logic, outputs are available at the same phase as inputs
    map_by_name -outputs -specphase 1 -implphase 1
}

# Register the assumes/lemmas procedure with DPV
set user_assumes_lemmas_procedure "setup_equivalence"

# -----------------------------------------------------------------------------
# Main Execution Flow
# -----------------------------------------------------------------------------

# Step 1: Compile both designs
puts "=== Compiling C++ Specification Design ==="
compile_spec

puts "=== Compiling RTL Implementation Design ==="
compile_impl

# Step 2: Compose the two designs for equivalence checking
puts "=== Composing Designs ==="
compose

# Step 3: Run the proof (non-blocking)
puts "=== Starting Equivalence Proof ==="
solveNB equiv_proof

# Step 4: Wait for proof to complete
puts "=== Waiting for Proof Completion ==="
proofwait

# Step 5: Display results
puts "=== Proof Results ==="
listproof

# proofstatus returns 1 (pass) or 0 (fail)
if {[proofstatus]} {
    puts "__DPV_RESULT__:PASS"
    exit 0
} else {
    puts "__DPV_RESULT__:FAIL"
    exit 1
}
"""

    # autoprove -all -dump_trace -dump_trace_type vcd -dump_trace_dir ../traces -silent

    return tcl_script

# MARK: DPV Runner
async def run_dpv(run: pc_core.Run, print_output: bool = True):
    process = await asyncio.create_subprocess_shell(
        f"csh -c \"vcf -fmode dpv -f {run.wrapper_fname}.tcl\"",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    output = ""

    # Process output asynchronously to avoid blocking and capture results
    if process.stdout is not None:
        async for line in process.stdout:
            if print_output:
                print(line.decode(), end="")
            output += line.decode()

    # Wait for the process to complete and get the return code
    await process.wait()

    run.valid = process.returncode == 0
    run.output = output
    return