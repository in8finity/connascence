module licenses

/*
  License-compatibility model for the `connascence` repo.

  Question: is every dependency's license compatible with the project's MIT
  distribution license, and is the combination distributable?

  Key real-world distinction the model encodes: license obligations trigger on
  REDISTRIBUTION (bundling/vendoring a dependency inside the shipped artifact),
  not on mere runtime use. The connascence skill ships only its own MIT code;
  every dependency is installed separately by the user (RuntimeDep) or invoked
  as an external CLI (BuildTool). So the obligation is voided in practice.

  Maps to: README.md "Dependencies & licenses" table and LICENSE (MIT).
    typescript     Apache-2.0     runtime (npm i)
    nikic/php-parser BSD-3-Clause runtime (composer)
    uopz           PHP-3.01       runtime (pecl)
    prism          MIT            runtime (bundled w/ Ruby)
    analyzer       BSD-3-Clause   runtime (dart pub)
    graphviz dot   EPL-1.0        external tool (optional)

  Gaps (intentional): license *versions* (Apache patent clause, GPLv2 vs v3) and
  EPL file-scope mechanics are abstracted into Category — they don't change the
  bundling verdict for this repo.
*/

abstract sig Bool {}
one sig True, False extends Bool {}

abstract sig Category {}
one sig Permissive, WeakCopyleft, StrongCopyleft extends Category {}

-- How a component relates to the distributed project artifact.
abstract sig Linkage {}
one sig Bundled,     -- vendored / redistributed INSIDE the shipped artifact
        RuntimeDep,  -- installed separately by the user at runtime
        BuildTool    -- external CLI invoked by hand, never linked
  extends Linkage {}

abstract sig License {
  category: one Category,
  requiresAttribution: one Bool   -- must preserve notices when redistributed?
}
one sig MIT, Apache2, BSD3, PHP301, EPL1, GPL3 extends License {}

fact LicenseCatalog {
  MIT.category     = Permissive     and MIT.requiresAttribution     = True
  Apache2.category = Permissive     and Apache2.requiresAttribution = True
  BSD3.category    = Permissive     and BSD3.requiresAttribution    = True
  PHP301.category  = Permissive     and PHP301.requiresAttribution  = True
  EPL1.category    = WeakCopyleft   and EPL1.requiresAttribution    = True
  GPL3.category    = StrongCopyleft and GPL3.requiresAttribution    = True
}

one sig Project { distLicense: one License }

sig Component {
  lic: one License,
  linkage: one Linkage,
  attributed: one Bool,        -- does the project preserve this dep's notices?
  dependsOn: set Component     -- this component's own (transitive) dependencies
}

-- Dependency graphs don't cycle.
fact Acyclic { all c: Component | c not in c.^dependsOn }

-- Bundling a component ships its entire transitive closure, not just its top.
fun shipped[c: Component]: set Component { c + c.^dependsOn }

-- The actual dependency set of the repo (for the concrete audit).
one sig TypeScript, PhpParser, Uopz, Prism, Analyzer, GraphvizDot extends Component {}
fun realDeps: set Component {
  TypeScript + PhpParser + Uopz + Prism + Analyzer + GraphvizDot
}

fact RealProject {
  Project.distLicense = MIT
  TypeScript.lic  = Apache2 and TypeScript.linkage  = RuntimeDep
  PhpParser.lic   = BSD3    and PhpParser.linkage   = RuntimeDep
  Uopz.lic        = PHP301  and Uopz.linkage        = RuntimeDep
  Prism.lic       = MIT     and Prism.linkage       = RuntimeDep
  Analyzer.lic    = BSD3    and Analyzer.linkage    = RuntimeDep
  GraphvizDot.lic = EPL1    and GraphvizDot.linkage = BuildTool
  all c: realDeps | c.attributed = True
  -- connascence treats each direct dep as an opaque runtime install; their own
  -- transitive trees are not redistributed by this repo. Enumerated + verified
  -- permissive (BSD-3-Clause / MIT) in licenses-reconciliation.md.
  no realDeps.dependsOn
}

