#!/usr/bin/env python3
"""Merge several TraceDocs into one cross-stack graph.

The point: a column defined in a SQL schema and the same field read in app code
(`users['email']` / `row['email']`) are the SAME coupling — but each adapter
names its record symbol differently (SQL: the table; app adapters: the local
variable). This merger canonicalizes record symbols to a shared key (the table
name) so they collapse into one node; then `trace-detect.py` reports the
column's blast radius across the WHOLE stack with no detector change.

  python3 trace-merge.py sql.json app.json [more.json ...] \
      --map row=users --map u=users  > merged.json

  --map <base>=<table>   rename an app record's base variable to the table it
                         represents, so it merges with the SQL table record.
                         Repeatable. SQL table records already use the table
                         name, so they need no map.

Record symbols merge by canonical id `sym:record:<key>` (key = mapped base, else
the base itself); their column/param sets are unioned. Every other symbol/step/
token is namespaced per input doc (`d0:`, `d1:`, …) so ids never collide, and
all links are rewritten through the same map. Emits `kind: "merged"`.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys

from trace_io import read_input

# link names by node type → which are single vs many (mirrors trace_io.LINK_ARITY)
SINGLE = ("callee", "in_symbol", "caller", "realizes", "returns", "locus")
MANY = ("args", "elements")


def canonical_record_id(base: str, mapping: dict) -> str:
    return f"sym:record:{mapping.get(base, base)}"


def doc_nodes(doc):
    """Yield (kind, node_dict) for a TraceDoc or an items list."""
    if isinstance(doc, dict) and ("symbols" in doc or "steps" in doc or "tokens" in doc):
        for s in doc.get("symbols", []):
            yield "symbol", s
        for t in doc.get("tokens", []):
            yield "token", t
        for st in doc.get("steps", []):
            yield "step", st
    else:
        items = doc if isinstance(doc, list) else doc.get("items", [])
        for it in items:
            yield "item", it


def merge(docs: list, mapping: dict) -> dict:
    symbols: dict = {}     # final id -> symbol
    steps: list = []
    tokens: dict = {}      # final id -> token

    for i, doc in enumerate(docs):
        tag = f"d{i}:"
        idmap: dict = {}    # original id -> final id (for this doc)

        # --- pass 1: assign final ids -------------------------------------
        raw = list(doc_nodes(doc))
        # symbols / items that are symbol-shaped
        for kind, n in raw:
            nid = n.get("id")
            if nid is None:
                continue
            ntype = n.get("type")
            nkind = n.get("kind")  # tracedoc symbols carry 'kind'; steps/tokens don't
            is_symbol = (kind == "symbol") or (kind == "item" and ntype == "TraceSymbol")
            is_record = is_symbol and nkind == "record"
            if is_record:
                idmap[nid] = canonical_record_id(n.get("qualname", nid), mapping)
            elif is_symbol:
                idmap[nid] = tag + nid
            else:
                idmap[nid] = tag + nid  # steps + tokens always namespaced

        def rewrite_links(links: dict) -> dict:
            out = {}
            for k, v in (links or {}).items():
                if k in MANY and isinstance(v, list):
                    out[k] = [idmap.get(x, x) for x in v]
                elif k in SINGLE:
                    out[k] = idmap.get(v, v)
                else:
                    out[k] = v
            return out

        # --- pass 2: emit with rewritten ids/links ------------------------
        for kind, n in raw:
            nid = n.get("id")
            if nid is None:
                continue
            fid = idmap[nid]
            ntype = n.get("type")
            nkind = n.get("kind")

            if kind in ("symbol",) or (kind == "item" and ntype == "TraceSymbol"):
                if fid in symbols:
                    # merge record column/param sets (union by name, keep order)
                    if nkind == "record":
                        have = symbols[fid].get("params", [])
                        names = {p.get("name") for p in have}
                        for p in (n.get("params") or []):
                            if p.get("name") not in names:
                                have.append(p)
                        symbols[fid]["params"] = have
                    continue
                node = dict(n)
                node["id"] = fid
                if nkind == "record":
                    node["qualname"] = mapping.get(n.get("qualname"), n.get("qualname"))
                symbols[fid] = node
            elif kind == "token" or (kind == "item" and ntype == "TraceToken"):
                node = dict(n)
                node["id"] = fid
                tokens[fid] = node
            else:  # step (tracedoc) or item that's a step/conn
                node = dict(n)
                node["id"] = fid
                # tracedoc steps carry links as top-level fields; items use .links
                if "links" in node:
                    node["links"] = rewrite_links(node.get("links"))
                else:
                    for k in SINGLE:
                        if node.get(k) is not None:
                            node[k] = idmap.get(node[k], node[k])
                    for k in MANY:
                        if isinstance(node.get(k), list):
                            node[k] = [idmap.get(x, x) for x in node[k]]
                steps.append(node)

    return {
        "version": "1", "kind": "merged", "entrypoint": "merged",
        "symbols": list(symbols.values()),
        "steps": steps,
        "tokens": list(tokens.values()),
    }


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Merge TraceDocs into one cross-stack graph.")
    ap.add_argument("inputs", nargs="+", help="two or more TraceDoc / dump JSON files")
    ap.add_argument("--map", action="append", default=[], metavar="base=table",
                    help="rename an app record's base variable to a table name")
    args = ap.parse_args(argv)

    mapping = {}
    for m in args.map:
        if "=" not in m:
            sys.stderr.write(f"bad --map (need base=table): {m}\n")
            return 2
        b, t = m.split("=", 1)
        mapping[b.strip()] = t.strip()

    docs = [read_input(p) for p in args.inputs]
    json.dump(merge(docs, mapping), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
