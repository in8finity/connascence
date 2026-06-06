<?php
/**
 * Static adapter: build a TraceDoc *static spine* from PHP source via
 * nikic/php-parser (the parser PHPStan / Psalm / Rector use).
 *
 * The PHP sibling of python_ast.py / typescript_ast.mjs. Captures the static
 * spine -> CoN / CoT / CoM / CoP, plus array-dim record shape
 * (`$row['user_id']`) -> record-shape CoN+Meaning. Emits `kind: "static"`.
 *
 *   php php_ast.php src/ [more.php ...] [--module-root src] > static.json
 *
 * REQUIRES nikic/php-parser (v5). In the project you analyze:
 *   composer require --dev nikic/php-parser
 * then run with `php`. The adapter finds vendor/autoload.php from your cwd,
 * the COMPOSER_VENDOR env var, or its own directory.
 *
 * Resolution (no type inference, like the Python adapter): a call resolves to
 * the declaration its name points at when unique, else to an `ext:` symbol
 * (name only). Method calls `$x->m()` resolve by method name when unique.
 * Property fetch (`$obj->prop`) is NOT treated as record shape — without type
 * info it's indistinguishable from class-member access; only array-dim
 * (`$arr['k']`, PHP's associative-array DB-row pattern) is captured.
 */

$autoloads = [];
if ($v = getenv('COMPOSER_VENDOR')) $autoloads[] = rtrim($v, '/') . '/autoload.php';
$autoloads[] = getcwd() . '/vendor/autoload.php';
$autoloads[] = __DIR__ . '/vendor/autoload.php';
$loaded = false;
foreach ($autoloads as $a) {
    if (is_file($a)) { require $a; $loaded = true; break; }
}
if (!$loaded || !class_exists('PhpParser\\ParserFactory')) {
    fwrite(STDERR, "ERROR: nikic/php-parser not found.\n" .
        "  composer require --dev nikic/php-parser   (in the project you analyze)\n" .
        "then re-run: php <path>/php_ast.php <paths...>\n");
    exit(2);
}

use PhpParser\ParserFactory;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\NodeVisitor\NameResolver;
use PhpParser\PrettyPrinter;
use PhpParser\Node;

// ---- args ---------------------------------------------------------------
$paths = [];
$moduleRoot = null;
$argvv = array_slice($argv, 1);
for ($i = 0; $i < count($argvv); $i++) {
    if ($argvv[$i] === '--module-root') { $moduleRoot = $argvv[++$i]; }
    else { $paths[] = $argvv[$i]; }
}

function collect_files(array $paths): array {
    $out = [];
    foreach ($paths as $p) {
        if (is_dir($p)) {
            $it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($p, FilesystemIterator::SKIP_DOTS));
            foreach ($it as $f) {
                if ($f->isFile() && preg_match('/\.php$/', $f->getFilename())) $out[] = $f->getPathname();
            }
        } elseif (is_file($p) && preg_match('/\.php$/', $p)) {
            $out[] = $p;
        }
    }
    sort($out);
    return array_values(array_unique($out));
}

function dotted(string $s): string { return str_replace('\\', '.', ltrim($s, '\\')); }
function vh(string $s): string { return 'sha256:' . substr(hash('sha256', $s), 0, 16); }
function pkg_of(?string $mod): ?string { return $mod ? explode('.', $mod)[0] : null; }

function module_name(string $file, ?string $root): string {
    if ($root) {
        $rel = ltrim(str_replace(rtrim($root, '/'), '', $file), '/');
    } else {
        $rel = basename($file);
    }
    return dotted(str_replace(['/', '.php'], ['.', ''], $rel));
}

// ---- shared state -------------------------------------------------------
$SYMBOLS = [];        // id => symbol assoc
$BY_QUAL = [];        // dotted qualname => id
$BY_FUNC = [];        // short func name => [id]
$BY_METHOD = [];      // short method name => [id]
$STEPS = [];
$TOKENS = [];
$N = ['t' => 0, 's' => 0];
$PP = new PrettyPrinter\Standard();

function type_to_string(?Node $t): ?string {
    if ($t === null) return null;
    if ($t instanceof Node\Identifier) return $t->name;
    if ($t instanceof Node\Name) return dotted($t->toString());
    if ($t instanceof Node\NullableType) return '?' . type_to_string($t->type);
    if ($t instanceof Node\UnionType) return implode('|', array_map('type_to_string', $t->types));
    if ($t instanceof Node\IntersectionType) return implode('&', array_map('type_to_string', $t->types));
    return null;
}

