# TCL script for formal verification of wrapper module


#Arguments are : (5) top_module_path, (6) spec_lib_path, (7) imp_module_path, (8) is_top

set is_top [lindex $argv 8]

if {[catch {
    clear -all

    check_sec -compile_context spec
    if {$is_top eq "True"} {
        analyze -sv17 -y [lindex $argv 6] [lindex $argv 7] +libext+.sv
    } else {
        analyze -sv17 -v [lindex $argv 7] -y [lindex $argv 6] [lindex $argv 5] +libext+.sv
    }
    elaborate -bbox_mul 64 -bbox_div 64 -bbox_mod 64
    # Analyze and elaborate the implementation design
    check_sec -compile_context imp
    analyze -sv -y [lindex $argv 6] [lindex $argv 5] +libext+.sv
    elaborate -bbox_mul 64 -bbox_div 64 -bbox_mod 64
    # Setup verification environment
    reset -none
    clock -none
    check_sec -setup
    check_sec -auto_map_reset_x_values on
    report -summary
    check_sec -interface
    # Run proof and check results
    set res [check_sec -prove -strategy proof]
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
}