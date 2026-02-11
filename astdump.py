from sys import argv
from pyslang import pyslang
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

    if isinstance(node, pyslang.ParameterPortListSyntax):
        param: pyslang.ParameterDeclarationSyntax
        for param in node.declarations:
            print(param.declarators)
            print(param.declarators.kind)
            decl: pyslang.DeclaratorSyntax
            for decl in param.declarators:
                print(decl.name)
                print(decl.getLastToken().valueText)

    if isinstance(node, pyslang.ModuleHeaderSyntax):
        print(type(node.parameters))

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


    if isinstance(node, pyslang.Token):
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
        children = list(node)
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


    if isinstance(node, pyslang.Token):
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

    print("children: ", list(node))


    # Process children
    children = list(node)
    for i, child in enumerate(children):
        is_last_child = (i == len(children) - 1)
        print_elaborated_ast_tree(child, indent + 1, child_prefix, is_last_child)

class ElaboratedVisiter:
    def __init__(self):
        pass

    def __call__(self, obj: Union[pyslang.Token, pyslang.SyntaxNode]) -> None:
        print("Visiting: ", obj)
        print("Type: ", type(obj))

def main():

    driver = pyslang.Driver()
    driver.addStandardArgs()

    # Parse command line arguments
    args = " ".join(argv)
    if not driver.parseCommandLine(args, pyslang.CommandLineOptions()):
        return

    # Process options and parse all provided sources
    if not driver.processOptions() or not driver.parseAllSources():
        return

    # Perform elaboration and report all diagnostics
    compilation = driver.createCompilation()
    driver.reportCompilation(compilation, False)

    pyslang.Compilation.getParseDiagnostics

    


    print("Hello from llm-rtl-opt!")
    print("\n" + "="*80)
    print("Loading and parsing Verilog file...")
    print("="*80 + "\n")

    # Load the syntax tree
    sw = pyslang.SyntaxTree.fromFiles(argv[1:])

    # Get the root node
    root = sw.root
    #root = compilation.getRoot()


    print("AST Tree Structure:")
    print("-" * 80)
    print_ast_tree(root)
    print("\n" + "="*80)
    print("AST traversal complete!")
    print("="*80)

    #print(pyslang.SyntaxPrinter.printFile(sw))



if __name__ == "__main__":
    main()