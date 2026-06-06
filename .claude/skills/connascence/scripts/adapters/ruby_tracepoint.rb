#!/usr/bin/env ruby
# frozen_string_literal: true
#
# Dynamic adapter: turn a real Ruby execution into a dynamic TraceDoc via
# TracePoint (Ruby's stdlib :call/:return hook — the analog of Python's
# sys.settrace). The Ruby sibling of python_settrace.py.
#
# Captures, per call: the callee + def site (-> TraceSymbol), the invoking step
# (-> caller link), and a DataToken per argument *and the receiver* carrying
# type, repr, object identity (run-scoped), value_hash, and whether the value is
# value-like. That feeds the dynamic kinds CoE / CoTm / CoV / CoI — the coupling
# a static call graph cannot see.
#
# NOTE: this is NOT ruby-prof. ruby-prof is a timing profiler; it records call
# counts and durations but neither argument values nor object identities, so it
# cannot drive the value/identity-based dynamic connascences. TracePoint can.
#
# Usage:
#   require_relative 'ruby_tracepoint'
#   require_relative 'lib/app'
#   tr = Tracer.new(run_id: 'run1', scope: ['lib'], module_root: '.')
#   tr.trace { App.main }                 # exercise the path you care about
#   File.write('trace.json', tr.to_json)  # a dynamic TraceDoc for trace-ingest
#
# `scope` records only frames whose file is under one of those roots (the
# dynamic analog of `trace-detect.py --exclude-external`); without it, stdlib /
# gem internals are traced too and bury your findings. Library code called
# between two of your frames is skipped transparently — the inner in-scope call's
# `caller` resolves to its nearest in-scope ancestor.

require 'json'
require 'digest'

class Tracer
  attr_reader :symbols, :steps, :tokens

  VALUE_LIKE = [Integer, Float, Symbol, String, TrueClass, FalseClass, NilClass].freeze

  def initialize(run_id: 'run', entrypoint: nil, module_root: nil, scope: nil)
    @run_id = run_id
    @entrypoint = entrypoint
    @module_root = module_root
    @scope = scope&.map { |r| File.expand_path(r) }
    @symbols = {}            # qualname -> symbol hash
    @tokens = {}             # id -> token hash
    @steps = []
    @stacks = Hash.new { |h, k| h[k] = [] }  # thread object_id -> [step id]
    @n = 0
    @tc = 0
  end

  def trace
    tp = TracePoint.new(:call, :return) { |t| handle(t) }
    tp.enable
    yield
  ensure
    tp&.disable
  end

  def doc
    {
      'version' => '1', 'kind' => 'dynamic', 'run_id' => @run_id,
      'entrypoint' => @entrypoint,
      'symbols' => @symbols.values,
      'steps' => @steps,
      'tokens' => @tokens.values
    }
  end

  def to_json(*) = JSON.pretty_generate(doc)

  private

  def vh(s) = "sha256:#{Digest::SHA256.hexdigest(s)[0, 16]}"

  def dot(s) = s.gsub('::', '.')

  def in_scope?(path)
    return false if path.include?('ruby_tracepoint')
    return true if @scope.nil?
    ap = File.expand_path(path)
    @scope.any? { |r| ap.start_with?(r) }
  end

  def module_name(path)
    rel = @module_root ? path.sub(%r{\A#{Regexp.escape(File.expand_path(@module_root))}/?}, '') : File.basename(path)
    dot(rel.sub(/\.rb\z/, '').gsub('/', '.'))
  end

  # qualname + whether the receiver is a data instance (identity-bearing)
  def qual_for(tp)
    kname = tp.defined_class.to_s
    if (m = kname.match(/\A#<Class:(.+)>\z/))
      ["#{dot(m[1])}.#{tp.method_id}", false] # class/module method: self is the class
    else
      name = tp.defined_class.name || kname
      name = 'Object' if name == 'Object'
      [dot("#{name}.#{tp.method_id}"), name != 'Object'] # instance method
    end
  end

  def safe_repr(v)
    r = begin
      v.inspect
    rescue StandardError
      "<#{v.class}>"
    end
    r.length > 120 ? r[0, 120] : r
  end

  def token_for(v)
    @tc += 1
    tid = "tok:#{@tc}"
    value_like = VALUE_LIKE.any? { |c| v.is_a?(c) }
    repr = safe_repr(v)
    @tokens[tid] = {
      'id' => tid, 'type' => v.class.name, 'repr' => repr,
      'identity' => value_like ? nil : "obj:#{v.object_id.to_s(16)}@#{@run_id}",
      'value_hash' => value_like ? vh(repr) : nil,
      'is_literal' => value_like,
      'literal_repr' => value_like ? repr : nil
    }
    tid
  end

  def symbol_for(tp, qual)
    id = "sym:#{qual}"
    unless @symbols.key?(id)
      mod = module_name(tp.path)
      @symbols[id] = {
        'id' => id, 'qualname' => qual, 'kind' => qual.include?('.') ? 'method' : 'function',
        'file' => tp.path, 'line' => tp.lineno,
        'module' => mod, 'package' => mod.split('.').first,
        'params' => []
      }
    end
    id
  end

  def param_tokens(tp)
    meth = begin
      tp.self.method(tp.method_id)
    rescue StandardError
      nil
    end
    return [] unless meth
    b = tp.binding
    out = []
    meth.parameters.each do |ptype, pname|
      next if pname.nil? || ptype == :block
      val = begin
        b.local_variable_get(pname)
      rescue StandardError
        next
      end
      out << token_for(val)
    end
    out
  end

  def handle(tp)
    return unless in_scope?(tp.path)
    key = Thread.current.object_id
    if tp.event == :return
      stack = @stacks[key]
      return if stack.empty?
      sid = stack.last
      step = @steps.find { |s| s['id'] == sid }
      step['returns'] = token_for(tp.return_value) if step
      stack.pop
      return
    end
    # :call
    qual, recv_identity = qual_for(tp)
    callee = symbol_for(tp, qual)
    args = []
    args << token_for(tp.self) if recv_identity # the receiver = shared instance
    args.concat(param_tokens(tp))
    @n += 1
    stack = @stacks[key]
    tname = Thread.current.name ||
            (Thread.current == Thread.main ? 'main' : "thread-#{Thread.current.object_id}")
    step = {
      'id' => "step:#{@n}",
      'callee' => callee,
      'site_file' => tp.path, 'site_line' => tp.lineno,
      'order' => @n, 'thread' => tname,
      'arg_style' => { 'positional' => args.length, 'keyword' => [] },
      'args' => args
    }
    step['caller'] = stack.last unless stack.empty?
    @steps << step
    stack.push(step['id'])
  end
end
