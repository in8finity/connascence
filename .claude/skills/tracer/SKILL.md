---
name: tracer
description: Build a call trace from code (a static call graph and/or a real runtime trace), store every step as a hash-chained node in hashharness linked to its caller, carry the data passed at each step, and expose connascences (Page-Jones coupling taxonomy) as a ranked, queryable analysis layer. Use when the user wants to map call flow, trace a function's callers/callees, reason about what data is passed between calls, find coupling/connascence, locate hidden shared state or execution-order dependencies, or prioritize refactoring by coupling strength. Trigger on "call trace", "call graph", "trace this code", "who calls / what does it call", "connascence", "coupling analysis", "shared state", "hidden dependency", "execution order dependency", or whenever reasoning about how data flows between functions matters.
---

# Tracer — call traces → connascence in Hashharness

This skill models a program's **call trace** as a graph in **hashharness** (an
append-only, hash-chained content store via the `mcp__hashharness__*` tools),
then exposes **connascence** — Meilir Page-Jones's coupling taxonomy — as a
ranked analysis layer over it.

A call trace is a DAG: each step points back at the step that invoked it. That
makes it the same shape as an AIF argument graph (premises → inference →
conclusion), so this skill is the structural twin of `aif-arguments`. The
storage idioms are identical; only the ontology differs.

The append-only nature is a feature: a trace never mutates. A re-run is a new
overlay; a corrected finding is a new node. Every node is content-addressed, so
references are stable and verifiable.

## What it answers

- *Who calls this, and what do they pass?* — the call graph + per-call data.
- *What is secretly coupled?* — connascence: the same instance threaded through
  many functions (Identity), a magic value duplicated across sites (Meaning), an
  execution-order contract no signature declares (Order), positional-argument
  fragility (Position), …
- *What should I refactor first?* — findings ranked by
  `strength × degree × locality`.

## Architecture (three layers — like aif-arguments)

| Layer | What it is | Lives in |
|---|---|---|
| **1 — node kinds + structural rules** | `TraceSymbol`, `TraceStep`, `TraceToken`, `TraceConn` with typed links; server-enforced | hashharness **schema** (`references/schemas.json`) |
| **2 — catalog** | the 9 connascence kinds, strength/degree/locality, detection + refactoring guidance | `scripts/connascence.py` (opaque `kind` keys) |
| **3 — instances** | one codebase/run's symbols, steps, tokens, findings | hashharness **items** under a `trace:` work package |

### The four node types

| Type | Purpose | Links | Key attributes |
|---|---|---|---|
| `TraceSymbol` | a function/method definition | (none) | `qualname`, `kind`, `file`, `line`, `package`, `module`, `params`, `returns_type` |
| `TraceStep` | a call site (static) **or** an invocation (dynamic) | `callee`→Symbol, `in_symbol`→Symbol, `caller`→Step, `realizes`→Step, `args`→Token[], `returns`→Token | `site_file`, `site_line`, `arg_style`, `order`, `ts`, `thread`, `callee_qualname` |
| `TraceToken` | a passed/returned value | (none) | `type`, `repr`, `identity` (run-scoped), `value_hash`, `is_literal`, `literal_repr` |
| `TraceConn` | a detected connascence (overlay) | `elements`→(Step\|Token\|Symbol)[], `locus`→Symbol | `kind`, `dynamic`, `strength_rank`, `degree`, `locality`, `severity`, `rationale`, `refactor`, `confidence` |

**Static spine + dynamic overlay in two links:** `in_symbol` (where a call is
written) builds the static call graph; `caller` (who actually invoked) builds
the runtime tree; `realizes` joins a dynamic step to the static call site it
executed. A `static`, `dynamic`, or `merged` document all use the same types.

All cross-references are `record_sha256` content-addresses → a Merkle DAG,
tamper-evident and replayable.

## Language-agnostic ingest

The skill **never parses source**. Per-language **adapters** emit one JSON
document — the **TraceDoc** (`references/ingest-contract.md`) — and the skill
ingests it. Static and dynamic dumps share the shape, so they merge.

| adapter | language | spine | emits | lights up |
|---|---|---|---|---|
| `adapters/python_ast.py` | Python | static | symbols + call sites + literal args + **dict-key access** (stdlib `ast`) | CoN, CoT, CoM, CoP, record-shape |
| `adapters/typescript_ast.mjs` | TS / JS | static | same + **property/element record access** (`row.k` / `row["k"]`), via the TypeScript compiler API | CoN, CoT, CoM, CoP, record-shape |
| `adapters/python_settrace.py` | Python | dynamic | real values, identities, order, threads (`sys.settrace`) | CoE, CoTm, CoV, CoI |

