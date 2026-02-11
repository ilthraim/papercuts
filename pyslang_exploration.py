from pyslang import pyslang

verilog_code = """
module my_module #(
    parameter WIDTH = 8,
    parameter DEPTH = 16
)(
    input [WIDTH-1:0] data_in,
    output [WIDTH-1:0] data_out
);
    assign data_out = data_in;
endmodule

module top;
    logic [31:0] in_sig;
    logic [31:0] out_sig;
    
    my_module #(.WIDTH(32), .DEPTH(64)) u_my_module (
        .data_in(in_sig),
        .data_out(out_sig)
    );
endmodule
"""

tree = pyslang.SyntaxTree.fromText(verilog_code)
compilation = pyslang.Compilation()
compilation.addSyntaxTree(tree)
root = compilation.getRoot()

# Test 1: SyntaxRewriter
print("=== SyntaxRewriter methods ===")
for attr in dir(pyslang.SyntaxRewriter):
    if not attr.startswith('_'):
        print(f"  {attr}")

# Test 2: CSTJsonMode
print("\n=== CSTJsonMode values ===")
for attr in dir(pyslang.CSTJsonMode):
    if not attr.startswith('_'):
        print(f"  {attr}")

# Test 3: Check to_json with different modes
print("\n=== to_json signature ===")
help(tree.to_json)

# Test 4: Try str() on various objects
print("\n=== str() on symbols ===")
inst = root.topInstances[0]
print(f"str(root): {str(root)}")
print(f"str(inst): {str(inst)}")
print(f"str(inst.body): {str(inst.body)}")

# Test 5: Check TypePrinter
print("\n=== TypePrinter methods ===")
for attr in dir(pyslang.TypePrinter):
    if not attr.startswith('_'):
        print(f"  {attr}")