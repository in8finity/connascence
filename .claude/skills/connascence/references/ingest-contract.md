# TraceDoc — the language-agnostic ingest contract

The boundary between "any language" and the connascence skill. A per-language
**adapter** (Python AST, `sys.settrace` harness, ruby-prof reader, V8
cpuprofile reader, …) emits **one JSON document** in this shape. The skill
never parses source — it only consumes TraceDocs. Static and dynamic dumps are
the *same shape*; dynamic-only fields are optional, so they merge.

`scripts/trace-ingest.py` validates a TraceDoc against this contract and
optionally dedups its tokens.

## Document

```jsonc
{
  "version": "1",
  "kind": "static" | "dynamic" | "merged",   // required
  "run_id": "2026-06-04T12:00Z#pid4821",       // REQUIRED if dynamic + tokens (identities are run-scoped)
  "entrypoint": "pkg.cli.main",                // optional; a label for this trace
  "symbols": [ Symbol, … ],
  "steps":   [ Step,   … ],
  "tokens":  [ Token,  … ]                       // dynamic only
}
```

## Symbol — a definition site

```jsonc
{
  "id": "sym:pkg.mod.Class.method",   // adapter-stable; link targets reference it
  "qualname": "pkg.mod.Class.method", // REQUIRED
  "kind": "function|method|closure|builtin",
  "file": "src/foo.py", "line": 42,
  "package": "pkg",                    // for locality scoring — set this!
  "module":  "pkg.mod",               // for locality scoring — set this!
  "params": [
    {"name": "x", "position": 0, "kind": "positional|keyword|vararg|kwarg",
     "type": "int", "has_default": false}
  ],
  "returns_type": "str"
}
```

`package`/`module` drive locality scoring (`same_module` < `same_package` <
`cross_package`). Omitting them collapses everything to `cross_package` and
inflates severities — always populate them.

## Step — a call site (static) or invocation (dynamic)

A static call site and the dynamic invocation that realizes it are the **same
node type**; populated links/attrs distinguish them.

```jsonc
{
  "id": "step:…",                  // adapter-stable
  "callee": "sym:pkg.mod.f",       // REQUIRED → Symbol id (target)
  "in_symbol": "sym:pkg.mod.g",    // → Symbol id (where the call is written) — static spine
  "caller": "step:…",              // → Step id (dynamic invoker); omit for static / roots
  "realizes": "step:…",            // → static Step id this dynamic step executed (merge only)
  "site_file": "src/foo.py", "site_line": 88,
  "arg_style": {"positional": 2, "keyword": ["timeout"]},  // for CoP / CoN (static)
  "args": ["tok:…", "tok:…"],      // → Token ids, ordered (dynamic)
  "returns": "tok:…",              // → Token id (dynamic)
  "order": 17, "ts": 1733312400.12, "thread": "main"        // dynamic
}
```

- **Static doc:** set `callee`, `in_symbol`, `site_*`, `arg_style`. No `caller`,
  no tokens.
- **Dynamic doc:** set `callee`, `caller`, `order`, `args`/`returns`, `thread`.
- **Merged doc:** dynamic step adds `realizes` → the static step it executed,
  keyed by `(callee qualname, site_file, site_line)`.
- **Dispatch ambiguity** (one static call site, many possible callees): emit
  one Step per candidate, all sharing `site_line` — each candidate edge is a
  real node.

## Token — a passed/returned value (dynamic)

```jsonc
{
  "id": "tok:…",
  "type": "User",
  "repr": "<User id=7>",          // truncated, safe — never dump secrets
  "identity": "obj:0x7f…@run",    // stable WITHIN run_id only; null for value types → CoI/CoTm
  "value_hash": "sha256:…",       // hash of canonical serialization; cross-run → CoV/CoA
  "is_literal": true,             // was a source literal at the call site → CoM
  "literal_repr": "0"
}
```

- `identity` = the object's process-local identity, **namespaced by `run_id`**.
  Drives CoI (Identity) and CoTm (Timing). Value types (int/str/…) have
  `identity: null`.
- `value_hash` = canonical-serialization hash; stable across runs, so CoV/CoA
  can span runs.
- `is_literal` marks a source literal → drives CoM (magic values).

### Token volume

A real run emits millions of tokens. `trace-ingest.py --dedup` collapses tokens
with identical `(value_hash, identity, type)` into one class and rewrites step
refs; CoV/CoI then run over **classes**, not occurrences. Run dedup before
detecting on a large dynamic trace.

## Minimal adapter responsibilities

1. Assign a stable `id` to every symbol/step/token (any unique string).
2. Set `callee` on every step; `in_symbol` for the static spine.
3. Populate `package`/`module` on symbols (locality).
4. For dynamic: set `run_id`, `order`, per-arg tokens with `identity`/`value_hash`.
5. Emit valid JSON; pipe through `trace-ingest.py` to validate.

## Shipped adapters

- `scripts/adapters/python_ast.py` — **static** Python spine via stdlib `ast`.
  `python3 python_ast.py pkg/ --module-root src > static.json`
- `scripts/adapters/typescript_ast.mjs` — **static** TS/JS spine via the
  TypeScript compiler API (accurate callee/type resolution). Needs
  `npm i -D typescript` in the analyzed project.
- `scripts/adapters/php_ast.php` — **static** PHP spine via nikic/php-parser
  (name-based resolution + `$row['key']` array-dim record shape). Needs
  `composer require --dev nikic/php-parser`; run with `php`.
- `scripts/adapters/ruby_ast.rb` — **static** Ruby spine via prism (name-based
  resolution + `row[:key]` / `.fetch(:key)` hash-access record shape). Ruby 3.4+
  bundles prism; run with `ruby`.
  `node typescript_ast.mjs src --module-root src > static.json`
- `scripts/adapters/python_settrace.py` — **dynamic** Python trace via
  `sys.settrace`; captures real values/identities/order/threads.

A static and a dynamic doc for the same code **merge** by matching
`(callee qualname, site_file, site_line)`: set `realizes` on each dynamic step
to the static step it executed. Read any of these as the worked example for a
new adapter.