function params_of($node): array {
    $out = [];
    $i = 0;
    foreach ($node->params as $p) {
        $name = ($p->var instanceof Node\Expr\Variable && is_string($p->var->name)) ? $p->var->name : '?';
        $out[] = [
            'name' => $name,
            'position' => $i,
            'kind' => $p->variadic ? 'vararg' : 'positional',
            'type' => type_to_string($p->type),
            'has_default' => $p->default !== null,
        ];
        $i++;
    }
    return $out;
}

// ---- pass 1: collect symbols -------------------------------------------
class CollectSymbols extends NodeVisitorAbstract {
    public string $file;
    public string $module;
    private array $classStack = [];
    function __construct(string $file, string $module) { $this->file = $file; $this->module = $module; }

    function enterNode(Node $node) {
        global $SYMBOLS, $BY_QUAL, $BY_FUNC, $BY_METHOD;
        if ($node instanceof Node\Stmt\ClassLike) {
            $fq = isset($node->namespacedName) ? $node->namespacedName->toString()
                : ($node->name ? $node->name->toString() : 'anon');
            $this->classStack[] = $fq;
            return;
        }
        if ($node instanceof Node\Stmt\Function_) {
            $fq = isset($node->namespacedName) ? $node->namespacedName->toString() : $node->name->toString();
            $this->addSym(dotted($fq), 'function', $node, $node->name->toString(), true, $this->nsOf($fq));
        } elseif ($node instanceof Node\Stmt\ClassMethod) {
            $cls = end($this->classStack) ?: '';
            $fq = $cls . '\\' . $node->name->toString();
            $this->addSym(dotted($fq), 'method', $node, $node->name->toString(), false, $this->nsOf($cls));
        }
    }
    function leaveNode(Node $node) {
        if ($node instanceof Node\Stmt\ClassLike) array_pop($this->classStack);
    }
    // The PHP namespace is the module. Fall back to the file when global (no ns).
    private function nsOf(string $fq): string {
        $p = strrpos($fq, '\\');
        $ns = $p === false ? '' : substr($fq, 0, $p);
        return $ns !== '' ? dotted($ns) : $this->module;
    }
    private function addSym(string $qual, string $kind, $node, string $short, bool $isFunc, string $module) {
        global $SYMBOLS, $BY_QUAL, $BY_FUNC, $BY_METHOD;
        $id = 'sym:' . $qual;
        if (isset($SYMBOLS[$id])) return;
        $SYMBOLS[$id] = [
            'id' => $id, 'qualname' => $qual, 'kind' => $kind,
            'file' => $this->file, 'line' => $node->getStartLine(),
            'module' => $module, 'package' => pkg_of($module),
            'params' => params_of($node),
            'returns_type' => type_to_string($node->returnType ?? null),
        ];
        $BY_QUAL[$qual] = $id;
        if ($isFunc) $BY_FUNC[$short][] = $id; else $BY_METHOD[$short][] = $id;
    }
}

// ---- pass 2: collect calls + record access ------------------------------
class CollectCalls extends NodeVisitorAbstract {
    public string $file;
    public string $module;
    private array $fnStack = [];   // enclosing symbol ids
    private ?string $moduleSym = null;
    function __construct(string $file, string $module) { $this->file = $file; $this->module = $module; }

    private function moduleSymbol(): string {
        global $SYMBOLS;
        if ($this->moduleSym) return $this->moduleSym;
        $id = 'sym:' . $this->module . '.<module>';
        if (!isset($SYMBOLS[$id])) {
            $SYMBOLS[$id] = ['id' => $id, 'qualname' => $this->module, 'kind' => 'module',
                'file' => $this->file, 'line' => 0, 'module' => $this->module,
                'package' => pkg_of($this->module), 'params' => []];
        }
        return $this->moduleSym = $id;
    }
    private function external(string $qual): string {
        global $SYMBOLS;
        $id = 'sym:ext:' . $qual;
        if (!isset($SYMBOLS[$id])) {
            $SYMBOLS[$id] = ['id' => $id, 'qualname' => $qual, 'kind' => 'external',
                'file' => null, 'line' => null, 'module' => null, 'package' => null, 'params' => []];
        }
        return $id;
    }
    private function enclosing(): string {
        return end($this->fnStack) ?: $this->moduleSymbol();
    }
    private function recordSymbol(string $base): string {
        global $SYMBOLS;
        $enc = $this->enclosing();
        $id = 'sym:record:' . $enc . '::' . $base;
        if (!isset($SYMBOLS[$id])) {
            $SYMBOLS[$id] = ['id' => $id, 'qualname' => $base, 'kind' => 'record',
                'file' => $this->file, 'line' => null, 'module' => $this->module,
                'package' => pkg_of($this->module),
                'in_function' => $SYMBOLS[$enc]['qualname'] ?? $this->module, 'params' => []];
        }
        return $id;
    }

