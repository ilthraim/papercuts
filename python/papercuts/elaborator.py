#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Michael Popoloski
# SPDX-License-Identifier: MIT

"""Re-emit SystemVerilog from a live slang elaboration, via pyslang.

Unrolls generate loops, resolves parameters, and makes conversions explicit by
reconstructing source from the *elaborated* design. Works from a live slang
elaboration (via pyslang) rather than a `slang --ast-json` dump: the live AST
keeps every module instance's body individually walkable, whereas the JSON dump
deduplicates identical instance bodies, so a hierarchical reference into the
2nd+ unrolled instance (e.g. stage[1].u_add.sum) is unrecoverable there. Here it
resolves, because pyslang exposes each instance.

Library entry point (used by the papercuts pipeline):
    elaborate(files, *, flatten=True, ignore=(), allow_missing=False) -> str
    elaborate_design(...) -> ElaboratedDesign   # same, plus the top module name

CLI:
    elaborator.py design.sv [more.sv ...] [--flatten] [--ignore MOD] [-o out.sv]

Modes:
  default   generate blocks emit as `if (1) begin : stage_1 ... end`; hierarchical
            references are rewritten to the flattened labels (stage_1.partial).
  --flatten generate blocks are dissolved into module scope with mangled names
            (stage[1].partial -> stage_1_partial); collisions are detected and
            refused.

Any AST construct not handled raises EmitError (fail-hard) so that nothing is
silently dropped.
"""

import argparse
import fnmatch
import sys
from dataclasses import dataclass

import pyslang
from pyslang.ast import Compilation, InstanceSymbol
from pyslang.syntax import SyntaxTree

INDENT = "    "


class EmitError(Exception):
    """Raised when the emitter encounters an AST node it cannot handle."""


UNARY_PREFIX = {
    "Plus": "+", "Minus": "-", "BitwiseNot": "~", "BitwiseAnd": "&",
    "BitwiseOr": "|", "BitwiseXor": "^", "BitwiseNand": "~&", "BitwiseNor": "~|",
    "BitwiseXnor": "~^", "LogicalNot": "!", "Preincrement": "++",
    "Predecrement": "--",
}
UNARY_POSTFIX = {"Postincrement": "++", "Postdecrement": "--"}
BINARY_OPS = {
    "Add": "+", "Subtract": "-", "Multiply": "*", "Divide": "/", "Mod": "%",
    "BinaryAnd": "&", "BinaryOr": "|", "BinaryXor": "^", "BinaryXnor": "~^",
    "Equality": "==", "Inequality": "!=", "CaseEquality": "===",
    "CaseInequality": "!==", "GreaterThanEqual": ">=", "GreaterThan": ">",
    "LessThanEqual": "<=", "LessThan": "<", "WildcardEquality": "==?",
    "WildcardInequality": "!=?", "LogicalAnd": "&&", "LogicalOr": "||",
    "LogicalImplication": "->", "LogicalEquivalence": "<->",
    "LogicalShiftLeft": "<<", "LogicalShiftRight": ">>",
    "ArithmeticShiftLeft": "<<<", "ArithmeticShiftRight": ">>>", "Power": "**",
}
EDGE_PREFIX = {"None": "", "PosEdge": "posedge ", "NegEdge": "negedge ",
               "BothEdges": "edge "}
PROCEDURE_KEYWORD = {
    "Initial": "initial", "Final": "final", "Always": "always",
    "AlwaysComb": "always_comb", "AlwaysLatch": "always_latch",
    "AlwaysFF": "always_ff",
}
DIRECTION = {"In": "input", "Out": "output", "InOut": "inout", "Ref": "ref"}


def kind(node):
    """Enum name of a symbol/expression/statement kind, e.g. 'BinaryOp'."""
    return node.kind.name


def tname(t):
    """Slang type string (same format the JSON emitter consumes)."""
    return str(t)


