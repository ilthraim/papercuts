// Sequential test: a saturating accumulator with three papercuts-style selects.
// pc_sel0: drop the saturation compare (cut is WRONG -> must be 0)
// pc_sel1: shrink the top bit of the accumulator register (cut is WRONG in general)
// pc_sel2: force the 'en' gating constant-true (WRONG)
// pc_sel3: remove a redundant "& 1'b1" (RIGHT -> may be 1)
module acc #(parameter W=8)(
    input logic clk, input logic rst, input logic en,
    input logic [W-1:0] d,
    output logic [W-1:0] q);
    initial q = '0;
    logic [W:0] sum;
    always_comb sum = {1'b0, q} + {1'b0, d};
    always_ff @(posedge clk) begin
        if (rst) q <= '0;
        else if (en & 1'b1) q <= sum[W] ? {W{1'b1}} : sum[W-1:0];
    end
endmodule

module acc_muxed #(parameter W=8)(
    input logic pc_sel0, pc_sel1, pc_sel2, pc_sel3,
    input logic clk, input logic rst, input logic en,
    input logic [W-1:0] d,
    output logic [W-1:0] q);
    initial q = '0;
    logic [W:0] sum;
    logic [W-1:0] q_pc;
    assign q_pc = {!pc_sel1 & q[W-1], q[W-2:0]};
    always_comb sum = {1'b0, q_pc} + {1'b0, d};
    always_ff @(posedge clk) begin
        if (rst) q <= '0;
        else if ((pc_sel2 | en) & (pc_sel3 | 1'b1)) q <= (!pc_sel0 & sum[W]) ? {W{1'b1}} : sum[W-1:0];
    end
endmodule

module seq_wrapper(
    input logic clk, input logic rst, input logic en, input logic [7:0] d,
    input logic pc_sel0, pc_sel1, pc_sel2, pc_sel3,
    output logic eq);
    logic [7:0] q, q_orig;
    acc_muxed mod(.pc_sel0, .pc_sel1, .pc_sel2, .pc_sel3, .clk, .rst, .en, .d, .q);
    acc orig(.clk, .rst, .en, .d, .q(q_orig));
    assign eq = (q == q_orig);
    always @(*) assert(eq);
endmodule
