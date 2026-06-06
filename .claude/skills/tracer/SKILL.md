---
name: tracer
description: Build a call trace from code (a static call graph and/or a real runtime trace) where every step is a node linked to its caller, carry the data passed at each step, and expose connascences (Page-Jones coupling taxonomy) as a ranked analysis layer. Operates entirely on local JSON — no server or database. Use when the user wants to map call flow, trace a function's callers/callees, reason about what data is passed between calls, find coupling/connascence, locate hidden shared state or execution-order dependencies, assess change/blast radius, or prioritize refactoring by coupling strength. Trigger on "call trace", "call graph", "trace this code", "who calls / what does it call", "connascence", "coupling analysis", "shared state", "hidden dependency", "execution order dependency", "blast radius", "impact radius", or whenever reasoning about how data flows between functions matters.
---

# Tracer — call traces → connascence

This skill models a program's **call trace** as a graph, then exposes
**connascence** — Meilir Page-Jones's coupling taxonomy — as a ranked analysis
layer over it.

A call trace is a DAG: each step points back at the step that invoked it. The
skill builds that graph from a language adapter's output, holds it in memory,
and runs detectors that surface the couplings the trace makes visible.

**No server, no database, no network.** Everything is plain JSON files and
stdlib scripts. A trace is derived data — regenerable from source at any time —
so it lives in files you keep, diff, or throw away; there is nothing to register
or persist remotely. (Findings are plain JSON too; store them wherever your
project already keeps artifacts.)

## What it answers

- *Who calls this, and what do they pass?* — the call graph + per-call data.
- *What is secretly coupled? / What's the blast radius of changing X?* —
  connascence: the same instance threaded through many functions (Identity), a
  magic value duplicated across sites (Meaning), an execution-order contract no
  signature declares (Order), positional-argument fragility (Position), a dict
  read by N string keys coupled to its producer (record shape), …
- *What should I refactor first?* — findings ranked by
  `strength × degree × locality`.

## Architecture

Two layers, plus the instances you analyze:

| Layer | What it is | Lives in |
|---|---|---|
| **Data model** | the four node kinds (`TraceSymbol`, `TraceStep`, `TraceToken`, `TraceConn`) and their typed links; checked structurally by the loader | `references/schemas.json` + `scripts/trace_io.py` |
| **Catalog** | the 9 connascence kinds, strength/degree/locality, detection + refactoring guidance | `scripts/connascence.py` (opaque `kind` keys) |
| **Instances** | one codebase/run's symbols, steps, tokens, findings | local **JSON** (a TraceDoc; findings JSON) |

### The four node types

| Type | Purpose | Links | Key attributes |
|---|---|---|---|
| `TraceSymbol` | a function/method definition (or a synthetic `record`) | (none) | `qualname`, `kind`, `file`, `line`, `package`, `module`, `params`, `returns_type` |
| `TraceStep` | a call site (static) **or** an invocation (dynamic) **or** a field access | `callee`→Symbol, `in_symbol`→Symbol, `caller`→Step, `realizes`→Step, `args`→Token[], `returns`→Token | `site_file`, `site_line`, `arg_style`, `order`, `ts`, `thread`, `callee_qualname`, `access` |
| `TraceToken` | a passed/returned value, or a dict key | (none) | `type`, `repr`, `identity` (run-scoped), `value_hash`, `is_literal`, `literal_repr`, `key` |
| `TraceConn` | a detected connascence (overlay) | `elements`→(Step\|Token\|Symbol)[], `locus`→Symbol | `kind`, `dynamic`, `strength_rank`, `degree`, `locality`, `severity`, `rationale`, `refactor`, `confidence`, `subkind` |

**Static spine + dynamic overlay in two links:** `in_symbol` (where a call is
written) builds the static call graph; `caller` (who actually invoked) builds
the runtime tree; `realizes` joins a dynamic step to the static call site it
executed. A `static`, `dynamic`, or `merged` document all use the same types.
Cross-references are adapter-assigned string ids, resolved in memory by
`trace_io.py` — see `references/query-recipes.md`.