Run a **static** adapter for breadth (whole codebase, no execution) and a
**dynamic** adapter for the strong, otherwise-invisible couplings; merge by
`(callee qualname, site_file, site_line)`. Other languages (ruby-prof reader,
V8 cpuprofile, …) are separate adapters emitting the same JSON.

`typescript_ast.mjs` needs the `typescript` package — `npm i -D typescript` in
the project being analyzed (it resolves the compiler from your cwd). The Python
adapters are stdlib-only.

## Workflow

### Step 1 — Register the schema

`mcp__hashharness__set_schema` stores a **full snapshot and REPLACES the
effective type set** — it does *not* merge a delta. The version chain is
append-only, but each new head is exactly the payload you submit. So:

1. `mcp__hashharness__get_schema` → the current full type map (dozens of types
   from other skills: `Aif*`, `Atam*`, `Stipo*`, `Task*`, …).
2. If `TraceSymbol`/`TraceToken`/`TraceStep`/`TraceConn` are already present,
   stop — nothing to do.
3. Otherwise **merge**: take the full current map, add the four Trace types from
   `references/schemas.json`, and submit the **entire merged set** via
   `set_schema(expected_prev=<current head record_sha256>, schema=<merged>)`.

> ⚠️ **Never submit only the four Trace types.** That replaces the head with a
> 4-type schema, dropping every other skill's types from the effective schema
> (new writes by those skills then fail). Existing items survive — they bind to
> their immutable creation-time version — but the shared head is broken until
> restored. Get `expected_prev` from `get_schema_history` (last version's
> `record_sha256`); a stale value is rejected with "schema head moved".

### Step 2 — Get a TraceDoc

Run an adapter to produce a TraceDoc — static call graph, a runtime trace, or
both pre-merged:

```bash
python3 scripts/adapters/python_ast.py src/ --module-root src > static.json   # static (Python)
node    scripts/adapters/typescript_ast.mjs src --module-root src > static.json  # static (TS/JS)
# dynamic: instrument with scripts/adapters/python_settrace.py (see its header)
```

Then validate and dedup tokens:

```bash
python3 scripts/trace-ingest.py --input doc.json            # validate + stats
python3 scripts/trace-ingest.py --input doc.json --dedup > doc.dedup.json
```

Dedup collapses tokens by `(value_hash, identity, type)` — essential before
materializing a real run (millions of token occurrences → a handful of classes).

### Step 3 — Materialize

`trace-ingest.py --plan` emits the ordered `create_item` plan
(Symbols → Tokens → Steps caller-before-callee). Create each item, mapping each
adapter `id` to the returned `record_sha256`, and resolve links through that map
(the link-id two-pass — see `references/query-recipes.md`). Use a
`trace:<service>:<entrypoint>` work-package id.

### Step 4 — Detect connascence

```bash
python3 scripts/trace-detect.py --input doc.dedup.json                 # ranked report
python3 scripts/trace-detect.py --input doc.dedup.json --format ca-stubs # TraceConn stubs
python3 scripts/trace-detect.py --input doc.dedup.json --only CoI,CoV    # subset
python3 scripts/trace-detect.py --input doc.dedup.json --exclude-external # in-codebase only
python3 scripts/trace-detect.py --input doc.dedup.json --include-provisional  # + CoA
```

Detectors run over the TraceDoc **or** a materialized hashharness dump — same
results either way (`trace_io.py` normalizes both). So you can detect *before*
materializing (cheap, no server) or *after* (auditable).

On a real static dump, pass **`--exclude-external`**: calls to unresolved /
builtin (`ext:`) symbols (`len`, `.get`, `logging.info`) otherwise dominate
CoN/CoV by raw degree but aren't actionable — you can't rename `len`. The flag
drops them and surfaces the in-codebase connascence that you can act on.

### Step 5 — Materialize findings

Feed `--format ca-stubs` output to `create_item` as `TraceConn` nodes, linking
`elements` to the implicated symbols/steps/tokens. This is the overlay — the
AIF-`CA` analog.

### Step 6 — Rank, report, render

Findings are pre-sorted by severity (worst coupling first). Render the graph:

```bash
python3 scripts/trace-render.py --input dump.json > trace.dot
python3 scripts/trace-render.py --input dump.json --conn-only   # just the overlay
dot -Tsvg trace.dot -o trace.svg
```

### Step 7 — Audit

```
mcp__hashharness__verify_work_package(work_package_id="trace:…", summary=true)
```

