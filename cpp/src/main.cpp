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
        module top;
            logic [7:0] a, b;
            logic [3:0] c, d;

            assign b = a[7] ? a : 8'h00;

            always_comb begin
                if (a[7]) begin
                    a = 8'hFF;
                    {c, d} = a;
                end else begin
                    a = 8'h00;
                    {c, d} = a;
                end
            end

        endmodule
    )");

    slang::ast::Compilation compilation;
    compilation.addSyntaxTree(tree);

    std::cout << "Papercuts C++ build successful!" << std::endl;

    papercuts::BitShrinker BSR(tree);
    papercuts::TernaryRemover TR(tree);
    papercuts::IfRemover IR;
    papercuts::ModuleNameRewriter MNR;
    papercuts::TestRewriter TRW;
    papercuts::ASTPrinter AP;

    std::vector<std::shared_ptr<SyntaxTree>> newTrees = TR.removeAllTernaries();

    for (const auto& newTree : newTrees) {
        std::cout << SyntaxPrinter::printFile(*newTree) << std::endl;
    }

    // auto newTree = papercuts::insertMuxes(tree, true, true, true);
    // std::cout << SyntaxPrinter::printFile(*newTree) << std::endl;

    return 0;
}