
module cavlc_top_cavlc_read_levels (
    input logic clk,
    input logic rst_n,
    input logic ena,
    input logic t1s_sel,
    input logic prefix_sel,
    input logic suffix_sel,
    input logic calc_sel,
    input logic[1:0] TrailingOnes,
    input logic[4:0] TotalCoeff,
    input logic[0:15] rbsp,
    input logic[3:0] i,
    output reg[8:0] level_0,
    output reg[8:0] level_1,
    output reg[8:0] level_2,
    output reg[8:0] level_3,
    output reg[8:0] level_4,
    output reg[8:0] level_5,
    output reg[8:0] level_6,
    output reg[8:0] level_7,
    output reg[8:0] level_8,
    output reg[8:0] level_9,
    output reg[8:0] level_10,
    output reg[8:0] level_11,
    output reg[8:0] level_12,
    output reg[8:0] level_13,
    output reg[8:0] level_14,
    output reg[8:0] level_15,
    output reg[4:0] len_comb
);
    reg[0:15] rbsp_internal;
    reg[3:0] level_prefix_comb;
    reg[8:0] level_suffix;
    reg[3:0] level_prefix;
    reg[2:0] suffixLength;
    reg[8:0] level;
    reg[8:0] level_abs;
    reg[8:0] level_code_tmp;
    always @* begin
        if (( ena)) begin
            rbsp_internal <= rbsp;
        end
        else begin
            rbsp_internal <= 32'd65535;
        end
    end
    always @* begin
        if (rbsp_internal[0]) begin
            level_prefix_comb <= 0;
        end
        else if (rbsp_internal[1]) begin
            level_prefix_comb <= 1;
        end
        else if (rbsp_internal[2]) begin
            level_prefix_comb <= 2;
        end
        else if (rbsp_internal[3]) begin
            level_prefix_comb <= 3;
        end
        else if (rbsp_internal[4]) begin
            level_prefix_comb <= 4;
        end
        else if (rbsp_internal[5]) begin
            level_prefix_comb <= 5;
        end
        else if (rbsp_internal[6]) begin
            level_prefix_comb <= 6;
        end
        else if (rbsp_internal[7]) begin
            level_prefix_comb <= 7;
        end
        else if (rbsp_internal[8]) begin
            level_prefix_comb <= 8;
        end
        else if (rbsp_internal[9]) begin
            level_prefix_comb <= 9;
        end
        else if (rbsp_internal[10]) begin
            level_prefix_comb <= 10;
        end
        else if (rbsp_internal[11]) begin
            level_prefix_comb <= 11;
        end
        else if (rbsp_internal[12]) begin
            level_prefix_comb <= 12;
        end
        else if (rbsp_internal[13]) begin
            level_prefix_comb <= 13;
        end
        else if (rbsp_internal[14]) begin
            level_prefix_comb <= 14;
        end
        else if (rbsp_internal[15]) begin
            level_prefix_comb <= 15;
        end
        else begin
            level_prefix_comb <= 32'dx;
        end
    end
    always @(posedge clk or negedge rst_n) begin
        if (!(rst_n)) begin
            level_prefix <= 0;
        end
        else if ((prefix_sel && ena)) begin
            level_prefix <= level_prefix_comb;
        end
    end
    wire logic first_level;
    assign first_level = (i == ((TotalCoeff - TrailingOnes) - 1));
    always @(posedge clk or negedge rst_n) begin
        if (!(rst_n)) begin
            suffixLength <= 0;
        end
        else if ((prefix_sel && ena)) begin
            if ((((TotalCoeff > 10) && (TrailingOnes < 3)) && first_level)) begin
                suffixLength <= 1;
            end
            else if (first_level) begin
                suffixLength <= 0;
            end
            else if (((suffixLength == 0) && (level_abs > 2'b11))) begin
                suffixLength <= 2;
            end
            else if ((suffixLength == 0)) begin
                suffixLength <= 1;
            end
            else if (((level_abs > (2'b11 << (suffixLength - 1'b1))) && (suffixLength < 6))) begin
                suffixLength <= (suffixLength + 1'b1);
            end
        end
    end
    always @* begin
        if (((suffixLength > 0) && (level_prefix <= 14))) begin
            level_suffix <= {3'b0, (rbsp_internal[0:5] >> (3'b110 - suffixLength))};
        end
        else if ((level_prefix == 14)) begin
            level_suffix <= {3'b0, rbsp_internal[0:3]};
        end
        else if ((level_prefix == 15)) begin
            level_suffix <= rbsp_internal[3:11];
        end
        else begin
            level_suffix <= 0;
        end
    end
    always @(posedge clk or negedge rst_n) begin
        if (!(rst_n)) begin
            level_code_tmp <= 0;
        end
        else if ((suffix_sel && ena)) begin
            level_code_tmp <= (((level_prefix << suffixLength) + level_suffix) + (((suffixLength == 0) && (level_prefix == 15)) ? 4'b1111 : 0));
        end
    end
    wire logic[2:0] tmp1;
    wire logic [2:0] tmp1_papercuts;
    assign tmp1_papercuts = {1'b0, tmp1[1:0]};
    assign tmp1 = ((first_level && (TrailingOnes < 3)) ? 2'b10 : 2'b0);
    always @* begin
        if (((level_code_tmp % 2) == 0)) begin
            level <= (((level_code_tmp + tmp1_papercuts) + 2) >> 1);
        end
        else begin
            level <= (((-(level_code_tmp) - tmp1_papercuts) - 1) >> 1);
        end
    end
    wire logic level_abs_refresh;
    assign level_abs_refresh = (calc_sel && ena);
    always @(posedge clk or negedge rst_n) begin
        if (!(rst_n)) begin
            level_abs <= 0;
        end
        else if (level_abs_refresh) begin
            level_abs <= (level[8] ? -(level) : level);
        end
    end
    always @(posedge clk or negedge rst_n) begin
        if (!(rst_n)) begin
            level_0 <= 0;
            level_1 <= 0;
            level_2 <= 0;
            level_3 <= 0;
            level_4 <= 0;
            level_5 <= 0;
            level_6 <= 0;
            level_7 <= 0;
            level_8 <= 0;
            level_9 <= 0;
            level_10 <= 0;
            level_11 <= 0;
            level_12 <= 0;
            level_13 <= 0;
            level_14 <= 0;
            level_15 <= 0;
        end
        else if ((t1s_sel && ena)) begin
            case (i)
                0: begin
                    level_0 <= (rbsp_internal[0] ? -1 : 1);
                end
                1: begin
                    level_1 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_0 <= (rbsp_internal[1] ? -1 : 1);
                    end
                end
                2: begin
                    level_2 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_1 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_0 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                3: begin
                    level_3 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_2 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_1 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                4: begin
                    level_4 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_3 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_2 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                5: begin
                    level_5 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_4 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_3 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                6: begin
                    level_6 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_5 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_4 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                7: begin
                    level_7 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_6 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_5 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                8: begin
                    level_8 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_7 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_6 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                9: begin
                    level_9 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_8 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_7 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                10: begin
                    level_10 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_9 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_8 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                11: begin
                    level_11 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_10 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_9 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                12: begin
                    level_12 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_11 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_10 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                13: begin
                    level_13 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_12 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_11 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                14: begin
                    level_14 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_13 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_12 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
                15: begin
                    level_15 <= (rbsp_internal[0] ? -1 : 1);
                    if (TrailingOnes[1]) begin
                        level_14 <= (rbsp_internal[1] ? -1 : 1);
                    end
                    if ((TrailingOnes == 3)) begin
                        level_13 <= (rbsp_internal[2] ? -1 : 1);
                    end
                end
            endcase
        end
        else if ((calc_sel && ena)) begin
            case (i)
                0: begin
                    level_0 <= level;
                end
                1: begin
                    level_1 <= level;
                end
                2: begin
                    level_2 <= level;
                end
                3: begin
                    level_3 <= level;
                end
                4: begin
                    level_4 <= level;
                end
                5: begin
                    level_5 <= level;
                end
                6: begin
                    level_6 <= level;
                end
                7: begin
                    level_7 <= level;
                end
                8: begin
                    level_8 <= level;
                end
                9: begin
                    level_9 <= level;
                end
                10: begin
                    level_10 <= level;
                end
                11: begin
                    level_11 <= level;
                end
                12: begin
                    level_12 <= level;
                end
                13: begin
                    level_13 <= level;
                end
                14: begin
                    level_14 <= level;
                end
                15: begin
                    level_15 <= level;
                end
            endcase
        end
    end
    always @* begin
        if (t1s_sel) begin
            len_comb <= TrailingOnes;
        end
        else if (prefix_sel) begin
            len_comb <= (level_prefix_comb + 1);
        end
        else if ((( (suffixLength > 0)) && (level_prefix <= 14))) begin
            len_comb <= suffixLength;
        end
        else if (( (level_prefix == 14))) begin
            len_comb <= 4;
        end
        else if (( (level_prefix == 15))) begin
            len_comb <= 12;
        end
        else begin
            len_comb <= 0;
        end
    end
endmodule