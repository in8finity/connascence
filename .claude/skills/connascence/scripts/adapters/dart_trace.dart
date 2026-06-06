// Dynamic adapter: turn a real Dart execution into a dynamic TraceDoc by
// SOURCE INSTRUMENTATION. The Dart sibling of the other dynamic adapters — but
// heavier and more constrained, because Dart has no runtime call hook
// (no sys.settrace / TracePoint / uopz) and no method replacement (classes are
// sealed; you can't wrap methods like in JS). So we rewrite the source: inject a
// recording call at the top of each function/method body, then compile + run the
// instrumented copy. Object identity comes from `identityHashCode`.
//
//   dart run dart_trace.dart app.dart --entry main [--module-root .] > trace.json
//
// Needs package:analyzer 6.x (for parsing) — `dart pub add --dev "analyzer:^6.0.0"`
// — and `dart` on PATH (to run the instrumented copy). Captures values,
// identities, order -> CoV / CoI / CoE (CoTm needs shared-memory threads, which
// Dart isolates don't have, so it stays empty — correct for Dart).
//
// CONSTRAINTS (the most limited dynamic adapter):
//  - single entry FILE (its top-level functions, classes, and their methods);
//    other files it imports are run normally but not instrumented.
//  - the entry file must be runnable with `dart run` with no package deps.
//  - getters/setters/operators and factory/redirecting constructors are skipped;
//    block and `=>` bodies of ordinary functions/methods/constructors are traced.

import 'dart:convert';
import 'dart:io';

import 'package:analyzer/dart/analysis/features.dart';
import 'package:analyzer/dart/analysis/utilities.dart';
import 'package:analyzer/dart/ast/ast.dart';
import 'package:analyzer/dart/ast/visitor.dart';

String dot(String s) => s.replaceAll('::', '.');

String moduleName(String file, String? root) {
  final f = File(file).absolute.path;
  String rel;
  if (root != null) {
    var r = Directory(root).absolute.path;
    if (!r.endsWith('/')) r += '/';
    rel = f.startsWith(r) ? f.substring(r.length) : f.split('/').last;
  } else {
    rel = f.split('/').last;
  }
  return dot(rel.replaceAll(RegExp(r'\.dart$'), '').replaceAll('/', '.'));
}

class _Edit {
  final int offset;
  final int end; // for replacements (== offset for pure inserts)
  final String text;
  _Edit(this.offset, this.end, this.text);
}

class Instrumenter extends RecursiveAstVisitor<void> {
  final String module;
  final List<_Edit> edits = [];
  final List<String> classStack = [];
  final List<String> fnStack = [];
  Instrumenter(this.module);

  void _pushClass(String? name, void Function() body) {
    classStack.add(name ?? 'anon');
    body();
    classStack.removeLast();
  }

  @override
  void visitClassDeclaration(ClassDeclaration n) =>
      _pushClass(n.name.lexeme, () => super.visitClassDeclaration(n));
  @override
  void visitMixinDeclaration(MixinDeclaration n) =>
      _pushClass(n.name.lexeme, () => super.visitMixinDeclaration(n));
  @override
  void visitEnumDeclaration(EnumDeclaration n) =>
      _pushClass(n.name.lexeme, () => super.visitEnumDeclaration(n));

  // collect the param expressions to pass to rec()
  List<String> _argExprs(FormalParameterList? list) {
    if (list == null) return [];
    final out = <String>[];
    for (final p in list.parameters) {
      var inner = p is DefaultFormalParameter ? p.parameter : p;
      final name = inner.name?.lexeme;
      if (name == null) continue;
      if (inner is FieldFormalParameter) {
        out.add('this.$name'); // field-formal: usable as this.x after init
      } else if (inner is SuperFormalParameter) {
        continue; // not a local
      } else {
        out.add(name);
      }
    }
    return out;
  }

  void _inject(String qual, bool isMethod, FormalParameterList? params,
      FunctionBody body,
      {String? returnType, bool isSetter = false}) {
    final self = isMethod ? 'this' : 'null';
    final kind = isMethod ? 'method' : 'function';
    final args = _argExprs(params).join(', ');
    final call = "__ct.rec(r'$qual', r'$module', '$kind', $self, [$args]);";
    if (body is BlockFunctionBody) {
      final brace = body.block.leftBracket.end;
      edits.add(_Edit(brace, brace, ' $call'));
    } else if (body is ExpressionFunctionBody) {
      final useReturn = !isSetter && (returnType == null || returnType != 'void');
      final expr = body.expression.toSource();
      final star = body.star != null ? '${body.star}' : '';
      // `async`/`sync*` keyword sits before `=>`; keep it on the new block.
      final kw = body.keyword != null ? '${body.keyword} ' : '';
      final repl = body.keyword != null && star.isNotEmpty
          ? '$kw$star { $call ${useReturn ? 'return $expr;' : '$expr;'} }'
          : '$kw{ $call ${useReturn ? 'return $expr;' : '$expr;'} }';
      edits.add(_Edit(body.offset, body.end, repl));
    }
  }

  @override
  void visitFunctionDeclaration(FunctionDeclaration n) {
    final qual = dot(([module, ...classStack, ...fnStack, n.name.lexeme]).join('.'));
    final fe = n.functionExpression;
    if (!n.isGetter && !n.isSetter) {
      _inject(qual, false, fe.parameters, fe.body,
          returnType: n.returnType?.toSource(), isSetter: n.isSetter);
    }
    fnStack.add(n.name.lexeme);
    super.visitFunctionDeclaration(n);
    fnStack.removeLast();
  }

