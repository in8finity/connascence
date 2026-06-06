// Static adapter: build a TraceDoc *static spine* from Dart source via
// package:analyzer (the Dart SDK's own analysis library).
//
// The Dart sibling of python_ast.py / php_ast.php / ruby_ast.rb. Captures the
// static spine -> CoN / CoT / CoM / CoP, plus index-access record shape
// (`row['user_id']`) -> record-shape CoN+Meaning. Emits `kind: "static"`.
//
//   dart run dart_ast.dart lib/ [more.dart ...] [--module-root lib] > static.json
//
// REQUIRES package:analyzer 6.x (its classic AST API). In the project you
// analyze (or any throwaway Dart package):
//   dart pub add --dev "analyzer:^6.0.0"
// then `dart run <path>/dart_ast.dart <paths...>`. A syntactic parse is used, so
// no `pub get` of the analyzed project's own deps is needed — only the analyzer.
// NOTE: analyzer 13+ restructured the AST API and is NOT compatible; pin ^6.0.0.
//
// Resolution is name-based (no type resolution): a call resolves to the uniquely
// named declaration, else to an `ext:` symbol. Dart *does* write parameter types
// inline, so those are captured (CoT is informative for Dart). Record shape
// captures `recv['key']` (Map / JSON / decoded-row access).

import 'dart:convert';
import 'dart:io';

import 'package:analyzer/dart/analysis/features.dart';
import 'package:analyzer/dart/analysis/utilities.dart';
import 'package:analyzer/dart/ast/ast.dart';
import 'package:analyzer/dart/ast/visitor.dart';
import 'package:analyzer/source/line_info.dart';

final Map<String, Map<String, dynamic>> symbols = {};
final Map<String, List<String>> byName = {}; // short name -> [id]
final List<Map<String, dynamic>> steps = [];
final List<Map<String, dynamic>> tokens = [];
int tCount = 0, sCount = 0;

String dot(String s) => s.replaceAll('::', '.');

String vh(String s) {
  // dependency-free FNV-1a (only needs to be stable within one doc)
  int h = 0xcbf29ce484222325;
  for (final c in s.codeUnits) {
    h ^= c;
    h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF;
  }
  return 'h:${h.toRadixString(16)}';
}

String? pkgOf(String? m) =>
    (m == null || m.isEmpty) ? null : m.split('.').first;

String moduleName(String file, String? root) {
  var rel = root != null
      ? file.replaceFirst(RegExp('^${RegExp.escape(root.replaceAll(RegExp(r"/$"), ""))}/?'), '')
      : file.split('/').last;
  rel = rel.replaceAll(RegExp(r'\.dart$'), '');
  return rel.replaceAll('/', '.');
}

List<String> collectFiles(List<String> paths) {
  final out = <String>{};
  for (final p in paths) {
    final type = FileSystemEntity.typeSync(p);
    if (type == FileSystemEntityType.directory) {
      for (final f in Directory(p).listSync(recursive: true)) {
        if (f is File && f.path.endsWith('.dart')) out.add(f.path);
      }
    } else if (type == FileSystemEntityType.file && p.endsWith('.dart')) {
      out.add(p);
    }
  }
  final list = out.toList()..sort();
  return list;
}

List<Map<String, dynamic>> paramsOf(FormalParameterList? list) {
  final out = <Map<String, dynamic>>[];
  if (list == null) return out;
  var pos = 0;
  for (final p in list.parameters) {
    var hasDefault = false;
    NormalFormalParameter normal;
    if (p is DefaultFormalParameter) {
      hasDefault = p.defaultValue != null;
      normal = p.parameter;
    } else {
      normal = p as NormalFormalParameter;
    }
    String? type;
    if (normal is SimpleFormalParameter) {
      type = normal.type?.toSource();
    } else if (normal is FieldFormalParameter) {
      type = normal.type?.toSource();
    } else if (normal is FunctionTypedFormalParameter) {
      type = 'Function';
    }
    final name = normal.name?.lexeme ?? '?';
    String kind;
    int? position;
    if (p.isNamed) {
      kind = 'keyword';
      position = null;
      if (!p.isRequired) hasDefault = true;
    } else {
      kind = 'positional';
      position = pos++;
      if (p.isOptionalPositional) hasDefault = true;
    }
    out.add({
      'name': name,
      'position': position,
      'kind': kind,
      'type': type,
      'has_default': hasDefault,
    });
  }
  return out;
}

String symId(List<String> cs, List<String> fs, String name, String module) =>
    'sym:${dot(([module, ...cs, ...fs, name]).join("."))}';

class SymVisitor extends RecursiveAstVisitor<void> {
  final String file;
  final String module;
  final LineInfo lineInfo;
  final List<String> cs = [];
  final List<String> fs = [];
  SymVisitor(this.file, this.module, this.lineInfo);

  void _pushClass(String? name, void Function() body) {
    cs.add(name ?? 'anon');
    body();
    cs.removeLast();
  }

