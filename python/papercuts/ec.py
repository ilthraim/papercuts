from __future__ import annotations
import asyncio
import os
import re
from typing import Dict, List, Tuple, TYPE_CHECKING
import pyslang

import papercuts.utils as pc_core
from papercuts.utils import clock_list, print_tree


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
# def generate_jasper_files(run: pc_core.Run, output_dir: str = ".") -> None:
#     """
#     Generate SystemVerilog wrapper and TCL script files for formal verification.

#     Args:
#         run: pc_core.Run object containing module information
#     """

#     # wrapper_str = generate_jasper_wrapper(
#     #     module1_str=print_tree(run.input_tree),
#     #     module2_str=print_tree(run.output_tree),
#     # )

#     try:
#         tcl_script = generate_jasper_tcl_script(f"{run.mod_fname}_wrapper")
#         run.wrapper_fname = f"{run.mod_fname}_wrapper"
#         with open(f"{output_dir}/{run.wrapper_fname}.tcl", "w") as fout:
#             fout.write(tcl_script)
#         with open(f"{output_dir}/{run.wrapper_fname}.sv", "w") as fout:
#             fout.write(wrapper_str)
#     except Exception as e:
#         print(f"Error generating files for {run.mod_fname}: {e}")


# MARK: Jasper TCL
#: Filename of the TCL script the Jasper backend writes and `jg` is pointed at.
#: Written to (and invoked from) the current working directory.
TCL_SCRIPT_NAME = "pcjg.tcl"
#: The same check with no reset (see ECBackend.check_from_power_up).
TCL_POWER_UP_SCRIPT_NAME = "pcjg_power_up.tcl"


def generate_jasper_tcl_script_old(wrapper_name: str) -> str:
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

