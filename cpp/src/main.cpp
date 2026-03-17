#include <iostream>
#include "slang/syntax/SyntaxPrinter.h"
#include "slang/syntax/SyntaxTree.h"
#include "slang/ast/Compilation.h"

#include "papercuts/papercuts.h"
#include <memory>

using namespace slang::syntax;

int main() {
    // Minimal example: parse a tiny SystemVerilog snippet
    auto tree = slang::syntax::SyntaxTree::fromText(R"(
        module top;
            logic [7:0] a, b, c;
            static const logic signed x;
            logic unsigned q;
            assign c = x ? a : b;

            always_comb begin
                if (x) begin
                    a = 8'hFF;
                end else begin
                    b = 8'h00;
                end
            end

        endmodule
    )");

    slang::ast::Compilation compilation;
    compilation.addSyntaxTree(tree);

    std::cout << "Papercuts C++ build successful!" << std::endl;
    
    papercuts::BitShrinkRewriter BSR;
    papercuts::TernaryRemover TR;
    papercuts::IfRemover IR;
    papercuts::ModuleNameRewriter MNR;
    papercuts::TestRewriter TRW;
    papercuts::ASTPrinter AP;
    std::vector<std::shared_ptr<SyntaxTree>> newTrees = BSR.shrinkBits(tree);

    for (const auto& newTree : newTrees) {
        std::cout << SyntaxPrinter::printFile(*newTree) << std::endl;
    }

    return 0;
}