  @override
  void visitClassDeclaration(ClassDeclaration node) =>
      _pushClass(node.name.lexeme, () => super.visitClassDeclaration(node));
  @override
  void visitMixinDeclaration(MixinDeclaration node) =>
      _pushClass(node.name.lexeme, () => super.visitMixinDeclaration(node));
  @override
  void visitExtensionDeclaration(ExtensionDeclaration node) =>
      _pushClass(node.name?.lexeme ?? 'extension',
          () => super.visitExtensionDeclaration(node));
  @override
  void visitEnumDeclaration(EnumDeclaration node) =>
      _pushClass(node.name.lexeme, () => super.visitEnumDeclaration(node));

  void _addDef(String name, int offset, FormalParameterList? params,
      String? returnType) {
    final isMethod = cs.isNotEmpty && fs.isEmpty;
    final qual = dot(([module, ...cs, ...fs, name]).join('.'));
    final id = 'sym:$qual';
    if (!symbols.containsKey(id)) {
      symbols[id] = {
        'id': id,
        'qualname': qual,
        'kind': isMethod ? 'method' : 'function',
        'file': file,
        'line': lineInfo.getLocation(offset).lineNumber,
        'module': module,
        'package': pkgOf(module),
        'params': paramsOf(params),
        'returns_type': returnType,
      };
      (byName[name] ??= []).add(id);
    }
  }

  @override
  void visitMethodDeclaration(MethodDeclaration node) {
    _addDef(node.name.lexeme, node.offset, node.parameters,
        node.returnType?.toSource());
    fs.add(node.name.lexeme);
    super.visitMethodDeclaration(node);
    fs.removeLast();
  }

  @override
  void visitFunctionDeclaration(FunctionDeclaration node) {
    _addDef(node.name.lexeme, node.offset,
        node.functionExpression.parameters, node.returnType?.toSource());
    fs.add(node.name.lexeme);
    super.visitFunctionDeclaration(node);
    fs.removeLast();
  }
}

class CallVisitor extends RecursiveAstVisitor<void> {
  final String file;
  final String module;
  final LineInfo lineInfo;
  final List<String> cs = [];
  final List<String> fs = [];
  final List<String> fn = [];
  String? modSym;
  CallVisitor(this.file, this.module, this.lineInfo);

  String _moduleSymbol() {
    final id = 'sym:$module.<module>';
    symbols.putIfAbsent(
        id,
        () => {
              'id': id,
              'qualname': module,
              'kind': 'module',
              'file': file,
              'line': 0,
              'module': module,
              'package': pkgOf(module),
              'params': []
            });
    return modSym = id;
  }

  String _enclosing() => fn.isNotEmpty ? fn.last : (modSym ?? _moduleSymbol());

  String _external(String qual) {
    final id = 'sym:ext:$qual';
    symbols.putIfAbsent(
        id,
        () => {
              'id': id,
              'qualname': qual,
              'kind': 'external',
              'file': null,
              'line': null,
              'module': null,
              'package': null,
              'params': []
            });
    return id;
  }

  String _recordSymbol(String base) {
    final enc = _enclosing();
    final id = 'sym:record:$enc::$base';
    symbols.putIfAbsent(
        id,
        () => {
              'id': id,
              'qualname': base,
              'kind': 'record',
              'file': file,
              'line': null,
              'module': module,
              'package': pkgOf(module),
              'in_function': symbols[enc]?['qualname'] ?? module,
              'params': []
            });
    return id;
  }

  void _pushClass(String? name, void Function() body) {
    cs.add(name ?? 'anon');
    body();
    cs.removeLast();
  }

  @override
  void visitClassDeclaration(ClassDeclaration node) =>
      _pushClass(node.name.lexeme, () => super.visitClassDeclaration(node));
  @override
  void visitMixinDeclaration(MixinDeclaration node) =>
      _pushClass(node.name.lexeme, () => super.visitMixinDeclaration(node));
  @override
  void visitExtensionDeclaration(ExtensionDeclaration node) =>
      _pushClass(node.name?.lexeme ?? 'extension',
          () => super.visitExtensionDeclaration(node));
  @override
  void visitEnumDeclaration(EnumDeclaration node) =>
      _pushClass(node.name.lexeme, () => super.visitEnumDeclaration(node));

  @override
  void visitMethodDeclaration(MethodDeclaration node) {
    fn.add(symId(cs, fs, node.name.lexeme, module));
    fs.add(node.name.lexeme);
    super.visitMethodDeclaration(node);
    fs.removeLast();
    fn.removeLast();
  }

  @override
  void visitFunctionDeclaration(FunctionDeclaration node) {
    fn.add(symId(cs, fs, node.name.lexeme, module));
    fs.add(node.name.lexeme);
    super.visitFunctionDeclaration(node);
    fs.removeLast();
    fn.removeLast();
  }

  int _line(int offset) => lineInfo.getLocation(offset).lineNumber;

  String? _stringValue(Expression e) =>
      e is SimpleStringLiteral ? e.value : null;

