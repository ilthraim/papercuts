#include "papercuts/papercuts.h"

#include "papercuts/utils.h"
#include <charconv>
#include <iostream>
#include <memory>
#include <span>
#include <stdexcept>
#include <vector>

#include "slang/parsing/TokenKind.h"
#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxKind.h"
#include "slang/syntax/SyntaxNode.h"
#include "slang/syntax/SyntaxTree.h"
#include "slang/util/SmallVector.h"
#include "slang/util/Util.h"

using namespace slang::syntax;
using namespace slang::parsing;

namespace papercuts {
void ModuleNameRewriter::handle(const ModuleHeaderSyntax& node) {
    auto pstring = persistString(this->alloc, this->newName);
    auto newToken = this->makeToken(TokenKind::Identifier, pstring);

    this->replaceToken(node, 2, newToken, true);
}

std::shared_ptr<SyntaxTree> ModuleNameRewriter::renameModule(const std::shared_ptr<SyntaxTree> tree,
                                                             std::string newName) {
    this->newName = newName;
    return this->transform(tree);
}

// MARK: BitShrink
std::vector<std::shared_ptr<SyntaxTree>> BitShrinkRewriter::shrinkBits(const std::shared_ptr<SyntaxTree> tree) {
    widthMap.clear();

    BitShrinkCollector collector;
    widthMap = collector.getFoundNodes(tree);

    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    while (!widthMap.empty()) {
        currentNode = widthMap.begin()->first; // Get the first node in the map to shrink
        nodeToShrink = currentNode->name.valueText();
        newName = std::string(nodeToShrink) + "_papercuts";
        newWidth = widthMap.begin()->second -
                   1; // Get the width of the node and calculate the new width after shrinking
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
        widthMap.erase(currentNode); // Remove the current node from the map after shrinking
    }

    return newTrees;
}

void BitShrinkRewriter::handle(const DeclaratorSyntax& node) {
    if (node.name.valueText() == nodeToShrink) {
        std::cout << "Found node to declare: " << node.kind << std::endl;
        auto& parentDecl = node.parent->as<DataDeclarationSyntax>();
        auto& type = parentDecl.type;

        auto& newDecl = factory.declarator(makeId(persistString(alloc, newName), SingleSpace),
                                           std::span<VariableDimensionSyntax*>{}, nullptr);

        auto declElem = std::span(alloc.emplace<TokenOrSyntax>(&newDecl), size_t{1});
        SeparatedSyntaxList<DeclaratorSyntax> declList(declElem);

        auto& newDataDecl = factory.dataDeclaration(std::span<AttributeInstanceSyntax*>{},
                                                    *deepClone(parentDecl.modifiers, alloc), *deepClone(*type, alloc),
                                                    declList, makeSemicolon());
        insertAfter(parentDecl, newDataDecl);

        std::string oldTriviaText;
        for (const auto& t : parentDecl.getFirstToken().trivia())
            oldTriviaText += t.getRawText();

        std::string assignText = oldTriviaText + "assign " + newName + " = {1'b0, " + std::string(nodeToShrink) + "[" + std::to_string(newWidth - 1) + ":0]}";
        auto& newAssign = parse(assignText);

        insertAfter(parentDecl, newAssign);
    }
}

void BitShrinkRewriter::handle(const IdentifierNameSyntax& node) {
    // Check to see if this is the left side of a declaration
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // If it is, we don't want to replace it
    }

    if (this->nodeToShrink == node.identifier.valueText()) {
        std::cout << "Found node to update: " << node.kind << std::endl;
        replaceToken(node, 0, makeId(persistString(alloc, newName)), true);
    }
}

void BitShrinkRewriter::handle(const IdentifierSelectNameSyntax& node) {
    return;
}

