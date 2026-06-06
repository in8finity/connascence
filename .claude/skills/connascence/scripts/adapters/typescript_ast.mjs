#!/usr/bin/env node
/**
 * Static adapter: build a TraceDoc *static spine* from TypeScript/JavaScript
 * source via the TypeScript Compiler API.
 *
 * The TS sibling of `python_ast.py`. Uses the real type checker, so callee
 * resolution and parameter types are accurate (far better than name matching).
 * Captures the static spine → CoN / CoT / CoM / CoP. Merge with a dynamic
 * trace for the dynamic kinds.
 *
 *   node typescript_ast.mjs src/ [more.ts …] [--module-root src] > static.json
 *   node typescript_ast.mjs --project tsconfig.json > static.json
 *
 * REQUIRES the `typescript` package (the compiler API):
 *   npm i -D typescript        # local, or `npm i -g typescript`
 * Run with `node` (Node >= 18). Adapters are per-language and use that
 * language's tooling — this is not part of the stdlib-only skill core.
 *
 * Emits `kind: "static"`. Resolution: a CallExpression resolves to the
 * declaration its signature points at; if that declaration is one of the
 * collected symbols it links to it, else to an `ext:` symbol (name only).
 */
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";

// Resolve `typescript` from the *analyzed project* (cwd), not this script's
// location — ESM bare imports ignore cwd/NODE_PATH, so a project-local
// `npm i -D typescript` would otherwise be invisible. Try cwd first, then the
// script dir, then a global/NODE_PATH require.
let ts;
for (const base of [path.join(process.cwd(), "index.js"),
                    new URL(import.meta.url).pathname]) {
  try { ts = createRequire(base)("typescript"); break; } catch { /* next */ }
}
if (!ts) {
  console.error(
    "ERROR: the `typescript` package is not installed.\n" +
    "  npm i -D typescript    (run in the project you are analyzing)\n" +
    "then re-run: node <path>/typescript_ast.mjs <paths…>");
  process.exit(2);
}

const vh = (s) => "sha256:" + createHash("sha256").update(s).digest("hex").slice(0, 16);

// ---- args ----------------------------------------------------------------
const argv = process.argv.slice(2);
let project = null, moduleRoot = null;
const paths = [];
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === "--project") project = argv[++i];
  else if (argv[i] === "--module-root") moduleRoot = argv[++i];
  else paths.push(argv[i]);
}

function walkDir(dir, acc) {
  for (const name of fs.readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const p = path.join(dir, name);
    const st = fs.statSync(p);
    if (st.isDirectory()) walkDir(p, acc);
    else if (/\.(ts|tsx|mts|cts|js|jsx|mjs)$/.test(name)) acc.push(p);
  }
  return acc;
}

// ---- build the program ---------------------------------------------------
let fileNames, options;
if (project) {
  const cfg = ts.readConfigFile(project, ts.sys.readFile);
  const parsed = ts.parseJsonConfigFileContent(
    cfg.config, ts.sys, path.dirname(project));
  fileNames = parsed.fileNames;
  options = parsed.options;
} else {
  fileNames = [];
  for (const p of paths) {
    const st = fs.existsSync(p) ? fs.statSync(p) : null;
    if (st && st.isDirectory()) walkDir(p, fileNames);
    else if (st) fileNames.push(p);
  }
  options = { allowJs: true, target: ts.ScriptTarget.ESNext,
              module: ts.ModuleKind.ESNext, noEmit: true, checkJs: false };
}
fileNames = [...new Set(fileNames.map((f) => path.resolve(f)))];

const program = ts.createProgram(fileNames, options);
const checker = program.getTypeChecker();
const inProgram = new Set(fileNames);

function moduleName(file) {
  let rel = moduleRoot ? path.relative(moduleRoot, file) : path.basename(file);
  rel = rel.replace(/\.(ts|tsx|mts|cts|js|jsx|mjs)$/, "");
  return rel.split(path.sep).join(".");
}
const pkgOf = (mod) => (mod ? mod.split(".")[0] : null);

// ---- pass 1: collect symbols --------------------------------------------
const symbols = new Map();     // id -> symbol obj
const nodeToId = new Map();    // decl node -> symbol id

function isFnLike(n) {
  return ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n) ||
    ts.isConstructorDeclaration(n) || ts.isFunctionExpression(n) ||
    ts.isArrowFunction(n) || ts.isGetAccessor(n) || ts.isSetAccessor(n);
}

function nameOf(n) {
  if (ts.isConstructorDeclaration(n)) return "constructor";
  if (n.name && ts.isIdentifier(n.name)) return n.name.text;
  // arrow/function expr assigned to a var or property
  const p = n.parent;
  if (p && ts.isVariableDeclaration(p) && ts.isIdentifier(p.name)) return p.name.text;
  if (p && ts.isPropertyAssignment(p) && p.name && ts.isIdentifier(p.name)) return p.name.text;
  if (p && ts.isPropertyDeclaration(p) && p.name && ts.isIdentifier(p.name)) return p.name.text;
  return null;
}

