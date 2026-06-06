// Dynamic adapter: turn a real Node.js execution into a dynamic TraceDoc by
// instrumenting (wrapping) functions at runtime. The JS sibling of
// python_settrace.py / ruby_tracepoint.rb.
//
// JS has no stdlib call-hook (no sys.settrace / TracePoint), but functions are
// first-class, so we WRAP them: each wrapped call records its arguments, the
// receiver `this` (-> object identity), order, and worker thread, then calls the
// original. That feeds the dynamic kinds CoE / CoTm / CoV / CoI.
//
// NOTE: this is NOT a --cpu-prof / V8 profiler reader. A CPU profile has call
// counts and timing but neither argument values nor object identities, so it
// cannot drive the value/identity-based dynamic connascences. Wrapping can.
//
// Usage (CommonJS — wrapping reassigns exports, so require() the target):
//   const { Tracer } = require('./js_instrument.js');
//   const app = require('./app.js');
//   const tr = new Tracer({ runId: 'run1' });
//   tr.instrument(app, 'app');            // wrap app's exported fns + class methods
//   const a = new app.Account(100);       // call THROUGH the instrumented exports
//   app.transfer(a, new app.Account(0), 30);
//   tr.dump('trace.json');                // a dynamic TraceDoc for trace-ingest
//
// Scope: you instrument exactly the modules you pass, so it is inherently scoped
// to your own code — no stdlib noise. Class-prototype methods are wrapped in
// place (every instance, any caller). LIMITATION: a same-module call made via an
// internal binding (e.g. main() calling a local helper, not module.helper) is
// not captured — JS can't intercept that without source transformation. Drive
// through the exported surface, or instrument each module (cross-module calls go
// through the wrapped exports). TS: compile to CJS first, or instrument the
// emitted JS.

const crypto = require('crypto');
let threadId = 0;
try { threadId = require('worker_threads').threadId; } catch { /* older node */ }

function vh(s) {
  return 'sha256:' + crypto.createHash('sha256').update(s).digest('hex').slice(0, 16);
}