class Emitter:
    def __init__(self):
        self.lines = []
        self.level = 0
        self.flatten = False
        # Definition names to emit verbatim (from original syntax) instead of
        # specializing per instance. Set from the --ignore CLI option.
        self.ignore = set()
        # Populated by run(): the module names actually emitted verbatim -- the
        # ignored definitions plus their whole subtree (the opaque region).
        self.verbatim = set()
        # symbol.hierarchicalPath -> flattened path ("stage_1.partial" scoped,
        # "stage_1_partial" flattened). Keyed by hierarchicalPath (not id()):
        # pyslang returns a fresh Python wrapper per access, so object identity
        # is unstable, but hierarchicalPath is a stable unique key per symbol.
        self.path_map = {}
        # symbol.hierarchicalPath -> constant value string, for in-generate
        # localparams (genvar + user localparams) inlined at use sites.
        self.param_subst = {}
        # instance.hierarchicalPath -> emitted (specialized) module name. One
        # distinct module is emitted per instance, so a submodule instantiated
        # with different params yields different modules.
        self.module_names = {}
        # Rendered text of the current compound-assignment target, used to emit
        # an LValueReference (slang's stand-in for the lvalue inside "x OP= y").
        self._lvalue = None

    # --- output helpers ------------------------------------------------------

    def emit(self, text=""):
        self.lines.append(INDENT * self.level + text if text else "")

    def result(self):
        return "\n".join(self.lines) + "\n"

    # --- type rendering (mirrors emit_sv_from_ast.py) ------------------------

    def split_type(self, type_str):
        type_str = str(type_str)
        if "$" in type_str:
            prefix, _, suffix = type_str.partition("$")
            return self._simplify_typename(prefix.strip()), suffix.strip()
        return self._simplify_typename(type_str.strip()), ""

    def _simplify_typename(self, s):
        if "}" in s:
            return s[: s.rindex("}") + 1]
        if any(c in s for c in "[] "):
            return s
        if "::" in s:
            return s
        if "." in s:
            return s.rsplit(".", 1)[-1]
        return s

    def decl(self, type_str, name, qualifier=""):
        prefix, suffix = self.split_type(type_str)
        parts = [p for p in (qualifier, prefix) if p]
        head = " ".join(parts)
        return f"{head} {name}{suffix}".strip()

    # --- top-level -----------------------------------------------------------

    def run(self, compilation):
        root = compilation.getRoot()

        # Compilation-unit scoped declarations (packages first).
        for pkg in compilation.getPackages():
            if pkg.name in ("std",):  # builtin
                continue
            self._emit_package(pkg)
            self.emit()

        # One emission unit per instance -- no dedup by definition name. Two
        # instances of the same definition with different parameter values emit
        # two distinct, specialized modules. Each unit is either
        #   ("mod", emitted_name, body)      -- specialized, walked and re-emitted
        #   ("verbatim", def_name, text)     -- ignored def, original source text
        self.module_names = {}
        units = []
        used_names = set()
        verbatim_seen = set()
        for inst in root.topInstances:
            self._collect_bodies(inst, units, used_names, verbatim_seen, top=True)

        # Record the opaque region (ignored defs + their subtrees) so callers can
        # skip cutting everything emitted verbatim, not just the top-level ignores.
        self.verbatim = {u[1] for u in units if u[0] == "verbatim"}

        for i, unit in enumerate(units):
            if unit[0] == "verbatim":
                self.emit(unit[2])
            else:
                self._emit_module(unit[1], unit[2])
            if i != len(units) - 1:
                self.emit()
        return self.result()

    def _collect_bodies(self, inst, units, used_names, verbatim_seen, top=False):
        # An ignored definition is emitted verbatim and forms an opaque boundary:
        # we don't specialize it or anything beneath it. The instance is wired at
        # its site using the original definition name.
        if inst.body.name in self.ignore:
            self.module_names[inst.hierarchicalPath] = inst.body.name
            self._collect_verbatim(inst, units, verbatim_seen)
            return

        emitted_name = self._make_module_name(inst, top, used_names)
        self.module_names[inst.hierarchicalPath] = emitted_name
        body = inst.body
        units.append(("mod", emitted_name, body))
        for m in body:
            if kind(m) == "Instance":
                self._collect_bodies(m, units, used_names, verbatim_seen)
            elif kind(m) in ("GenerateBlockArray", "GenerateBlock"):
                self._collect_from_scope(m, units, used_names, verbatim_seen)

    def _collect_from_scope(self, scope, units, used_names, verbatim_seen):
        for m in scope:
            if kind(m) == "Instance":
                self._collect_bodies(m, units, used_names, verbatim_seen)
            elif kind(m) in ("GenerateBlockArray", "GenerateBlock"):
                self._collect_from_scope(m, units, used_names, verbatim_seen)

    def _collect_verbatim(self, inst, units, verbatim_seen):
        # Emit the ignored module's original definition text, plus the verbatim
        # text of every definition reachable beneath it (deduped by name) so the
        # opaque subtree still compiles -- its instantiations reference children
        # by their original names, which specialized emission would have renamed.
        def_name = inst.body.name
        if def_name in verbatim_seen:
            return
        verbatim_seen.add(def_name)
        definition = inst.body.definition
        units.append(("verbatim", def_name, str(definition.syntax).strip()))
        self._collect_verbatim_children(inst.body, units, verbatim_seen)

    def _collect_verbatim_children(self, scope, units, verbatim_seen):
        for m in scope:
            k = kind(m)
            if k == "Instance":
                child = m.body.name
                if child not in verbatim_seen:
                    verbatim_seen.add(child)
                    units.append(("verbatim", child,
                                  str(m.body.definition.syntax).strip()))
                self._collect_verbatim_children(m.body, units, verbatim_seen)
            elif k in ("GenerateBlockArray", "GenerateBlock"):
                self._collect_verbatim_children(m, units, verbatim_seen)

    def _make_module_name(self, inst, top, used_names):
        # Top instances keep the plain definition name; nested instances are
        # named by their hierarchical path so identical definitions with
        # different parameterizations don't collide.
        base = inst.body.name if top else self._mangle_path(inst.hierarchicalPath)
        name, n = base, 1
        while name in used_names:
            n += 1
            name = f"{base}_{n}"
        used_names.add(name)
        return name

    def _mangle_path(self, hp):
        return hp.replace(".", "_").replace("[", "_").replace("]", "")

    # --- packages ------------------------------------------------------------

    def _emit_package(self, pkg):
        self.emit(f"package {pkg.name};")
        self.level += 1
        for m in pkg:
            k = kind(m)
            if k == "TypeAlias":
                self._emit_typedef(m)
            elif k == "Parameter":
                self.emit(self._parameter_decl(m) + ";")
            elif k == "TransparentMember":
                continue  # enum values, emitted by their typedef
            elif k in ("Genvar",):
                continue
            else:
                raise EmitError(f"unsupported package member kind: {k!r}")
        self.level -= 1
        self.emit("endpackage")

    # --- modules -------------------------------------------------------------

    def _emit_module(self, emitted_name, body):
        name = emitted_name
        members = list(body)

        self.path_map = {}
        gen_sep = "_" if self.flatten else "."
        self._build_path_map(members, "", self.path_map, gen_sep)
        if self.flatten:
            self._check_flatten_collisions(name)

        params = [m for m in members if kind(m) == "Parameter" and m.isPortParam]
        ports = [m for m in members if kind(m) == "Port"]

        if params:
            self.emit(f"module {name} #(")
            self.level += 1
            for i, p in enumerate(params):
                comma = "," if i != len(params) - 1 else ""
                self.emit(self._parameter_decl(p) + comma)
            self.level -= 1
            self.emit(") (" if ports else ") ();")
        elif ports:
            self.emit(f"module {name} (")
        else:
            self.emit(f"module {name} ();")

        if ports:
            self.level += 1
            for i, port in enumerate(ports):
                comma = "," if i != len(ports) - 1 else ""
                self.emit(self._port_decl(port) + comma)
            self.level -= 1
            self.emit(");")

        self.level += 1
        self._emit_body_members(members)
        self.level -= 1
        self.emit("endmodule")

    def _emit_body_members(self, members):
        port_names = {m.name for m in members if kind(m) == "Port"}
        for m in members:
            k = kind(m)
            if k == "Port":
                continue
            if k == "Parameter" and m.isPortParam:
                continue
            if k in ("Net", "Variable") and m.name in port_names:
                continue
            if k == "Genvar":
                continue
            self._emit_member(m)

    def _emit_member(self, m):
        k = kind(m)
        if k == "Parameter":
            self.emit(self._parameter_decl(m) + ";")
        elif k == "Net":
            self._emit_net(m)
        elif k == "Variable":
            self._emit_variable(m)
        elif k == "TypeAlias":
            self._emit_typedef(m)
        elif k == "ContinuousAssign":
            self.emit("assign " + self._assignment_inner(m.assignment) + ";")
        elif k == "ProceduralBlock":
            self._emit_procedural_block(m)
        elif k == "Instance":
            self._emit_instance(m)
        elif k == "GenerateBlockArray":
            self._emit_generate_array(m)
        elif k == "GenerateBlock":
            self._emit_generate_block(m)
        elif k == "Subroutine":
            self._emit_subroutine(m)
        elif k == "UninstantiatedDef":
            self._emit_uninstantiated_def(m)
        elif k == "WildcardImport":
            self.emit(f"import {m.packageName}::*;")
        elif k == "ExplicitImport":
            self.emit(f"import {m.packageName}::{m.importName};")
        elif k in ("TransparentMember", "EmptyMember", "Genvar", "StatementBlock"):
            return
        else:
            raise EmitError(f"unsupported module member kind: {k!r}")

    # --- declarations --------------------------------------------------------

    def _parameter_decl(self, p):
        keyword = "localparam" if p.isLocalParam else "parameter"
        body = self.decl(tname(p.type), p.name, qualifier=keyword)
        body += " = " + self._render_constant(p.value)
        return body

    def _port_decl(self, port):
        qualifier = DIRECTION.get(port.direction.name, "")
        return self.decl(tname(port.type), port.name, qualifier=qualifier)

    def _decl_name(self, sym):
        if self.flatten:
            mapped = self.path_map.get(sym.hierarchicalPath)
            if mapped is not None:
                return mapped
        return sym.name

    def _emit_net(self, net):
        net_type = net.netType.name if hasattr(net, "netType") else "wire"
        body = self.decl(tname(net.type), self._decl_name(net), qualifier=net_type)
        if getattr(net, "initializer", None) is not None:
            body += " = " + self.expr(net.initializer)
        self.emit(body + ";")

    def _emit_variable(self, var):
        body = self.decl(tname(var.type), self._decl_name(var))
        if getattr(var, "initializer", None) is not None:
            body += " = " + self.expr(var.initializer)
        self.emit(body + ";")

    def _emit_typedef(self, alias):
        canonical = alias.canonicalType
        if kind(canonical) == "Enum" or type(canonical).__name__ == "EnumType":
            base = tname(canonical.baseType) if hasattr(canonical, "baseType") else ""
            base_str = (" " + base) if base else ""
            values = []
            for ev in canonical:
                values.append(f"{ev.name} = {self._render_constant(ev.value)}")
            self.emit(f"typedef enum{base_str} {{ {', '.join(values)} }} {alias.name};")
        else:
            target = self._simplify_typename(tname(canonical))
            self.emit(f"typedef {target} {alias.name};")

    # --- instances -----------------------------------------------------------

    def _emit_instance(self, inst):
        body = inst.body
        def_name = self.module_names[inst.hierarchicalPath]
        inst_name = self._decl_name(inst)

        params = [m for m in body if kind(m) == "Parameter" and m.isPortParam]
        conns = inst.portConnections

        if params:
            self.emit(f"{def_name} #(")
            self.level += 1
            for i, p in enumerate(params):
                comma = "," if i != len(params) - 1 else ""
                self.emit(f".{p.name}({self._render_constant(p.value)}){comma}")
            self.level -= 1
            self.emit(f") {inst_name} (")
        else:
            self.emit(f"{def_name} {inst_name} (")

        self.level += 1
        for i, conn in enumerate(conns):
            comma = "," if i != len(conns) - 1 else ""
            pname = conn.port.name
            text = self._port_conn_expr(conn)
            self.emit(f".{pname}({text or ''}){comma}")
        self.level -= 1
        self.emit(");")

    def _emit_uninstantiated_def(self, u):
        # A submodule with no available definition (only reachable when compiled
        # with --allow-missing-modules). It cannot be elaborated, so its port
        # connections and parameter values are unresolved in the AST. We re-emit
        # the instantiation from its source syntax instead. Note: connections
        # are taken verbatim from source, so --flatten renaming is NOT applied to
        # them -- a black box wired to a hoisted/renamed generate signal would
        # need manual fixup (uncommon; black boxes usually sit at module scope).
        parts = [u.definitionName]
        params = self._uninst_params_text(u)
        if params:
            parts.append(params)
        text = str(u.syntax).strip()  # "u_y (.a(din), .b(w), .c(o))"
        if self.flatten:
            mangled = self.path_map.get(u.hierarchicalPath)
            if mangled and text.startswith(u.name):
                # Rewrite only the leading instance-name token so hoisting a
                # black box out of a generate scope doesn't collide with its
                # siblings. (Connection operands stay verbatim -- see note above.)
                text = mangled + text[len(u.name):]
        parts.append(text)
        self.emit(" ".join(parts) + ";")

    def _uninst_params_text(self, u):
        node = getattr(getattr(u, "syntax", None), "parent", None)
        while node is not None and "HierarchyInstantiation" not in type(node).__name__:
            node = getattr(node, "parent", None)
        if node is not None and getattr(node, "parameters", None) is not None:
            return str(node.parameters).strip()
        return ""

    def _port_conn_expr(self, conn):
        ex = conn.expression
        if ex is None:
            return None
        # Output/inout ports serialize as an Assignment of the connected lvalue
        # to an EmptyArgument placeholder; unwrap to the lvalue.
        if kind(ex) == "Assignment":
            return self.expr(ex.left)
        if kind(ex) == "EmptyArgument":
            return None
        return self.expr(ex)

    # --- hierarchical path map ----------------------------------------------

    def _build_path_map(self, members, prefix, out, gen_sep):
        for m in members:
            k = kind(m)
            if k in ("Net", "Variable"):
                out[m.hierarchicalPath] = prefix + m.name
            elif k == "Instance":
                out[m.hierarchicalPath] = prefix + m.name
                self._build_path_map(list(m.body), prefix + m.name + ".",
                                     out, gen_sep)
            elif k == "UninstantiatedDef":
                out[m.hierarchicalPath] = prefix + m.name
            elif k == "GenerateBlockArray":
                for idx, blk in self._array_blocks(m):
                    label = self._gen_block_label(blk, array_name=m.name, index=idx)
                    self._build_path_map(list(blk), prefix + label + gen_sep,
                                         out, gen_sep)
            elif k == "GenerateBlock":
                if getattr(m, "isUninstantiated", False):
                    continue
                label = self._gen_block_label(m)
                self._build_path_map(list(m), prefix + label + gen_sep,
                                     out, gen_sep)

    def _check_flatten_collisions(self, module_name):
        by_name = {}
        for hpath, path in self.path_map.items():
            if "." in path:
                continue
            by_name.setdefault(path, set()).add(hpath)
        collisions = {n: s for n, s in by_name.items() if len(s) > 1}
        if collisions:
            detail = "; ".join(f"{n!r} <- {len(s)} symbols"
                               for n, s in sorted(collisions.items()))
            raise EmitError(
                f"--flatten would create colliding module-scope names in "
                f"{module_name!r}: {detail}. Rename the source symbol(s) or emit "
                f"without --flatten."
            )

    # --- generate ------------------------------------------------------------

    def _array_blocks(self, node):
        """Yield (index, block) for the instantiated GenerateBlock children of a
        GenerateBlockArray. Robust across pyslang versions: iterating the array
        may also yield non-block members (e.g. the loop GenvarSymbol), which we
        skip. The index prefers the block's own array index attribute and falls
        back to a running counter; the same helper is used by both the emitter
        and the path-map builder so the labels always agree."""
        counter = 0
        for child in node:
            if kind(child) != "GenerateBlock":
                continue
            if getattr(child, "isUninstantiated", False):
                continue
            idx = getattr(child, "constructIndex", None)
            if idx is None:
                idx = getattr(child, "arrayIndex", None)
            if idx is None:
                idx = counter
            counter += 1
            yield idx, child

    def _gen_block_label(self, node, array_name=None, index=None):
        if array_name is not None:
            return self._sanitize_label(f"{array_name}[{index}]")
        return self._sanitize_label(node.name or "gen")

    def _emit_generate_array(self, node):
        for idx, blk in self._array_blocks(node):
            self._emit_generate_block(blk, array_name=node.name, index=idx)

    def _emit_generate_block(self, node, array_name=None, index=None):
        if getattr(node, "isUninstantiated", False):
            return
        if not self.flatten:
            label = self._gen_block_label(node, array_name, index)
            self.emit(f"if (1) begin : {label}")
            self.level += 1
        members = list(node)
        registered = []
        for m in members:
            if kind(m) == "Parameter" and m.isLocalParam:
                self.param_subst[m.hierarchicalPath] = self._render_constant(m.value)
                registered.append(m.hierarchicalPath)
        for m in members:
            if kind(m) == "Parameter" and m.isLocalParam:
                continue
            self._emit_member(m)
        for hp in registered:
            del self.param_subst[hp]
        if not self.flatten:
            self.level -= 1
            self.emit("end")

    def _sanitize_label(self, label):
        return label.replace("[", "_").replace("]", "")

    # --- subroutines ---------------------------------------------------------

    def _emit_subroutine(self, node):
        is_func = node.subroutineKind.name == "Function"
        life = {"Automatic": "automatic ", "Static": "static "}.get(
            node.defaultLifetime.name if hasattr(node, "defaultLifetime") else "", "")
        port_list = ", ".join(self._formal_arg(a) for a in node.arguments)
        if is_func:
            ret = self.split_type(tname(node.returnType))[0]
            self.emit(f"function {life}{ret} {node.name}({port_list});")
        else:
            self.emit(f"task {life}{node.name}({port_list});")
        self.level += 1
        self._emit_stmt_body(node.body)
        self.level -= 1
        self.emit("endfunction" if is_func else "endtask")

    def _formal_arg(self, arg):
        direction = DIRECTION.get(arg.direction.name, "input")
        body = self.decl(tname(arg.type), arg.name, qualifier=direction)
        return body

    # --- statements ----------------------------------------------------------

    def _emit_procedural_block(self, node):
        keyword = PROCEDURE_KEYWORD.get(node.procedureKind.name)
        if keyword is None:
            raise EmitError(f"unsupported procedure kind: {node.procedureKind.name!r}")
        body = node.body
        if kind(body) == "Timed":
            timing = self.timing(body.timing)
            self._emit_statement_open(body.stmt, prefix=f"{keyword} {timing} ")
        else:
            self._emit_statement_open(body, prefix=f"{keyword} ")

    def _emit_statement_open(self, stmt, prefix=""):
        if kind(stmt) in ("Block", "List"):
            self.emit(prefix + "begin")
            self.level += 1
            self._emit_stmt_body(stmt)
            self.level -= 1
            self.emit("end")
        elif prefix:
            self.emit(prefix + "begin")
            self.level += 1
            self.statement(stmt)
            self.level -= 1
            self.emit("end")
        else:
            self.statement(stmt)

    def _emit_stmt_body(self, stmt):
        if stmt is None:
            return
        k = kind(stmt)
        if k == "List":
            for s in stmt.list:
                self.statement(s)
        elif k == "Block":
            self._emit_stmt_body(stmt.body)
        else:
            self.statement(stmt)

    def statement(self, stmt):
        handler = getattr(self, f"_stmt_{kind(stmt)}", None)
        if handler is None:
            raise EmitError(f"unsupported statement kind: {kind(stmt)!r}")
        handler(stmt)

    def _stmt_Block(self, stmt):
        self.emit("begin")
        self.level += 1
        self._emit_stmt_body(stmt.body if hasattr(stmt, "body") else stmt)
        self.level -= 1
        self.emit("end")

    def _stmt_List(self, stmt):
        for s in stmt.list:
            self.statement(s)

    def _stmt_Empty(self, stmt):
        self.emit(";")

    def _stmt_ExpressionStatement(self, stmt):
        self.emit(self.expr(stmt.expr) + ";")

    def _stmt_VariableDeclaration(self, stmt):
        var = stmt.symbol
        qualifier = ""
        if getattr(var, "initializer", None) is not None:
            qualifier = {"Automatic": "automatic", "Static": "static"}.get(
                var.lifetime.name if hasattr(var, "lifetime") else "", "")
        d = self.decl(tname(var.type), var.name, qualifier=qualifier)
        if getattr(var, "initializer", None) is not None:
            d += " = " + self.expr(var.initializer)
        self.emit(d + ";")

    def _stmt_Conditional(self, stmt):
        cond = " &&& ".join(self.expr(c.expr) for c in stmt.conditions)
        self._emit_statement_open(stmt.ifTrue, prefix=f"if ({cond}) ")
        if getattr(stmt, "ifFalse", None) is not None:
            self._emit_statement_open(stmt.ifFalse, prefix="else ")

    def _stmt_Case(self, stmt):
        self.emit(f"case ({self.expr(stmt.expr)})")
        self.level += 1
        for item in stmt.items:
            labels = ", ".join(self.expr(e) for e in item.expressions)
            self._emit_statement_open(item.stmt, prefix=f"{labels}: ")
        if getattr(stmt, "defaultCase", None) is not None:
            self._emit_statement_open(stmt.defaultCase, prefix="default: ")
        self.level -= 1
        self.emit("endcase")

    def _stmt_ForLoop(self, stmt):
        inits = ", ".join(self._assignment_inner(i) for i in stmt.initializers)
        stop = self.expr(stmt.stopExpr) if getattr(stmt, "stopExpr", None) else ""
        steps = ", ".join(self.expr(s) for s in stmt.steps)
        self._emit_statement_open(stmt.body, prefix=f"for ({inits}; {stop}; {steps}) ")

    def _stmt_Return(self, stmt):
        if getattr(stmt, "expr", None) is not None:
            self.emit(f"return {self.expr(stmt.expr)};")
        else:
            self.emit("return;")

    def _stmt_Timed(self, stmt):
        self._emit_statement_open(stmt.stmt, prefix=self.timing(stmt.timing) + " ")

    # --- timing --------------------------------------------------------------

    def timing(self, node):
        k = kind(node)
        if k == "SignalEvent":
            return f"@({EDGE_PREFIX.get(node.edge.name, '')}{self.expr(node.expr)})"
        if k == "EventList":
            return "@(" + " or ".join(self._event_inner(e) for e in node.events) + ")"
        if k == "Delay":
            return f"#{self.expr(node.expr)}"
        if k == "ImplicitEvent":
            return "@*"
        raise EmitError(f"unsupported timing control kind: {k!r}")

    def _event_inner(self, node):
        if kind(node) != "SignalEvent":
            raise EmitError(f"unsupported event kind: {kind(node)!r}")
        return f"{EDGE_PREFIX.get(node.edge.name, '')}{self.expr(node.expr)}"

    # --- expressions ---------------------------------------------------------

    def _assignment_inner(self, node):
        left = self.expr(node.left)
        if node.isCompound:
            base = BINARY_OPS.get(node.op.name)
            if base is None:
                raise EmitError(f"unsupported compound assign op: {node.op.name!r}")
            # slang models "x OP= y" as right = BinaryOp(OP, LValueReference, y).
            # Recover y and re-emit the compound operator (preserves it and
            # avoids evaluating the lvalue twice).
            rhs = node.right
            if kind(rhs) == "BinaryOp" and kind(rhs.left) == "LValueReference":
                return f"{left} {base}= {self.expr(rhs.right)}"
            # Fallback: render the LValueReference as the lvalue itself, giving an
            # equivalent plain assignment "x = (x OP y)".
            saved, self._lvalue = self._lvalue, left
            try:
                return f"{left} = {self.expr(rhs)}"
            finally:
                self._lvalue = saved
        op = "<=" if node.isNonBlocking else "="
        return f"{left} {op} {self.expr(node.right)}"

    def expr(self, node):
        if node is None:
            raise EmitError("encountered null expression")
        handler = getattr(self, f"_expr_{kind(node)}", None)
        if handler is None:
            raise EmitError(f"unsupported expression kind: {kind(node)!r}")
        return handler(node)

    def _value_ref(self, sym):
        hp = sym.hierarchicalPath
        if hp in self.param_subst:
            return self.param_subst[hp]
        if self.flatten and hp in self.path_map:
            return self.path_map[hp]
        return sym.name

    def _expr_NamedValue(self, node):
        return self._value_ref(node.symbol)

    def _expr_HierarchicalValue(self, node):
        sym = node.symbol
        hp = sym.hierarchicalPath
        if hp in self.param_subst:
            return self.param_subst[hp]
        mapped = self.path_map.get(hp)
        if mapped is not None:
            return mapped
        raise EmitError(
            f"cannot re-emit hierarchical reference to {sym.name!r} "
            f"({hp}): target outside emitted module scope"
        )

    def _lit(self, node):
        # Regular literals expose a folded .constant; unbased-unsized ('0/'1/'x)
        # leave .constant None but carry the resolved sized .value.
        c = getattr(node, "constant", None)
        if c is not None:
            return str(c)
        return str(node.value)

    def _expr_IntegerLiteral(self, node):
        return self._lit(node)

    def _expr_UnbasedUnsizedIntegerLiteral(self, node):
        return self._lit(node)

    def _expr_RealLiteral(self, node):
        return str(node.constant)

    def _expr_StringLiteral(self, node):
        raw = node.value if hasattr(node, "value") else str(node.constant)
        return '"' + str(raw).replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _expr_UnaryOp(self, node):
        op = node.op.name
        operand = self.expr(node.operand)
        # Increment/decrement require a bare variable_lvalue operand (IEEE 1800
        # §11.4.2); a parenthesized "(i)++" is illegal. The slang operand is
        # always an assignable lvalue, so no precedence parens are needed.
        if op in ("Preincrement", "Predecrement"):
            return f"{UNARY_PREFIX[op]}{operand}"
        if op in UNARY_POSTFIX:
            return f"{operand}{UNARY_POSTFIX[op]}"
        if op in UNARY_PREFIX:
            return f"{UNARY_PREFIX[op]}({operand})"
        raise EmitError(f"unsupported unary op: {op!r}")

    def _expr_BinaryOp(self, node):
        op = BINARY_OPS.get(node.op.name)
        if op is None:
            raise EmitError(f"unsupported binary op: {node.op.name!r}")
        return f"({self.expr(node.left)} {op} {self.expr(node.right)})"

    def _expr_ConditionalOp(self, node):
        cond = " &&& ".join(self.expr(c.expr) for c in node.conditions)
        return f"({cond} ? {self.expr(node.left)} : {self.expr(node.right)})"

    def _expr_Assignment(self, node):
        return self._assignment_inner(node)

    def _expr_Conversion(self, node):
        return self.expr(node.operand)

    def _expr_ElementSelect(self, node):
        return f"{self.expr(node.value)}[{self.expr(node.selector)}]"

    def _expr_RangeSelect(self, node):
        value = self.expr(node.value)
        left = self.expr(node.left)
        right = self.expr(node.right)
        sel = node.selectionKind.name
        if sel == "Simple":
            return f"{value}[{left}:{right}]"
        if sel == "IndexedUp":
            return f"{value}[{left}+:{right}]"
        if sel == "IndexedDown":
            return f"{value}[{left}-:{right}]"
        raise EmitError(f"unsupported range selection kind: {sel!r}")

    def _expr_MemberAccess(self, node):
        return f"{self.expr(node.value)}.{node.member.name}"

    def _expr_Concatenation(self, node):
        return "{" + ", ".join(self.expr(o) for o in node.operands) + "}"

    def _expr_Replication(self, node):
        return "{" + self.expr(node.count) + self.expr(node.concat) + "}"

    def _expr_Call(self, node):
        # subroutineName works for both system calls ("$signed") and regular
        # subroutines; node.subroutine is a SystemCallInfo (no .name) for the
        # former, so don't rely on it.
        name = getattr(node, "subroutineName", None)
        if not name:
            sub = node.subroutine
            name = getattr(sub, "name", None) or str(sub)
        args = ", ".join(self.expr(a) for a in node.arguments)
        return f"{name}({args})"

    def _expr_LValueReference(self, node):
        # Only meaningful inside a compound-assignment RHS; the common case is
        # handled structurally in _assignment_inner, this covers the fallback.
        if self._lvalue is None:
            raise EmitError("LValueReference outside a compound-assignment context")
        return self._lvalue

    def _expr_EmptyArgument(self, node):
        return ""

    def _render_constant(self, value):
        return str(value)


