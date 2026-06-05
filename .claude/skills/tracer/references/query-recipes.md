# Query recipes

All examples use the `mcp__hashharness__*` MCP tools. Hashes are abbreviated
`h:…`. The scripts (`trace_io.py`) do all of this for you — these recipes are
for ad-hoc inspection and for understanding what the detectors compute.

## Create (materialize a TraceDoc)

`create_item` returns the new item's **`record_sha256`** — the value you pass as
a link target later (not `text_sha256`). Materialize in dependency order
(`trace-ingest.py --plan` emits exactly this order):

1. **Symbols** → build `{adapter_id: record_sha256}`.
2. **Tokens** (no links) → extend the map.
3. **Steps** caller-before-callee; resolve `callee`/`in_symbol`/`caller`/
   `realizes`/`args`/`returns` through the map.
4. **TraceConn** findings last (after detection).

```
mcp__hashharness__create_item(
  type="TraceSymbol", work_package_id="trace:billing:main",
  title="billing.transfer",
  text="def transfer(a, b, amt)",
  attributes={"qualname":"billing.transfer","module":"billing","package":"billing",
              "params":[{"name":"a","position":0},{"name":"b","position":1},
                        {"name":"amt","position":2}]})
→ record_sha256 h:sym1

mcp__hashharness__create_item(
  type="TraceStep", work_package_id="trace:billing:main",
  title="call transfer", text="transfer(a,b,30)",
  attributes={"order":3,"thread":"main","site_line":12},
  links={"callee":"h:sym1","caller":"h:step1","args":["h:t1","h:t2","h:t3"]})
```

Name the work package with a **`trace:`** prefix so
`list_work_packages(prefix="trace:")` enumerates every trace in one call.

## Read — load once, traverse in memory

A trace is bounded after dedup, so fetch the whole work package once and build
the `by_record` map (same idiom as aif-arguments):

```
items     = get_work_package(work_package_id="trace:billing:main")["items"]
by_record = {it["record_sha256"]: it for it in items}   # link targets resolve here
```

> **Link-id vs get-by-hash.** Links hold `record_sha256`, but
> `get_item_by_hash` takes `text_sha256` and `find_items` can't filter by link
> value. So resolve every link through `by_record` — never try to "follow a
> link" with a single get-by-hash call.

### The static spine under a symbol

```
sites = [it for it in items if it["type"]=="TraceStep"
         and (it.get("links") or {}).get("callee")==symbol_hash]
```

### The dynamic call tree from a root step

```
def subtree(step_hash):
    kids = [it for it in items if it["type"]=="TraceStep"
            and (it.get("links") or {}).get("caller")==step_hash]
    return {"step": by_record[step_hash],
            "calls": [subtree(k["record_sha256"]) for k in kids]}
```

### Everywhere an instance flows (the CoI query, by hand)

```
ident = "obj:0x7f…@run"
touch = [it for it in items if it["type"]=="TraceStep"
         and any(by_record.get(t,{}).get("attributes",{}).get("identity")==ident
                 for t in ((it.get("links") or {}).get("args") or [])
                          + [ (it.get("links") or {}).get("returns") ])]
symbols = { by_record[(it["links"]["callee"])]["attributes"]["qualname"] for it in touch }
# |symbols| ≥ 2  →  Connascence of Identity, degree |symbols|
```

`scripts/trace-detect.py` runs this and the other eight detectors for you.

### Findings on a node

```
conns = [it for it in items if it["type"]=="TraceConn"
         and node_hash in ((it.get("links") or {}).get("elements") or [])]
```

## Cross-trace fallback — `find_items` by type

To query *across* traces (not one work package), list by type and filter
client-side (`find_items` filters by `type`/`attributes`, never by link value):

```
mcp__hashharness__find_items(type="TraceConn", attributes={"kind":"CoI"})
```

## Verify — audit the whole trace

```
mcp__hashharness__verify_work_package(work_package_id="trace:billing:main", summary=true)
```

A trace + its findings is a **multi-root DAG** (symbols, free-floating TraceConn
overlays), so `verify_work_package` (re-hashes *every* record) is the right
audit — a single-root `verify_chain` walk would skip orphan findings. Fold the
result into the structural report:

```
# verify_work_package(..., summary=false) → vwp.json
python3 trace-validate.py --input dump.json --crypto vwp.json
```

## Render

```
python3 trace-render.py --input dump.json > trace.dot && dot -Tsvg trace.dot -o trace.svg
python3 trace-render.py --input dump.json --conn-only   # just the connascence overlay
```

TraceSymbol = ellipse, TraceStep = box, TraceConn = colored edge (cool = weak
static, hot = strong dynamic).
