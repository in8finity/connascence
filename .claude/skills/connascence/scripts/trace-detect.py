#!/usr/bin/env python3
"""Run the 9 connascence detectors over a trace graph.

Consumes a TraceDoc (see trace_io). Emits findings ranked by severity
(strength × degree × locality_penalty).

  python3 trace-detect.py --input doc.json                  # ranked report
  python3 trace-detect.py --input doc.json --format json     # findings as JSON
  python3 trace-detect.py --input doc.json --only CoI,CoV    # subset
  python3 trace-detect.py --input doc.json --exclude-external # in-codebase only
  python3 trace-detect.py --input doc.json --include-provisional  # CoA et al.
  python3 trace-detect.py --input doc.json --min-degree 3    # CoN/CoP threshold

Each finding:
  {kind, dynamic, strength, degree, locality, severity, confidence,
   rationale, refactor, elements:[id…], locus:id|None}

Stdlib only. No server.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from connascence import CONNASCENCES
from trace_io import (Node, Graph, load_graph, read_input, locality_of,
                      severity)

MAGIC_OK = {0, 1, -1, "", True, False, None}
# token reprs that are never meaningful "values must agree" connascence
TRIVIAL_REPRS = {"None", "0", "1", "-1", "''", '""', "True", "False", "[]", "{}", "()"}


def _is_external(sym):
    return sym is None or sym.attrs.get("kind") == "external" or (
        sym.attrs.get("package") is None and sym.attrs.get("module") is None)


def _finding(kind, elements, locus, degree, locality, rationale,
             confidence="firm"):
    c = CONNASCENCES[kind]
    return {
        "kind": kind,
        "name": c["name"],
        "dynamic": c["dynamic"],
        "strength": c["strength"],
        "degree": degree,
        "locality": locality,
        "severity": severity(c["strength"], degree, locality),
        "confidence": confidence,
        "rationale": rationale,
        "refactor": c["refactor"],
        "elements": [e.id if isinstance(e, Node) else e for e in elements],
        "locus": locus.id if isinstance(locus, Node) else locus,
    }


def _symbols_for(g: Graph, ids):
    out = []
    for i in ids:
        n = g.get(i)
        if n and n.type == "TraceSymbol":
            out.append(n)
    return out


# --------------------------------------------------------------------------
# Static detectors
# --------------------------------------------------------------------------

def detect_CoN(g: Graph, min_degree: int, exclude_external: bool = False):
    by_callee = defaultdict(list)
    for st in g.steps():
        c = st.links.get("callee")
        if c:
            by_callee[c].append(st)
    out = []
    for sym_id, steps in by_callee.items():
        if len(steps) < min_degree:
            continue
        sym = g.get(sym_id)
        if sym and sym.attrs.get("kind") == "record":
            continue  # handled by detect_records (degree = distinct keys)
        if exclude_external and _is_external(sym):
            continue
        callers = _symbols_for(g, {s.links.get("in_symbol") for s in steps})
        loc = locality_of(callers + ([sym] if sym else []))
        qn = sym.attrs.get("qualname") if sym else sym_id
        out.append(_finding(
            "CoN", [sym] + steps if sym else steps, sym, len(steps), loc,
            f"`{qn}` is referenced by {len(steps)} call sites; renaming it "
            f"requires {len(steps)} coordinated edits."))
    return out


def detect_records(g: Graph, min_keys: int):
    """Record-shape connascence: a dict read by N distinct string keys is
    coupled by Name+Meaning to whatever produces it. Reported as CoN with
    degree = number of distinct keys (the field set that must stay coordinated).
    """
    out = []
    for sym in g.symbols():
        if sym.attrs.get("kind") != "record":
            continue
        steps = [s for s in g.steps() if s.links.get("callee") == sym.id]
        keys = {}
        for s in steps:
            for tid in s.link_ids("args"):
                t = g.get(tid)
                if t and t.attrs.get("key"):
                    keys.setdefault(t.attrs["key"], []).append(s)
        if len(keys) < max(min_keys, 4):   # a few fields → a real shape coupling
            continue
        degree = len(keys)
        # The producer of the dict is almost always another layer (DB row
        # mapper / serializer), so treat the coupling as crossing a boundary.
        loc = "cross_package"
        infn = sym.attrs.get("in_function") or sym.attrs.get("module")
        shown = sorted(keys)
        klist = ", ".join(shown[:6]) + ("…" if degree > 6 else "")
        f = _finding(
            "CoN", [sym] + steps, sym, degree, loc,
            f"`{sym.attrs.get('qualname')}` (in {infn}) is read by {degree} "
            f"distinct string keys ({klist}) — Connascence of Name+Meaning with "
            f"whatever produces it; a field rename upstream breaks this silently.")
        f["refactor"] = ("Parse into a typed dataclass / `from_db_row` at the "
                         "boundary so the key set is defined and checked once.")
        f["subkind"] = "record_shape"
        out.append(f)
    return out


def detect_CoT(g: Graph):
    out = []
    for sym in g.symbols():
        # CoT is about callable parameter-type agreement. `record` symbols carry
        # their columns/keys as params (for record-shape / positional CoP), not a
        # typed call signature — skip them and other non-callables.
        if sym.attrs.get("kind") in ("record", "module", "external"):
            continue
        params = sym.attrs.get("params") or []
        callsites = [s for s in g.steps() if s.links.get("callee") == sym.id]
        for p in params:
            pos = p.get("position")
            observed = defaultdict(list)  # token type -> steps
            undeclared = not p.get("type")
            for st in callsites:
                args = st.link_ids("args")
                if pos is None or pos >= len(args):
                    continue
                tok = g.get(args[pos])
                if tok:
                    observed[tok.attrs.get("type")].append(st)
            unstable = len([t for t in observed if t]) > 1
            if not (unstable or (undeclared and callsites)):
                continue
            steps = [s for ss in observed.values() for s in ss] or callsites
            callers = _symbols_for(g, {s.links.get("in_symbol") for s in steps})
            loc = locality_of(callers + [sym])
            kind_note = ("type-unstable across calls"
                         if unstable else "implicit (no declared type)")
            out.append(_finding(
                "CoT", [sym] + steps, sym, max(len(steps), 1), loc,
                f"Param `{p.get('name')}` of `{sym.attrs.get('qualname')}` is "
                f"{kind_note}; {len(callsites)} sites rely on type agreement."))
    return out


def detect_CoM(g: Graph, exclude_external: bool = False):
    # group literal tokens by (callee, position, literal value)
    groups = defaultdict(list)  # (callee, pos, value) -> [(step, tok)]
    for st in g.steps():
        callee = st.links.get("callee")
        if exclude_external and _is_external(g.get(callee)):
            continue
        for pos, tid in enumerate(st.link_ids("args")):
            tok = g.get(tid)
            if not tok or not tok.attrs.get("is_literal"):
                continue
            if tok.attrs.get("key"):       # record field keys → detect_records
                continue
            val = tok.attrs.get("literal_repr", tok.attrs.get("repr"))
            raw = tok.attrs.get("repr")
            if raw in MAGIC_OK or val in ("0", "1", "-1", "''", '""'):
                continue
            groups[(callee, pos, val)].append((st, tok))
    out = []
    for (callee, pos, val), pairs in groups.items():
        if len(pairs) < 2:  # a magic value reused at >=2 sites is the smell
            continue
        sym = g.get(callee)
        steps = [s for s, _ in pairs]
        toks = [t for _, t in pairs]
        callers = _symbols_for(g, {s.links.get("in_symbol") for s in steps})
        loc = locality_of(callers + ([sym] if sym else []))
        out.append(_finding(
            "CoM", [sym] + steps + toks if sym else steps + toks, sym,
            len(pairs), loc,
            f"Magic literal {val} passed to position {pos} of "
            f"`{sym.attrs.get('qualname') if sym else callee}` at "
            f"{len(pairs)} sites — encodes an implicit shared meaning."))
    return out


def detect_CoP(g: Graph, min_degree: int):
    out = []
    by_callee = defaultdict(list)
    for st in g.steps():
        style = st.attrs.get("arg_style") or {}
        npos = style.get("positional", 0)
        if isinstance(npos, list):
            npos = len(npos)
        if npos >= 2 and st.links.get("callee"):
            by_callee[st.links["callee"]].append(st)
    for callee, steps in by_callee.items():
        sym = g.get(callee)
        params = (sym.attrs.get("params") if sym else None) or []
        eligible = [p for p in params
                    if p.get("has_default") or p.get("kind") in (None, "positional")]
        if len(eligible) < 2:
            continue
        if len(steps) < min_degree and len(steps) < 1:
            continue
        callers = _symbols_for(g, {s.links.get("in_symbol") for s in steps})
        loc = locality_of(callers + [sym])
        out.append(_finding(
            "CoP", [sym] + steps, sym, len(steps), loc,
            f"`{sym.attrs.get('qualname')}` is called positionally (>=2 args) "
            f"at {len(steps)} sites with {len(eligible)} order-sensitive params."))
    return out


def detect_CoA(g: Graph):
    # value_hash produced as a `returns` then consumed as an `arg` in a
    # symbol whose name pairs with the producer (encode/decode etc.)
    PAIRS = [("encode", "decode"), ("serialize", "deserialize"),
             ("sign", "verify"), ("marshal", "unmarshal"),
             ("to_", "from_"), ("pack", "unpack"), ("dump", "load"),
             ("encrypt", "decrypt"), ("hash", "verify"), ("checksum", "verify")]
    produced = defaultdict(list)  # value_hash -> [(step, sym)]
    consumed = defaultdict(list)
    for st in g.steps():
        sym = g.callee_of(st)
        rt = g.get(st.links.get("returns"))
        if rt and rt.attrs.get("value_hash"):
            produced[rt.attrs["value_hash"]].append((st, sym))
        for tid in st.link_ids("args"):
            tok = g.get(tid)
            if tok and tok.attrs.get("value_hash"):
                consumed[tok.attrs["value_hash"]].append((st, sym))
    out = []
    seen = set()
    for vh, prods in produced.items():
        for pstep, psym in prods:
            for cstep, csym in consumed.get(vh, []):
                if not psym or not csym or psym.id == csym.id:
                    continue
                pn = (psym.attrs.get("qualname") or "").lower()
                cn = (csym.attrs.get("qualname") or "").lower()
                if not any((a in pn and b in cn) or (b in pn and a in cn)
                           for a, b in PAIRS):
                    continue
                key = tuple(sorted((psym.id, csym.id)))
                if key in seen:
                    continue
                seen.add(key)
                loc = locality_of([psym, csym])
                out.append(_finding(
                    "CoA", [psym, csym, pstep, cstep], psym, 2, loc,
                    f"`{psym.attrs.get('qualname')}` and "
                    f"`{csym.attrs.get('qualname')}` exchange a value via "
                    f"value_hash and must share an algorithm.",
                    confidence="provisional"))
    return out


# --------------------------------------------------------------------------
# Dynamic detectors (need identity / order / ts / thread)
# --------------------------------------------------------------------------

def _steps_by_identity(g: Graph):
    """identity -> ordered list of (order, step, role)."""
    idx = defaultdict(list)
    for st in g.steps():
        order = st.attrs.get("order", 0)
        for tid in st.link_ids("args") + [st.links.get("returns")]:
            tok = g.get(tid)
            if tok and tok.attrs.get("identity"):
                idx[tok.attrs["identity"]].append((order, st))
    for ident in idx:
        idx[ident].sort(key=lambda x: x[0])
    return idx


def detect_CoE(g: Graph):
    idx = _steps_by_identity(g)
    # collect (A,B) callee pairs where A always precedes B for a given identity
    pair_seen = defaultdict(lambda: [0, 0])  # (A,B) -> [a_before_b, b_before_a]
    pair_steps = defaultdict(set)
    for ident, seq in idx.items():
        callees = [(o, g.callee_of(st), st) for o, st in seq]
        callees = [(o, c, st) for o, c, st in callees if c]
        for i in range(len(callees)):
            for j in range(i + 1, len(callees)):
                a, b = callees[i][1], callees[j][1]
                if a.id == b.id:
                    continue
                key = tuple(sorted((a.id, b.id)))
                first = a.id if key[0] == a.id else b.id
                if first == key[0]:
                    pair_seen[key][0] += 1
                else:
                    pair_seen[key][1] += 1
                pair_steps[key].add(callees[i][2].id)
                pair_steps[key].add(callees[j][2].id)
    out = []
    for key, (ab, ba) in pair_seen.items():
        total = ab + ba
        if total < 2 or (ab and ba):  # need a consistent, repeated ordering
            continue
        a, b = g.get(key[0]), g.get(key[1])
        first, second = (a, b) if ab else (b, a)
        loc = locality_of([a, b])
        steps = [g.get(s) for s in pair_steps[key]]
        out.append(_finding(
            "CoE", [a, b] + steps, None, 2, loc,
            f"`{first.attrs.get('qualname')}` consistently precedes "
            f"`{second.attrs.get('qualname')}` on a shared instance "
            f"({total}×) — an implicit execution-order contract."))
    return out


def detect_CoTm(g: Graph):
    idx = _steps_by_identity(g)
    out = []
    for ident, seq in idx.items():
        threads = {st.attrs.get("thread") for _, st in seq}
        threads.discard(None)
        if len(threads) < 2:
            continue
        steps = [st for _, st in seq]
        syms = _symbols_for(g, {st.links.get("callee") for st in steps})
        loc = locality_of(syms)
        out.append(_finding(
            "CoTm", steps, None, len(threads), loc,
            f"Instance {ident} is touched by {len(threads)} threads "
            f"({', '.join(sorted(map(str, threads)))}) — timing is "
            f"load-bearing (possible race)."))
    return out


def detect_CoV(g: Graph, exclude_external: bool = False):
    # same value_hash required across steps in >=2 different symbols,
    # value semantics (identity null)
    by_vh = defaultdict(list)  # value_hash -> [(step, sym, tok)]
    for st in g.steps():
        sym = g.callee_of(st)
        if exclude_external and _is_external(sym):
            continue
        for tid in st.link_ids("args"):
            tok = g.get(tid)
            if not (tok and tok.attrs.get("value_hash") and not tok.attrs.get("identity")):
                continue
            if tok.attrs.get("key"):      # record field keys → detect_records
                continue
            r = tok.attrs.get("literal_repr") or tok.attrs.get("repr")
            if r in TRIVIAL_REPRS:        # None/0/1/''… are not meaningful CoV
                continue
            by_vh[tok.attrs["value_hash"]].append((st, sym, tok))
    out = []
    for vh, recs in by_vh.items():
        syms = {s.id: s for _, s, _ in recs if s}
        if len(syms) < 2:
            continue
        steps = [st for st, _, _ in recs]
        toks = [t for _, _, t in recs]
        loc = locality_of(list(syms.values()))
        sample = next((t.attrs.get("repr") for t in toks if t.attrs.get("repr")), vh[:12])
        out.append(_finding(
            "CoV", list(syms.values()) + steps + toks, None, len(syms), loc,
            f"Value {sample} must agree across {len(syms)} symbols "
            f"({', '.join(s.attrs.get('qualname','?') for s in syms.values())})."))
    return out


def detect_CoI(g: Graph):
    by_ident = defaultdict(list)  # identity -> [(step, sym)]
    for st in g.steps():
        sym = g.callee_of(st)
        for tid in st.link_ids("args") + [st.links.get("returns")]:
            tok = g.get(tid)
            if tok and tok.attrs.get("identity"):
                by_ident[tok.attrs["identity"]].append((st, sym))
    out = []
    for ident, recs in by_ident.items():
        syms = {s.id: s for _, s in recs if s}
        if len(syms) < 2:
            continue
        steps = list({st.id: st for st, _ in recs}.values())
        loc = locality_of(list(syms.values()))
        out.append(_finding(
            "CoI", list(syms.values()) + steps, None, len(syms), loc,
            f"The same instance {ident} is shared across {len(syms)} symbols "
            f"({', '.join(s.attrs.get('qualname','?') for s in syms.values())}) "
            f"— they are connascent on identity."))
    return out


DETECTORS = {
    "CoN": lambda g, a: detect_CoN(g, a.min_degree, a.exclude_external)
                        + detect_records(g, a.min_degree),
    "CoT": lambda g, a: detect_CoT(g),
    "CoM": lambda g, a: detect_CoM(g, a.exclude_external),
    "CoP": lambda g, a: detect_CoP(g, a.min_degree),
    "CoA": lambda g, a: detect_CoA(g),
    "CoE": lambda g, a: detect_CoE(g),
    "CoTm": lambda g, a: detect_CoTm(g),
    "CoV": lambda g, a: detect_CoV(g, a.exclude_external),
    "CoI": lambda g, a: detect_CoI(g),
}
PROVISIONAL = {"CoA"}


def run(g: Graph, args) -> list:
    only = set(args.only.split(",")) if args.only else set(DETECTORS)
    findings = []
    for kind, fn in DETECTORS.items():
        if kind not in only:
            continue
        if kind in PROVISIONAL and not args.include_provisional:
            continue
        findings.extend(fn(g, args))
    findings.sort(key=lambda f: f["severity"], reverse=True)
    return findings


def _fmt_report(findings: list) -> str:
    if not findings:
        return "No connascence findings."
    lines = [f"{len(findings)} findings (worst first):\n"]
    for f in findings:
        tag = " [provisional]" if f["confidence"] != "firm" else ""
        lines.append(
            f"  sev {f['severity']:>6.0f}  {f['kind']:5} {f['name']:<18} "
            f"deg={f['degree']} loc={f['locality']}{tag}")
        lines.append(f"        {f['rationale']}")
        lines.append(f"        → {f['refactor']}\n")
    return "\n".join(lines)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Detect connascence in a trace graph.")
    ap.add_argument("--input", "-i", required=True, help="TraceDoc; - for stdin")
    ap.add_argument("--format", choices=["report", "json"], default="report")
    ap.add_argument("--only", help="comma-separated kinds, e.g. CoI,CoV")
    ap.add_argument("--min-degree", type=int, default=3, help="threshold for CoN")
    ap.add_argument("--include-provisional", action="store_true")
    ap.add_argument("--exclude-external", action="store_true",
                    help="ignore calls to unresolved/builtin (ext:) symbols — "
                         "surfaces actionable, in-codebase connascence")
    args = ap.parse_args(argv)

    g = load_graph(read_input(args.input))
    findings = run(g, args)

    if args.format == "json":
        print(json.dumps(findings, indent=2))
    else:
        print(_fmt_report(findings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
