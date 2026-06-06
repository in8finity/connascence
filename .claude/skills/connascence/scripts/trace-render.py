#!/usr/bin/env python3
"""Render a trace graph to Graphviz DOT.

  python3 trace-render.py --input dump.json > trace.dot
  python3 trace-render.py --input dump.json --conn-only   # only connascence edges
  dot -Tsvg trace.dot -o trace.svg

Convention:
  TraceSymbol  → ellipse
  TraceStep    → box (label = callee qualname @ site_line)
  TraceConn    → colored edge between its elements; color by strength
                 (cool = weak/static, hot = strong/dynamic)

Stdlib only.
"""
from __future__ import annotations

import argparse
import html
import sys

from trace_io import load_graph, read_input

STRENGTH_COLOR = {
    1: "#4575b4", 2: "#74add1", 3: "#abd9e9", 4: "#e0f3f8", 5: "#fee090",
    6: "#fdae61", 7: "#f46d43", 8: "#d73027", 9: "#a50026",
}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def node_label(n) -> str:
    if n.type == "TraceSymbol":
        return esc(n.attrs.get("qualname") or n.id[:10])
    if n.type == "TraceStep":
        callee = n.attrs.get("callee_qualname") or "?"
        line = n.attrs.get("site_line")
        return esc(f"{callee}" + (f"@{line}" if line else ""))
    if n.type == "TraceToken":
        return esc(n.attrs.get("repr") or n.attrs.get("type") or n.id[:8])
    return esc(n.id[:8])


def render(g, conn_only: bool) -> str:
    out = ["digraph trace {", '  rankdir=LR;',
           '  node [fontname="Helvetica", fontsize=10];',
           '  edge [fontname="Helvetica", fontsize=8];']

    if not conn_only:
        for s in g.symbols():
            out.append(f'  "{s.id}" [shape=ellipse, style=filled, '
                       f'fillcolor="#eef", label="{node_label(s)}"];')
        for st in g.steps():
            out.append(f'  "{st.id}" [shape=box, label="{node_label(st)}"];')
        # structural edges
        for st in g.steps():
            if st.links.get("callee"):
                out.append(f'  "{st.id}" -> "{st.links["callee"]}" '
                           f'[color="#999", arrowhead=onormal];')
            if st.links.get("caller"):
                out.append(f'  "{st.links["caller"]}" -> "{st.id}" '
                           f'[color="#333", style=bold, label="calls"];')
            elif st.links.get("in_symbol"):
                out.append(f'  "{st.links["in_symbol"]}" -> "{st.id}" '
                           f'[color="#bbb", style=dashed, label="site"];')

    # connascence edges
    for c in g.conns():
        kind = c.attrs.get("kind", "?")
        strength = int(c.attrs.get("strength_rank", 5) or 5)
        color = STRENGTH_COLOR.get(strength, "#888")
        els = c.link_ids("elements")
        sev = c.attrs.get("severity", "")
        label = f'{kind} (sev {sev})' if sev != "" else kind
        # connect element pairs (star from first element)
        if len(els) >= 2:
            hub = els[0]
            for e in els[1:]:
                out.append(f'  "{hub}" -> "{e}" [color="{color}", '
                           f'penwidth=2, constraint=false, dir=none, '
                           f'label="{esc(label)}"];')
    out.append("}")
    return "\n".join(out)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Render a trace graph to DOT.")
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--conn-only", action="store_true")
    args = ap.parse_args(argv)
    g = load_graph(read_input(args.input))
    print(render(g, args.conn_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
