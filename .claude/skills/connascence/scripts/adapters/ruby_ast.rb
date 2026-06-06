#!/usr/bin/env ruby
# frozen_string_literal: true
#
# Static adapter: build a TraceDoc *static spine* from Ruby source via prism
# (Ruby's official parser, bundled with Ruby 3.4+; `gem install prism` on older).
#
# The Ruby sibling of python_ast.py / php_ast.php. Captures the static spine ->
# CoN / CoT / CoM / CoP, plus hash-access record shape (`row[:user_id]`) ->
# record-shape CoN+Meaning. Emits `kind: "static"`.
#
#   ruby ruby_ast.rb lib/ [more.rb ...] [--module-root lib] > static.json
#
# Resolution (no type inference — Ruby is duck-typed): a call resolves to the
# uniquely-named method definition, else to an `ext:` symbol (name only).
# Record shape captures `recv[:key]` / `recv['key']` (and `recv.fetch(:key)`) —
# Ruby's params-hash / JSON / DB-row pattern. Note: Ruby has no inline parameter
# types, so CoT fires on *every* parameter; use `trace-detect.py --only ...` to
# focus on CoN/CoP/CoM/record-shape when analyzing Ruby.

begin
  require "prism"
rescue LoadError
  warn "ERROR: prism not available. Ruby 3.4+ bundles it; otherwise: gem install prism"
  exit 2
end
require "json"
require "digest"

module_root = nil
paths = []
args = ARGV.dup
until args.empty?
  a = args.shift
  if a == "--module-root" then module_root = args.shift else paths << a end
end

def dot(s) = s.gsub("::", ".").sub(/\A\./, "")
def vh(s) = "sha256:" + Digest::SHA256.hexdigest(s)[0, 16]
def pkg_of(m) = m.nil? || m.empty? ? nil : m.split(".").first

