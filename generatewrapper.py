import re
from typing import Dict, List, Tuple, Optional

def generate_wrapper(module1_str: str, module2_str: str, wrapper_name: str = "wrapper") -> str:
    """
    Generate a SystemVerilog wrapper module for formal verification of two modules.
    
    Args:
        module1_str: String containing the first SystemVerilog module
        module2_str: String containing the second SystemVerilog module
        wrapper_name: Optional name for the wrapper module (default: "wrapper")
    
    Returns:
        String containing the wrapper module
    """
    
    def parse_module(module_str: str) -> Tuple[str, List[Dict], List[Dict]]:
        """Parse a SystemVerilog module to extract name, inputs, and outputs."""
        
        # Extract module name
        module_match = re.search(r'module\s+(\w+)', module_str)
        if not module_match:
            raise ValueError("Could not find module name")
        module_name = module_match.group(1)
        
        # Extract port declarations
        # This regex handles both ANSI-style and traditional port declarations
        port_pattern = r'(input|output)\s+(logic\s+)?(\[.*?\])?\s*(\w+)'
        
        inputs = []
        outputs = []
        
        for match in re.finditer(port_pattern, module_str):
            direction = match.group(1)
            logic_keyword = match.group(2) or ""
            width = match.group(3) or ""
            port_name = match.group(4)
            
            port_info = {
                'name': port_name,
                'type': f"{logic_keyword}{width}".strip() or "logic",
                'width': width
            }
            
            if direction == 'input':
                inputs.append(port_info)
            else:
                outputs.append(port_info)
        
        return module_name, inputs, outputs
    
    # Parse both modules
    module1_name, module1_inputs, module1_outputs = parse_module(module1_str)
    module2_name, module2_inputs, module2_outputs = parse_module(module2_str)
    
    # Generate wrapper module
    wrapper = []
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
    port_lines.append(f"    output logic equiv")
    
    # Join ports with commas
    wrapper.append(',\n'.join(port_lines))
    wrapper.append(");\n")
    
    # Instantiate first module
    wrapper.append(f"\n{module1_name} {module1_name}_inst (")
    conn_lines = []
    for inp in module1_inputs:
        conn_lines.append(f"    .{inp['name']}({inp['name']})")
    for out in module1_outputs:
        conn_lines.append(f"    .{out['name']}({module1_name}_{out['name']})")
    wrapper.append(',\n'.join(conn_lines))
    wrapper.append(");\n")
    
    # Instantiate second module
    wrapper.append(f"\n{module2_name} {module2_name}_inst (")
    conn_lines = []
    for inp in module2_inputs:
        conn_lines.append(f"    .{inp['name']}({inp['name']})")
    for out in module2_outputs:
        conn_lines.append(f"    .{out['name']}({module2_name}_{out['name']})")
    wrapper.append(',\n'.join(conn_lines))
    wrapper.append(");\n")
    
    # Generate equivalence check
    if module1_outputs and module2_outputs:
        equiv_checks = []
        
        # Match outputs by name and create equality checks
        for out1 in module1_outputs:
            for out2 in module2_outputs:
                if out1['name'] == out2['name']:
                    equiv_checks.append(f"({module1_name}_{out1['name']} == {module2_name}_{out2['name']})")
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
    
    return '\n'.join(wrapper)


if __name__ == "__main__":
    module1 = """
    module prefix_adder_8bit (
        input logic  [7:0] in_a,
        input logic  [7:0] in_b,
        output logic [8:0] out,
        output logic carry
    );
    // module implementation
    endmodule
    """
    
    module2 = """
    module spec_adder_8bit (
        input logic  [7:0] in_a,
        input logic  [7:0] in_b,
        output logic [8:0] out,
        output logic carry
    );
    // module implementation
    endmodule
    """
    
    wrapper_code = generate_wrapper(module1, module2, "wrapper")
    print(wrapper_code)