<?php
// Dynamic adapter: turn a real PHP execution into a dynamic TraceDoc via the
// uopz extension's function/method hooks. The PHP sibling of python_settrace.py
// / ruby_tracepoint.rb / js_instrument.js.
//
// PHP has no core call hook, and Xdebug's trace renders objects by VALUE
// (no stable handle), so it cannot reconstruct object identity — only CoV. uopz
// hooks run with `$this` bound and the live arguments, so `spl_object_id()`
// gives real identity → the full dynamic four (CoE / CoV / CoI; CoTm only with
// real threads, which PHP rarely has).
//
//   pecl install uopz && docker-php-ext-enable uopz   (or enable in php.ini)
//
// Usage:
//   require 'php_uopz.php';
//   require 'app.php';                       // load the code FIRST (defines fns/classes)
//   $tr = new UopzTracer('run1', [__DIR__]); // scope = roots of your own code
//   $tr->instrument();                        // hook every in-scope fn/method
//   App\main();                               // exercise the path you care about
//   file_put_contents('trace.json', $tr->toJson());
//
// Captures per call: callee qualname, the receiver `$this` (-> identity), each
// argument (object -> identity, scalar/array -> value_hash), order, thread.
// LIMITATIONS: uopz_set_hook is a pre-hook, so there are no `returns` tokens and
// no `caller` links (the dynamic connascences need neither — they group by
// identity/value, not by call tree). Classes autoloaded AFTER instrument() are
// not hooked; require/trigger them first.

final class UopzTracer
{
    private array $symbols = [];
    private array $tokens = [];
    private array $steps = [];
    private int $n = 0;
    private int $tc = 0;
    private array $scope;

    public function __construct(private string $runId = 'run', array $scope = [])
    {
        $this->scope = array_map('realpath', $scope);
        if (!function_exists('uopz_set_hook')) {
            fwrite(STDERR, "ERROR: uopz not loaded. pecl install uopz && docker-php-ext-enable uopz\n");
            exit(2);
        }
    }

    public function instrument(): void
    {
        foreach (get_defined_functions()['user'] as $fn) {
            try { $rf = new ReflectionFunction($fn); } catch (Throwable) { continue; }
            if (!$this->inScope($rf->getFileName())) continue;
            $qual = $this->dot($rf->getName());
            $mod = $this->dot($rf->getNamespaceName());
            $this->symbol($qual, 'function', $mod, $rf);
            uopz_set_hook($fn, function () use ($qual) {
                // @phpstan-ignore-next-line  (no $this in a plain function hook)
                $GLOBALS['__uopz_tracer']->record($qual, null, func_get_args());
            });
        }
        foreach (get_declared_classes() as $cls) {
            try { $rc = new ReflectionClass($cls); } catch (Throwable) { continue; }
            if ($rc->isInternal() || !$this->inScope($rc->getFileName())) continue;
            $mod = $this->dot($rc->getNamespaceName());
            foreach ($rc->getMethods() as $rm) {
                if ($rm->getDeclaringClass()->getName() !== $cls) continue; // own methods only
                if ($rm->isAbstract()) continue;
                $method = $rm->getName();
                $qual = $this->dot($cls) . '.' . $method;
                $this->symbol($qual, 'method', $mod, $rm);
                uopz_set_hook($cls, $method, function () use ($qual) {
                    $self = isset($this) ? $this : null;
                    $GLOBALS['__uopz_tracer']->record($qual, $self, func_get_args());
                });
            }
        }
        $GLOBALS['__uopz_tracer'] = $this;
    }

    public function record(string $qual, ?object $self, array $args): void
    {
        $tokens = [];
        if (is_object($self)) $tokens[] = $this->token($self);   // receiver = shared instance
        foreach ($args as $a) $tokens[] = $this->token($a);
        $this->n++;
        $this->steps[] = [
            'id' => 'step:' . $this->n,
            'callee' => 'sym:' . $qual,
            'site_file' => null, 'site_line' => null,
            'order' => $this->n, 'thread' => 'main',
            'arg_style' => ['positional' => count($args), 'keyword' => []],
            'callee_qualname' => $qual,
            'args' => $tokens,
        ];
    }

    public function toJson(): string
    {
        $doc = [
            'version' => '1', 'kind' => 'dynamic', 'run_id' => $this->runId,
            'entrypoint' => null,
            'symbols' => array_values($this->symbols),
            'steps' => $this->steps,
            'tokens' => array_values($this->tokens),
        ];
        return json_encode($doc, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    }

    // ---- internals -------------------------------------------------------

    private function dot(string $s): string { return str_replace('\\', '.', ltrim($s, '\\')); }

    private function vh(string $s): string { return 'sha256:' . substr(hash('sha256', $s), 0, 16); }

    private function inScope(string|false $file): bool
    {
        if ($file === false) return false;
        if (str_contains($file, 'php_uopz')) return false;
        if (empty($this->scope)) return true;
        $rp = realpath($file);
        foreach ($this->scope as $root) {
            if ($root && str_starts_with($rp, $root)) return true;
        }
        return false;
    }

    private function symbol(string $qual, string $kind, string $module, ReflectionFunctionAbstract $r): void
    {
        $id = 'sym:' . $qual;
        if (isset($this->symbols[$id])) return;
        $params = [];
        foreach ($r->getParameters() as $i => $p) {
            $t = $p->getType();
            $params[] = [
                'name' => $p->getName(), 'position' => $i, 'kind' => 'positional',
                'type' => $t instanceof ReflectionNamedType ? $t->getName() : ($t ? (string)$t : null),
                'has_default' => $p->isOptional(),
            ];
        }
        $this->symbols[$id] = [
            'id' => $id, 'qualname' => $qual, 'kind' => $kind,
            'file' => $r->getFileName() ?: null, 'line' => $r->getStartLine() ?: null,
            'module' => $module ?: null, 'package' => $module ? explode('.', $module)[0] : null,
            'params' => $params,
        ];
    }

    private function repr($v): string
    {
        $r = match (true) {
            is_bool($v) => $v ? 'true' : 'false',
            is_null($v) => 'null',
            is_array($v) => json_encode($v),
            is_scalar($v) => (string)$v,
            default => gettype($v),
        };
        return mb_strlen((string)$r) > 120 ? mb_substr((string)$r, 0, 120) : (string)$r;
    }

    private function token($v): string
    {
        $tid = 'tok:' . (++$this->tc);
        if (is_object($v)) {
            $tok = [
                'id' => $tid, 'type' => get_class($v),
                'repr' => get_class($v) . '#' . spl_object_id($v),
                'identity' => 'obj:' . spl_object_id($v) . '@' . $this->runId,
                'value_hash' => null, 'is_literal' => false, 'literal_repr' => null,
            ];
        } else {
            $r = $this->repr($v);
            $tok = [
                'id' => $tid, 'type' => gettype($v), 'repr' => $r, 'identity' => null,
                'value_hash' => $this->vh($r),
                'is_literal' => !is_array($v), 'literal_repr' => is_array($v) ? null : $r,
            ];
        }
        $this->tokens[$tid] = $tok;
        return $tid;
    }
}