def module_name(file, root)
  rel = root ? file.sub(%r{\A#{Regexp.escape(root.chomp('/'))}/?}, "") : File.basename(file)
  dot(rel.sub(/\.rb\z/, "").gsub("/", "."))
end

def collect_files(paths)
  out = []
  paths.each do |p|
    if File.directory?(p)
      out.concat(Dir.glob(File.join(p, "**", "*.rb")))
    elsif File.file?(p) && p.end_with?(".rb")
      out << p
    end
  end
  out.uniq.sort
end

SYMBOLS = {}     # id => hash
BY_METHOD = Hash.new { |h, k| h[k] = [] }  # short name => [id]
STEPS = []
TOKENS = []
COUNTER = { t: 0, s: 0 }

# qualname + module from a namespace stack of {name:, kind:} frames + method.
# module = the namespace ABOVE the innermost *class* (so two methods of one class
# are same_class), or all module frames when the def sits directly in a module
# (so module-functions of the same module are same_module).
def qual_and_module(ns, name, file_module)
  return [dot("#{file_module}.#{name}"), file_module] if ns.empty?
  names = ns.map { |f| f[:name] }
  qual = dot((names + [name]).join("."))
  cls_idx = ns.rindex { |f| f[:kind] == :class }
  mod = dot((cls_idx ? names[0...cls_idx] : names).join("."))
  [qual, mod.empty? ? nil : mod]
end

def sym_id(ns, name, file_module)
  q, = qual_and_module(ns, name, file_module)
  "sym:#{q}"
end

def params_of(node)
  ps = node.parameters
  out = []
  pos = 0
  return out if ps.nil?
  (ps.requireds + (ps.respond_to?(:posts) ? ps.posts : [])).each do |p|
    nm = p.respond_to?(:name) && p.name ? p.name.to_s : "?"
    out << { "name" => nm, "position" => pos, "kind" => "positional", "type" => nil, "has_default" => false }
    pos += 1
  end
  ps.optionals.each do |p|
    out << { "name" => p.name.to_s, "position" => pos, "kind" => "positional", "type" => nil, "has_default" => true }
    pos += 1
  end
  if ps.rest && ps.rest.respond_to?(:name)
    out << { "name" => (ps.rest.name || "*").to_s, "position" => pos, "kind" => "vararg", "type" => nil, "has_default" => false }
    pos += 1
  end
  ps.keywords.each do |p|
    has_def = p.class.name.include?("Optional")
    out << { "name" => p.name.to_s, "position" => nil, "kind" => "keyword", "type" => nil, "has_default" => has_def }
  end
  out
end

# ---- pass 1: collect method symbols -------------------------------------
class SymVisitor < Prism::Visitor
  def initialize(file, file_module)
    @file = file; @fm = file_module; @ns = []
  end

  def visit_class_node(node)
    @ns.push({ name: node.constant_path.slice, kind: :class }); super; @ns.pop
  end
  def visit_module_node(node)
    @ns.push({ name: node.constant_path.slice, kind: :module }); super; @ns.pop
  end

  def visit_def_node(node)
    name = node.name.to_s
    q, mod = qual_and_module(@ns, name, @fm)
    id = "sym:#{q}"
    unless SYMBOLS.key?(id)
      SYMBOLS[id] = {
        "id" => id, "qualname" => q, "kind" => @ns.empty? ? "function" : "method",
        "file" => @file, "line" => node.location.start_line,
        "module" => mod, "package" => pkg_of(mod),
        "params" => params_of(node)
      }
      BY_METHOD[name] << id
    end
    super
  end
end

# ---- pass 2: collect calls + record access ------------------------------
class CallVisitor < Prism::Visitor
  def initialize(file, file_module)
    @file = file; @fm = file_module; @ns = []; @fn = []; @modsym = nil
  end

  def visit_class_node(node)
    @ns.push({ name: node.constant_path.slice, kind: :class }); super; @ns.pop
  end
  def visit_module_node(node)
    @ns.push({ name: node.constant_path.slice, kind: :module }); super; @ns.pop
  end
  def visit_def_node(node)
    @fn.push(sym_id(@ns, node.name.to_s, @fm)); super; @fn.pop
  end

  def visit_call_node(node)
    handle_call(node)
    super
  end

  private

  def module_symbol
    return @modsym if @modsym
    id = "sym:#{@fm}.<module>"
    SYMBOLS[id] ||= { "id" => id, "qualname" => @fm, "kind" => "module",
                      "file" => @file, "line" => 0, "module" => @fm,
                      "package" => pkg_of(@fm), "params" => [] }
    @modsym = id
  end

  def enclosing = @fn.last || module_symbol

  def external(qual)
    id = "sym:ext:#{qual}"
    SYMBOLS[id] ||= { "id" => id, "qualname" => qual, "kind" => "external",
                      "file" => nil, "line" => nil, "module" => nil, "package" => nil, "params" => [] }
    id
  end

  def record_symbol(base)
    enc = enclosing
    id = "sym:record:#{enc}::#{base}"
    SYMBOLS[id] ||= { "id" => id, "qualname" => base, "kind" => "record",
                      "file" => @file, "line" => nil, "module" => @fm, "package" => pkg_of(@fm),
                      "in_function" => SYMBOLS.dig(enc, "qualname") || @fm, "params" => [] }
    id
  end

  def literal_key(arg)
    case arg
    when Prism::SymbolNode then arg.respond_to?(:value) && arg.value ? arg.value.to_s : arg.slice.sub(/\A:/, "")
    when Prism::StringNode then arg.respond_to?(:unescaped) ? arg.unescaped : arg.slice.gsub(/\A['"]|['"]\z/, "")
    end
  end

  def record_access(base_node, key, line)
    return if key.nil? || key.empty?
    base = (base_node ? base_node.slice : "self")[0, 40]
    rid = record_symbol(base)
    COUNTER[:t] += 1
    tid = "tok:#{@fm}:#{COUNTER[:t]}"
    TOKENS << { "id" => tid, "type" => "key", "repr" => key[0, 120], "identity" => nil,
                "value_hash" => vh(key.inspect), "is_literal" => true, "literal_repr" => key[0, 120], "key" => key }
    COUNTER[:s] += 1
    STEPS << { "id" => "step:#{@fm}:#{COUNTER[:s]}", "callee" => rid, "in_symbol" => enclosing,
               "site_file" => @file, "site_line" => line,
               "arg_style" => { "positional" => 1, "keyword" => [] },
               "callee_qualname" => base, "access" => "field", "args" => [tid] }
  end

  def token_for(expr)
    COUNTER[:t] += 1
    tid = "tok:#{@fm}:#{COUNTER[:t]}"
    lit = true; type = "expr"; rep = nil
    case expr
    when Prism::StringNode then type = "string"; rep = (expr.respond_to?(:unescaped) ? expr.unescaped : expr.slice)
    when Prism::SymbolNode then type = "symbol"; rep = expr.slice
    when Prism::IntegerNode then type = "int"; rep = expr.slice
    when Prism::FloatNode then type = "float"; rep = expr.slice
    when Prism::TrueNode, Prism::FalseNode, Prism::NilNode then type = "literal"; rep = expr.slice
    else lit = false
    end
    if lit
      TOKENS << { "id" => tid, "type" => type, "repr" => rep.to_s[0, 120], "identity" => nil,
                  "value_hash" => vh(rep.to_s), "is_literal" => true, "literal_repr" => rep.to_s[0, 120] }
    else
      TOKENS << { "id" => tid, "type" => "expr", "repr" => expr.slice[0, 120], "identity" => nil,
                  "value_hash" => nil, "is_literal" => false, "literal_repr" => nil }
    end
    tid
  end

  def handle_call(node)
    name = node.name.to_s
    arglist = node.arguments ? node.arguments.arguments : []

    # record access: recv[:key] / recv['key']  (CallNode name :[])
    if name == "[]" && arglist.length == 1
      k = literal_key(arglist[0])
      return record_access(node.receiver, k, node.location.start_line) if k
    end
    # recv.fetch(:key)
    if name == "fetch" && node.receiver && !arglist.empty?
      k = literal_key(arglist[0])
      return record_access(node.receiver, k, node.location.start_line) if k
    end

    # resolve callee by method name (unique-or-external)
    ids = BY_METHOD[name]
    if ids.length == 1
      callee = ids[0]; cq = SYMBOLS[callee]["qualname"]
    else
      callee = external(name); cq = name
    end

    pos = 0; kw = []; targs = []
    arglist.each do |a|
      case a
      when Prism::KeywordHashNode
        a.elements.each { |e| kw << (e.respond_to?(:key) && e.key.respond_to?(:slice) ? e.key.slice.sub(/:\z/, "").sub(/\A:/, "") : "kw") }
      when Prism::SplatNode, Prism::BlockArgumentNode
        next
      else
        targs << token_for(a); pos += 1
      end
    end
    COUNTER[:s] += 1
    STEPS << { "id" => "step:#{@fm}:#{COUNTER[:s]}", "callee" => callee, "in_symbol" => enclosing,
               "site_file" => @file, "site_line" => node.location.start_line,
               "arg_style" => { "positional" => pos, "keyword" => kw },
               "callee_qualname" => cq, "args" => targs }
  end
end

# ---- drive --------------------------------------------------------------
files = collect_files(paths)
parsed = []
files.each do |f|
  src = File.read(f)
  res = Prism.parse(src)
  fm = module_name(f, module_root)
  res.value.accept(SymVisitor.new(f, fm))
  parsed << [res, f, fm]
rescue StandardError => e
  warn "skip #{f}: #{e.message}"
end
parsed.each do |res, f, fm|
  res.value.accept(CallVisitor.new(f, fm))
end

doc = {
  "version" => "1", "kind" => "static",
  "entrypoint" => files.empty? ? nil : module_name(files.first, module_root),
  "symbols" => SYMBOLS.values,
  "steps" => STEPS,
  "tokens" => TOKENS
}
puts JSON.pretty_generate(doc)