def build_compilation(files, allow_missing=False):
    comp = _new_compilation(allow_missing)
    for path in files:
        comp.addSyntaxTree(SyntaxTree.fromFile(path))
    return comp


def _new_compilation(allow_missing):
    if not allow_missing:
        return Compilation()
    # Ignore instantiations of modules that have no available definition; they
    # become UninstantiatedDefSymbols the emitter re-emits from source syntax.
    try:
        from pyslang.ast import CompilationOptions, CompilationFlags
        opts = CompilationOptions()
        opts.flags = CompilationFlags.IgnoreUnknownModules
        return Compilation(pyslang.Bag([opts]))
    except Exception as e:  # pragma: no cover - version-dependent API
        print(f"Error: could not enable --allow-missing-modules on this pyslang "
              f"build ({type(e).__name__}: {e}).", file=sys.stderr)
        raise


def report_diagnostics(comp):
    diags = comp.getAllDiagnostics()
    client = pyslang.TextDiagnosticClient()
    engine = pyslang.DiagnosticEngine(comp.sourceManager)
    engine.addClient(client)
    errored = False
    for d in diags:
        engine.issue(d)
        errored = errored or d.isError()
    text = client.getString()
    if text.strip():
        print(text, file=sys.stderr)
    return errored


class ElaborationError(Exception):
    """Raised when the input cannot be elaborated (has compilation errors)."""