Re-hashes **every** record — the right audit for a multi-root DAG (symbols +
free-floating findings). Fold into the structural report:

```bash
python3 scripts/trace-validate.py --input dump.json --crypto vwp.json
```

## The connascence catalog (Layer 2)

Weakest → strongest. Refactoring lowers **strength**, lowers **degree**, raises
**locality**. Full detail in `references/trace-cheatsheet.md`;
`python3 scripts/connascence.py --json` is the machine-readable contract.

| # | key | name | static/dyn | the trace signal |
|---|-----|------|-----------|------------------|
| 1 | `CoN`  | Name | static | a symbol referenced by many call sites (rename blast-radius) |
| 2 | `CoT`  | Type | static | arg type vs param type; undeclared/unstable types |
| 3 | `CoM`  | Meaning | static | a magic literal reused at ≥2 sites |
| 4 | `CoP`  | Position | static | ≥2 positional args into ≥2 order-sensitive params |
| 5 | `CoA`  | Algorithm | static* | paired value_hash exchange (encode/decode) — *provisional* |
| 6 | `CoE`  | Execution Order | dynamic | A-on-X always precedes B-on-X |
| 7 | `CoTm` | Timing | dynamic | a shared instance touched by ≥2 threads |
| 8 | `CoV`  | Value | dynamic | the same value required across ≥2 symbols |
| 9 | `CoI`  | Identity | dynamic | the same instance shared across ≥2 symbols |

Kinds 1–5 come from the static spine; 6–9 need the dynamic overlay. `severity =
strength × degree × locality_penalty` (`same_function` 1 … `cross_service` 13).

## Conventions

- **Name every trace `trace:<service>:<entrypoint>[#run]`.** One prefix →
  store-wide discovery (`list_work_packages(prefix="trace:")`) and a one-call
  audit (`verify_work_package(work_package_ids=[…])`).
- **Dedup before materialize.** Never create an item per raw token occurrence.
- **Identities are run-scoped.** `identity` is only meaningful within one
  `run_id`; CoI/CoTm never claim coupling across runs. `value_hash` *is*
  cross-run, so CoV/CoA may span runs.
- **CoA is provisional.** Algorithm connascence is heuristic — only emitted with
  `--include-provisional`, always tagged `confidence: "provisional"`.
- **Never put secrets in `repr`/`literal_repr`.** Adapters truncate and should
  redact; tokens are content-addressed and permanent.
- **Hashes are opaque** — never parse them, always pass through. Links hold
  `record_sha256`; resolve them through the load-once `by_record` map, never via
  `get_item_by_hash` (which takes `text_sha256`).
- **Append, never mutate.** Supersede a wrong finding with a new node, don't edit.

## When to use this skill vs prose

Use the tracer when:
- mapping call flow across many functions/modules,
- reasoning about *what data* crosses call boundaries,
- hunting coupling/connascence, hidden shared state, or order dependencies,
- prioritizing a refactor by coupling strength.

Prose is fine for a single short call chain or a throwaway question.

## Scripts (`scripts/` — stdlib Python 3, no live server for analysis)

- `connascence.py` — Layer-2 catalog; `--json` contract, `--kind <k>` detail.
- `trace_io.py` — shared loader; normalizes a TraceDoc *or* a hashharness dump
  into one in-memory graph; locality + severity scoring.
- `trace-ingest.py` — validate a TraceDoc, `--dedup` tokens, `--plan` the
  ordered `create_item` materialization.
- `trace-detect.py` — the 9 detectors; `--format report|json|ca-stubs`,
  `--only`, `--min-degree`, `--include-provisional`; ranked by severity.
- `trace-validate.py` — Layer-1 structural well-formedness + `--crypto` fold-in.
- `trace-render.py` — Graphviz DOT (`--conn-only` for the overlay alone).
- `adapters/python_ast.py` — static Python spine via `ast` (stdlib).
- `adapters/typescript_ast.mjs` — static TS/JS spine via the TypeScript compiler
  API (needs `npm i -D typescript`; run with `node`).
- `adapters/python_settrace.py` — dynamic Python trace via `sys.settrace`.

## References

- `references/ingest-contract.md` — the language-agnostic TraceDoc JSON shape.
- `references/schemas.json` — the four hashharness type definitions (Layer 1).
- `references/trace-cheatsheet.md` — connascence taxonomy, reduction rules,
  worked example.
- `references/query-recipes.md` — load-once + traversal/detection snippets.
- Upstream: [hashharness](https://github.com/in8finity/hashharness);
  Page-Jones, *What Every Programmer Should Know About OO Design* (1995);
  connascence.io.
