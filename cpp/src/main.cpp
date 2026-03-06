#include <iostream>
#include "slang/syntax/SyntaxPrinter.h"
#include "slang/syntax/SyntaxTree.h"
#include "slang/ast/Compilation.h"

#include "papercuts/papercuts.h"

using namespace slang::syntax;

int main() {
    // Minimal example: parse a tiny SystemVerilog snippet
    auto tree = slang::syntax::SyntaxTree::fromText(R"(
        module top;
            logic [7:0] a, b, c;
            assign c = a + b;
        endmodule
    )");

    slang::ast::Compilation compilation;
    compilation.addSyntaxTree(tree);

    std::cout << "Papercuts C++ build successful!" << std::endl;
    
    papercuts::BitShrinkRewriter rewriter;
    std::shared_ptr newTree = rewriter.transform(tree);

    std::cout << SyntaxPrinter::printFile(*newTree);

    return 0;
}