  @override
  void visitMethodDeclaration(MethodDeclaration n) {
    if (!n.isGetter && !n.isSetter && !n.isOperator && !n.isAbstract &&
        n.body is! EmptyFunctionBody) {
      final qual = dot(([module, ...classStack, n.name.lexeme]).join('.'));
      _inject(qual, !n.isStatic, n.parameters, n.body,
          returnType: n.returnType?.toSource(), isSetter: n.isSetter);
    }
    super.visitMethodDeclaration(n);
  }

  @override
  void visitConstructorDeclaration(ConstructorDeclaration n) {
    if (n.body is BlockFunctionBody && n.factoryKeyword == null &&
        n.redirectedConstructor == null) {
      final cname = n.name?.lexeme;
      final qual = dot(([module, ...classStack, cname ?? 'new']).join('.'));
      _inject(qual, true, n.parameters, n.body);
    }
    super.visitConstructorDeclaration(n);
  }
}

String applyEdits(String src, List<_Edit> edits) {
  edits.sort((a, b) => b.offset.compareTo(a.offset)); // back to front
  var s = src;
  for (final e in edits) {
    s = s.substring(0, e.offset) + e.text + s.substring(e.end);
  }
  return s;
}

const _runtime = r'''
import 'dart:convert';
import 'dart:io';

final _T t = _T();
void rec(String qual, String module, String kind, Object? self, List<Object?> args) =>
    t.rec(qual, module, kind, self, args);
void dump(String path) => t.dump(path);

class _T {
  final Map<String, Map<String, Object?>> symbols = {};
  final List<Map<String, Object?>> tokens = [];
  final List<Map<String, Object?>> steps = [];
  int n = 0, tc = 0;

  String _vh(String s) {
    int h = 0xcbf29ce484222325;
    for (final c in s.codeUnits) { h ^= c; h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF; }
    return 'h:${h.toRadixString(16)}';
  }

  String _tok(Object? v) {
    tc++;
    final id = 'tok:$tc';
    final valueLike = v == null || v is num || v is String || v is bool;
    String repr;
    try { repr = v == null ? 'null' : (valueLike ? v.toString() : '<${v.runtimeType}>'); }
    catch (_) { repr = '<unrepr>'; }
    if (repr.length > 120) repr = repr.substring(0, 120);
    tokens.add({
      'id': id, 'type': v == null ? 'Null' : v.runtimeType.toString(), 'repr': repr,
      'identity': valueLike ? null : 'obj:${identityHashCode(v)}@run',
      'value_hash': valueLike ? _vh(repr) : null,
      'is_literal': valueLike, 'literal_repr': valueLike ? repr : null,
    });
    return id;
  }

  void rec(String qual, String module, String kind, Object? self, List<Object?> args) {
    final sid = 'sym:$qual';
    symbols.putIfAbsent(sid, () => {
      'id': sid, 'qualname': qual, 'kind': kind,
      'module': module, 'package': module.isEmpty ? null : module.split('.').first,
      'file': null, 'line': null, 'params': [],
    });
    final toks = <String>[];
    if (self != null) toks.add(_tok(self));
    for (final a in args) toks.add(_tok(a));
    n++;
    steps.add({
      'id': 'step:$n', 'callee': sid, 'site_file': null, 'site_line': null,
      'order': n, 'thread': 'main',
      'arg_style': {'positional': args.length, 'keyword': <String>[]},
      'callee_qualname': qual, 'args': toks,
    });
  }

  void dump(String path) {
    final doc = {
      'version': '1', 'kind': 'dynamic', 'run_id': 'run',
      'entrypoint': null, 'symbols': symbols.values.toList(),
      'steps': steps, 'tokens': tokens,
    };
    File(path).writeAsStringSync(const JsonEncoder.withIndent('  ').convert(doc));
  }
}
''';

Future<void> main(List<String> argv) async {
  String? entry, moduleRoot, file;
  for (var i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--entry': entry = argv[++i]; break;
      case '--module-root': moduleRoot = argv[++i]; break;
      default: file = argv[i];
    }
  }
  if (file == null || entry == null) {
    stderr.writeln('usage: dart run dart_trace.dart <file.dart> --entry <fn> [--module-root .]');
    exit(2);
  }

  final src = File(file).readAsStringSync();
  final result = parseString(content: src, throwIfDiagnostics: false,
      featureSet: FeatureSet.latestLanguageVersion());
  final module = moduleName(file, moduleRoot);

  final inst = Instrumenter(module);
  result.unit.accept(inst);

  // add the runtime import after the last directive (or at offset 0)
  var importOffset = 0;
  for (final d in result.unit.directives) { importOffset = d.end; }
  inst.edits.add(_Edit(importOffset, importOffset, "\nimport '__ct.dart' as __ct;\n"));

  final instrumented = applyEdits(src, inst.edits);

  final tmp = Directory.systemTemp.createTempSync('ct_dart_');
  try {
    File('${tmp.path}/__ct.dart').writeAsStringSync(_runtime);
    File('${tmp.path}/target.dart').writeAsStringSync(instrumented);
    File('${tmp.path}/__run.dart').writeAsStringSync('''
import 'target.dart' as app;
import '__ct.dart' as __ct;
Future<void> main() async {
  try { await (app.$entry() as dynamic); } catch (_) {}
  __ct.dump('${tmp.path}/trace.json');
}
''');
    final r = await Process.run('dart', ['run', '${tmp.path}/__run.dart']);
    if (!File('${tmp.path}/trace.json').existsSync()) {
      stderr.writeln('instrumented run produced no trace:\n${r.stderr}');
      exit(1);
    }
    stdout.write(File('${tmp.path}/trace.json').readAsStringSync());
  } finally {
    tmp.deleteSync(recursive: true);
  }
}
