# Query recipes

The tracer holds the graph in memory and runs everything as plain dict
comprehensions over it — no server, no query language. These recipes show the
patterns the scripts use; reach for them for ad-hoc inspection or when writing a
new detector. The `scripts/` already implement all of this.

## Load the graph

`trace_io.load_graph` normalizes a TraceDoc (or a flat items list) into a
`Graph` of `Node`s keyed by id. Every link value is an id into the same graph.

```python
import json
from trace_io import load_graph

g = load_graph(json.load(open("doc.dedup.json")))
g.symbols()          # all TraceSymbol nodes
g.steps()            # all TraceStep nodes
g.tokens()           # all TraceToken nodes
g.conns()            # all TraceConn findings
g.get(some_id)       # node by id (link targets resolve here)
g.callee_of(step)    # the TraceSymbol a step calls
```

A `Node` has `.id`, `.type`, `.attrs` (dict), `.links` (name → id or [ids]).
`node.link_ids("args")` returns a flat list for single- or many-links.

## Read — over the in-memory graph

### Inferences/sites leading to a symbol (the static spine)

```python
sym = next(s for s in g.symbols() if s.attrs.get("qualname") == "pkg.mod.f")
sites = [st for st in g.steps() if st.links.get("callee") == sym.id]
# len(sites) == the symbol's call-site count (CoN degree / rename blast-radius)
```

### The dynamic call tree from a root step

```python
def subtree(step):
    kids = [s for s in g.steps() if s.links.get("caller") == step.id]
    return {"step": step, "calls": [subtree(k) for k in kids]}
```

### Everywhere an instance flows (the CoI query, by hand)

```python
ident = "obj:0x7f…@run"
touch = [st for st in g.steps()
         if any((g.get(t) or Node("", "")).attrs.get("identity") == ident
                for t in st.link_ids("args") + [st.links.get("returns")])]
symbols = {g.callee_of(st).attrs.get("qualname") for st in touch if g.callee_of(st)}
# |symbols| >= 2  →  Connascence of Identity, degree |symbols|
```

`scripts/trace-detect.py` runs this and the other eight detectors for you.

### A record's field set (record-shape coupling)

```python
rec = next(s for s in g.symbols() if s.attrs.get("kind") == "record")
keys = {g.get(t).attrs["key"]
        for st in g.steps() if st.links.get("callee") == rec.id
        for t in st.link_ids("args") if g.get(t) and g.get(t).attrs.get("key")}
# len(keys) distinct keys read off this record → CoN+Meaning with its producer
```

### Findings on a node

```python
node_id = sym.id
hits = [c for c in g.conns() if node_id in c.link_ids("elements")]
```

## Locality & severity

`trace_io.locality_of([symbols…])` returns the coarsest shared bucket
(`same_function` … `cross_package`), and `trace_io.severity(strength, degree,
locality)` applies the penalty table. Detectors use these so every finding is
ranked consistently.

## Render

```bash
python3 scripts/trace-render.py --input doc.dedup.json > trace.dot
python3 scripts/trace-render.py --input doc.dedup.json --conn-only   # overlay only
dot -Tsvg trace.dot -o trace.svg
```

TraceSymbol = ellipse, TraceStep = box, TraceConn = colored edge (cool = weak
static, hot = strong dynamic).

## Persisting findings

Findings are plain JSON (`trace-detect.py --format json` → an array of finding
objects). Keep that file as your project's coupling report, diff it across
commits to watch coupling drift, or feed it to any downstream tool. There is no
required store — the graph is regenerated from source whenever you re-run an
adapter.