function qualPath(node) {
  const segs = [];
  let cur = node;
  while (cur) {
    if (ts.isClassDeclaration(cur) || ts.isClassExpression(cur)) {
      if (cur.name) segs.unshift(cur.name.text);
    } else if (isFnLike(cur) && cur !== node) {
      const nm = nameOf(cur);
      if (nm) segs.unshift(nm);
    }
    cur = cur.parent;
  }
  const self = nameOf(node);
  if (self) segs.push(self);
  return segs;
}

function paramsOf(node) {
  return (node.parameters || []).map((p, i) => ({
    name: p.name.getText(),
    position: i,
    kind: p.dotDotDotToken ? "vararg" : "positional",
    // DECLARED annotation only (null if absent) — mirrors python_ast and makes
    // CoT fire on untyped params. An inferred `any` is not a written type.
    type: p.type ? p.type.getText() : null,
    inferred_type: checker.typeToString(checker.getTypeAtLocation(p)),
    has_default: !!p.initializer || !!p.questionToken,
  }));
}

function collectSymbols(sf) {
  const module = moduleName(sf.fileName);
  const visit = (node) => {
    if (isFnLike(node)) {
      const segs = qualPath(node);
      const name = nameOf(node);
      if (name) {
        const qual = [module, ...segs].join(".");
        const id = `sym:${qual}`;
        const isMethod = ts.isMethodDeclaration(node) ||
          ts.isConstructorDeclaration(node) || ts.isGetAccessor(node) ||
          ts.isSetAccessor(node);
        const lc = sf.getLineAndCharacterOfPosition(node.getStart());
        symbols.set(id, {
          id, qualname: qual, kind: isMethod ? "method" : "function",
          file: sf.fileName, line: lc.line + 1,
          module, package: pkgOf(module),
          params: paramsOf(node),
          returns_type: node.type ? node.type.getText() : null,
        });
        nodeToId.set(node, id);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
}

// ---- pass 2: collect call sites -----------------------------------------
const steps = [];
const tokens = [];
let tcount = 0, scount = 0;

function moduleSymbol(module, file) {
  const id = `sym:${module}:<module>`;
  if (!symbols.has(id))
    symbols.set(id, { id, qualname: module, kind: "module", file, line: 0,
      module, package: pkgOf(module), params: [] });
  return id;
}

function externalSymbol(qual) {
  const id = `sym:ext:${qual}`;
  if (!symbols.has(id))
    symbols.set(id, { id, qualname: qual, kind: "external", file: null,
      line: null, module: null, package: null, params: [] });
  return id;
}

function enclosing(node, module, file) {
  let cur = node.parent;
  while (cur) {
    if (isFnLike(cur) && nodeToId.has(cur)) return nodeToId.get(cur);
    cur = cur.parent;
  }
  return moduleSymbol(module, file);
}

function resolveCallee(call) {
  const sig = checker.getResolvedSignature(call);
  let decl = sig && sig.declaration;
  if (decl && nodeToId.has(decl)) return { id: nodeToId.get(decl), q: symbols.get(nodeToId.get(decl)).qualname };
  // fall back to the symbol at the call target
  let sym = checker.getSymbolAtLocation(call.expression);
  if (sym && sym.flags & ts.SymbolFlags.Alias) sym = checker.getAliasedSymbol(sym);
  let name = sym ? sym.getName() : call.expression.getText();
  if (sym && sym.declarations) {
    for (const d of sym.declarations) {
      const fn = isFnLike(d) ? d : (d.initializer && isFnLike(d.initializer) ? d.initializer : null);
      if (fn && nodeToId.has(fn)) return { id: nodeToId.get(fn), q: symbols.get(nodeToId.get(fn)).qualname };
    }
  }
  // last resort: method-name only
  if (ts.isPropertyAccessExpression(call.expression))
    name = "." + call.expression.name.text;
  return { id: externalSymbol(name), q: name };
}

function tokenFor(arg, module) {
  tcount++;
  const id = `tok:${module}:${tcount}`;
  const isLit = ts.isStringLiteral(arg) || ts.isNumericLiteral(arg) ||
    ts.isBigIntLiteral(arg) ||
    arg.kind === ts.SyntaxKind.TrueKeyword ||
    arg.kind === ts.SyntaxKind.FalseKeyword ||
    arg.kind === ts.SyntaxKind.NullKeyword;
  const text = arg.getText();
  if (isLit) {
    tokens.push({ id, type: ts.isStringLiteral(arg) ? "string" :
      ts.isNumericLiteral(arg) ? "number" : "literal",
      repr: text.slice(0, 120), identity: null, value_hash: vh(text),
      is_literal: true, literal_repr: text.slice(0, 120) });
  } else {
    tokens.push({ id, type: "expr", repr: text.slice(0, 120), identity: null,
      value_hash: null, is_literal: false, literal_repr: null });
  }
  return id;
}

// ---- record-shape access (dict-key / property connascence) --------------
function recordSymbol(base, encId, module, file) {
  const id = `sym:record:${encId}::${base}`;
  if (!symbols.has(id)) {
    const enc = symbols.get(encId);
    symbols.set(id, { id, qualname: base, kind: "record", file, line: null,
      module, package: pkgOf(module),
      in_function: enc ? enc.qualname : module, params: [] });
  }
  return id;
}

function recordAccess(baseNode, key, node, sf, module) {
  if (!key) return;
  const base = baseNode.getText().slice(0, 40);
  const encId = enclosing(node, module, sf.fileName);
  const rid = recordSymbol(base, encId, module, sf.fileName);
  tcount++;
  const tid = `tok:${module}:${tcount}`;
  tokens.push({ id: tid, type: "key", repr: key.slice(0, 120), identity: null,
    value_hash: vh(JSON.stringify(key)), is_literal: true,
    literal_repr: key.slice(0, 120), key });
  const lc = sf.getLineAndCharacterOfPosition(node.getStart());
  scount++;
  steps.push({ id: `step:${module}:${scount}`, callee: rid, in_symbol: encId,
    site_file: sf.fileName, site_line: lc.line + 1,
    arg_style: { positional: 1, keyword: [] }, callee_qualname: base,
    access: "field", args: [tid] });
}

function isCallTarget(node) {
  const p = node.parent;
  return p && (ts.isCallExpression(p) || ts.isNewExpression(p)) &&
    p.expression === node;
}

// Is `obj.x` a read of a record-like value (dict / interface / object literal),
// as opposed to a class member, method, namespace, enum, or array builtin?
const RECORD_EXCLUDE = ts.SymbolFlags.Class | ts.SymbolFlags.Method |
  ts.SymbolFlags.Function | ts.SymbolFlags.Namespace | ts.SymbolFlags.Module |
  ts.SymbolFlags.Enum | ts.SymbolFlags.EnumMember;

const BUILTIN_TYPES = new Set(["Array", "ReadonlyArray", "String", "Number",
  "Boolean", "Function", "Promise", "Map", "Set", "WeakMap", "WeakSet", "Date",
  "RegExp", "Error", "Symbol"]);

function isRecordLike(baseNode) {
  if (baseNode.kind === ts.SyntaxKind.ThisKeyword ||
      baseNode.kind === ts.SyntaxKind.SuperKeyword) return false;
  let type;
  try { type = checker.getTypeAtLocation(baseNode); } catch { return false; }
  if (!type) return false;
  // universal exclusions FIRST (TS models Array/String/… as interfaces, so
  // these must be ruled out before the interface check below).
  try {
    if (checker.isArrayLikeType && checker.isArrayLikeType(type)) return false;
    if (type.getCallSignatures && type.getCallSignatures().length) return false;
  } catch { /* ignore */ }
  const sym = type.getSymbol && type.getSymbol();
  if (sym && (sym.flags & RECORD_EXCLUDE)) return false;          // class/ns/enum/fn
  if (sym && sym.getName && BUILTIN_TYPES.has(sym.getName())) return false;
  if (type.flags & ts.TypeFlags.Any) return true;                 // untyped JS object
  if (type.getStringIndexType && type.getStringIndexType()) return true; // Record<>/index sig
  if (sym && (sym.flags & (ts.SymbolFlags.Interface | ts.SymbolFlags.TypeLiteral)))
    return true;                                                  // DB row interface, {…}
  if (type.flags & ts.TypeFlags.Object) return true;              // plain object type
  return false;
}

function collectCalls(sf) {
  const module = moduleName(sf.fileName);
  const visit = (node) => {
    if (ts.isCallExpression(node)) {
      const { id: callee, q } = resolveCallee(node);
      const lc = sf.getLineAndCharacterOfPosition(node.getStart());
      const args = node.arguments.map((a) => tokenFor(a, module));
      scount++;
      steps.push({
        id: `step:${module}:${scount}`,
        callee, in_symbol: enclosing(node, module, sf.fileName),
        site_file: sf.fileName, site_line: lc.line + 1,
        arg_style: { positional: node.arguments.length, keyword: [] },
        callee_qualname: q,
        args,
      });
    } else if (ts.isElementAccessExpression(node)) {
      const a = node.argumentExpression;
      if (a && ts.isStringLiteralLike(a) && !isCallTarget(node))
        recordAccess(node.expression, a.text, node, sf, module);  // row["key"]
    } else if (ts.isPropertyAccessExpression(node)) {
      if (!isCallTarget(node) && isRecordLike(node.expression))
        recordAccess(node.expression, node.name.text, node, sf, module);  // row.key
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
}

for (const sf of program.getSourceFiles()) {
  if (!inProgram.has(path.resolve(sf.fileName))) continue;
  if (sf.isDeclarationFile) continue;
  collectSymbols(sf);
}
for (const sf of program.getSourceFiles()) {
  if (!inProgram.has(path.resolve(sf.fileName))) continue;
  if (sf.isDeclarationFile) continue;
  collectCalls(sf);
}

const doc = {
  version: "1", kind: "static",
  entrypoint: fileNames.length ? moduleName(fileNames[0]) : null,
  symbols: [...symbols.values()], steps, tokens,
};
process.stdout.write(JSON.stringify(doc, null, 2) + "\n");
