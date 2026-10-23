module poly_wrapper (input logic [7:0] x,
                      input logic pc_sel0, pc_sel1, pc_sel2, pc_sel3, pc_sel4,
                      output logic eq
                      );

    logic [16:0] y;
    logic [16:0] y_orig;

    poly_muxed mod (
        .x(x),
        .pc_sel0(pc_sel0),
        .pc_sel1(pc_sel1),
        .pc_sel2(pc_sel2),
        .pc_sel3(pc_sel3),
        .pc_sel4(pc_sel4),
        .y(y)
    );

    poly orig (
        .x(x),
        .y(y_orig)
    );

    assign eq = (y == y_orig);

    always @(*) begin
        assert (eq) ;
    end

endmodule

module poly_muxed #(
    parameter WIDTH=8)(input logic pc_sel0, pc_sel1, pc_sel2, pc_sel3, pc_sel4,
    input logic[7:0] x,
    output logic[16:0] y
    );
    logic[16:0] z;
    logic[16:0] z_papercuts;
    assign z_papercuts = {!pc_sel2 & z[16], z[15:0]};
	logic[16:0] N;
	logic[16:0] N_papercuts;
	assign N_papercuts = {!pc_sel3 & N[16], N[15:0]};
    logic[16:0] K;
    logic[16:0] K_papercuts;
    assign K_papercuts = {!pc_sel4 & K[16], K[15:0]};
    localparam N_val =256;
    localparam K_val =65535;
  
	assign N =256;
    assign K =65535;
    // polynomial (2^n - x) * x
    always_comb begin
        z = (N_papercuts - x) * x; 
        y =(pc_sel1 | (!pc_sel0 & ( (z_papercuts > K_papercuts)))) ? K_papercuts : z_papercuts; // This is unnecessary
    end
endmodule

module poly #(
    parameter WIDTH=8)(
    input logic [7:0] x,
    output logic [16:0] y
    );

    logic [16:0] z;
	logic [16:0] N;
    logic [16:0] K;

    localparam N_val = 256;
    localparam K_val = 65535;
  
	assign N = 256;
    assign K = 65535;

    // polynomial (2^n - x) * x

    always_comb begin
        z = (N - x) * x;
        y = (z > K) ? K : z;
    end

endmodule
