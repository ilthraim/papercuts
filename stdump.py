from sys import argv
import pyslang
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

    # Determine the connector characters
    if indent == 0:
        connector = ""
    else:
        connector = "└── " if is_last else "├── "

    # Print the current node with its kind
    node_info = f"{node.kind, type(node)}"

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
        if isinstance(node, pyslang.ParameterSymbol):
            print(node.value.convertToInt())
            print(node.syntax)

        if isinstance(node, pyslang.InstanceSymbol):
            print_ast_tree(node.body, indent + 1, child_prefix, True)
        else:
            children = list(node)
            for i, child in enumerate(children):
                is_last_child = (i == len(children) - 1)
                print_ast_tree(child, indent + 1, child_prefix, is_last_child)
    except (TypeError, AttributeError):
        pass

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

    pyslang.Compilation.getParseDiagnostics(compilation)
    #print(pyslang.SyntaxPrinter.printFile(compilation.getSyntaxTrees()[0]))
    print(compilation.isElaborated)
    print(compilation.isFinalized)
    root = compilation.getRoot()

    print("Hello from llm-rtl-opt!")
    print("\n" + "="*80)
    print("Loading and parsing Verilog file...")
    print("="*80 + "\n")

    # Get the root node
    #root = compilation.getRoot()


    print("AST Tree Structure:")
    print("-" * 80)
    trees = compilation.getSyntaxTrees()
    print("Number of trees:", len(trees))
    print("Trees:", trees)
    instances = root.topInstances
    print("Number of top instances:", len(instances))

    defs = compilation.getDefinitions()
    for defi in defs:
        if isinstance(defi, pyslang.DefinitionSymbol):
            print("Number of instances of definition", defi.name, ":", defi.instanceCount)
            print("Syntax tree at:", type(defi.syntax))
            print("Declaring definition:", defi.declaringDefinition)


    if isinstance(root.topInstances[0].body, pyslang.InstanceBodySymbol):
        print_ast_tree(root.topInstances[0].body)
        print(root.topInstances[0].body.syntax)
    print("\n" + "="*80)
    print("AST traversal complete!")
    print("="*80)

    #print(pyslang.SyntaxPrinter.printFile(sw))



if __name__ == "__main__":
    main()