def _all_definition_names(comp):
    """Definition names of every instantiated module in the compilation.

    Used to resolve ``ignore`` globs to the concrete definition names the
    emitter matches against (``inst.body.name``)."""
    names = set()

    def _v(obj):
        if isinstance(obj, InstanceSymbol):
            names.add(obj.body.name)

    comp.getRoot().visit(_v)
    return names


def _resolve_ignore(comp, patterns):
    """Resolve ``ignore`` fnmatch globs to concrete definition names.

    The emitter's verbatim-boundary check is exact membership on the module
    definition name, but callers (the papercuts ``--exclude-module`` flag) pass
    fnmatch globs like ``lib_*``. Expand them against the definitions actually
    present so a glob and a bare name both work."""
    patterns = list(patterns or ())
    if not patterns:
        return set()
    names = _all_definition_names(comp)
    return {n for n in names if any(fnmatch.fnmatch(n, p) for p in patterns)}


@dataclass
class ElaboratedDesign:
    """Result of :func:`elaborate_design`."""

    source: str            # re-emitted SystemVerilog for the whole design
    top: str               # top module name (empty if there is no top instance)
    tops: list             # all top-instance names (usually exactly one)
    verbatim: set          # module names emitted verbatim (the opaque/uncut region)


def elaborate_design(files, *, flatten=True, ignore=(), allow_missing=False):
    """Elaborate ``files`` and re-emit the whole design as one source blob.

    ``ignore`` accepts fnmatch globs or bare names; matching module definitions
    are emitted verbatim (an opaque boundary) instead of specialized per
    instance. Raises :class:`ElaborationError` on compilation errors and
    ``EmitError`` on an unhandled construct -- callers gate on both.

    Returns an :class:`ElaboratedDesign` carrying the source and the top module
    name (the pipeline needs the top to wire the elaborated-vs-original gate)."""
    comp = build_compilation(files, allow_missing=allow_missing)
    if report_diagnostics(comp):
        raise ElaborationError("input has compilation errors; aborting")

    emitter = Emitter()
    emitter.flatten = flatten
    emitter.ignore = _resolve_ignore(comp, ignore)
    source = emitter.run(comp)

    tops = [inst.name for inst in comp.getRoot().topInstances]
    return ElaboratedDesign(
        source=source,
        top=tops[0] if tops else "",
        tops=tops,
        verbatim=set(emitter.verbatim),
    )


