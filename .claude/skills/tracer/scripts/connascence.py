"""Layer-2 catalog: the Page-Jones connascence taxonomy.

The analog of the aif-arguments `schemes.py` — an opaque-key registry the
hashharness store knows nothing about. Detectors (`trace-detect.py`) and the
validator import this catalog and interpret the `kind` strings stored on
`TraceConn` nodes.

Connascence (Meilir Page-Jones, *What Every Programmer Should Know About
Object-Oriented Design*, 1995): two elements are connascent when a change in one
requires a change in the other to preserve correctness. The reduction rules:

  1. minimize **strength** (prefer static/weak over dynamic/strong),
  2. minimize **degree** (fewer elements bound together),
  3. maximize **locality** (keep connascent elements close).

`strength` is the canonical weakest→strongest ordering 1..9.

CLI:
  python3 connascence.py              # human-readable, grouped static/dynamic
  python3 connascence.py --json       # whole catalog as the cross-skill contract
  python3 connascence.py --kind CoI   # one entry, detail
"""
from __future__ import annotations

import json
import sys

# Additive-only records. Never remove or rename a field; only append.
CONNASCENCES = {
    # ---- static (detectable from source / the static spine) ---------------
    "CoN": {
        "kind": "CoN", "name": "Name", "dynamic": False, "strength": 1,
        "degree_note": "degree = number of call sites that must agree on the name",
        "locality_note": "weak; tolerable even at a distance",
        "detector": "Every call edge agrees on the callee name (and keyword-arg "
                    "names). Surface high-degree symbols: a symbol referenced by "
                    "N sites has rename blast-radius N.",
        "refactor": "Usually fine. If the degree is large and the name is poor, "
                    "rename once via tooling; consider a facade to lower degree.",
        "reference": "Page-Jones (1995); Connascence.io",
    },
    "CoT": {
        "kind": "CoT", "name": "Type", "dynamic": False, "strength": 2,
        "degree_note": "degree = number of call sites relying on a param's type",
        "locality_note": "weak-moderate; worse when the type is implicit",
        "detector": "Compare arg token `type` against the param's declared "
                    "`type`. Flag (a) undeclared params (implicit CoT) and "
                    "(b) type instability — the same param position observed "
                    "with different token types across calls.",
        "refactor": "Declare types / use a typed interface or value object so the "
                    "agreement is checked by the compiler, not by convention.",
        "reference": "Page-Jones (1995)",
    },
    "CoM": {
        "kind": "CoM", "name": "Meaning / Convention", "dynamic": False, "strength": 3,
        "degree_note": "degree = number of sites sharing the magic value",
        "locality_note": "moderate; magic values leaking across modules is bad",
        "detector": "A literal token (`is_literal`) carrying a magic value passed "
                    "where a param implies an enumerated meaning (mode flags, "
                    "status codes). Group by (callee, position, value).",
        "refactor": "Replace the magic value with a named constant / enum so the "
                    "shared meaning is defined once.",
        "reference": "Page-Jones (1995)",
    },
    "CoP": {
        "kind": "CoP", "name": "Position", "dynamic": False, "strength": 4,
        "degree_note": "degree per site = 2 (call site + def); aggregate by #sites",
        "locality_note": "moderate-strong; positional coupling is fragile",
        "detector": "A call site passing >=2 positional args into a callee with "
                    ">=2 interchangeable/optional params: caller and def are "
                    "connascent on argument order.",
        "refactor": "Use keyword arguments or a parameter object so order stops "
                    "mattering.",
        "reference": "Page-Jones (1995)",
    },
    "CoA": {
        "kind": "CoA", "name": "Algorithm", "dynamic": False, "strength": 5,
        "degree_note": "degree = number of elements that must share the algorithm",
        "locality_note": "strong; the strongest static form",
        "detector": "Producer/consumer pair of a `value_hash` (encode/decode, "
                    "sign/verify, checksum, serialize/deserialize) that must run "
                    "the same algorithm. HEURISTIC — emitted as `provisional`.",
        "refactor": "Extract the shared algorithm into one module both sides call, "
                    "so it can never drift.",
        "reference": "Page-Jones (1995)",
    },
    # ---- dynamic (only visible with a real runtime trace) -----------------
    "CoE": {
        "kind": "CoE", "name": "Execution Order", "dynamic": True, "strength": 6,
        "degree_note": "degree = number of steps whose order is load-bearing",
        "locality_note": "strong; order coupling is invisible in the source",
        "detector": "Within a run, for a shared identity X, a stable ordering "
                    "invariant: method A on X always precedes method B on X "
                    "(both seen >=2 times). Inferred from `order` + `identity`.",
        "refactor": "Make order explicit (a state machine, a builder, or a single "
                    "orchestrating method) so callers can't get it wrong.",
        "reference": "Page-Jones (1995)",
    },
    "CoTm": {
        "kind": "CoTm", "name": "Timing", "dynamic": True, "strength": 7,
        "degree_note": "degree = number of concurrent participants",
        "locality_note": "very strong; races are the hardest to reproduce",
        "detector": "A shared `identity` touched by >=2 distinct `thread`s — "
                    "execution timing is load-bearing (potential race / "
                    "ordering-by-luck).",
        "refactor": "Add explicit synchronization, immutability, or ownership so "
                    "correctness no longer depends on timing.",
        "reference": "Page-Jones (1995)",
    },
    "CoV": {
        "kind": "CoV", "name": "Value", "dynamic": True, "strength": 8,
        "degree_note": "degree = number of sites/values that must agree",
        "locality_note": "very strong; co-varying values drift silently",
        "detector": "The same `value_hash` required at >=2 call sites in different "
                    "symbols (a value that must stay equal across places, or "
                    "fields that must co-vary). Value semantics (identity null).",
        "refactor": "Compute the value once and pass it, or enforce the invariant "
                    "in a single owning type, instead of duplicating it.",
        "reference": "Page-Jones (1995)",
    },
    "CoI": {
        "kind": "CoI", "name": "Identity", "dynamic": True, "strength": 9,
        "degree_note": "degree = number of symbols sharing the same instance",
        "locality_note": "strongest; the hardest coupling to refactor",
        "detector": "The same non-null `identity` appearing as arg/return across "
                    "steps in >=2 different symbols — those symbols are "
                    "connascent on referencing the exact same instance.",
        "refactor": "Make the shared reference explicit (dependency injection, a "
                    "single owner) and minimize how far the identity travels.",
        "reference": "Page-Jones (1995)",
    },
}