function isClass(fn) {
  return typeof fn === 'function' &&
    /^class[\s{]/.test(Function.prototype.toString.call(fn));
}

function safeRepr(v) {
  let r;
  try {
    if (v === null) r = 'null';
    else if (typeof v === 'object') r = `<${v.constructor ? v.constructor.name : 'Object'}>`;
    else if (typeof v === 'function') r = `<fn ${v.name || 'anon'}>`;
    else r = String(v);
  } catch { r = '<unrepr>'; }
  return r.length > 120 ? r.slice(0, 120) : r;
}

class Tracer {
  constructor({ runId = 'run', entrypoint = null } = {}) {
    this.runId = runId;
    this.entrypoint = entrypoint;
    this.symbols = new Map();      // id -> symbol
    this.tokens = [];
    this.steps = [];
    this._byId = new Map();        // stepId -> step
    this._stack = [];
    this._ids = new WeakMap();     // object -> numeric id
    this._idn = 0;
    this._n = 0;
    this._tc = 0;
  }

  // ---- public ----------------------------------------------------------

  instrument(obj, moduleName) {
    for (const key of Object.keys(obj)) {
      const v = obj[key];
      if (typeof v !== 'function') continue;
      if (isClass(v)) {
        this._wrapPrototype(v, `${moduleName}.${key}`, moduleName);
        try { obj[key] = this._wrapClass(v, `${moduleName}.${key}`, moduleName); }
        catch { /* non-writable export: prototype methods still wrapped */ }
      } else {
        try { obj[key] = this._wrap(v, `${moduleName}.${key}`, moduleName, false); }
        catch { /* non-writable: skip */ }
      }
    }
    return obj;
  }

  doc() {
    return {
      version: '1', kind: 'dynamic', run_id: this.runId,
      entrypoint: this.entrypoint,
      symbols: [...this.symbols.values()],
      steps: this.steps,
      tokens: this.tokens,
    };
  }

  dump(path) {
    require('fs').writeFileSync(path, JSON.stringify(this.doc(), null, 2));
  }

  // ---- internals -------------------------------------------------------

  _thread() { return threadId === 0 ? 'main' : `thread-${threadId}`; }

  _oid(v) {
    let id = this._ids.get(v);
    if (id === undefined) { id = ++this._idn; this._ids.set(v, id); }
    return id;
  }

  _token(v) {
    const id = `tok:${++this._tc}`;
    const t = typeof v;
    const valueLike = v === null || v === undefined || (t !== 'object' && t !== 'function');
    const repr = safeRepr(v);
    this.tokens.push({
      id, type: v === null ? 'null' : (v === undefined ? 'undefined' : (v.constructor ? v.constructor.name : t)),
      repr,
      identity: valueLike ? null : `obj:${this._oid(v)}@${this.runId}`,
      value_hash: valueLike ? vh(repr) : null,
      is_literal: valueLike,
      literal_repr: valueLike ? repr : null,
    });
    return id;
  }

  _symbol(qual, moduleName, kind) {
    const id = `sym:${qual}`;
    if (!this.symbols.has(id)) {
      this.symbols.set(id, {
        id, qualname: qual, kind,
        file: null, line: null,
        module: moduleName, package: moduleName ? moduleName.split('.')[0] : null,
        params: [],
      });
    }
    return id;
  }

  _enter(symId, self, args) {
    const stepId = `step:${++this._n}`;
    const argTokens = [];
    if (self !== undefined && self !== null && typeof self === 'object') {
      argTokens.push(this._token(self));     // the receiver = shared instance
    }
    for (const a of args) argTokens.push(this._token(a));
    const step = {
      id: stepId, callee: symId, site_file: null, site_line: null,
      order: this._n, thread: this._thread(),
      arg_style: { positional: args.length, keyword: [] },
      args: argTokens,
    };
    if (this._stack.length) step.caller = this._stack[this._stack.length - 1];
    this.steps.push(step);
    this._byId.set(stepId, step);
    this._stack.push(stepId);
    return stepId;
  }

  _return(stepId, ret) {
    const step = this._byId.get(stepId);
    if (step) step.returns = this._token(ret);
  }

  _pop() { this._stack.pop(); }

  _wrap(orig, qual, moduleName, isMethod) {
    const tracer = this;
    const symId = this._symbol(qual, moduleName, isMethod ? 'method' : 'function');
    const wrapped = function (...args) {
      const stepId = tracer._enter(symId, isMethod ? this : undefined, args);
      try {
        const ret = orig.apply(this, args);
        tracer._return(stepId, ret);
        return ret;
      } finally {
        tracer._pop();
      }
    };
    try {
      Object.defineProperty(wrapped, 'name', { value: orig.name, configurable: true });
      Object.defineProperty(wrapped, 'length', { value: orig.length, configurable: true });
    } catch { /* ignore */ }
    wrapped.__orig = orig;
    return wrapped;
  }

  _wrapPrototype(klass, classQual, moduleName) {
    const proto = klass.prototype;
    if (!proto) return;
    for (const m of Object.getOwnPropertyNames(proto)) {
      if (m === 'constructor') continue;
      const d = Object.getOwnPropertyDescriptor(proto, m);
      if (d && typeof d.value === 'function' && !d.get && !d.set) {
        proto[m] = this._wrap(d.value, `${classQual}.${m}`, moduleName, true);
      }
    }
  }

  _wrapClass(klass, qual, moduleName) {
    const tracer = this;
    const symId = this._symbol(qual, moduleName, 'method'); // the constructor
    return new Proxy(klass, {
      construct(target, args, newTarget) {
        const stepId = tracer._enter(symId, undefined, args);
        try {
          const inst = Reflect.construct(target, args, newTarget);
          tracer._return(stepId, inst);
          return inst;
        } finally {
          tracer._pop();
        }
      },
    });
  }
}

module.exports = { Tracer };
