from pyslang import pyslang
from dataclasses import dataclass
from typing import List, Tuple

@dataclass
class Rewrite:
    """Represents a single rewrite operation."""
    start_offset: int
    end_offset: int
    original_text: str
    replacement_text: str
    branch: str  # 'true' or 'false'

@dataclass
class TernaryVariant:
    """A source variant with its rewrite history."""
    source: str
    rewrites: List[Rewrite]

def find_all_ternaries(node, results=None):
    """Find all ConditionalExpressionSyntax nodes."""
    if results is None:
        results = []
    
    if node.kind == pyslang.SyntaxKind.ConditionalExpression:
        results.append(node)
    
    try:
        for child in node:
            find_all_ternaries(child, results)
    except TypeError:
        pass
    
    return results

def remove_ternaries(tree, source_text: str) -> List[TernaryVariant]:
    """
    Given a syntax tree and source text, return all variants
    with ternary conditionals replaced.
    
    For n ternaries, returns 2^n variants.
    """
    ternaries = find_all_ternaries(tree.root)
    
    if not ternaries:
        return [TernaryVariant(source=source_text, rewrites=[])]
    
    # Build list of (ternary_node, true_replacement, false_replacement) info
    ternary_info = []
    for t in ternaries:
        t_range = t.sourceRange
        left_range = t.left.sourceRange
        right_range = t.right.sourceRange
        
        ternary_info.append({
            'start': t_range.start.offset,
            'end': t_range.end.offset,
            'original': source_text[t_range.start.offset:t_range.end.offset],
            'true_text': source_text[left_range.start.offset:left_range.end.offset],
            'false_text': source_text[right_range.start.offset:right_range.end.offset],
        })
    
    # Generate all combinations (2^n)
    from itertools import product
    
    variants = []
    for choices in product(['true', 'false'], repeat=len(ternary_info)):
        rewrites = []
        new_source = source_text
        
        # Apply rewrites in reverse order to preserve offsets
        for i in reversed(range(len(ternary_info))):
            info = ternary_info[i]
            branch = choices[i]
            replacement = info['true_text'] if branch == 'true' else info['false_text']
            
            rewrite = Rewrite(
                start_offset=info['start'],
                end_offset=info['end'],
                original_text=info['original'],
                replacement_text=replacement,
                branch=branch
            )
            rewrites.insert(0, rewrite)  # Keep in original order
            
            new_source = new_source[:info['start']] + replacement + new_source[info['end']:]
        
        variants.append(TernaryVariant(source=new_source, rewrites=rewrites))
    
    return variants


# Test it
source = """module test;
    logic a, b, c, d;
    assign a = b ? c : d;
endmodule"""

tree = pyslang.SyntaxTree.fromText(source)
variants = remove_ternaries(tree, source)

for i, variant in enumerate(variants):
    print(f"=== Variant {i} ===")
    print(f"Rewrites: {[(r.original_text, '->', r.replacement_text, f'({r.branch})') for r in variant.rewrites]}")
    print(variant.source)
    print()