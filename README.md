# connascence

**Before you change code, know the blast radius.** `connascence` maps the impact
radius of a change — which other places must change with it, and *why* — by
building a dependency graph of your code (who calls what, what data flows between
them) and surfacing the couplings as a ranked list. So "what breaks if I touch
this?" gets a concrete, evidence-backed answer instead of a guess.

It is a Claude Code skill: point it at a codebase, get back a prioritized map of
where the coupling — and therefore the risk — actually lives.

---

## Why it exists

You're about to change a function, a data shape, or a cache. The real cost isn't
the edit — it's everything *else* that silently depends on it and breaks when you
forget it. That dependency web is invisible in the source: a magic value
duplicated across nine call sites, a dict whose 33 keys couple you to whoever
produces it, a method that must run before another on the same object. Grep finds
names; it doesn't find *coupling*, and it doesn't tell you which coupling is
dangerous.

`connascence` makes that web explicit and ranks it, so you spend your attention
where a change will actually hurt.

## What it tells you — three things

1. **The dependencies are real, not textual.** It builds the actual call graph
   (who calls what), carries the data passed at each call (values, object
   identity, dict/record keys), and — with a runtime trace — captures execution
   order and shared state. Static breadth across a whole codebase; dynamic depth
   where it matters.

2. **Each coupling is named and ranked.** Every dependency is classified by the
   Page-Jones **connascence** taxonomy (9 kinds, weakest→strongest) and scored
   `severity = strength × degree × locality`. The worst offenders — strong,
   high-degree, far-apart coupling — sort to the top. You get a refactoring
   worklist, not a wall of findings.

3. **It answers blast-radius questions directly.** *"What's the impact of
   changing the history cache / the theme model / this function's signature?"* →
   the exact call sites, the data-shape couplings, and the hidden ordering or
   shared-state dependencies that a change would disturb.

## How it works

```
  source code ──> adapter ──> TraceDoc (JSON) ──> detect ──> ranked findings
                  (per lang)   call graph +        (9 kinds)   (worst first)
                               data + record shapes
```

- **Adapters** parse your code into one language-agnostic JSON contract (a
  *TraceDoc*) — the only language-specific part. The skill itself never parses
  source.
- **Detectors** run nine graph queries over the TraceDoc and emit findings.
- **Everything is local JSON and stdlib scripts** — no server, no database, no
  network. A trace is derived data, regenerated from source whenever you re-run.

## The taxonomy

Weakest → strongest. Refactoring lowers **strength**, lowers **degree** (how many
elements are bound), and raises **locality** (keeps them close).

| # | kind | what couples | example signal |
|---|------|--------------|----------------|
| 1 | **Name** | callers ↔ a name | a symbol called from N sites; a dict read by N string keys (record shape) |
| 2 | **Type** | callers ↔ a param's type | undeclared / unstable types across calls |
| 3 | **Meaning** | sites sharing a magic value | a literal `3` passed at 9 call sites |
| 4 | **Position** | call site ↔ argument order | ≥2 positional args into ≥2 swappable params |
| 5 | **Algorithm** | producer ↔ consumer | encode/decode, sign/verify must match *(provisional)* |
| 6 | **Execution order** | steps whose order matters | A-on-X always precedes B-on-X |
| 7 | **Timing** | concurrent participants | one instance touched by ≥2 threads |
| 8 | **Value** | values that must agree | the same value required across symbols |
| 9 | **Identity** | symbols sharing one instance | the same object threaded through N functions |

Kinds 1–5 come from a static call graph; 6–9 need a runtime trace.

## Quick start

```bash
S=.claude/skills/connascence/scripts

# 1. turn your code into a TraceDoc (pick your language)
python3 $S/adapters/python_ast.py     src/ --module-root src      > doc.json   # Python
node     $S/adapters/typescript_ast.mjs src --module-root src      > doc.json   # TS/JS
php      $S/adapters/php_ast.php       src/ --module-root src      > doc.json   # PHP
ruby     $S/adapters/ruby_ast.rb       lib/ --module-root lib      > doc.json   # Ruby
dart run $S/adapters/dart_ast.dart     lib/ --module-root lib      > doc.json   # Dart

# 2. rank the couplings (drop builtins/library calls to keep it actionable)
python3 $S/trace-detect.py --input doc.json --exclude-external

# 3. optional: dedup tokens, validate structure, render a graph
python3 $S/trace-ingest.py   --input doc.json --dedup > doc.dedup.json
python3 $S/trace-validate.py --input doc.json
python3 $S/trace-render.py   --input doc.json > trace.dot && dot -Tsvg trace.dot -o trace.svg
```

As a skill, just ask in natural language — *"what's the impact radius if I change
how the history cache works?"* — and it drives this pipeline for you.

## Languages

| language | adapter | spine | needs |
|----------|---------|-------|-------|
| Python | `python_ast.py` (static), `python_settrace.py` (dynamic) | static + dynamic | stdlib only |
| TypeScript / JS | `typescript_ast.mjs` (static), `js_instrument.js` (dynamic, Node) | static + dynamic | `npm i -D typescript`; instrumentation is stdlib |
| PHP | `php_ast.php` (static), `php_uopz.php` (dynamic) | static + dynamic | `composer require --dev nikic/php-parser`; `pecl install uopz` |
| Ruby | `ruby_ast.rb` (static), `ruby_tracepoint.rb` (dynamic) | static + dynamic | Ruby 3.4+ (prism bundled); TracePoint is stdlib |
| Dart | `dart_ast.dart` | static | `dart pub add --dev "analyzer:^6.0.0"` |

Each static adapter also captures **record shape** — `row['user_id']` /
`row.userId` / `$row['id']` / `row[:id]` (Dart/PHP/JS/Ruby/Python alike) — the
dict/DB-row key coupling that
breaks silently when a producer renames a field. The Python (`sys.settrace`), Ruby (`TracePoint`), Node (function-wrapping),
and PHP (uopz hooks) dynamic adapters add the runtime-only kinds (execution
order, timing, value, identity).

## What it is not

- **Not a linter or type checker.** It finds *coupling*, not correctness bugs.
- **Not source of truth.** A TraceDoc is derived; regenerate it, don't archive it.
- **Static resolution is name-based** for Python, PHP, Ruby, and Dart —
  ambiguous calls degrade to "external", never to a wrong target. (TypeScript
  uses the compiler's type checker for precise resolution.)

## Layout

```
.claude/skills/connascence/
  SKILL.md                  # the skill: workflow + conventions
  references/               # data model, ingest contract, taxonomy, query recipes
  scripts/
    connascence.py          # the taxonomy catalog
    trace_io.py             # graph loader + locality/severity scoring
    trace-detect.py         # the 9 detectors, ranked
    trace-ingest.py         # validate + dedup
    trace-validate.py       # structural checks
    trace-render.py         # Graphviz DOT
    adapters/               # python / typescript / php / ruby / dart
```

## Background

Connascence is Meilir Page-Jones's taxonomy of coupling, from *What Every
Programmer Should Know About Object-Oriented Design* (1995); see
[connascence.io](https://connascence.io). This tool operationalizes it: it finds
connascence in real code, ranks it, and turns "what's coupled to this?" into a
question with an answer.

## License

MIT — see [LICENSE](LICENSE). Use it freely, including in commercial work; no
warranty.
