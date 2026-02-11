from __future__ import annotations
import pyslang
from dataclasses import dataclass
from typing import Callable, Any, List, Union

from pc_core import Rewrite, RewriteSet, visitor_wrapper

@dataclass
class MuxedRewrite(Rewrite):
    num_selections: int = 1

@dataclass
class MuxedRewriteSet:
    rewrites: List[MuxedRewrite]

    def apply(self, tree) -> pyslang.SyntaxTree:
        """Apply all rewrites to tree using pyslang.rewrite()."""

        #TODO: check for overlapping rewrites

        current_tree = tree

        sel_index = 0
        
        def handler(node, rewriter, r=self):
            matching_rewrites = [rw for rw in r.rewrites if rw.matcher(node)]

            if len(matching_rewrites) > 1:
                print(f"Warning: multiple rewrites match node at offsets {node.sourceRange.start.offset}-{node.sourceRange.end.offset}")
                for rw in matching_rewrites:
                    print(f" - Rewrite: {rw.description}")

                replacement = matching_rewrites[0].get_replacement(node)
                rewriter.replace(node, replacement)
            else:
                for rw in matching_rewrites:
                    replacement = rw.get_replacement(node)
                    rewriter.replace(node, replacement)
            
        new_tree = pyslang.rewrite(current_tree, handler)
        
        return new_tree

    def merge(self, other: MuxedRewriteSet) -> MuxedRewriteSet:
        """Combine with another RewriteSet."""
        return MuxedRewriteSet(rewrites=self.rewrites + other.rewrites)

    
def mux_ternary_conditionals(tree: pyslang.SyntaxTree) -> MuxedRewriteSet:
    """Generate new SyntaxTrees with muxes guarding the ternarys."""
    nodes = []

    def _count_ternary_conditionals(obj: Union[pyslang.Token, pyslang.SyntaxNode], nodes) -> None:
        if isinstance(obj, pyslang.ConditionalExpressionSyntax):
            nodes.append(obj)

    tree.root.visit(visitor_wrapper(_count_ternary_conditionals, nodes))
    print(f"Found {len(nodes)} ternary nodes.")

    if not nodes:
        return MuxedRewriteSet(rewrites=[])

    rewrites = []

    for index in range(len(nodes)):
        def make_matcher(target):
            def matcher(node):
                return node == target
            return matcher
        
        def make_replacement():
            def get_replacement(node, sel_index):
                return pyslang.SyntaxTree.fromText(f"").root
            return get_replacement
        
        rewrites.append(MuxedRewrite(
            start_offset=nodes[index].sourceRange.start.offset,
            end_offset=nodes[index].sourceRange.end.offset,
            replacement_text=nodes[index].left.getFirstToken().rawText,
            matcher=make_matcher(nodes[index]),
            get_replacement=make_replacement(True),
            description=f"Remove ternary conditional at index {index} using 'true' branch"
        ))

        rewrites.append(Rewrite(
            start_offset=nodes[index].sourceRange.start.offset,
            end_offset=nodes[index].sourceRange.end.offset,
            replacement_text=nodes[index].right.getFirstToken().rawText,
            matcher=make_matcher(nodes[index]),
            get_replacement=make_replacement(False),
            description=f"Remove ternary conditional at index {index} using 'false' branch"
        ))

    return RewriteSet(rewrites=rewrites)