    function enterNode(Node $node) {
        global $SYMBOLS, $BY_QUAL, $BY_FUNC, $BY_METHOD, $STEPS, $TOKENS, $N, $PP;

        if ($node instanceof Node\Stmt\Function_ || $node instanceof Node\Stmt\ClassMethod) {
            // resolve this declaration's symbol id to push as enclosing
            $cls = '';
            $name = $node->name->toString();
            if ($node instanceof Node\Stmt\Function_) {
                $fq = isset($node->namespacedName) ? $node->namespacedName->toString() : $name;
            } else {
                $fq = ($node->getAttribute('parentClassFq') ?? '') . '\\' . $name;
            }
            $this->fnStack[] = 'sym:' . dotted($fq);
            return;
        }

        // array-dim record access: $arr['key']
        if ($node instanceof Node\Expr\ArrayDimFetch && $node->dim instanceof Node\Scalar\String_) {
            $this->recordAccess($PP->prettyPrintExpr($node->var), $node->dim->value, $node->getStartLine());
            return;
        }

        // calls
        $callee = null; $cq = null;
        if ($node instanceof Node\Expr\FuncCall && $node->name instanceof Node\Name) {
            $res = $node->name->getAttribute('resolvedName');
            $fq = $res ? $res->toString() : $node->name->toString();
            [$callee, $cq] = $this->resolveFunc($fq);
        } elseif ($node instanceof Node\Expr\MethodCall && $node->name instanceof Node\Identifier) {
            [$callee, $cq] = $this->resolveMethod($node->name->toString());
        } elseif ($node instanceof Node\Expr\StaticCall && $node->class instanceof Node\Name && $node->name instanceof Node\Identifier) {
            $res = $node->class->getAttribute('resolvedName');
            $cls = $res ? $res->toString() : $node->class->toString();
            [$callee, $cq] = $this->resolveQual(dotted($cls) . '.' . $node->name->toString());
        } elseif ($node instanceof Node\Expr\New_ && $node->class instanceof Node\Name) {
            $res = $node->class->getAttribute('resolvedName');
            $cls = $res ? $res->toString() : $node->class->toString();
            [$callee, $cq] = $this->resolveQual(dotted($cls) . '.__construct', dotted($cls));
        }
        if ($callee === null) return;

        // args: positional tokens (named args go to arg_style.keyword)
        $pos = 0; $kw = []; $args = [];
        foreach ($node->args as $a) {
            if (!($a instanceof Node\Arg)) continue;
            if ($a->name !== null) { $kw[] = $a->name->toString(); continue; }
            if ($a->unpack) continue;
            $args[] = $this->tokenFor($a->value);
            $pos++;
        }
        $N['s']++;
        $STEPS[] = [
            'id' => 'step:' . $this->module . ':' . $N['s'],
            'callee' => $callee, 'in_symbol' => $this->enclosing(),
            'site_file' => $this->file, 'site_line' => $node->getStartLine(),
            'arg_style' => ['positional' => $pos, 'keyword' => $kw],
            'callee_qualname' => $cq, 'args' => $args,
        ];
    }
    function leaveNode(Node $node) {
        if ($node instanceof Node\Stmt\Function_ || $node instanceof Node\Stmt\ClassMethod) array_pop($this->fnStack);
    }

    private function recordAccess(string $base, string $key, int $line) {
        global $TOKENS, $STEPS, $N;
        $base = substr($base, 0, 40);
        $rid = $this->recordSymbol($base);
        $N['t']++; $tid = 'tok:' . $this->module . ':' . $N['t'];
        $TOKENS[] = ['id' => $tid, 'type' => 'key', 'repr' => substr($key, 0, 120),
            'identity' => null, 'value_hash' => vh(json_encode($key)),
            'is_literal' => true, 'literal_repr' => substr($key, 0, 120), 'key' => $key];
        $N['s']++;
        $STEPS[] = ['id' => 'step:' . $this->module . ':' . $N['s'],
            'callee' => $rid, 'in_symbol' => $this->enclosing(),
            'site_file' => $this->file, 'site_line' => $line,
            'arg_style' => ['positional' => 1, 'keyword' => []],
            'callee_qualname' => $base, 'access' => 'field', 'args' => [$tid]];
    }