## Language-agnostic ingest

The skill **never parses source**. Per-language **adapters** emit one JSON
document — the **TraceDoc** (`references/ingest-contract.md`) — and the skill
ingests it. Static and dynamic dumps share the shape, so they merge.

| adapter | language | spine | emits | lights up |
|---|---|---|---|---|
| `adapters/python_ast.py` | Python | static | symbols + call sites + literal args + **dict-key access** (stdlib `ast`) | CoN, CoT, CoM, CoP, record-shape |
| `adapters/typescript_ast.mjs` | TS / JS | static | same + **property/element record access** (`row.k` / `row["k"]`), via the TypeScript compiler API | CoN, CoT, CoM, CoP, record-shape |
| `adapters/php_ast.php` | PHP | static | same + **array-dim record access** (`$row['k']`), via nikic/php-parser | CoN, CoT, CoM, CoP, record-shape |
| `adapters/ruby_ast.rb` | Ruby | static | same + **hash-access record shape** (`row[:k]` / `.fetch(:k)`), via prism | CoN, CoT, CoM, CoP, record-shape |
| `adapters/python_settrace.py` | Python | dynamic | real values, identities, order, threads (`sys.settrace`) | CoE, CoTm, CoV, CoI |

Run a **static** adapter for breadth (whole codebase, no execution) and a
**dynamic** adapter for the strong, otherwise-invisible couplings; merge by
`(callee qualname, site_file, site_line)`. Other languages (ruby-prof reader,
V8 cpuprofile, …) are separate adapters emitting the same JSON.

`typescript_ast.mjs` needs the `typescript` package — `npm i -D typescript` in
the project being analyzed (it resolves the compiler from your cwd).
`php_ast.php` needs PHP + nikic/php-parser — `composer require --dev
nikic/php-parser` in the project (it finds `vendor/autoload.php` from your cwd or
`COMPOSER_VENDOR`). `ruby_ast.rb` needs Ruby 3.4+ (prism is bundled) or
`gem install prism` on older Rubies — no other deps. The Python adapters are
stdlib-only.

> **Ruby + CoT.** Ruby has no inline parameter types, so CoT fires on *every*
> parameter — uniformly uninformative. When analyzing Ruby, focus with
> `trace-detect.py --only CoN,CoP,CoM,CoV,CoI` (or just read past the CoT block).

## Workflow

### Step 1 — Get a TraceDoc

Run an adapter to produce a TraceDoc — static call graph, a runtime trace, or
both pre-merged:

```bash
python3 scripts/adapters/python_ast.py src/ --module-root src > doc.json     # static (Python)
node    scripts/adapters/typescript_ast.mjs src --module-root src > doc.json  # static (TS/JS)
# dynamic: instrument with scripts/adapters/python_settrace.py (see its header)
```

### Step 2 — Validate and dedup

```bash
python3 scripts/trace-ingest.py --input doc.json            # validate + stats
python3 scripts/trace-ingest.py --input doc.json --dedup > doc.dedup.json
```

Dedup collapses tokens by `(value_hash, identity, type)` — important on a real
runtime trace, where millions of token occurrences become a handful of classes.

### Step 3 — Detect connascence

```bash
python3 scripts/trace-detect.py --input doc.dedup.json                 # ranked report
python3 scripts/trace-detect.py --input doc.dedup.json --format json   # findings as JSON
python3 scripts/trace-detect.py --input doc.dedup.json --only CoI,CoV  # subset
python3 scripts/trace-detect.py --input doc.dedup.json --exclude-external # in-codebase only
python3 scripts/trace-detect.py --input doc.dedup.json --include-provisional  # + CoA
```

On a real static dump, pass **`--exclude-external`**: calls to unresolved /
builtin (`ext:`) symbols (`len`, `.get`, `logging.info`) otherwise dominate
CoN/CoV by raw degree but aren't actionable — you can't rename `len`. The flag
drops them and surfaces the in-codebase connascence you can act on.