void BitShrinkCollector::handle(const DeclaratorSyntax& node) {
    // I'm not sure if/when we will ever have a DeclaratorSyntax node that isn't a child of a
    // DataDeclarationSyntax node, so lets throw an assert if not
    auto& dataDecl = node.parent->as<DataDeclarationSyntax>();
    auto& type = dataDecl.type;
    if (type->kind == SyntaxKind::LogicType) {
        auto& intType = type->as<IntegerTypeSyntax>();
        if (intType.signing && intType.signing.kind != TokenKind::UnsignedKeyword) {
            std::cout << "Skipping signed logic declaration: " << node.name.valueText() << std::endl;
            return; // Skip this node if it's a signed logic declaration
        }
        auto& dims = intType.dimensions;
        if (dims.size() == 0) {
            std::cout << "Skipping single-bit logic declaration: " << node.name.valueText() << std::endl;
            return; // Skip this node if it's a single-bit logic declaration
        }
        if (dims.size() > 1) {
            std::cout << "Skipping multi-dimensional logic declaration: " << node.name.valueText() << std::endl;
            return; // Skip this node if it's a multi-dimensional logic declaration
        }
        // We only want to look at one-dimensional logic decls, and no wildcard or queue dimensions
        // (idk even what those are)
        auto& dim = dims[0]->specifier->as<RangeDimensionSpecifierSyntax>();
        // We shouldn't ever have bit selection here but in case we do throw an assert
        auto& dimSelect = dim.selector->as<RangeSelectSyntax>();

        // Get the left and right bounds of the range and calculate the width -> should be fine to
        // cast here because (theoretically) concretization should have occurred already
        auto& left = dimSelect.left->as<LiteralExpressionSyntax>();
        auto& right = dimSelect.right->as<LiteralExpressionSyntax>();

        int leftVal, rightVal;
        leftVal = tokenToInt(left.literal);
        rightVal = tokenToInt(right.literal);
        int width = std::abs(leftVal - rightVal) + 1;

        if (width <= 1) {
            std::cout << "Skipping single-bit logic declaration: " << node.name.valueText() << std::endl;
            return; // Skip this node if it's a single-bit logic declaration
        }

        this->widthMap.insert({&node, width}); // Insert the node and its width into the map
    }
}

std::unordered_map<const DeclaratorSyntax*, int> BitShrinkCollector::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->widthMap;
}

// MARK: TernaryRemover
void TernaryRemover::handle(const ConditionalExpressionSyntax& node) {
    if ((this->nodesToChange.find(&node) != nodesToChange.end()) && !this->done) {
        std::cout << "Found ternary operator to remove: " << node.kind << std::endl;
        auto replacement = this->LR ? node.left : node.right;
        this->replace(node, *replacement);
        if (this->LR)
            this->nodesToChange.erase(&node); // Remove the node from the set after promoting both sides
        this->LR = !this->LR;                 // Alternate between replacing with the left and right side of the
                                              // ternary operator
        this->done = true;                    // Set the flag to indicate we've hit this node already
    }
}

std::vector<std::shared_ptr<SyntaxTree>> TernaryRemover::removeTernaries(const std::shared_ptr<SyntaxTree> tree) {
    TernaryCollector visitor;
    auto foundNodes = visitor.getFoundNodes(tree);
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    this->nodesToChange.clear();
    for (const auto& node : foundNodes) {
        this->nodesToChange.insert(node);
    }

    while (!this->nodesToChange.empty()) {
        this->done = false; // Reset the flag for each call to removeTernaries
        newTrees.push_back(this->transform(tree));
    }

    return newTrees;
}

void TernaryCollector::handle(const ConditionalExpressionSyntax& node) {
    this->foundNodes.insert(&node);
    this->visitDefault(node);
}

std::unordered_set<const ConditionalExpressionSyntax*> TernaryCollector::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->foundNodes;
}

// MARK: IfRemover
void IfRemover::handle(const ConditionalStatementSyntax& node) {
    if ((this->nodesToChange.find(&node) != nodesToChange.end()) && !this->done) {
        std::cout << "Found if statement to remove: " << node.kind << std::endl;

        if (this->TF) {
            if (node.elseClause == nullptr) {
                std::cout << "If statement has no else clause, replacing with empty statement" << std::endl;
                this->remove(node);
            }
            else {
                auto replacement = node.elseClause->clause;
                this->replace(node, *replacement);
            }
        }
        else {
            auto replacement = node.statement;
            this->replace(node, *replacement);
        }
        if (this->TF)
            this->nodesToChange.erase(&node); // Remove the node from the set after promoting both branches
        this->TF = !this->TF;                 // Alternate between replacing with the true and false branch of the
                                              // if statement
        this->done = true;                    // Set the flag to indicate we've hit this node already
    }
}

std::vector<std::shared_ptr<SyntaxTree>> IfRemover::removeIfs(const std::shared_ptr<SyntaxTree> tree) {
    IfCollector visitor;
    auto foundNodes = visitor.getFoundNodes(tree);
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    this->nodesToChange.clear();
    for (const auto& node : foundNodes) {
        this->nodesToChange.insert(node);
    }

    while (!this->nodesToChange.empty()) {
        this->done = false; // Reset the flag for each call to removeIfs
        newTrees.push_back(this->transform(tree));
    }

    return newTrees;
}

void IfCollector::handle(const ConditionalStatementSyntax& node) {
    this->foundNodes.insert(&node);
    this->visitDefault(node);
}

std::unordered_set<const ConditionalStatementSyntax*> IfCollector::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->foundNodes;
}

} // namespace papercuts