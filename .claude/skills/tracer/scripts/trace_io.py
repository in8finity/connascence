"""Shared I/O + graph normalization for the tracer skill.

Every tracer script runs over a uniform in-memory graph regardless of source:

  * a raw **TraceDoc** (adapter ids, before materialization) — keys are the
    adapter-assigned `id` strings (`sym:…`, `step:…`, `tok:…`);
  * a **hashharness dump** (`{"items":[...]}` or a bare `find_items` list) —
    keys are `record_sha256` content-addresses.

Either way `load_graph()` returns a `Graph` whose nodes have `.type`, `.attrs`,
and `.links` (link values are ids into the same graph). Detectors, the
validator, and the renderer never care which form they came from.

Node types: TraceSymbol, TraceStep, TraceToken, TraceConn.

Stdlib only. No live hashharness server required.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

NODE_TYPES = ("TraceSymbol", "TraceStep", "TraceToken", "TraceConn")

# Which link names on each type are "single" (one id) vs "many" (list of ids).
LINK_ARITY = {
    "TraceStep": {
        "callee": "single",
        "in_symbol": "single",
        "caller": "single",
        "realizes": "single",
        "args": "many",
        "returns": "single",
    },
    "TraceConn": {
        "elements": "many",
        "locus": "single",
    },
    "TraceSymbol": {},
    "TraceToken": {},
}


@dataclass
class Node:
    id: str
    type: str
    attrs: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)  # name -> id (single) | [id,…] (many)

    def link_ids(self, name: str) -> list:
        """All ids for a link name, single or many, as a flat list."""
        v = self.links.get(name)
        if v is None:
            return []
        return list(v) if isinstance(v, list) else [v]


@dataclass
class Graph:
    nodes: dict  # id -> Node

    def of_type(self, t: str) -> list:
        return [n for n in self.nodes.values() if n.type == t]

    def get(self, nid: str | None) -> Node | None:
        return self.nodes.get(nid) if nid else None

    # ---- convenience accessors -------------------------------------------

    def symbols(self):
        return self.of_type("TraceSymbol")

    def steps(self):
        return self.of_type("TraceStep")

    def tokens(self):
        return self.of_type("TraceToken")

    def conns(self):
        return self.of_type("TraceConn")

    def callee_of(self, step: Node) -> Node | None:
        return self.get(step.links.get("callee"))

    def in_symbol_of(self, step: Node) -> Node | None:
        return self.get(step.links.get("in_symbol"))


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def read_input(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _is_tracedoc(obj: Any) -> bool:
    return isinstance(obj, dict) and (
        "symbols" in obj or "steps" in obj or "tokens" in obj
    ) and "items" not in obj


def _is_hh_dump(obj: Any) -> bool:
    if isinstance(obj, list):
        return True
    return isinstance(obj, dict) and "items" in obj


def load_graph(obj: Any) -> Graph:
    """Normalize a TraceDoc or hashharness dump into a Graph."""
    if _is_tracedoc(obj):
        return _from_tracedoc(obj)
    if _is_hh_dump(obj):
        return _from_hh(obj)
    raise ValueError(
        "input is neither a TraceDoc (symbols/steps/tokens) nor a "
        "hashharness dump (items / bare list)"
    )


def _from_hh(obj: Any) -> Graph:
    items = obj if isinstance(obj, list) else obj.get("items", [])
    nodes: dict = {}
    for it in items:
        nid = it.get("record_sha256") or it.get("id")
        if not nid:
            continue
        nodes[nid] = Node(
            id=nid,
            type=it.get("type", ""),
            attrs=dict(it.get("attributes") or {}),
            links=dict(it.get("links") or {}),
        )
    return Graph(nodes)


def _from_tracedoc(doc: dict) -> Graph:
    nodes: dict = {}
    run_id = doc.get("run_id")

    for s in doc.get("symbols", []):
        nodes[s["id"]] = Node(
            id=s["id"],
            type="TraceSymbol",
            attrs={k: v for k, v in s.items() if k != "id"},
            links={},
        )

    for t in doc.get("tokens", []):
        attrs = {k: v for k, v in t.items() if k != "id"}
        attrs.setdefault("run_id", run_id)
        nodes[t["id"]] = Node(id=t["id"], type="TraceToken", attrs=attrs, links={})

    for st in doc.get("steps", []):
        links = {}
        for ln in ("callee", "in_symbol", "caller", "realizes", "returns"):
            if st.get(ln) is not None:
                links[ln] = st[ln]
        if st.get("args"):
            links["args"] = list(st["args"])
        attrs = {
            k: v
            for k, v in st.items()
            if k not in ("id", "callee", "in_symbol", "caller", "realizes",
                         "args", "returns")
        }
        attrs.setdefault("run_id", run_id)
        # denormalize callee qualname for cheap filtering
        callee = nodes.get(st.get("callee"))
        if callee and "callee_qualname" not in attrs:
            attrs["callee_qualname"] = callee.attrs.get("qualname")
        nodes[st["id"]] = Node(id=st["id"], type="TraceStep", attrs=attrs, links=links)

    return Graph(nodes)


# --------------------------------------------------------------------------
# Locality scoring — shared by detectors
# --------------------------------------------------------------------------

LOCALITY_ORDER = [
    "same_function", "same_class", "same_module",
    "same_package", "cross_package", "cross_service",
]
LOCALITY_PENALTY = {
    "same_function": 1, "same_class": 2, "same_module": 3,
    "same_package": 5, "cross_package": 8, "cross_service": 13,
}


def _class_of(sym: Node) -> str | None:
    """The enclosing class of a symbol, or None for a module-level function.

    Module-aware: strips the `module` prefix from the qualname first, so a
    module-level function `pkg.mod.f` is NOT mistaken for a method of class
    `pkg.mod`. Needs a dotted remainder after the module to count as a class.
    """
    qual = sym.attrs.get("qualname")
    if not qual:
        return None
    module = sym.attrs.get("module")
    if module and qual.startswith(module + "."):
        suffix = qual[len(module) + 1:]
    elif module and qual == module:
        return None
    else:
        suffix = qual
    if "." not in suffix:
        return None  # module-level function
    cls = suffix.rsplit(".", 1)[0]
    return f"{module}.{cls}" if module else cls


def locality_of(symbols: Iterable[Node]) -> str:
    """Coarsest containment shared by a set of symbols → locality bucket."""
    syms = [s for s in symbols if s is not None]
    if not syms:
        return "cross_package"
    if len({s.id for s in syms}) == 1:
        return "same_function"
    classes = {_class_of(s) for s in syms}
    if len(classes) == 1 and None not in classes:
        return "same_class"
    modules = {s.attrs.get("module") for s in syms}
    if len(modules) == 1 and None not in modules:
        return "same_module"
    packages = {s.attrs.get("package") for s in syms}
    if len(packages) == 1 and None not in packages:
        return "same_package"
    return "cross_package"


def severity(strength: int, degree: int, locality: str) -> float:
    return float(strength * max(degree, 1) * LOCALITY_PENALTY.get(locality, 8))
