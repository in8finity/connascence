#!/usr/bin/env python3
"""Structural well-formedness of a materialized trace graph (Layer-1 rules),
plus an optional crypto fold-in from verify_work_package.

  python3 trace-validate.py --input dump.json
  python3 trace-validate.py --input dump.json --strict          # warnings → errors
  python3 trace-validate.py --input dump.json --crypto vwp.json  # fold in integrity

Checks (mirrors aif-validate.py):
  [S1] every TraceStep has a `callee` resolving to a TraceSymbol
  [S2] caller/realizes resolve to TraceStep; in_symbol to TraceSymbol
  [S3] args/returns resolve to TraceToken
  [S4] dynamic caller-tree is acyclic
  [S5] TraceConn.elements / locus resolve to existing nodes
  [W1] dynamic token identity is run-scoped (warn if no run_id)
  [W2] TraceConn.kind is a known connascence key
  [C1] crypto integrity (from --crypto verify_work_package JSON)

Exit 1 on any error. The structural pass needs no live server.
"""
from __future__ import annotations

import argparse
import json
import sys

from connascence import CONNASCENCES
from trace_io import load_graph, read_input


def validate(g, strict: bool, crypto: dict | None) -> tuple[list, list]:
    errs, warns = [], []

    def is_type(nid, t):
        n = g.get(nid)
        return n is not None and n.type == t

    for st in g.steps():
        callee = st.links.get("callee")
        if not callee:
            errs.append(f"[S1] step {st.id[:12]} has no callee")
        elif not is_type(callee, "TraceSymbol"):
            errs.append(f"[S1] step {st.id[:12]} callee -> non-symbol {callee}")
        for ln in ("caller", "realizes"):
            v = st.links.get(ln)
            if v and not is_type(v, "TraceStep"):
                errs.append(f"[S2] step {st.id[:12]} {ln} -> non-step {v}")
        if st.links.get("in_symbol") and not is_type(st.links["in_symbol"], "TraceSymbol"):
            errs.append(f"[S2] step {st.id[:12]} in_symbol -> non-symbol")
        for a in st.link_ids("args"):
            if not is_type(a, "TraceToken"):
                errs.append(f"[S3] step {st.id[:12]} arg -> non-token {a}")
        r = st.links.get("returns")
        if r and not is_type(r, "TraceToken"):
            errs.append(f"[S3] step {st.id[:12]} returns -> non-token {r}")

    # [S4] caller-tree acyclic
    WHITE, GREY, BLACK = 0, 1, 2
    color = {}

    def visit(sid, stack):
        color[sid] = GREY
        nxt = g.get(sid).links.get("caller") if g.get(sid) else None
        if nxt:
            if color.get(nxt) == GREY:
                errs.append(f"[S4] caller cycle through {sid[:12]}")
                return
            if color.get(nxt, WHITE) == WHITE:
                visit(nxt, stack + [sid])
        color[sid] = BLACK

    for st in g.steps():
        if color.get(st.id, WHITE) == WHITE:
            visit(st.id, [])

    # [S5] conn elements
    for c in g.conns():
        for e in c.link_ids("elements"):
            if g.get(e) is None:
                errs.append(f"[S5] TraceConn {c.id[:12]} element -> missing {e}")
        if c.links.get("locus") and g.get(c.links["locus"]) is None:
            errs.append(f"[S5] TraceConn {c.id[:12]} locus -> missing")
        # [W2]
        k = c.attrs.get("kind")
        if k and k not in CONNASCENCES:
            warns.append(f"[W2] TraceConn {c.id[:12]} unknown kind {k!r}")

    # [W1] run-scoped identities
    for t in g.tokens():
        if t.attrs.get("identity") and not (t.attrs.get("run_id")
                                            or "@" in str(t.attrs["identity"])):
            warns.append(f"[W1] token {t.id[:12]} identity not run-scoped")

    # [C1] crypto
    if crypto is not None:
        if isinstance(crypto, dict):
            ok = crypto.get("ok")
            ec = crypto.get("errors_count", crypto.get("errors"))
            if ok is False or (isinstance(ec, int) and ec > 0):
                errs.append(f"[C1] verify_work_package reported {ec} integrity errors")
            for err in (crypto.get("errors") or []) if isinstance(crypto.get("errors"), list) else []:
                errs.append(f"[C1] {err}")

    if strict:
        errs += warns
        warns = []
    return errs, warns


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Validate a materialized trace graph.")
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--crypto", help="verify_work_package JSON to fold in as [C1]")
    args = ap.parse_args(argv)

    g = load_graph(read_input(args.input))
    crypto = read_input(args.crypto) if args.crypto else None
    errs, warns = validate(g, args.strict, crypto)

    for w in warns:
        print(f"WARN:  {w}", file=sys.stderr)
    for e in errs:
        print(f"ERROR: {e}", file=sys.stderr)
    if errs:
        print(f"\n{len(errs)} errors, {len(warns)} warnings.", file=sys.stderr)
        return 1
    print(f"OK: {len(g.steps())} steps, {len(g.symbols())} symbols, "
          f"{len(g.tokens())} tokens, {len(g.conns())} findings; "
          f"{len(warns)} warnings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