`--format json` writes the findings as a plain array — keep it as your project's
coupling report, diff it across commits, or feed it to another tool.

### Step 4 — Report and render

Findings are pre-sorted by severity (worst coupling first). Render the graph:

```bash
python3 scripts/trace-render.py --input doc.dedup.json > trace.dot
python3 scripts/trace-render.py --input doc.dedup.json --conn-only   # just the overlay
dot -Tsvg trace.dot -o trace.svg
```

### Step 5 — (optional) Validate graph structure

```bash
python3 scripts/trace-validate.py --input doc.dedup.json
```

Checks structural well-formedness (every step has a resolvable `callee`,
`caller`/`realizes` resolve to steps, args/returns to tokens, the dynamic
caller-tree is acyclic, identities are run-scoped). Useful when writing a new
adapter.

## The connascence catalog

Weakest → strongest. Refactoring lowers **strength**, lowers **degree**, raises
**locality**. Full detail in `references/trace-cheatsheet.md`;
`python3 scripts/connascence.py --json` is the machine-readable contract.

| # | key | name | static/dyn | the trace signal |
|---|-----|------|-----------|------------------|
| 1 | `CoN`  | Name | static | a symbol referenced by many call sites (rename blast-radius); a dict read by N string keys (record shape, `subkind: record_shape`) |
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

- **Identities are run-scoped.** `identity` is only meaningful within one
  `run_id`; CoI/CoTm never claim coupling across runs. `value_hash` *is*
  cross-run, so CoV/CoA may span runs.
- **CoA is provisional.** Algorithm connascence is heuristic — only emitted with
  `--include-provisional`, always tagged `confidence: "provisional"`.
- **Never put secrets in `repr`/`literal_repr`.** Adapters truncate and should
  redact before emitting a token.
- **A trace is derived, not source of truth.** Regenerate it from code rather
  than treating a stored TraceDoc as authoritative; re-running an adapter is the
  cheap, correct refresh.

## When to use this skill vs prose

Use the tracer when:
- mapping call flow across many functions/modules,
- reasoning about *what data* crosses call boundaries,
- hunting coupling/connascence, hidden shared state, or order dependencies,
- assessing the blast radius of a change, or prioritizing a refactor by
  coupling strength.

Prose is fine for a single short call chain or a throwaway question.

## Scripts (`scripts/` — stdlib Python 3, no server)

- `connascence.py` — the catalog; `--json` contract, `--kind <k>` detail.
- `trace_io.py` — shared loader; normalizes a TraceDoc into one in-memory graph;
  locality + severity scoring.
- `trace-ingest.py` — validate a TraceDoc; `--dedup` tokens.
- `trace-detect.py` — the 9 detectors (+ record-shape); `--format report|json`,
  `--only`, `--min-degree`, `--exclude-external`, `--include-provisional`;
  ranked by severity.
- `trace-validate.py` — structural well-formedness checks.
- `trace-render.py` — Graphviz DOT (`--conn-only` for the overlay alone).
- `adapters/python_ast.py` — static Python spine via `ast` (stdlib).
- `adapters/typescript_ast.mjs` — static TS/JS spine via the TypeScript compiler
  API (needs `npm i -D typescript`; run with `node`).
- `adapters/php_ast.php` — static PHP spine via nikic/php-parser (needs
  `composer require --dev nikic/php-parser`; run with `php`).
- `adapters/ruby_ast.rb` — static Ruby spine via prism (Ruby 3.4+ bundles it,
  else `gem install prism`; run with `ruby`).
- `adapters/python_settrace.py` — dynamic Python trace via `sys.settrace`.

## References

- `references/ingest-contract.md` — the language-agnostic TraceDoc JSON shape.
- `references/schemas.json` — the four node-type / link definitions (data model).
- `references/trace-cheatsheet.md` — connascence taxonomy, reduction rules,
  worked example.
- `references/query-recipes.md` — load-once + traversal/detection snippets.
- Page-Jones, *What Every Programmer Should Know About OO Design* (1995);
  connascence.io.