def generate_jasper_tcl_script(clock: "list[str] | None" = None,
                               reset: "str | None" = None,
                               bbox_array: "int | None" = None,
                               engines: int = 0) -> str:
    """Render the SEC script, optionally declaring a clock and a reset.

    ``clock`` is the list of top-level clock signals and ``reset`` a top-level
    expression, emitted verbatim as Jasper's ``clock``/``reset`` commands. All
    clocks go in one ``clock c1 c2 ...``: declared together, they run at the same
    rate and in phase. An undeclared clock is a plain input that changes at most
    once per declared cycle, so logic needing it faster is never exercised and
    cuts there "prove" (rs_decoder, ssp_uart). Both default to None, which keeps
    the clock-free ``clock -none`` / ``reset -none`` setup.

    Every check elaborates the design top, named with ``elaborate -top`` in both
    compile contexts, so a single pair of top-level names is correct for every
    run -- per-cut, consolidated, and the two load-bearing gates alike. Left to
    itself Jasper picks the first module nothing instantiates, and the spec
    context reads whole override files: an in-situ joint file keeps the modules
    the top never instantiates, so Jasper elaborated one of those (ch_intrinsics:
    DFF) and the check errored.

    ``bbox_array`` raises Jasper's array black-box limit (``elaborate -bbox_a``).
    Jasper black-boxes arrays above a small default size and compares every
    write into the box, read or not, which is stricter than equivalence (hls
    covariance, ISSUES.md 81); a higher limit models them instead, at a large
    memory cost on array-heavy designs (koios, ISSUES.md 93), so it is per
    design, not the default.

    ``engines`` caps the proof-engine jobs of one check
    (``set_proofgrid_max_local_jobs``; 0 = the whole portfolio, Jasper's own
    default, stated explicitly), always at one job per engine. Both settings are
    written so the load of a check is set here, not by Jasper's defaults.
    """
    tcl_script = "# TCL script for formal verification of wrapper module\n"
    tcl_script += """\n
#Arguments are : (5) top_module_path, (6) spec_lib_path, (7) imp_module_path, (8) is_top,
#                (9) a Tcl file of mapping commands, or "-"
#                (10) check_sec -prove -time_limit, or "-"  (11) -cex_limit, or "-"
#                (12) the top module name, elaborated in both contexts

set is_top [lindex $argv 8]
set top [lindex $argv 12]

if {[catch {
    clear -all

    check_sec -compile_context spec
    if {$is_top eq "True"} {
        analyze -sv17 -y [lindex $argv 6] [lindex $argv 7] +libext+.sv
    } else {
        # The module under test is a plain source, not a -v library file: a
        # module instantiated by one that -y pulled in resolves through -y,
        # so the golden copy shadowed a -v cut and the check proved golden
        # against golden (every module 2+ levels below the top).
        analyze -sv17 [lindex $argv 7] -y [lindex $argv 6] [lindex $argv 5] +libext+.sv
    }
    elaborate -top $top -bbox_mul 64 -bbox_div 64 -bbox_mod 64 BBOX_A
    # Analyze and elaborate the implementation design
    check_sec -compile_context imp
    analyze -sv -y [lindex $argv 6] [lindex $argv 5] +libext+.sv
    elaborate -top $top -bbox_mul 64 -bbox_div 64 -bbox_mod 64 BBOX_A
    # Setup verification environment
"""
    # A reset expression routinely contains spaces (e.g. "rst == 1'b1"), which
    # TCL would otherwise split into separate arguments; braces make it one
    # argument and suppress TCL substitution of any $ or [ inside the SV
    # expression. A bare signal name is unaffected by the bracing.
    tcl_script += f"    reset {'{' + reset + '}' if reset else '-none'}\n"
    clocks = clock_list(clock)
    tcl_script += f"    clock {' '.join(clocks) if clocks else '-none'}\n"
    tcl_script += """    check_sec -setup
    check_sec -auto_map_reset_x_values on
    # Names the candidate changed (Run.renames); auto-mapping re-runs with them
    if {[lindex $argv 9] ne "-"} {
        source [lindex $argv 9]
        check_sec -map -auto
    }
    report -summary
    check_sec -interface
    # A cut that gives an uninitialized flop (or one bit of it) a constant input
    # makes Jasper treat that flop as a constant from power-up and leave it out of
    # the reset_x auto-mapping, so the imp's copy starts free and the check fails
    # on a power-up value. Pair every imp reset_x signal still unmapped with the
    # spec signal of the same name and width, as auto-mapping does for the rest
    # (and as CEGIS's power-up model does). One that the spec does not have stays
    # unmapped: it really is one-sided. (hls correlation, ISSUES.md 102)
    set imp_top "[check_sec -get_imp_top_instance]."
    set init_maps 0
    foreach i [check_sec -interface -unmapped -imp -signal_type reset_x] {
        if {[string first $imp_top $i] != 0} { error "unmapped imp signal outside $imp_top: $i" }
        set s [string range $i [string length $imp_top] end]
        if {[catch {get_signal_info -width $s} spec_width]} { continue }
        if {$spec_width != [get_signal_info -width $i]} { continue }
        check_sec -map -init -spec $s -imp $i
        puts "__PC_INIT_MAP__:$s"
        incr init_maps
    }
    if {$init_maps} { check_sec -interface }
    # Run proof and check results
    set_proofgrid_per_engine_max_local_jobs 1
    set_proofgrid_max_local_jobs ENGINES
    set prove_limits {}
    if {[lindex $argv 10] ni {"" "-"}} { lappend prove_limits -time_limit [lindex $argv 10] }
    if {[lindex $argv 11] ni {"" "-"}} { lappend prove_limits -cex_limit [lindex $argv 11] }
    set res [check_sec -prove -strategy proof {*}$prove_limits]
    report -summary
    #do_sum
    # prove -wait

    # Emit a machine-readable verdict marker so the runner can record WHY a check
    # failed (disproven vs. inconclusive), not just pass/fail. $res is whatever
    # check_sec reports (e.g. proven / cex / inconclusive); the runner normalizes it.
    puts "__PC_VERDICT__:$res"

    if {$res eq "proven"} {
            exit 0
        } else {
            exit 1
        }

} err]} {
    puts "__PC_VERDICT__:error"
    puts "Error during formal verification: $err"
    exit 1
}"""

    bbox = f" -bbox_a {bbox_array}" if bbox_array else ""
    if engines and engines < 4:
        raise ValueError("--jasper-engines: the proof strategy needs at least 4 (0 = no cap)")
    return tcl_script.replace(" BBOX_A", bbox).replace("ENGINES", str(engines))

# Verdict marker printed by the generated TCL; see generate_jasper_tcl_script.
_VERDICT_MARKER = "__PC_VERDICT__:"