LOCALITY_PENALTY = {
    "same_function": 1, "same_class": 2, "same_module": 3,
    "same_package": 5, "cross_package": 8, "cross_service": 13,
}


def catalog_json() -> dict:
    return {
        "source": "Page-Jones, M. (1995); connascence.io taxonomy",
        "count": len(CONNASCENCES),
        "ordering": "strength 1 (weakest, static) .. 9 (strongest, dynamic)",
        "reduction_rules": [
            "minimize strength", "minimize degree", "maximize locality",
        ],
        "locality_penalty": LOCALITY_PENALTY,
        "kinds": list(CONNASCENCES.values()),
    }


def _print_human() -> None:
    rows = sorted(CONNASCENCES.values(), key=lambda r: r["strength"])
    print("Connascence taxonomy (weakest → strongest)\n")
    last = None
    for r in rows:
        band = "dynamic" if r["dynamic"] else "static"
        if band != last:
            print(f"── {band.upper()} ──")
            last = band
        print(f"  [{r['strength']}] {r['kind']:5} {r['name']}")
        print(f"        detect:   {r['detector']}")
        print(f"        refactor: {r['refactor']}\n")
    print("Reduction rules: minimize strength, minimize degree, maximize locality.")


def main(argv: list) -> int:
    if "--json" in argv:
        print(json.dumps(catalog_json(), indent=2))
        return 0
    if "--kind" in argv:
        k = argv[argv.index("--kind") + 1]
        rec = CONNASCENCES.get(k)
        if not rec:
            print(f"unknown kind: {k}; known: {', '.join(CONNASCENCES)}",
                  file=sys.stderr)
            return 1
        print(json.dumps(rec, indent=2))
        return 0
    _print_human()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