def elaborate(files, *, flatten=True, ignore=(), allow_missing=False) -> str:
    """Elaborate ``files`` and return the re-emitted design as a string.

    Thin wrapper over :func:`elaborate_design` for callers that only need the
    source text (the CLI). Use ``elaborate_design`` when you also need the top
    module name (e.g. to wire the elaborated-vs-original equivalence gate)."""
    return elaborate_design(
        files, flatten=flatten, ignore=ignore, allow_missing=allow_missing
    ).source


def main():
    parser = argparse.ArgumentParser(
        description="Re-emit SystemVerilog from a live slang elaboration (pyslang)"
    )
    parser.add_argument("files", nargs="+", help="SystemVerilog source files")
    parser.add_argument("--output", "-o", help="Output file (default: stdout)")
    parser.add_argument(
        "--flatten", action=argparse.BooleanOptionalAction, default=True,
        help="dissolve generate blocks into module scope with mangled names "
        "(default: on; use --no-flatten to keep scoped `if (1) begin : ...` blocks)",
    )
    parser.add_argument(
        "--ignore", action="append", default=[], metavar="MODULE",
        help="emit this module definition verbatim (original source) instead of "
        "specializing it per instance; forms an opaque boundary (its subtree is "
        "emitted verbatim too). Accepts fnmatch globs (e.g. 'lib_*'). Repeatable.",
    )
    parser.add_argument(
        "--allow-missing-modules", action="store_true",
        help="treat instantiations of undefined modules as black boxes "
        "(re-emitted from source) instead of erroring",
    )
    args = parser.parse_args()

    try:
        output = elaborate(
            args.files,
            flatten=args.flatten,
            ignore=args.ignore,
            allow_missing=args.allow_missing_modules,
        )
    except (ElaborationError, EmitError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
