#!/usr/bin/env python3
"""Validate a TraceDoc against the ingest contract and dedup tokens.

  python3 trace-ingest.py --input doc.json                 # validate + stats
  python3 trace-ingest.py --input doc.json --dedup         # write deduped doc

Token dedup: tokens with identical (value_hash, identity, type) collapse to one
class; step arg/return refs are rewritten to the class id. CoV/CoI then run over
classes, not occurrences — important on a real runtime trace where one value
appears millions of times.

Stdlib only. No server.
"""
from __future__ import annotations

import argparse
import json
import sys

from trace_io import read_input

REQUIRED_DOC = {"version", "kind"}
KINDS = {"static", "dynamic", "merged"}


def validate(doc: dict) -> list:
    errs = []
    if not isinstance(doc, dict):
        return ["top-level value is not a TraceDoc object"]
    for k in REQUIRED_DOC:
        if k not in doc:
            errs.append(f"missing top-level field: {k}")
    if doc.get("kind") not in KINDS:
        errs.append(f"kind must be one of {sorted(KINDS)}; got {doc.get('kind')!r}")

    sym_ids, tok_ids, step_ids = set(), set(), set()
    for s in doc.get("symbols", []):
        if "id" not in s or "qualname" not in s:
            errs.append(f"symbol missing id/qualname: {s.get('id', s)}")
        if s.get("id") in sym_ids:
            errs.append(f"duplicate symbol id: {s['id']}")
        sym_ids.add(s.get("id"))
    for t in doc.get("tokens", []):
        if "id" not in t:
            errs.append(f"token missing id: {t}")
        if t.get("id") in tok_ids:
            errs.append(f"duplicate token id: {t['id']}")
        tok_ids.add(t.get("id"))
    for st in doc.get("steps", []):
        if "id" not in st:
            errs.append(f"step missing id: {st}")
        if st.get("id") in step_ids:
            errs.append(f"duplicate step id: {st['id']}")
        step_ids.add(st.get("id"))

    dyn = doc.get("kind") in ("dynamic", "merged")
    if dyn and doc.get("tokens") and not doc.get("run_id"):
        errs.append("dynamic/merged doc with tokens must set run_id "
                    "(identities are run-scoped)")

    # referential integrity
    for st in doc.get("steps", []):
        sid = st.get("id")
        if "callee" not in st:
            errs.append(f"step {sid}: missing required `callee`")
        for ln in ("callee", "in_symbol"):
            if st.get(ln) and st[ln] not in sym_ids:
                errs.append(f"step {sid}: {ln} -> unknown symbol {st[ln]}")
        for ln in ("caller", "realizes"):
            if st.get(ln) and st[ln] not in step_ids:
                errs.append(f"step {sid}: {ln} -> unknown step {st[ln]}")
        for tid in st.get("args", []) or []:
            if tid not in tok_ids:
                errs.append(f"step {sid}: arg -> unknown token {tid}")
        if st.get("returns") and st["returns"] not in tok_ids:
            errs.append(f"step {sid}: returns -> unknown token {st['returns']}")
    return errs


def dedup_tokens(doc: dict) -> tuple[dict, dict]:
    """Collapse tokens by (value_hash, identity, type). Returns (new_doc, stats)."""
    canon = {}   # (vh, identity, type) -> canonical id
    remap = {}   # old id -> canonical id
    kept = []
    for t in doc.get("tokens", []):
        key = (t.get("value_hash"), t.get("identity"), t.get("type"))
        # tokens with neither value_hash nor identity are not dedupable
        if key == (None, None, t.get("type")):
            kept.append(t)
            remap[t["id"]] = t["id"]
            continue
        if key in canon:
            remap[t["id"]] = canon[key]
        else:
            canon[key] = t["id"]
            remap[t["id"]] = t["id"]
            kept.append(t)
    new = dict(doc)
    new["tokens"] = kept
    new_steps = []
    for st in doc.get("steps", []):
        st = dict(st)
        if st.get("args"):
            st["args"] = [remap.get(a, a) for a in st["args"]]
        if st.get("returns"):
            st["returns"] = remap.get(st["returns"], st["returns"])
        new_steps.append(st)
    new["steps"] = new_steps
    stats = {"tokens_in": len(doc.get("tokens", [])),
             "tokens_out": len(kept),
             "collapsed": len(doc.get("tokens", [])) - len(kept)}
    return new, stats


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Validate + dedup a TraceDoc.")
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--dedup", action="store_true", help="emit deduped TraceDoc")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)

    doc = read_input(args.input)
    errs = validate(doc)
    if errs:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        if args.strict or not args.dedup:
            return 1

    deduped, stats = dedup_tokens(doc)

    if args.dedup:
        print(json.dumps(deduped, indent=2))
        return 0

    print(f"OK: {len(doc.get('symbols',[]))} symbols, "
          f"{len(doc.get('steps',[]))} steps, "
          f"{stats['tokens_in']} tokens "
          f"({stats['collapsed']} dedupable → {stats['tokens_out']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
