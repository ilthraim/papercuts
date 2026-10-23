// Sequential test for lane escalation: an 8-bit counter that wraps at 127, so
// its top bit is never set in any reachable state.
// pc_sel0: bitshrink the top bit of q at its read (RIGHT, but only because of
//          the reachability invariant q[7]==0; 1-induction and k-induction with
//          small k cannot see that, PDR can)
// pc_sel1: force-const(1) on 'done' (WRONG at frame 0 -> must be 0)
module ctr #(parameter W=8)(
    input logic clk, input logic rst, input logic en,
    output logic done);
    logic [W-1:0] q;
    initial q = '0;
    assign done = (q == {1'b0, {(W-1){1'b1}}});
    always_ff @(posedge clk) begin
        if (rst) q <= '0;
        else if (en) q <= done ? '0 : q + 1'b1;
    end
endmodule

module ctr_muxed #(parameter W=8)(
    input logic pc_sel0, pc_sel1,
    input logic clk, input logic rst, input logic en,
    output logic done);
    logic [W-1:0] q, q_pc;
    initial q = '0;
    assign q_pc = {!pc_sel0 & q[W-1], q[W-2:0]};
    assign done = pc_sel1 | (q_pc == {1'b0, {(W-1){1'b1}}});
    always_ff @(posedge clk) begin
        if (rst) q <= '0;
        else if (en) q <= done ? '0 : q_pc + 1'b1;
    end
endmodule

module ctr_wrapper(
    input logic clk, input logic rst, input logic en,
    input logic pc_sel0, pc_sel1,
    output logic eq);
    logic done, done_orig;
    ctr_muxed mod(.pc_sel0, .pc_sel1, .clk, .rst, .en, .done);
    ctr orig(.clk, .rst, .en, .done(done_orig));
    assign eq = (done == done_orig);
    always @(*) assert(eq);
endmodule
