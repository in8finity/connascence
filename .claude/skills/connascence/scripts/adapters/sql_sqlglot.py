#!/usr/bin/env python3
"""Static adapter: build a TraceDoc *static spine* from SQL via sqlglot.

SQL is the purest case for this skill: the schema is an explicit shared
contract, and every query/view/procedure is a consumer coupled to it BY NAME.
"What breaks if I rename this column / drop this table?" is the canonical
blast-radius question, and SQL has no compile-time check for it.

Mapping onto the connascence model:
  * a TABLE  -> a `record` TraceSymbol whose keys are its columns
  * a column reference (SELECT/WHERE/JOIN/UPDATE/INSERT(col)) -> a record-access
    step against that table (key = column) -> record-shape CoN (table coupling
    width; degree = distinct columns referenced across the corpus)
  * INSERT INTO t VALUES (...) with no column list -> a positional step against
    the table (whose params are its columns) -> CoP (the classic SQL fragility)
  * SELECT * -> expands to a reference of every known column (couples to all)
  * CREATE PROCEDURE/FUNCTION -> a TraceSymbol; CALL p(...) / f(...) -> a call
    edge -> CoN/CoP on procedures

Run with `python3`. Emits `kind: "static"`.

    python3 sql_sqlglot.py schema.sql queries.sql [--dialect postgres] > static.json

REQUIRES sqlglot (MIT): `pip install sqlglot`. Multi-dialect (postgres, mysql,
tsql, snowflake, ...); pass --dialect for best parsing, else a generic parse.

LIMITATIONS: deep stored-procedure bodies (PL/pgSQL, T-SQL control flow) are
dialect-specific and only partially parsed by sqlglot — schema DDL, top-level
queries, and CALL graphs are the sweet spot. Unqualified columns in multi-table
(JOIN) statements are attributed only when resolvable from a single in-scope
table; genuinely ambiguous ones are skipped (reported on stderr).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

try:
    import sqlglot
    from sqlglot import exp
except ImportError:
    sys.stderr.write("ERROR: sqlglot not installed. pip install sqlglot\n")
    raise SystemExit(2)


def vh(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()[:16]


def module_name(path: str, root: str | None) -> str:
    rel = os.path.relpath(path, root) if root else os.path.basename(path)
    return rel[:-4].replace(os.sep, ".") if rel.endswith(".sql") else rel.replace(os.sep, ".")


class SqlTracer:
    def __init__(self):
        self.symbols: dict = {}
        self.tokens: dict = {}
        self.steps: list = []
        self.table_cols: dict = {}   # table -> [column names] (from DDL)
        self.n = 0
        self.tc = 0

    # -- symbols -----------------------------------------------------------

    def table_record(self, table: str) -> str:
        sid = f"sym:record:{table}"
        if sid not in self.symbols:
            cols = self.table_cols.get(table, [])
            self.symbols[sid] = {
                "id": sid, "qualname": table, "kind": "record",
                "file": None, "line": None, "module": None, "package": None,
                "in_function": "schema",
                "params": [{"name": c, "position": i, "kind": "positional"}
                           for i, c in enumerate(cols)],
            }
        return sid

    def proc_symbol(self, name: str, defined: bool, module: str, line=None) -> str:
        sid = (f"sym:proc:{name}" if defined else f"sym:ext:{name}")
        if sid not in self.symbols:
            self.symbols[sid] = {
                "id": sid, "qualname": name,
                "kind": "procedure" if defined else "external",
                "file": None, "line": line,
                "module": module if defined else None,
                "package": module.split(".")[0] if defined else None,
                "params": [],
            }
        return sid

    def module_symbol(self, module: str, file: str) -> str:
        sid = f"sym:{module}:<module>"
        if sid not in self.symbols:
            self.symbols[sid] = {"id": sid, "qualname": module, "kind": "module",
                                 "file": file, "line": 0, "module": module,
                                 "package": module.split(".")[0], "params": []}
        return sid

    # -- tokens ------------------------------------------------------------

    def key_token(self, col: str) -> str:
        self.tc += 1
        tid = f"tok:{self.tc}"
        self.tokens[tid] = {"id": tid, "type": "column", "repr": col,
                            "identity": None, "value_hash": vh(col),
                            "is_literal": True, "literal_repr": col, "key": col}
        return tid

    def value_token(self, e) -> str:
        self.tc += 1
        tid = f"tok:{self.tc}"
        if isinstance(e, exp.Literal):
            v = e.name
            self.tokens[tid] = {"id": tid, "type": "string" if e.is_string else "number",
                                "repr": v[:120], "identity": None, "value_hash": vh(v),
                                "is_literal": True, "literal_repr": v[:120]}
        else:
            src = e.sql()[:120]
            self.tokens[tid] = {"id": tid, "type": "expr", "repr": src, "identity": None,
                                "value_hash": None, "is_literal": False, "literal_repr": None}
        return tid

    # -- steps -------------------------------------------------------------

    def field_step(self, table: str, col: str, in_sym: str):
        rid = self.table_record(table)
        self.n += 1
        self.steps.append({
            "id": f"step:{self.n}", "callee": rid, "in_symbol": in_sym,
            "site_file": None, "site_line": None,
            "arg_style": {"positional": 1, "keyword": []},
            "callee_qualname": table, "access": "field",
            "args": [self.key_token(col)]})

    def call_step(self, callee: str, qual: str, in_sym: str, argtoks: list):
        self.n += 1
        self.steps.append({
            "id": f"step:{self.n}", "callee": callee, "in_symbol": in_sym,
            "site_file": None, "site_line": None,
            "arg_style": {"positional": len(argtoks), "keyword": []},
            "callee_qualname": qual, "args": argtoks})


def _table_name(t: exp.Table) -> str:
    parts = [p for p in (t.catalog, t.db, t.name) if p]
    return ".".join(parts)


def collect_ddl(tracer: SqlTracer, statements):
    for stmt in statements:
        if isinstance(stmt, exp.Create) and (stmt.kind or "").upper() == "TABLE":
            tbl = stmt.this
            name = _table_name(tbl.this) if isinstance(tbl, exp.Schema) else (
                _table_name(tbl) if isinstance(tbl, exp.Table) else None)
            if not name:
                continue
            cols = [c.name for c in stmt.find_all(exp.ColumnDef)]
            tracer.table_cols[name] = cols


def alias_map(stmt) -> dict:
    """alias-or-name -> real table name, for every table in the statement."""
    m = {}
    for t in stmt.find_all(exp.Table):
        real = _table_name(t)
        m[real] = real
        if t.alias:
            m[t.alias] = real
    return m


def walk_statement(tracer: SqlTracer, stmt, module: str, file: str, dialect):
    msym = tracer.module_symbol(module, file)

    # CREATE PROCEDURE / FUNCTION -> define a symbol
    if isinstance(stmt, exp.Create) and (stmt.kind or "").upper() in ("PROCEDURE", "FUNCTION"):
        nm = stmt.this.name if hasattr(stmt.this, "name") else stmt.this.sql()
        tracer.proc_symbol(nm, defined=True, module=module)
        # body parsing is limited; fall through to scan any nested refs below

    amap = alias_map(stmt)
    in_scope_tables = sorted(set(amap.values()))

    def resolve(col: exp.Column):
        q = col.table
        if q and q in amap:
            return amap[q]
        if q and q in in_scope_tables:
            return q
        if not q and len(in_scope_tables) == 1:
            return in_scope_tables[0]
        return None  # ambiguous / unqualified multi-table

    # SELECT * -> couple to every known column of each in-scope table
    for star in stmt.find_all(exp.Star):
        for t in in_scope_tables:
            for c in tracer.table_cols.get(t, ["*"]):
                tracer.field_step(t, c, msym)

    # column references
    for col in stmt.find_all(exp.Column):
        t = resolve(col)
        if t is None:
            sys.stderr.write(f"  (skipped ambiguous column {col.sql()})\n")
            continue
        tracer.field_step(t, col.name, msym)

    # INSERT INTO t VALUES (...) with NO column list -> positional coupling
    if isinstance(stmt, exp.Insert):
        target = stmt.this
        if isinstance(target, exp.Table):  # bare table, no (col,...) schema
            name = _table_name(target)
            vals = stmt.find(exp.Values)
            ncols = 0
            if vals and vals.expressions:
                first = vals.expressions[0]
                ncols = len(first.expressions) if isinstance(first, exp.Tuple) else 0
            if ncols >= 2:
                rid = tracer.table_record(name)
                toks = [tracer.value_token(exp.Literal.string("?")) for _ in range(ncols)]
                tracer.call_step(rid, name, msym, toks)  # positional INSERT -> CoP

    # CALL proc / function invocations
    for cmd in stmt.find_all(exp.Command):
        if (cmd.this or "").upper() == "CALL":
            nm = cmd.expression.sql().split("(")[0].strip() if cmd.expression else "?"
            tracer.call_step(tracer.proc_symbol(nm, False, module), nm, msym, [])
    for fn in stmt.find_all(exp.Anonymous):
        nm = fn.name
        toks = [tracer.value_token(a) for a in fn.expressions]
        tracer.call_step(tracer.proc_symbol(nm, nm in
                         {s["qualname"] for s in tracer.symbols.values()
                          if s["kind"] == "procedure"}, module), nm, msym, toks)


def build(paths, dialect, module_root) -> dict:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for r, _, ns in os.walk(p):
                files += [os.path.join(r, n) for n in ns if n.endswith(".sql")]
        elif p.endswith(".sql"):
            files.append(p)
    files = sorted(set(files))

    tracer = SqlTracer()
    parsed = []
    for f in files:
        try:
            stmts = [s for s in sqlglot.parse(open(f).read(), read=dialect) if s]
        except Exception as e:
            sys.stderr.write(f"skip {f}: {e}\n")
            continue
        parsed.append((f, stmts))
        collect_ddl(tracer, stmts)        # pass 1: learn table->columns first
    for f, stmts in parsed:
        module = module_name(f, module_root)
        for stmt in stmts:
            walk_statement(tracer, stmt, module, f, dialect)

    return {"version": "1", "kind": "static",
            "entrypoint": module_name(files[0], module_root) if files else None,
            "symbols": list(tracer.symbols.values()),
            "steps": tracer.steps, "tokens": list(tracer.tokens.values())}


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Static TraceDoc from SQL via sqlglot.")
    ap.add_argument("paths", nargs="+", help=".sql files or directories")
    ap.add_argument("--dialect", default=None, help="postgres|mysql|tsql|snowflake|...")
    ap.add_argument("--module-root")
    args = ap.parse_args(argv)
    json.dump(build(args.paths, args.dialect, args.module_root), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
