#!/usr/bin/env python3
"""Static adapter: build a TraceDoc *static spine* from Python source via `ast`.

Complements `python_settrace.py` (dynamic). Where the dynamic adapter captures
real values/identities (→ CoE/CoTm/CoV/CoI), this one reads the source without
running it and captures the static spine (→ CoN/CoT/CoM/CoP):

  * every function/method/module-scope → a TraceSymbol (with params + types);
  * every call site → a TraceStep with `in_symbol` (where it is written),
    `callee` (resolved where possible), `arg_style`, and a TraceToken per
    positional arg (literals flagged `is_literal` → CoM).

Emits `kind: "static"`. Merge with a dynamic doc by `(callee qualname,
site_file, site_line)` to get the full picture.

  python3 python_ast.py path/to/pkg [more.py …] [--module-root SRC] > static.json

Callee resolution (best-effort, no type inference):
  * `f(...)`      → the unique function/method named `f`, else `sym:ext:f`;
  * `obj.m(...)`  → the unique method named `m`, else `sym:ext:.m`.
Ambiguous or imported targets become `ext:` symbols (no params) — they still
support CoN (by name) and CoM (by literal), just not CoP/CoT (which need params).

Stdlib only.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys

_KEYRE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _vh(s: str):
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _module_name(path: str, root: str | None) -> str:
    if root:
        rel = os.path.relpath(path, root)
        rel = rel[:-3] if rel.endswith(".py") else rel
        rel = rel[: -len("/__init__")] if rel.endswith("/__init__") else rel
        return rel.replace(os.sep, ".")
    return os.path.splitext(os.path.basename(path))[0]


def _params(fn: ast.AST) -> list:
    a = fn.args
    out = []
    pos = 0
    ndef = len(a.defaults)
    nposonly = len(a.posonlyargs)
    positional = list(a.posonlyargs) + list(a.args)
    first_default = len(positional) - ndef
    for i, arg in enumerate(positional):
        out.append({
            "name": arg.arg, "position": pos,
            "kind": "positional",
            "type": ast.unparse(arg.annotation) if arg.annotation else None,
            "has_default": i >= first_default,
            "posonly": i < nposonly,
        })
        pos += 1
    if a.vararg:
        out.append({"name": a.vararg.arg, "position": pos, "kind": "vararg",
                    "type": ast.unparse(a.vararg.annotation) if a.vararg.annotation else None,
                    "has_default": False})
        pos += 1
    nkwdef = sum(1 for d in a.kw_defaults if d is not None)
    for arg in a.kwonlyargs:
        out.append({"name": arg.arg, "position": None, "kind": "keyword",
                    "type": ast.unparse(arg.annotation) if arg.annotation else None,
                    "has_default": nkwdef > 0})
    if a.kwarg:
        out.append({"name": a.kwarg.arg, "position": None, "kind": "kwarg",
                    "type": ast.unparse(a.kwarg.annotation) if a.kwarg.annotation else None,
                    "has_default": False})
    return out


class SymbolTable:
    """Pass 1: collect every def as a symbol; index by simple/method name."""

    def __init__(self):
        self.symbols = {}            # id -> symbol dict
        self.by_func_name = {}       # name -> [id]
        self.by_method_name = {}     # name -> [id]
        self.func_returns = {}       # id -> qualname (for value flow, unused statically)

    def add_module(self, module: str, file: str):
        sid = f"sym:{module}:<module>"
        if sid not in self.symbols:
            self.symbols[sid] = {"id": sid, "qualname": module, "kind": "module",
                                 "file": file, "line": 0, "module": module,
                                 "package": module.split(".")[0], "params": []}
        return sid

    def add_def(self, node, module, file, class_stack, func_stack):
        qual = ".".join([module] + class_stack + func_stack + [node.name])
        sid = f"sym:{qual}"
        is_method = bool(class_stack) and not func_stack
        self.symbols[sid] = {
            "id": sid, "qualname": qual,
            "kind": "method" if is_method else "function",
            "file": file, "line": node.lineno,
            "module": module, "package": module.split(".")[0],
            "params": _params(node),
            "returns_type": ast.unparse(node.returns) if node.returns else None,
        }
        self.by_func_name.setdefault(node.name, []).append(sid)
        if is_method:
            self.by_method_name.setdefault(node.name, []).append(sid)
        return sid

    def external(self, qualname: str, kind: str):
        sid = f"sym:ext:{qualname}"
        if sid not in self.symbols:
            self.symbols[sid] = {"id": sid, "qualname": qualname,
                                 "kind": kind, "file": None, "line": None,
                                 "module": None, "package": None, "params": []}
        return sid

    def record(self, base, in_function, module, file):
        """A record-shape access target (dict read by string keys). One per
        (enclosing function, base expression) — the field set read off it is a
        Connascence of Name+Meaning with whatever produces the dict."""
        sid = f"sym:record:{in_function}::{base}"
        if sid not in self.symbols:
            self.symbols[sid] = {
                "id": sid, "qualname": base, "kind": "record",
                "file": file, "line": None,
                "module": module, "package": module.split(".")[0] if module else None,
                "in_function": in_function, "params": []}
        return sid

    def resolve_name(self, name: str):
        ids = self.by_func_name.get(name, [])
        if len(ids) == 1:
            return ids[0]
        return self.external(name, "external")

    def resolve_method(self, attr: str):
        ids = self.by_method_name.get(attr, [])
        if len(ids) == 1:
            return ids[0]
        return self.external(f".{attr}", "external")


class DefCollector(ast.NodeVisitor):
    def __init__(self, table, module, file):
        self.t, self.module, self.file = table, module, file
        self.class_stack, self.func_stack = [], []

    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def _def(self, node):
        self.t.add_def(node, self.module, self.file,
                       list(self.class_stack), list(self.func_stack))
        self.func_stack.append(node.name)
        # don't recurse classes-inside-functions for qualnames; keep simple
        for child in node.body:
            self.visit(child)
        self.func_stack.pop()

    visit_FunctionDef = _def
    visit_AsyncFunctionDef = _def


class CallCollector(ast.NodeVisitor):
    """Pass 2: emit a step per call, with in_symbol = enclosing def/module."""

    def __init__(self, table, module, file, steps, tokens, counter):
        self.t, self.module, self.file = table, module, file
        self.steps, self.tokens, self.counter = steps, tokens, counter
        self.class_stack, self.func_stack = [], []
        self.module_sym = table.add_module(module, file)

    def _enclosing(self):
        # mirror DefCollector: inside a def body, func_stack holds the current
        # function name, so module+class_stack+func_stack == the def's qualname.
        if self.func_stack:
            qual = ".".join([self.module] + self.class_stack + self.func_stack)
            return f"sym:{qual}"
        return self.module_sym

    def _enclosing_qual(self):
        if self.func_stack:
            return ".".join([self.module] + self.class_stack + self.func_stack)
        return self.module

    def _record_access(self, base_node, key, line, require_ident):
        """Emit a field-access step: a read of `base[key]` / `base.get(key)`.
        Modeled as a step against a synthetic `record` symbol, with the key as a
        literal token → record-shape Connascence of Name+Meaning."""
        if not isinstance(key, str) or not key:
            return
        if require_ident and not _KEYRE.match(key):
            return  # filters URLs / non-key strings in .get()
        try:
            base = ast.unparse(base_node)[:40]
        except Exception:
            base = "?"
        rid = self.t.record(base, self._enclosing_qual(), self.module, self.file)
        self.counter[0] += 1
        tid = f"tok:{self.module}:{self.counter[0]}"
        self.tokens.append({"id": tid, "type": "key", "repr": key[:120],
                            "identity": None, "value_hash": _vh(repr(key)),
                            "is_literal": True, "literal_repr": key[:120],
                            "key": key})
        self.counter[1] += 1
        self.steps.append({
            "id": f"step:{self.module}:{self.counter[1]}",
            "callee": rid, "in_symbol": self._enclosing(),
            "site_file": self.file, "site_line": line,
            "arg_style": {"positional": 1, "keyword": []},
            "callee_qualname": base, "access": "field", "args": [tid]})

    def visit_Subscript(self, node):
        key = node.slice.value if isinstance(node.slice, ast.Constant) else None
        if isinstance(key, str):
            self._record_access(node.value, key, node.lineno, require_ident=False)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def _def(self, node):
        self.func_stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self.func_stack.pop()

    visit_FunctionDef = _def
    visit_AsyncFunctionDef = _def

    def _token_for(self, expr):
        self.counter[0] += 1
        tid = f"tok:{self.module}:{self.counter[0]}"
        if isinstance(expr, ast.Constant):
            val = expr.value
            r = repr(val)
            tok = {"id": tid, "type": type(val).__name__, "repr": r[:120],
                   "identity": None, "value_hash": _vh(r),
                   "is_literal": True, "literal_repr": r[:120]}
        else:
            try:
                src = ast.unparse(expr)
            except Exception:
                src = type(expr).__name__
            tok = {"id": tid, "type": "expr", "repr": src[:120],
                   "identity": None, "value_hash": None,
                   "is_literal": False, "literal_repr": None}
        self.tokens.append(tok)
        return tid

    def visit_Call(self, node):
        func = node.func
        # dict-get pattern: base.get('key') / .pop / .setdefault → field access,
        # not a generic call. Also strips the `.get` external-call noise.
        if (isinstance(func, ast.Attribute)
                and func.attr in ("get", "pop", "setdefault")
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            self._record_access(func.value, node.args[0].value, node.lineno,
                                require_ident=True)
            self.generic_visit(node)
            return
        if isinstance(func, ast.Name):
            callee = self.t.resolve_name(func.id)
            cqual = func.id
        elif isinstance(func, ast.Attribute):
            callee = self.t.resolve_method(func.attr)
            cqual = func.attr
        else:
            callee = self.t.external("<dynamic>", "external")
            cqual = "<dynamic>"
        posargs = [a for a in node.args if not isinstance(a, ast.Starred)]
        kwnames = [k.arg for k in node.keywords if k.arg]
        args = [self._token_for(a) for a in posargs]
        self.counter[1] += 1
        callee_sym = self.t.symbols.get(callee, {})
        step = {
            "id": f"step:{self.module}:{self.counter[1]}",
            "callee": callee, "in_symbol": self._enclosing(),
            "site_file": self.file, "site_line": node.lineno,
            "arg_style": {"positional": len(posargs), "keyword": kwnames},
            "callee_qualname": callee_sym.get("qualname", cqual),
            "args": args,
        }
        self.steps.append(step)
        self.generic_visit(node)


def collect_files(paths: list) -> list:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                for n in names:
                    if n.endswith(".py"):
                        files.append(os.path.join(root, n))
        elif p.endswith(".py"):
            files.append(p)
    return sorted(set(files))


def build(paths: list, module_root: str | None) -> dict:
    files = collect_files(paths)
    table = SymbolTable()
    trees = {}
    for f in files:
        try:
            with open(f) as fh:
                tree = ast.parse(fh.read(), filename=f)
        except (SyntaxError, UnicodeDecodeError) as e:
            print(f"skip {f}: {e}", file=sys.stderr)
            continue
        module = _module_name(f, module_root)
        trees[f] = (tree, module)
        DefCollector(table, module, f).visit(tree)

    steps, tokens, counter = [], [], [0, 0]
    for f, (tree, module) in trees.items():
        CallCollector(table, module, f, steps, tokens, counter).visit(tree)

    return {
        "version": "1", "kind": "static",
        "entrypoint": next(iter(trees.values()))[1] if trees else None,
        "symbols": list(table.symbols.values()),
        "steps": steps, "tokens": tokens,
    }


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Static TraceDoc from Python source.")
    ap.add_argument("paths", nargs="+", help=".py files or directories")
    ap.add_argument("--module-root", help="root for dotted module names")
    args = ap.parse_args(argv)
    json.dump(build(args.paths, args.module_root), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