def _parse_verdict(output: str, returncode: int) -> str:
    """Normalize the tool's proof verdict into a small, stable vocabulary.

    Returns one of "proven" | "cex" | "inconclusive" | "error". The TCL prints
    ``__PC_VERDICT__:<res>`` (or ``:error`` on a caught exception); if the marker
    is absent (e.g. the tool was killed before it printed), fall back to the exit
    code so a crashed/aborted run is still recorded as an error rather than lost.

    The vocabulary the prover reports is wider than it first appears. A proof
    only returns "proven" when *every* property has that same status; a run that
    completed with a mix (i.e. at least one refutation) returns "determined"
    instead. Reading only "cex" therefore files ordinary refuted cuts under
    "error", which reads as a broken environment rather than a working check
    that said no. Anything genuinely unrecognized still falls through to "error".

    "cex_threshold_reached" is the same story one level down: it is what
    ``check_sec -prove -cex_limit N`` returns when it abandons the run after N
    refutations. Some properties are left unprocessed, so it is not "determined"
    -- but reaching the threshold at all means an output assertion was refuted,
    which settles the cut. Cutpoint helper properties do not count toward the
    threshold, so this cannot fire on a design whose outputs all match.
    """
    raw = None
    for line in output.splitlines():
        if line.startswith(_VERDICT_MARKER):
            raw = line[len(_VERDICT_MARKER):].strip().lower()
    if raw is None:
        return "proven" if returncode == 0 else "error"
    if raw in ("proven", "marked_proven"):
        return "proven"
    # Fully decided, and not all-proven => at least one property was refuted.
    if raw in ("cex", "ar_cex", "determined", "ar_determined",
               "falsified", "disproven", "not_proven", "not-proven",
               "cex_threshold_reached"):
        return "cex"
    # Ran, but did not decide everything -- the cut is unusable either way, and
    # the distinction from a refutation is worth keeping (it is a budget/effort
    # problem, not a design difference).
    if raw in ("inconclusive", "undetermined", "unknown", "unprocessed",
               "determined_or_skipped", "time_limit", "per_property_time_limit",
               "max_trace_length"):
        return "inconclusive"
    # Everything else (setup problems and aborts: overconstrained, out_of_memory,
    # aborted, failed, no_properties, stopped_by_user, ...) stays an error.
    return "error"


_INTERFACE_SUMMARY = "Interface Check Summary"
_BBOX_INPUT_UNMAPPED = re.compile(r"^(?:auto|user)_bbox_input\s+- FAILED", re.M)


def _sec_verdict(output: str, returncode: int, jg_log: str) -> str:
    """The verdict of a check, refusing a proof that left a black-box input unmatched.

    A black box's outputs are shared free values and its inputs are what the check
    compares. An input with no partner is compared with nothing, so a cut that
    deletes a memory's only write gets "proven": the read data is free in both
    copies either way (mips_16 data_mem, fv_dataset ISSUES.md 16). Other unmapped
    signals (reset_x, X sources) stay independent free values, which only makes a
    proof harder, so they are left alone.

    The summary is read from Jasper's jg.log. The last one counts: the script
    repeats the interface check after pairing reset_x signals. A proof with no
    summary means the log format changed, which would disable this check silently.
    """
    verdict = _parse_verdict(output, returncode)
    if verdict != "proven":
        return verdict
    with open(jg_log, errors="replace") as f:
        log = f.read()
    start = log.rfind(_INTERFACE_SUMMARY)
    if start < 0:
        raise RuntimeError(f"proven, but no interface check summary in {jg_log}")
    return "unmapped" if _BBOX_INPUT_UNMAPPED.search(log, start) else verdict


# MARK: Jasper rendering of a Run
_JASPER_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(\[\d+\])*(\.[A-Za-z_][A-Za-z0-9_$]*(\[\d+\])*)*")


