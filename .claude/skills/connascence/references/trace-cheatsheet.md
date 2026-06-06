# Connascence cheatsheet

**Connascence** (Meilir Page-Jones, 1995): two software elements are
*connascent* when a change in one requires a change in the other to preserve
correctness. It is a vocabulary for the *coupling* a call trace makes visible.

The skill builds a call-trace graph in memory, then detects connascence as graph
queries over it. Each finding is a `TraceConn` node pointing at the coupled
elements.

## The taxonomy (weakest → strongest)

`strength` is the canonical ordering. Refactoring lowers strength, lowers
degree, and raises locality.

### Static — visible from source / the static spine

| # | key | name | what couples | trace signal |
|---|-----|------|--------------|--------------|
| 1 | `CoN`  | Name | callers ↔ a symbol's name | a symbol referenced by N call sites (rename blast-radius) |
| 1 | `CoN`* | Name+Meaning (record shape) | a dict's consumers ↔ its producer | a record read by N distinct string keys (`msg['id']`, `d.get('role')`) — degree N coupling to the producer's schema |
| 2 | `CoT`  | Type | callers ↔ a param's type | arg token type vs param type; undeclared params; type instability |
| 3 | `CoM`  | Meaning / Convention | sites sharing a magic value | a literal token with a magic value reused at ≥2 sites |
| 4 | `CoP`  | Position | call site ↔ def argument order | ≥2 positional args into a callee with ≥2 order-sensitive params |
| 5 | `CoA`  | Algorithm | producer ↔ consumer of a shared algorithm | paired value_hash exchange (encode/decode, sign/verify) — *provisional* |

### Dynamic — only visible with a real runtime trace

| # | key | name | what couples | trace signal |
|---|-----|------|--------------|--------------|
| 6 | `CoE`  | Execution Order | steps whose order is load-bearing | A-on-X always precedes B-on-X for a shared identity |
| 7 | `CoTm` | Timing | concurrent participants | a shared identity touched by ≥2 threads |
| 8 | `CoV`  | Value | sites that must agree on a value | the same value_hash required across ≥2 symbols |
| 9 | `CoI`  | Identity | symbols sharing one instance | the same non-null identity across ≥2 symbols |

`*` **Record-shape connascence** is a CoN sub-finding (`subkind: record_shape`):
stringly-typed record access (`row['user_id']`, `row.userId`) couples every
reader to the record's producer by the *name and meaning* of each key. Both
adapters model each access against a synthetic `record` symbol; the detector
reports the field set (degree = distinct keys). Refactor by parsing into a typed
dataclass / `from_db_row` / typed interface at the boundary.

- **Python** captures `base[key]` and `base.get('key')`/`.pop`/`.setdefault`.
- **TypeScript** captures `row["key"]` (element access) and `row.key`
  (property access), using the type checker to keep property access precise —
  only `any`, index-signature (`Record<>`), interface, and object-literal bases
  count; class instances, methods, arrays, namespaces, and builtins are excluded.
- **PHP** captures `$row['key']` (array-dim — PHP's associative-array DB-row /
  JSON pattern). Property fetch `$obj->prop` is *not* captured: with no type
  checker it's indistinguishable from class-member access.
- **Ruby** captures `row[:key]` / `row['key']` and `row.fetch(:key)` (Ruby's
  params-hash / JSON / DB-row pattern). Symbol and string keys both count.
- **Dart** captures `row['key']` (index access — Map / JSON / decoded-row
  pattern). Dart writes parameter types inline, so CoT is informative here.

The static spine alone yields kinds 1–5; a dynamic overlay (real values,
identities, ordering, threads) unlocks 6–9. This is why the skill supports
**both** — a static call graph gives breadth, a runtime trace gives the strong,
otherwise-invisible couplings.

## The three reduction rules

When a finding bites, you have three levers (in order of preference):

1. **Lower strength** — convert a strong/dynamic connascence into a weaker/static
   one. E.g. an execution-order contract (CoE) → a builder/state-machine that
   makes order a type error.
2. **Lower degree** — reduce how many elements are bound. A magic value at 9
   sites (CoM, degree 9) → one named constant (degree 1 at the definition).
3. **Raise locality** — move connascent elements closer. Cross-package identity
   sharing (CoI) is far worse than two methods on the same class; pull the
   coupling inward.

## Severity

The ranked refactoring list sorts by

```
severity = strength × degree × locality_penalty
```

with `locality_penalty`: same_function 1 · same_class 2 · same_module 3 ·
same_package 5 · cross_package 8 · cross_service 13.

So the worst offenders are **strong + high-degree + far-apart** — exactly
Page-Jones's prioritization, computed instead of guessed.

## Static vs dynamic, mapped to the graph

| | static spine | dynamic overlay |
|---|---|---|
| node | `TraceStep` with `in_symbol` set | `TraceStep` with `caller` set |
| edges | call sites inside functions | actual invocations (a tree by `order`) |
| data | `arg_style`, literals | `DataToken` per arg/return: type, identity, value_hash |
| join | — | `realizes` link → the static step executed |
| finds | CoN, CoT, CoM, CoP, (CoA) | CoE, CoTm, CoV, CoI (+ confirms CoA) |

## Worked example (from the reference adapter)

Tracing a tiny banking program where one `Account` instance flows through
`__init__ → transfer → withdraw → deposit → settle`:

- **CoI, degree 5, severity 135** — the same `Account` instance is referenced by
  5 symbols. The strongest coupling in the program: any of them could mutate it.
  *Refactor:* a single owner / explicit injection.
- **CoV, degree 3** — the transfer amount `30` must agree across `transfer`,
  `withdraw`, `deposit`. *Refactor:* compute once, pass through.
- **CoE, degree 2** — `withdraw` consistently precedes `deposit` on the shared
  account. *Refactor:* make the order explicit so callers can't invert it.

None of these are visible in the source text — they only emerge from the trace.

## References

- Page-Jones, M. (1995). *What Every Programmer Should Know About Object-Oriented
  Design.* Dorset House. (Connascence taxonomy.)
- connascence.io — the modern restatement of the taxonomy.
- `scripts/connascence.py` — the machine-readable catalog (`--json`).
