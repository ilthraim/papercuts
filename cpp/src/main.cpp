#include "papercuts/papercuts.h"
#include <iostream>
#include <memory>

#include "slang/ast/Compilation.h"
#include "slang/syntax/SyntaxPrinter.h"
#include "slang/syntax/SyntaxTree.h"

using namespace slang::syntax;

int main() {
    // Minimal example: parse a tiny SystemVerilog snippet
    // auto tree = slang::syntax::SyntaxTree::fromText(R"(
    //     module top;
    //         logic [7:0] a, b, c;
    //         static const logic signed x;
    //         logic unsigned q;
    //         assign c = x ? a : b;

    //         always_comb begin
    //             if (x) begin
    //                 a = 8'hFF;
    //             end else begin
    //                 b = 8'h00;
    //             end
    //         end

    //     endmodule
    // )");

    // Minimal example: parse a tiny SystemVerilog snippet
    auto tree = slang::syntax::SyntaxTree::fromText(R"(
//-----------------------------------------------------------------------------
// Copyright 2024 Andrea Miele
// 
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
// 
//     http://www.apache.org/licenses/LICENSE-2.0
// 
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//-----------------------------------------------------------------------------
// fpMultiplier.sv
// IEEE Floating Point multiplier
module fpMultiplier
#(parameter BITS = 32, parameter MANTISSA_BITS = 23, parameter EXPONENT_BITS = 8) // MANTISSA_BITS + EXPONENT_BITS must be equal to BITS - 1 (1 bit is for sign)
(
 input logic[31:0] x,
 input logic[31:0] y,
 output logic[31:0] out
);
localparam exponentBias =127;
localparam maxExponent =127;
localparam minExponent =-126;
localparam minBiasedExponent =1;
localparam maxBiasedExponent =254;
localparam infExponent =128;
localparam infBiasedExponent =255;
localparam zeroOrDenormBiasedExponent =0;
localparam nanMantissa =4194304; // must be different than 0
logic[22:0] xM;
logic[23:0] xMantissa; // includes hidden bit
logic[22:0] yM;
logic[23:0] yMantissa; // includes hidden bit
logic[23:0] normalizedMantissa; // includes hidden bit
logic[23:0] normalizedMantissa2; // includes hidden bit
logic[23:0] zMantissa; // includes hidden bit
logic[47:0] prod; // includes hidden bit * 2
logic[7:0] xE; 
logic[7:0] xExponent; 
logic[7:0] yE; 
logic[7:0] yExponent; 
logic[9:0] tentativeExponent; // includes carry bit and sign bit to handle overflow and underflow
logic[9:0] tentativeExponent2; // includes carry bit and sign bit to handle overflow and underflow
logic[7:0] zExponent; 
logic[5:0] normalizeShiftAmount;
logic[9:0] rightShiftAmount;
logic xS;
logic yS;
logic zSign;
// extra bits for round to nearest
// normalization
logic guardBit;
logic roundBit;
logic stickyBit;
// underflow recovery / right shift
logic guardBit2;
logic roundBit2;
logic stickyBit2;
logic roundFlag;
logic shiftUnderflowFlag;
// input unpacking
assign xM = x[22 : 0];
assign yM = y[22 : 0];
assign xE = x[30 :23];
assign yE = y[30 :23];
assign xS = x[31];
assign yS = y[31];
// output
assign out[31] = zSign;
assign out[30 :23] = zExponent;
assign out[22 : 0] = zMantissa[22 : 0];
assign prod = xMantissa * yMantissa;
// Handle regular and denormal numbers
always_comb
begin: denormOrRegular
	xMantissa[22 : 0] = {xM};
	yMantissa[22 : 0] = {yM};
	if (xE ==0) // x is denormal, set exponent to min
	begin: xDenorm
		xExponent =1;
		xMantissa[23] = 1'b0;					
	end
	else // x is regular, set hidden bit to 1
	begin: xRegular
		xExponent = xE;
		xMantissa[23] = 1'b1;					
	end
	if (yE ==0) // y is denormal, set exponent to min
	begin: yDenorm
		yExponent =1;
		yMantissa[23] = 1'b0;					
	end
	else // y is regular, set hidden bit to 1
	begin: yRegular
		yExponent = yE;
		yMantissa[23] = 1'b1;					
	end
end
localparam MSBN =48;
logic[47:0] MSBIn;
assign MSBIn = prod[47 : 0];
always_comb begin
    normalizeShiftAmount =47;  // Default: all zeros case (max representable value)
    
    // Iterate from LSB to MSB
    // Each time we find a '1', we update the count
    // The last update corresponds to the MSB position
    for (int i = 0; i <48; i++) begin
        if (MSBIn[i]) begin
            normalizeShiftAmount =47 - i;
        end
    end
end
// round-to-nearest extra bits
//assign guardBit = (normalizeShiftAmount != 0) ? ((normalizeShiftAmount > 1) ? 1'b0 : prod[MANTISSA_BITS - 1]) : prod[MANTISSA_BITS];
assign guardBit = (normalizeShiftAmount <=23) ? prod[23 - normalizeShiftAmount] : 1'b0;
assign roundBit = (normalizeShiftAmount <=32'd22) ? prod[32'd22 - normalizeShiftAmount] : 1'b0;
assign stickyBit = prod[21 : 0] != 0;
// normalized mantissa
assign normalizedMantissa = (prod[47 : 0] << normalizeShiftAmount) >>24;
// tentative exponent, add 1 to exponent to "move decimal point one digit to the left" as prod has form DD.ddd....d and will be interpreted as D.Dddd....d
assign tentativeExponent = {2'b00, xExponent} + {2'b00, yExponent} -127 + 1 - normalizeShiftAmount;
assign shiftUnderflowFlag = $signed(tentativeExponent) <1; 
assign rightShiftAmount =1 - tentativeExponent;
assign tentativeExponent2 = shiftUnderflowFlag ? tentativeExponent + rightShiftAmount : tentativeExponent;
assign guardBit2 = (shiftUnderflowFlag) ? (normalizedMantissa >> (rightShiftAmount - 1)) & 1 : guardBit;
assign roundBit2 = (shiftUnderflowFlag) ? rightShiftAmount > 1 ? (normalizedMantissa >> rightShiftAmount - 2) & 1 : guardBit : roundBit;
assign stickyBit2 = stickyBit | (shiftUnderflowFlag ? ((rightShiftAmount > 2) ? (normalizedMantissa >> rightShiftAmount - 3) & 1 : ((rightShiftAmount > 1) ? guardBit : roundBit)) : 0);
// normalized mantissa
assign normalizedMantissa2 = shiftUnderflowFlag ? normalizedMantissa >> rightShiftAmount : normalizedMantissa;
assign roundFlag = guardBit2 && (normalizedMantissa2[0] | roundBit2 | stickyBit2);
always_comb
begin: handleCases
	if (((xE ==255) && (xM != 0)) ||  ((yE ==255) && (yM != 0)))
	begin: NaN
		zSign = 1'b0; 
		zExponent =255;
		zMantissa =33'h400000;
	end
	// if x is infinity
	else if (xE ==255) // xM == 0
	begin: xInf
		if ((yE ==0) && (yM == 0)) // if y is zero return NaN
		begin: infTimesZero
			zSign = 1'b0; 
			zExponent =255;	
			zMantissa =33'h400000;
		end
		else
		begin: xInfRes
			zSign = xS ^ yS;
			zExponent =255;
			zMantissa = 0;
		end
	end
	else if (yE ==255) // if y is infinity
	begin: yInf
		if ((xE ==0) && (xM == 0)) // if x is zero return NaN
		begin: ZeroTimesInf
			zSign = 1'b0;
			zExponent =255;
			zMantissa =33'h400000;
		end
		else
		begin: yInfRes
			zSign = xS ^ yS; 
			zExponent =255;	
			zMantissa = 0;
		end
	end
	else if (((xE ==0) && (xM == 0)) || ((yE ==0) && (yM == 0))) // either x or y are zero
	begin: xZeroOryZero 
		zSign = xS ^ yS;
		zExponent =0;
		zMantissa = 0;
	end
	else // denormal number or regular number
	begin: denormOrRegularAdd
		if(roundFlag == 1'b1)
		begin: doRounding
			zMantissa = (tentativeExponent2 <255) ? normalizedMantissa2 + 1 :24'd0;
			if(normalizedMantissa2 ==24'd16777215) // if carry out after rounding
			begin: roundingCarry
				if(!(tentativeExponent2 ==254 || tentativeExponent2 ==255)) // if not overflow or infinity
				begin: roundingExpPlus1
					zExponent = tentativeExponent2[7 : 0] + 1;
				end
				else
				begin: roundingInf
					zExponent =255;
				end
			end
			else
			begin: roundingNoCarry
				if((tentativeExponent2 ==1) && (normalizedMantissa2[23] == 1'b0)) // denorm or zero
				begin: roundingDenorm
					zExponent =0;
				end
				else
				begin: roundingNumber
					zExponent = (tentativeExponent2 <255) ? tentativeExponent2[7 : 0] :255;
				end
			end
		end
		else
		begin: noRounding
			zMantissa = (tentativeExponent2 <255) ? normalizedMantissa2 :24'd0;
			if((tentativeExponent2 ==1) && (normalizedMantissa2[23] == 1'b0)) // denorm or zero
			begin: noRoundingDenorm
				zExponent =0;	
			end
			else
			begin: noRoundingNumber
				zExponent = (tentativeExponent2 <255) ? tentativeExponent2[7 : 0] :255;
			end
		end
		zSign = xS ^ yS;
	end
end
endmodule
    )");

    slang::ast::Compilation compilation;
    compilation.addSyntaxTree(tree);

    std::cout << "Papercuts C++ build successful!" << std::endl;

    // papercuts::BitShrinker BSR(tree);
    // papercuts::TernaryRemover TR(tree);
    // papercuts::IfRemover IR(tree);
    // papercuts::ModuleNameRewriter MNR;
    // papercuts::TestRewriter TRW;
    // papercuts::ASTPrinter AP;

    // papercuts::Papercutter PC(tree);

    // std::vector<std::shared_ptr<SyntaxTree>> newTrees = PC.cutAll();

    // for (const auto& newTree : newTrees) {
    //     std::cout << SyntaxPrinter::printFile(*newTree) << std::endl;
    // }

    // for (size_t i = 0; i < PC.getCutCount(); i++) {
    //     auto newTree = PC.cutIndex({i});
    //     std::cout << SyntaxPrinter::printFile(*newTree) << std::endl;
    // }

    // std::cout << "Original module name: " << papercuts::getModuleName(tree) << std::endl;
    auto newTree = papercuts::insertMuxes(tree, true, true, true);
    std::cout << SyntaxPrinter::printFile(*newTree) << std::endl;


    return 0;
}