    private function tokenFor(Node $expr): string {
        global $TOKENS, $N, $PP;
        $N['t']++; $tid = 'tok:' . $this->module . ':' . $N['t'];
        $short = (new ReflectionClass($expr))->getShortName();
        $isLit = true; $type = 'expr'; $rep = null;
        if ($expr instanceof Node\Scalar\String_) { $type = 'string'; $rep = $expr->value; }
        elseif ($short === 'Int_' || $short === 'LNumber') { $type = 'int'; $rep = (string)$expr->value; }
        elseif ($short === 'Float_' || $short === 'DNumber') { $type = 'float'; $rep = (string)$expr->value; }
        elseif ($expr instanceof Node\Expr\ConstFetch && in_array(strtolower($expr->name->toString()), ['true','false','null'])) {
            $type = 'literal'; $rep = strtolower($expr->name->toString());
        } else { $isLit = false; }
        if ($isLit) {
            $TOKENS[] = ['id' => $tid, 'type' => $type, 'repr' => substr((string)$rep, 0, 120),
                'identity' => null, 'value_hash' => vh((string)$rep),
                'is_literal' => true, 'literal_repr' => substr((string)$rep, 0, 120)];
        } else {
            $src = $PP->prettyPrintExpr($expr);
            $TOKENS[] = ['id' => $tid, 'type' => 'expr', 'repr' => substr($src, 0, 120),
                'identity' => null, 'value_hash' => null, 'is_literal' => false, 'literal_repr' => null];
        }
        return $tid;
    }

    private function resolveFunc(string $fq): array {
        global $BY_QUAL, $BY_FUNC;
        $d = dotted($fq);
        if (isset($BY_QUAL[$d])) return [$BY_QUAL[$d], $d];
        $short = $this->lastSeg($d);
        if (isset($BY_FUNC[$short]) && count($BY_FUNC[$short]) === 1) return [$BY_FUNC[$short][0], $d];
        return [$this->external($short), $short];
    }
    private function resolveMethod(string $short): array {
        global $BY_METHOD, $SYMBOLS;
        if (isset($BY_METHOD[$short]) && count($BY_METHOD[$short]) === 1) {
            $id = $BY_METHOD[$short][0];
            return [$id, $SYMBOLS[$id]['qualname']];
        }
        return [$this->external('.' . $short), $short];
    }
    private function resolveQual(string $qual, ?string $extName = null): array {
        global $BY_QUAL;
        if (isset($BY_QUAL[$qual])) return [$BY_QUAL[$qual], $qual];
        return [$this->external($extName ?? $qual), $qual];
    }
    private function lastSeg(string $d): string {
        $parts = explode('.', $d); return end($parts);
    }
}

// ---- drive --------------------------------------------------------------
$files = collect_files($paths);
$parser = (new ParserFactory())->createForNewestSupportedVersion();
$asts = [];
foreach ($files as $f) {
    try {
        $code = file_get_contents($f);
        $ast = $parser->parse($code);
    } catch (Throwable $e) {
        fwrite(STDERR, "skip $f: " . $e->getMessage() . "\n");
        continue;
    }
    if ($ast === null) continue;
    $module = module_name($f, $moduleRoot);
    // pass 1: NameResolver + symbols (mutates AST to resolved names)
    $tr = new NodeTraverser();
    $tr->addVisitor(new NameResolver(null, ['preserveOriginalNames' => true]));
    $tr->addVisitor(new CollectSymbols($f, $module));
    $ast = $tr->traverse($ast);
    // stamp parent class FQ on methods so CollectCalls can rebuild the enclosing id
    $stamp = new class($module) extends NodeVisitorAbstract {
        private array $cs = [];
        function enterNode(Node $n) {
            if ($n instanceof Node\Stmt\ClassLike) {
                $this->cs[] = isset($n->namespacedName) ? $n->namespacedName->toString() : ($n->name ? $n->name->toString() : 'anon');
            } elseif ($n instanceof Node\Stmt\ClassMethod) {
                $n->setAttribute('parentClassFq', end($this->cs) ?: '');
            }
        }
        function leaveNode(Node $n) { if ($n instanceof Node\Stmt\ClassLike) array_pop($this->cs); }
        function __construct($m){}
    };
    $tr2 = new NodeTraverser(); $tr2->addVisitor($stamp); $ast = $tr2->traverse($ast);
    $asts[] = [$ast, $f, $module];
}
foreach ($asts as [$ast, $f, $module]) {
    $tr = new NodeTraverser();
    $tr->addVisitor(new CollectCalls($f, $module));
    $tr->traverse($ast);
}

$doc = [
    'version' => '1', 'kind' => 'static',
    'entrypoint' => $files ? module_name($files[0], $moduleRoot) : null,
    'symbols' => array_values($SYMBOLS),
    'steps' => $STEPS,
    'tokens' => $TOKENS,
];
echo json_encode($doc, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
