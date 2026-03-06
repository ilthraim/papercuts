#pragma once
#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxVisitor.h"

using namespace slang::syntax;

// Your project headers will go here as you build out the C++ side
namespace papercuts {
    class BitShrinkRewriter : public SyntaxRewriter<BitShrinkRewriter> {
    public:
        void handle(const ContinuousAssignSyntax&);
        void handle(const RangeSelectSyntax&);
    };
    int exampleFunction();

} 