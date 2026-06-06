#!/usr/bin/env python3
"""Reference adapter: turn a real Python execution into a dynamic TraceDoc.

NOT part of the skill core — it exists to prove the language-agnostic ingest
contract round-trips end to end. Other languages (ruby-prof reader, V8
cpuprofile reader, …) are separate adapters that emit the same JSON shape.

Usage:
    from python_settrace import Tracer
    with Tracer(run_id="demo", scope=["src/"]) as tr:   # scope = your code only
        my_entrypoint()
    tr.dump("trace.json")        # a dynamic TraceDoc ready for trace-ingest.py

`scope` restricts recording to frames whose file lives under one of the given
roots (the dynamic analog of `trace-detect.py --exclude-external`). Without it,
everything except this adapter is traced — which includes stdlib/library
internals (threading, asyncio, …) and usually buries your code in noise. Library
code called *between* two of your frames is skipped but transparent: the inner
in-scope call's `caller` resolves to its nearest in-scope ancestor.

What it captures per call: callee qualname + def site (→ TraceSymbol), the
invoking step (→ caller link), and a DataToken per argument and return value
carrying type, repr, object identity (run-scoped), value_hash, and whether the
arg was a literal-ish immutable.

Identity is `obj:<id(obj)>@<run_id>` — meaningful only within this process, as
the design requires. value_hash is sha256 of a best-effort canonical repr.

Stdlib only. Honest limitations: sys.settrace sees frames, not source call
sites, so `is_literal`/`arg_style.positional` are approximated from arg values
and the function signature; a static AST adapter fills those in precisely.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading


def _safe_repr(v, limit=120):
    try:
        r = repr(v)
    except Exception:
        r = f"<unreprable {type(v).__name__}>"
    return r[:limit]


def _value_hash(v):
    try:
        return "sha256:" + hashlib.sha256(repr(v).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return None


_IMMUTABLE = (int, float, str, bool, bytes, type(None), tuple, frozenset)


class Tracer:
    def __init__(self, run_id="run", entrypoint=None, max_steps=100_000,
                 scope=None):
        self.run_id = run_id
        self.entrypoint = entrypoint
        self.max_steps = max_steps
        self.symbols = {}        # qualname -> symbol dict
        self.tokens = {}         # token id -> token dict
        self.steps = []
        self._stack = []         # step ids (per thread, simplified to one)
        self._n = 0
        # scope: only record frames whose file is under one of these roots.
        # None → record everything except this adapter's own machinery.
        if scope is None:
            self.scope = None
        else:
            roots = scope if isinstance(scope, (list, tuple)) else [scope]
            self.scope = tuple(os.path.abspath(r) for r in roots)

    def _in_scope(self, filename: str) -> bool:
        if "python_settrace" in filename:
            return False                       # never trace our own machinery
        if self.scope is None:
            return True
        return os.path.abspath(filename).startswith(self.scope)

    # -- symbol/token interning -------------------------------------------

    def _symbol(self, code, qualname, module=None):
        if qualname in self.symbols:
            return self.symbols[qualname]["id"]
        sid = f"sym:{qualname}"
        params = [{"name": code.co_varnames[i], "position": i, "kind": "positional"}
                  for i in range(code.co_argcount)]
        self.symbols[qualname] = {
            "id": sid, "qualname": qualname, "kind": "function",
            "file": code.co_filename, "line": code.co_firstlineno,
            "module": module,
            "package": module.split(".")[0] if module else None,
            "params": params,
        }
        return sid

    def _token(self, v):
        tid = f"tok:{id(v)}:{self._n}"
        is_imm = isinstance(v, _IMMUTABLE)
        tok = {
            "id": tid, "type": type(v).__name__, "repr": _safe_repr(v),
            "identity": None if is_imm else f"obj:{id(v):x}@{self.run_id}",
            "value_hash": _value_hash(v),
            "is_literal": is_imm and not isinstance(v, (tuple, frozenset)),
            "literal_repr": _safe_repr(v) if is_imm else None,
        }
        self.tokens[tid] = tok
        return tid

    # -- the trace hook ----------------------------------------------------

    def _trace(self, frame, event, arg):
        if event != "call":
            if event == "return" and self._stack:
                # attach the return token to the step we are leaving
                step_id = self._stack[-1]
                for st in reversed(self.steps):
                    if st["id"] == step_id:
                        st["returns"] = self._token(arg)
                        break
            if event == "return" and self._stack:
                self._stack.pop()
            return self._trace
        if self._n >= self.max_steps:
            return None
        code = frame.f_code
        # Out-of-scope frame: don't record it and don't push. Returning None
        # disables only THIS frame's local (line/return) events — the global
        # trace still fires for nested frames, so in-scope code called from
        # library code is still captured, and its `caller` resolves to the
        # nearest in-scope ancestor still on the stack.
        if not self._in_scope(code.co_filename):
            return None
        qualname = getattr(code, "co_qualname", code.co_name)
        module = frame.f_globals.get("__name__", "")
        if module and not qualname.startswith(module):
            qualname = f"{module}.{qualname}"
        self._n += 1
        callee = self._symbol(code, qualname, module=module or None)
        # arguments by position
        argvals = [frame.f_locals.get(code.co_varnames[i])
                   for i in range(code.co_argcount)]
        args = [self._token(v) for v in argvals]
        step = {
            "id": f"step:{self._n}",
            "callee": callee,
            "caller": self._stack[-1] if self._stack else None,
            "site_file": code.co_filename,
            "site_line": frame.f_lineno,
            "order": self._n,
            "thread": threading.current_thread().name,
            "arg_style": {"positional": len(args)},
            "args": args,
        }
        if step["caller"] is None:
            del step["caller"]
        self.steps.append(step)
        self._stack.append(step["id"])
        return self._trace

    # -- context manager ---------------------------------------------------

    def __enter__(self):
        sys.settrace(self._trace)
        threading.settrace(self._trace)
        return self

    def __exit__(self, *exc):
        sys.settrace(None)
        threading.settrace(None)
        return False

    # -- output ------------------------------------------------------------

    def doc(self) -> dict:
        return {
            "version": "1", "kind": "dynamic", "run_id": self.run_id,
            "entrypoint": self.entrypoint,
            "symbols": list(self.symbols.values()),
            "steps": [s for s in self.steps if s["id"]],
            "tokens": list(self.tokens.values()),
        }

    def dump(self, path):
        with open(path, "w") as fh:
            json.dump(self.doc(), fh, indent=2)


if __name__ == "__main__":
    # self-demo: trace a tiny program with a shared mutable instance (→ CoI)
    class Account:
        def __init__(self, n): self.n = n
        def deposit(self, x): self.n += x
        def withdraw(self, x): self.n -= x

    def transfer(a, b, amt):
        a.withdraw(amt)
        b.deposit(amt)

    def main():
        a, b = Account(100), Account(0)
        transfer(a, b, 30)
        transfer(b, a, 10)

    with Tracer(run_id="demo", entrypoint="__main__.main") as tr:
        main()
    json.dump(tr.doc(), sys.stdout, indent=2)
