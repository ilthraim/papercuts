# SPDX-FileCopyrightText: Michael Popoloski
# SPDX-License-Identifier: MIT

import sys

from pyslang import pyslang
#import CommandLineOptions, Driver

from .pc_core import _visitor_wrapper

def print_elaborated_ast_tree(node, depth):
    print("  " * depth + str(node))
    if isinstance(node, pyslang.InstanceSymbol):
        #print(dir(node))
        print(node.portConnections)
        print(node.body)
        print_elaborated_ast_tree(node.body, depth + 1)

    if isinstance(node, pyslang.PortSymbol):
        #print(dir(node))
        print(node.direction)
        print(node.declaredType)
        print(node.internalSymbol)

    if isinstance(node, pyslang.VariableSymbol):
        dtype: pyslang.DeclaredType = node.declaredType
        print(node.declaredType)
        print(dtype.type)
        print(type(dtype.type))
        # print(dtype.initializer)
    #     print(dtype.initializerLocation)
    #     print(dtype.initializerSyntax)
    #     print(dtype.typeSyntax)
    try:
        iter(node)
        for child in node:
            print_elaborated_ast_tree(child, depth + 1)
    except TypeError:
        return
    
        

def main():
    """Reads a list of files as command line arguments and parses them using the slang driver.

    After compilation/elaboration, any diagnostics (e.g., syntax errors) are reported to the console.
    Writes to both stderr and stdout.
    """
    # Create a slang driver with default command line arguments
    driver = pyslang.Driver()
    driver.addStandardArgs()

    # Parse command line arguments
    args = " ".join(sys.argv)
    if not driver.parseCommandLine(args, pyslang.CommandLineOptions()):
        return

    # Process options and parse all provided sources
    if not driver.processOptions() or not driver.parseAllSources():
        return

    # Perform elaboration and report all diagnostics
    driver.runFullCompilation(quiet=False)

    comp = driver.createCompilation()

    def _visit_handler(node):
        print("Visiting node: ", str(node))
        print("Type: ", type(node))
        if isinstance(node, pyslang.InstanceSymbol):
            print("Instance Symbol: ", node.name)
            print("Syntax: ", type(node.syntax))
            print("Body: ", type(node.body))

        if isinstance(node, pyslang.InstanceBodySymbol):
            print("Instance Body Symbol: ", node.name)
            print(node.definition)

        if isinstance(node, pyslang.PortSymbol):
            print("Port Symbol: ", node.name)
            print("Direction: ", node.direction)
            print("Declared Type: ", node.declaredType)
            print(node.syntax)


    comp.getRoot().visit(_visit_handler)



if __name__ == "__main__":
    main()