from sys import argv
# from pyslang import syntax, parsing, driver
#import pyslang
import pyslang
from pyslang import parsing, syntax, driver
from pyslang.parsing import Token


from typing import Union



def print_ast_tree(node, indent=0, prefix="", is_last=True):
    """
    Recursively print the AST in a pretty tree format.

    Args:
        node: SyntaxNode to print
        indent: Current indentation level
        prefix: Prefix string for tree drawing
        is_last: Whether this is the last child of its parent
    """

    # if isinstance(node, pyslang.ParameterPortListSyntax):
    #     param: pyslang.ParameterDeclarationSyntax
    #     for param in node.declarations:
    #         print(param.declarators)
    #         print(param.declarators.kind)
    #         decl: pyslang.DeclaratorSyntax
    #         # for decl in param.declarators:
    #         #     print(decl.name)
    #         #     print(decl.getLastToken().valueText)

    # if isinstance(node, pyslang.ModuleHeaderSyntax):
    #     print(type(node.parameters))

    if isinstance(node, pyslang.syntax.DataDeclarationSyntax):
        print(node.modifiers)
        print(node.attributes)
        print("--"*40)
        print(node.declarators.kind)
        #pyslang.IntegerTypeSyntax.
        print("signing", node.type.signing)
        print(node.type.dimensions.kind)
        for decl in node.declarators:
            print(decl, type(decl))

        

    # if isinstance(node, pyslang.DeclaratorSyntax):
    #     print(node.name)
    #     print(len(node.dimensions))
    #     for child in node.dimensions:
    #         print(child)

    # Determine the connector characters
    if indent == 0:
        connector = ""
    else:
        connector = "└── " if is_last else "├── "

    # Print the current node with its kind
    node_info = f"{node.kind, type(node)}"

    # if type(node) == pyslang.ConditionalStatementSyntax:
    #     print(type(node.predicate))
    #     print(node.predicate)
    #     print(type(node.statement))
    #     print(node.statement)

    # if type(node) == pyslang.ConditionalPredicateSyntax:
    #     print(type(node.conditions))
    #     print(node.conditions)

    # if type(node) == pyslang.IfGenerateSyntax:
    #     print(node.block)
    #     print(node.elseClause)

    # if type(node) == pyslang.ConditionalExpressionSyntax:
    #     print(node.predicate)
    #     print(node.left)
    #     print(node.right)
    #node_info = f"{node.kind}"


    if isinstance(node, parsing.Token):
        node_info += f" (Token: {node.rawText})"
    else:
        if hasattr(node, 'getFirstToken'):
            first_token = node.getFirstToken()
            if first_token and hasattr(first_token, 'valueText'):
                token_text = first_token.valueText
                if token_text:
                    node_info += f" [{token_text}]"


    print(f"{prefix}{connector}{node_info}")

    # Calculate the prefix for children
    if indent == 0:
        child_prefix = ""
    else:
        child_prefix = prefix + ("    " if is_last else "│   ")

    # Process children
    try:
        children = list(node) # type: ignore
        for i, child in enumerate(children):
            is_last_child = (i == len(children) - 1)
            print_ast_tree(child, indent + 1, child_prefix, is_last_child)
    except (TypeError, AttributeError):
        pass

def print_elaborated_ast_tree(node, indent=0, prefix="", is_last=True):
    """
    Recursively print the AST in a pretty tree format.

    Args:
        node: SyntaxNode to print
        indent: Current indentation level
        prefix: Prefix string for tree drawing
        is_last: Whether this is the last child of its parent
    """
    # Determine the connector characters
    if indent == 0:
        connector = ""
    else:
        connector = "└── " if is_last else "├── "

    # Print the current node with its kind
    node_info = f"{node.kind, type(node)}"

    # if type(node) == pyslang.ConditionalStatementSyntax:
    #     print(type(node.predicate))
    #     print(node.predicate)
    #     print(type(node.statement))
    #     print(node.statement)

    # if type(node) == pyslang.ConditionalPredicateSyntax:
    #     print(type(node.conditions))
    #     print(node.conditions)

    # if type(node) == pyslang.IfGenerateSyntax:
    #     print(node.block)
    #     print(node.elseClause)

    # if type(node) == pyslang.ConditionalExpressionSyntax:
    #     print(node.predicate)
    #     print(node.left)
    #     print(node.right)
    #node_info = f"{node.kind}"


    if isinstance(node, parsing.Token):
        node_info += f" (Token: {node.rawText})"
    else:
        if hasattr(node, 'getFirstToken'):
            first_token = node.getFirstToken()
            if first_token and hasattr(first_token, 'valueText'):
                token_text = first_token.valueText
                if token_text:
                    node_info += f" [{token_text}]"


    print(f"{prefix}{connector}{node_info}")

    # Calculate the prefix for children
    if indent == 0:
        child_prefix = ""
    else:
        child_prefix = prefix + ("    " if is_last else "│   ")

    print("children: ", list(node)) # type: ignore


    # Process children
    children = list(node) # type: ignore
    for i, child in enumerate(children):
        is_last_child = (i == len(children) - 1)
        print_elaborated_ast_tree(child, indent + 1, child_prefix, is_last_child)

class ElaboratedVisiter:
    def __init__(self):
        pass

    def __call__(self, obj: Union[parsing.Token, syntax.SyntaxNode]) -> None:
        print("Visiting: ", obj)
        print("Type: ", type(obj))

def main():

    d = driver.Driver()
    d.addStandardArgs()

    # Parse command line arguments
    args = " ".join(argv)
    if not d.parseCommandLine(args, driver.CommandLineOptions()):
        return

    # Process options and parse all provided sources
    if not d.processOptions() or not d.parseAllSources():
        return

    # Perform elaboration and report all diagnostics
    compilation = d.createCompilation()
    d.reportCompilation(compilation, False)

    pyslang.ast.Compilation.getParseDiagnostics(compilation)
    #print(pyslang.SyntaxPrinter.printFile(compilation.getSyntaxTrees()[0]))
    print(compilation.isElaborated)
    root = compilation.getRoot()

    # curSym = root.topInstances[0].body
    # newSym = None

    # #evcx = pyslang.EvalContext(root)

    # for item in curSym:
    #     if type(item) == pyslang.GenerateBlockArraySymbol:
    #         iitem = item.entries[0]
    #         for subitem in iitem:
    #             if type(subitem) == pyslang.ContinuousAssignSymbol:
    #                 di = dir(subitem.assignment.left.value)
    #                 for entry in di:
    #                     print(entry, getattr(subitem.assignment.left.value, entry), type(getattr(subitem.assignment.left.value, entry)))
                        #print(subitem.assignment.eval

        # for item in newSym:
        #     print(type(item), item)


    printtree = True

    if printtree:
        print("Hello from llm-rtl-opt!")
        print("\n" + "="*80)
        print("Loading and parsing Verilog file...")
        print("="*80 + "\n")

        # Load the syntax tree
        sw = syntax.SyntaxTree.fromFiles(argv[1:])

        # Get the root node
        root = sw.root
        #root = compilation.getSyntaxTrees()[0].root


        print("AST Tree Structure:")
        print("-" * 80)
        print_ast_tree(root)
        print("\n" + "="*80)
        print("AST traversal complete!")
        print("="*80)

    #print(pyslang.SyntaxPrinter.printFile(sw))



if __name__ == "__main__":
    main()