def jasper_map_script(renames) -> str:
    """check_sec commands pairing what the candidate renamed (``Run.renames``).
    The script reads the candidate as Jasper's spec, so a rule maps candidate
    names (-spec) to golden ones (-imp).

    Measured on Jasper 2026.03 (runs/cegis_comb_dataset/ISSUES.md 96):
    `-hierarchy` rules make a renamed instance's contents map by name, its
    uninitialized flops included, once `check_sec -map -auto` re-runs; a renamed
    flop needs `-init` (a plain map is a connection, one free value driving both
    sides). `-init` on a wire only warns and maps nothing. Deeper rules come first
    so an enclosing rule never claims their names. A golden name Jasper may spell
    differently (an escaped one) is left out: unmapped fails the gate loudly where
    a wrong spelling could pair the wrong flop."""
    keep = [r for r in renames if _JASPER_PATH.fullmatch(r.golden)]
    # One command per depth, deepest first: a rule cannot enclose another of the
    # same depth, so order within a command does not matter. One command per rule
    # cost Jasper ~1 s each and grew with the count: opencores pairing's 10966
    # stalled the gate for hours (ISSUES.md 109); as lists, 87 s, same mappings.
    by_depth = {}
    for r in keep:
        if r.kind == "instance":
            by_depth.setdefault(r.candidate.count("."), []).append((r.candidate, r.golden))
    lines = [f"check_sec -map -hierarchy -spec {{{' '.join('{' + s + '}' for s, _ in rs)}}} "
             f"-imp {{{' '.join('{' + i + '}' for _, i in rs)}}}"
             for _, rs in sorted(by_depth.items(), reverse=True)]
    lines += [f"check_sec -map -init -spec {{{r.candidate}}} -imp {{{r.golden}}}" for r in keep if r.kind == "signal"]
    return "\n".join(lines) + "\n" if lines else ""


def jasper_args(run: pc_core.Run, map_path: "str | None") -> list[str]:
    """The script arguments (5)-(12) for `run` (see generate_jasper_tcl_script).

    The script reads the candidate as Jasper's spec and the golden design as its
    imp, from one library: the imp context is `-y lib lib/<top>.sv`, and the spec
    context the candidate's one override as a plain source beside the same `-y
    lib` (with `lib/<top>.sv` too when the override is below the top). That is
    exactly what a Design states, for the designs the pipeline builds; anything
    else is refused rather than approximated. Both contexts elaborate `top`
    by name (12)."""
    g, c = run.golden, run.candidate
    if g.overrides:
        raise ValueError(f"Jasper backend: the golden design must have no overrides, got {g.overrides}")
    if (c.top, c.lib_dir) != (g.top, g.lib_dir):
        raise ValueError(f"Jasper backend: golden and candidate must share top and library, got "
                         f"{(g.top, g.lib_dir)} and {(c.top, c.lib_dir)}")
    if len(c.overrides) > 1:
        raise ValueError(f"Jasper backend: at most one override per candidate, got {c.overrides}")
    top_path = f"{g.lib_dir}/{g.top}.sv"
    definition, impl_path = c.overrides[0] if c.overrides else (c.top, top_path)
    limits = [f"{run.time_limit_s}s" if run.time_limit_s else "-", "1" if run.stop_at_first_cex else "-"]
    return [top_path, g.lib_dir, impl_path, str(definition == c.top), map_path or "-", *limits, g.top]


# MARK: Jasper Runner
def _jg_command() -> str:
    """The Jasper launcher: `jg`, or $PAPERCUTS_JG (e.g. a wrapper that throttles
    concurrent runs on a shared license). It runs inside `csh -c`, so the user's
    .cshrc has already set up Jasper's environment."""
    return os.environ.get("PAPERCUTS_JG", "jg")


async def run_jasper(run: pc_core.Run, print_output: bool = False, script: str = TCL_SCRIPT_NAME) -> str:
    """Run `script` on `run`; set ``run.output`` and return the verdict."""
    project = f"{run.work_dir}/jgproject{run.index}"
    map_path = None
    if run.renames:
        map_text = jasper_map_script(run.renames)
        if map_text:
            os.makedirs(run.work_dir, exist_ok=True)
            map_path = os.path.abspath(f"{run.work_dir}/rename_map{run.index}.tcl")
            with open(map_path, "w") as f:
                f.write(map_text)
    # stdin=DEVNULL detaches jg from our controlling terminal; otherwise it puts
    # the inherited tty into raw mode (ONLCR off) and doesn't restore it, making
    # subsequent status prints "stairstep" across the screen.
    process = await asyncio.create_subprocess_shell(
        f"csh -c '{_jg_command()} -no_gui -proj {project} {script} --- {' '.join(jasper_args(run, map_path))}'",
        stdin=asyncio.subprocess.DEVNULL,
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

    run.output = output
    verdict = _sec_verdict(output, process.returncode, f"{project}/jg.log")
    # The script exits 0 exactly when it printed "proven"; a proof whose tool then
    # failed is not trusted.
    return "error" if verdict == "proven" and process.returncode != 0 else verdict

async def run_jasper_old(run: pc_core.Run, print_output: bool = False):
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