-- Obligations trigger only when the dependency is redistributed inside the artifact.
pred redistributes[c: Component] { c.linkage = Bundled }

-- Can code under license `a` be bundled into a work distributed under `b`?
--  permissive / weak-copyleft: combinable into anything (weak is file-scoped).
--  strong copyleft: only into a strong-copyleft distribution (it "infects").
pred compatibleInto[a: License, b: License] {
  a.category = Permissive
  or a.category = WeakCopyleft
  or (a.category = StrongCopyleft and b.category = StrongCopyleft)
}

-- A component meets its license obligations toward the project. Bundling it
-- ships its whole transitive closure, so EVERY shipped license must be
-- compatible — not just the top-level dep's.
pred obligationSatisfied[c: Component] {
  redistributes[c] implies {
    all d: shipped[c] | compatibleInto[d.lic, Project.distLicense]
    c.lic.requiresAttribution = True implies c.attributed = True
  }
}

-- ---- safety properties (should hold) -----------------------------------

-- (1) Every dependency the repo actually uses meets its obligations.
assert ActualProjectCompatible { all c: realDeps | obligationSatisfied[c] }
check ActualProjectCompatible for 8

-- (2) Permissive licenses are safe to combine into the MIT distribution — even
--     if we ever vendored them. Covers typescript/php-parser/uopz/prism/analyzer.
assert PermissiveSafeToBundle {
  all c: Component | c.lic.category = Permissive
                       implies compatibleInto[c.lic, Project.distLicense]
}
check PermissiveSafeToBundle for 8

-- (3) Runtime/tool dependencies impose NO redistribution obligation, whatever
--     their license — this is why EPL `dot` and PHP-licensed uopz are fine.
assert RuntimeAndToolImposeNoObligation {
  all c: Component | c.linkage in (RuntimeDep + BuildTool)
                       implies obligationSatisfied[c]
}
check RuntimeAndToolImposeNoObligation for 8

-- ---- gap assertions (EXPECTED to produce counterexamples) --------------
-- These document the hazards the repo avoids by not bundling.

-- (4) Bundling strong copyleft into the permissive MIT distribution is NOT safe.
assert StrongCopyleftSafeToBundle {
  all c: Component | (redistributes[c] and c.lic.category = StrongCopyleft)
                       implies compatibleInto[c.lic, Project.distLicense]
}
check StrongCopyleftSafeToBundle for 8   -- EXPECT counterexample (the hazard)

-- (5) Redistributing a dependency without preserving its notices violates it.
assert NoUnattributedRedistribution {
  all c: Component | (redistributes[c] and c.lic.requiresAttribution = True)
                       implies c.attributed = True
}
check NoUnattributedRedistribution for 8 -- EXPECT counterexample (the hazard)

-- (6) GAP — bundling a dep whose TRANSITIVE closure contains strong copyleft is
--     still incompatible, even if the top-level dep is permissive. Proves you
--     must vet the whole tree, not just the direct dependency.
assert BundledClosureCompatible {
  all c: Component | redistributes[c]
    implies (all d: shipped[c] | compatibleInto[d.lic, Project.distLicense])
}
check BundledClosureCompatible for 8     -- EXPECT counterexample (transitive copyleft)

-- ---- scenarios ---------------------------------------------------------

-- The real repo configuration: nothing is bundled, so nothing triggers.
run ActualConfig { no c: realDeps | redistributes[c] } for 8

-- The hazard is structurally reachable: a bundled GPL dep in a permissive project.
run BundledStrongCopyleftHazard {
  some c: Component | c.linkage = Bundled
                      and c.lic = GPL3
                      and Project.distLicense.category = Permissive
} for 8

-- Bundling a permissive dep with a permissive transitive closure is fine —
-- this is the shape of every real adapter's dependency tree.
run BundledPermissiveClosure {
  some c: Component | redistributes[c] and some c.dependsOn
                      and all d: shipped[c] | d.lic.category in (Permissive + WeakCopyleft)
} for 8