  void _recordAccess(Expression? baseNode, String key, int offset) {
    if (key.isEmpty) return;
    var base = (baseNode?.toSource() ?? 'this');
    if (base.length > 40) base = base.substring(0, 40);
    final rid = _recordSymbol(base);
    final tid = 'tok:$module:${++tCount}';
    tokens.add({
      'id': tid,
      'type': 'key',
      'repr': key,
      'identity': null,
      'value_hash': vh(key),
      'is_literal': true,
      'literal_repr': key,
      'key': key
    });
    steps.add({
      'id': 'step:$module:${++sCount}',
      'callee': rid,
      'in_symbol': _enclosing(),
      'site_file': file,
      'site_line': _line(offset),
      'arg_style': {'positional': 1, 'keyword': []},
      'callee_qualname': base,
      'access': 'field',
      'args': [tid]
    });
  }

  String _tokenFor(Expression e) {
    final tid = 'tok:$module:${++tCount}';
    String type = 'expr';
    String? rep;
    var lit = true;
    if (e is SimpleStringLiteral) {
      type = 'string';
      rep = e.value;
    } else if (e is IntegerLiteral) {
      type = 'int';
      rep = e.value?.toString() ?? e.toSource();
    } else if (e is DoubleLiteral) {
      type = 'float';
      rep = e.value.toString();
    } else if (e is BooleanLiteral) {
      type = 'literal';
      rep = e.value.toString();
    } else if (e is NullLiteral) {
      type = 'literal';
      rep = 'null';
    } else {
      lit = false;
    }
    if (lit) {
      final r = (rep ?? '');
      tokens.add({
        'id': tid,
        'type': type,
        'repr': r.length > 120 ? r.substring(0, 120) : r,
        'identity': null,
        'value_hash': vh(r),
        'is_literal': true,
        'literal_repr': r.length > 120 ? r.substring(0, 120) : r
      });
    } else {
      final src = e.toSource();
      tokens.add({
        'id': tid,
        'type': 'expr',
        'repr': src.length > 120 ? src.substring(0, 120) : src,
        'identity': null,
        'value_hash': null,
        'is_literal': false,
        'literal_repr': null
      });
    }
    return tid;
  }

  @override
  void visitIndexExpression(IndexExpression node) {
    final k = _stringValue(node.index);
    if (k != null) _recordAccess(node.target, k, node.offset);
    super.visitIndexExpression(node);
  }

  void _emitCall(String name, NodeList<Expression> arglist, int offset) {
    final ids = byName[name];
    String callee, cq;
    if (ids != null && ids.length == 1) {
      callee = ids.first;
      cq = symbols[callee]!['qualname'] as String;
    } else {
      callee = _external(name);
      cq = name;
    }
    var pos = 0;
    final kw = <String>[];
    final args = <String>[];
    for (final a in arglist) {
      if (a is NamedExpression) {
        kw.add(a.name.label.name);
      } else {
        args.add(_tokenFor(a));
        pos++;
      }
    }
    steps.add({
      'id': 'step:$module:${++sCount}',
      'callee': callee,
      'in_symbol': _enclosing(),
      'site_file': file,
      'site_line': _line(offset),
      'arg_style': {'positional': pos, 'keyword': kw},
      'callee_qualname': cq,
      'args': args
    });
  }

  @override
  void visitMethodInvocation(MethodInvocation node) {
    _emitCall(node.methodName.name, node.argumentList.arguments, node.offset);
    super.visitMethodInvocation(node);
  }

  @override
  void visitInstanceCreationExpression(InstanceCreationExpression node) {
    final t = node.constructorName.type.toSource().split('<').first;
    _emitCall(t, node.argumentList.arguments, node.offset);
    super.visitInstanceCreationExpression(node);
  }
}

void main(List<String> argv) {
  String? moduleRoot;
  final paths = <String>[];
  for (var i = 0; i < argv.length; i++) {
    if (argv[i] == '--module-root') {
      moduleRoot = argv[++i];
    } else {
      paths.add(argv[i]);
    }
  }

  final files = collectFiles(paths);
  final units = <List<dynamic>>[];
  for (final f in files) {
    try {
      final result = parseString(
          content: File(f).readAsStringSync(),
          throwIfDiagnostics: false,
          featureSet: FeatureSet.latestLanguageVersion());
      final module = moduleName(f, moduleRoot);
      result.unit.accept(SymVisitor(f, module, result.lineInfo));
      units.add([result, f, module]);
    } catch (e) {
      stderr.writeln('skip $f: $e');
    }
  }
  for (final u in units) {
    final result = u[0];
    result.unit.accept(CallVisitor(u[1] as String, u[2] as String, result.lineInfo));
  }

  final doc = {
    'version': '1',
    'kind': 'static',
    'entrypoint': files.isEmpty ? null : moduleName(files.first, moduleRoot),
    'symbols': symbols.values.toList(),
    'steps': steps,
    'tokens': tokens,
  };
  print(const JsonEncoder.withIndent('  ').convert(doc));
}
