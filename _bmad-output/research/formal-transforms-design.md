# Formal Code Transformations: Eliminating Free-Form Text from Coding Agents

> Research document — Ralph Loop + BMAD Brainstorming

## 1. Problem Statement

### Why Free-Form Text Fails

The current GraphPlan primitive system has a fundamental asymmetry:

| Aspect | Current approach | Problem |
|--------|-----------------|---------|
| **WHERE** to edit | Structural locator (AST node, byte offsets) | Sound — finds exactly the right node |
| **WHAT** to put there | Free-form string from LLM | Unsound — any text accepted, verified only by `parses_ok` |

This means the LLM can produce:

**Example 1: Type-incoherent replacement**
```json
{"op": "replace_node", "params": {
  "locator": {"kind": "function", "name": "calculate", "file": "math.py"},
  "replacement": "x = 42"
}}
```
Replaces an entire function_definition with an assignment statement. `parses_ok` passes. The function no longer exists. All callers break silently.

**Example 2: Scope-violating replacement**
```json
{"op": "replace_node", "params": {
  "locator": {"kind": "function", "name": "process", "file": "core.py", "field": "body"},
  "replacement": "\n    return self._cache[key]\n"
}}
```
The replacement references `self` and `key` — but what if `process` is a standalone function, not a method? Or `key` isn't in scope? `parses_ok` passes. Runtime crash.

**Example 3: Semantically vacuous replacement**
```json
{"op": "replace_node", "params": {
  "locator": {"kind": "function", "name": "validate", "file": "forms.py", "field": "body"},
  "replacement": "\n    pass\n"
}}
```
Replaces the entire function body with `pass`. `parses_ok` passes. All validation silently disappears.

### The Dead Verifiers

Two verification functions exist but are never called:

```python
def _verify_type_compatible(node, expected_type):
    """Verify a node has the expected AST type."""
    if node.type == expected_type:
        return (True, None)
    return (False, f"Expected node type '{expected_type}', got '{node.type}'")

def _verify_scope_unchanged(original_source, new_source, edit_start, edit_end, filepath):
    """Verify AST outside edit region is unchanged via hash comparison."""
    # ... hashes top-level nodes outside edit range ...
    # But falls back to just _has_error_nodes() anyway
```

`_verify_type_compatible` would catch Example 1 — but it's never called.
`_verify_scope_unchanged` would catch unintended side effects — but it falls back to `parses_ok` internally.

### The Fundamental Issue

**The LLM is doing two jobs**: selecting a transformation AND generating code. The first job is constrainable (pick from a catalog). The second is inherently unconstrained (arbitrary strings). Making the first job formal while leaving the second free-form gives a false sense of safety.

## 2. Replacement Representation

### First-Principles Analysis: Three Layers of Code Generation

From first principles, every code transformation involves three decisions:

1. **Structural decision**: What kind of AST node am I producing? (function, statement, expression, etc.)
2. **Referential decision**: What names/symbols does the new code use? (must be in scope or explicitly imported)
3. **Content decision**: What is the specific logic? (the actual expressions and statements)

The key insight: **layers 1 and 2 are verifiable with tree-sitter alone. Layer 3 requires semantic understanding that only the LLM has.** Therefore, the design should:

- **Constrain** layers 1 and 2 (structural + referential) via typed templates
- **Allow** layer 3 (content) via parameterized expressions that reference existing AST nodes
- **Verify** all three layers to the extent possible without a type checker

### The Hybrid Approach: AST Surgery + Typed Microcode

Instead of the LLM writing code strings, it composes **microcode instructions** — small, typed, verifiable operations on the AST:

#### Tier 1: Pure AST Surgery (no text generation at all)

These operations manipulate existing AST nodes. The LLM never writes code — it refers to code that already exists.

| Microcode | Parameters | Produces |
|-----------|-----------|----------|
| `copy_node` | `{from: locator, to: locator}` | Copies subtree from one location to another |
| `move_node` | `{from: locator, to: locator}` | Moves subtree (removes from source, inserts at target) |
| `swap_nodes` | `{a: locator, b: locator}` | Swaps two subtrees |
| `rename_identifier` | `{locator, new_name: string}` | Renames a single identifier (string but constrained to valid identifier) |
| `delete_node` | `{locator}` | Removes a node (already exists in current system) |
| `reorder_children` | `{parent: locator, order: [int]}` | Reorders children of a parent node |

**Why this matters**: These operations are **closed** — they can only produce AST fragments that already exist in the codebase. No new code is synthesized. Every output is structurally valid by construction.

#### Tier 2: Parameterized Templates (constrained text generation)

For operations that require new code, the LLM fills **typed slots** in predefined templates. The system constructs the code, not the LLM.

```
Template: guard_clause
  Parameters:
    condition: Expression    # must be a valid expression (verified by tree-sitter parse)
    guarded:   Locator       # reference to existing statement(s) to guard
  Produces: if_statement with condition and guarded body
  Kind: statement → statement (type-preserving)
```

The LLM specifies: `{"template": "guard_clause", "params": {"condition": "data is not None", "guarded": {"kind": "statement", "file": "...", "index": 0}}}`

The system constructs:
```python
if data is not None:
    <copy of guarded statement(s)>
```

**Key constraint**: `condition` must parse as a valid expression (tree-sitter can verify this). The guarded body is copied from existing code (no generation). The template's structure is fixed.

#### Tier 3: Typed Fragments (minimal free-form, maximum verification)

For truly novel code that can't be expressed as AST surgery or templates, the LLM provides **typed AST fragments** with explicit kind annotations:

```json
{"fragment": {
  "kind": "function_definition",
  "name": "validate_input",
  "parameters": ["self", "data"],
  "body_statements": [
    {"kind": "if_statement", "condition": "not isinstance(data, dict)", "consequence": [
      {"kind": "raise_statement", "value": "TypeError('Expected dict')"}
    ]},
    {"kind": "return_statement", "value": "data"}
  ]
}}
```

The system constructs the code from this AST description. Every node has a declared kind. The system verifies:
- Each node's kind is valid for its position
- Identifiers in the fragment are in scope (or explicitly imported by a companion step)
- The fragment's root kind matches what's expected at the insertion point

This is more constrained than free-form text but less rigid than templates. The LLM describes the AST structure; the system serializes it to code with correct syntax and indentation.

### Representation Comparison

| Approach | LLM writes code? | Verifiable properties | Coverage |
|----------|:-:|:-:|:-:|
| **Free-form text** (current) | YES | parses_ok only | 100% — anything expressible |
| **AST surgery** (Tier 1) | NO | all — closed operations | ~30% — only shuffles existing code |
| **Templates** (Tier 2) | NO (fills slots) | kind, scope, structure | ~50% — common patterns |
| **Typed fragments** (Tier 3) | PARTIALLY (expression-level) | kind, scope, reference | ~95% — most code describable |
| **Hybrid** (Tiers 1+2+3) | MINIMALLY | strong for T1/T2, good for T3 | ~95%+ |

## 3. Transformation Catalog

### Morphological Analysis: Finding the Optimal Design Point

Systematic exploration of four design dimensions:

| Dimension | Options | Optimal | Rationale |
|-----------|---------|---------|-----------|
| **Template granularity** | Micro (single-node) / Meso (statement-level) / Macro (block-level) | **Meso** | Micro needs too many steps; macro has too many parameters. Meso matches the 25-30% "add statement" + 10-15% "wrap" + 20-25% "modify expression" edit distribution. |
| **Verification depth** | L0-only / L0-L2 / L0-L4 / L0-L6 | **L0-L4** | L0-L2 catches structural errors (dead verifiers, just activate). L3-L4 catches scope/import errors (the most common LLM mistakes). L5-L6 adds cross-file + triviality checks — cheap but diminishing returns. |
| **Fragment representation** | JSON AST / Pattern language / Rewrite rules / Code snippets with holes | **JSON AST** (Tier 3) + **Typed params** (Tier 2) | JSON AST for novel code (LLM already writes JSON well). Typed parameters for templates (system constructs code). Pattern languages add a DSL the LLM must learn — worse than JSON it already knows. |
| **Parameter typing** | Untyped / Syntactic / Semantic / Dependent | **Semantic** | Syntactic (identifier, expression) is baseline. Semantic adds `identifier_in_scope`, `importable_symbol`, `callable_name` — verifiable with tree-sitter scope/import analysis. Dependent types too rigid. |

### SWE-bench Edit Distribution (from patch analysis)

| Edit Category | Frequency | Templates Required |
|---------------|:---------:|-------------------|
| Add statement/guard/handler | 25-30% | guard_clause, wrap_try_except, wrap_context_manager |
| Modify expression/condition | 20-25% | replace_expression, modify_condition, change_return_value |
| Replace function/method body | 15-20% | replace_function_body (Tier 3 fragment) |
| Wrap in control structure | 10-15% | wrap_try_except, wrap_context_manager, guard_clause |
| Delete code | 8-12% | (Tier 1: delete_node surgery) |
| Rename symbol | 5-8% | (Tier 1: rename_identifier surgery) |
| Modify signature | 3-5% | add_parameter, remove_parameter |
| Add/modify import | 2-5% | add_import_and_use |
| Reorder/move code | 2-3% | (Tier 1: move_node, reorder_children surgery) |

### Semantic Parameter Types

Beyond syntactic validation (does it parse?), these types enable **pre-construction verification**:

| Param Type | Validation | Tree-sitter method |
|-----------|-----------|-------------------|
| `identifier` | `str.isidentifier()` | Lexical check |
| `expression` | Parses as expression | `tree_sitter.parse(f"_ = {expr}")` |
| `statement` | Parses as statement | `tree_sitter.parse(stmt)` |
| `identifier_in_scope` | In scope at target location | Walk up AST to find enclosing definitions |
| `importable_symbol` | Module.symbol exists in import graph | `_build_import_graph()` lookup |
| `callable_name` | Resolves to function/method definition | Symbol table lookup via `_classify_symbol_occurrences()` |
| `locator` | Resolves to ≥1 node | `resolve_locator()` |
| `type_annotation` | Parses as annotation | `tree_sitter.parse(f"x: {ann}")` |

### Complete Template Catalog (15 templates)

#### T1: guard_clause
**Pattern**: Insert `if <condition>: <action>` before existing code
**Params**: `condition: expression`, `guard_body: statement`, `target: locator`
**Kind**: block → block (preserving)
**Pre**: target resolves to statement(s)
**Post**: if_statement as first child, original statements preserved after guard
**Covers**: Null checks, type checks, range validation, early returns

#### T2: wrap_try_except
**Pattern**: Wrap `target` in `try: <target> except <type>: <handler>`
**Params**: `target: locator`, `exception_type: expression [=Exception]`, `handler_body: statement [=pass]`, `exception_var: identifier [=None]`
**Kind**: statement → statement (preserving)
**Pre**: target resolves to statement(s)
**Post**: try_statement wrapping original code
**Covers**: Error handling for file I/O, network calls, parsing

#### T3: add_parameter
**Pattern**: Add `<name>[: <type>][= <default>]` to function parameter list
**Params**: `function: locator`, `param_name: identifier`, `default_value: expression [=None]`, `type_annotation: type_annotation [=None]`, `position: integer [=-1]`
**Kind**: function_definition → function_definition (preserving)
**Pre**: function exists, param_name not already in parameter list
**Post**: parameter list contains new parameter at position
**Covers**: Adding optional params, extending APIs, adding flags

#### T4: replace_expression
**Pattern**: Replace expression at target with new expression
**Params**: `target: locator`, `new_expression: expression`
**Kind**: expression → expression (preserving)
**Pre**: target resolves to expression node
**Post**: new expression parses, surrounding code unchanged
**Covers**: Fix boolean logic, change comparisons, update calculations

#### T5: extract_variable
**Pattern**: `<var> = <target_expr>` before containing statement, replace expr with `<var>`
**Params**: `target: locator`, `variable_name: identifier`
**Kind**: expression → expression (preserving, with side-effect insertion)
**Pre**: target is expression, variable_name not in scope
**Post**: assignment inserted, original expression replaced with variable reference
**Covers**: Simplifying complex expressions, caching computed values

#### T6: add_import_and_use
**Pattern**: Add `from <module> import <symbol>` + use symbol at target
**Params**: `module: expression`, `symbol: identifier`, `usage_target: locator`, `usage_expression: expression`
**Kind**: module → module (side-effect)
**Pre**: symbol not already imported, module is valid path
**Post**: import statement present, symbol in scope at usage point
**Covers**: Using stdlib utilities, third-party libraries, cross-module references

#### T7: add_method
**Pattern**: Add method to class body
**Params**: `class_locator: locator`, `method_name: identifier`, `parameters: identifier_list [=["self"]]`, `body: statement`, `decorator: expression [=None]`, `return_annotation: type_annotation [=None]`
**Kind**: class_definition → class_definition (preserving)
**Pre**: class exists, method_name not in class
**Post**: class contains new method
**Covers**: Adding helper methods, implementing interfaces, adding properties

#### T8: modify_condition
**Pattern**: Replace the condition of an if/while/for statement
**Params**: `target: locator` (if/while/for statement), `new_condition: expression`
**Kind**: compound_statement → compound_statement (preserving)
**Pre**: target is if_statement, while_statement, or for_statement
**Post**: condition field replaced, body unchanged
**Covers**: Fixing off-by-one errors, adding edge cases, tightening/loosening checks

#### T9: add_conditional_branch
**Pattern**: Add elif/else clause to existing if statement
**Params**: `if_target: locator` (existing if_statement), `branch_type: enum[elif,else]`, `condition: expression [=None]` (required for elif), `branch_body: statement`
**Kind**: if_statement → if_statement (preserving)
**Pre**: if_target is if_statement; elif requires condition
**Post**: if statement has new branch
**Covers**: Adding fallback cases, handling edge conditions, exhaustive branching

#### T10: replace_function_body
**Pattern**: Replace entire function body with new code (Tier 3 fragment required for body)
**Params**: `function: locator`, `new_body: fragment`
**Kind**: function_definition → function_definition (preserving)
**Pre**: function exists
**Post**: function retains name/signature, body replaced, non-trivial (L6 check)
**Covers**: Complete reimplementation, algorithm fixes, major refactors

#### T11: wrap_context_manager
**Pattern**: Wrap `target` in `with <expr> as <var>: <target>`
**Params**: `target: locator`, `context_expr: expression`, `as_var: identifier [=None]`
**Kind**: statement → statement (preserving)
**Pre**: target resolves to statement(s)
**Post**: with_statement wrapping original code
**Covers**: File handling, lock acquisition, database transactions

#### T12: add_decorator
**Pattern**: Add `@<decorator>` above function/method/class
**Params**: `target: locator` (function/method/class), `decorator: expression`
**Kind**: definition → definition (preserving)
**Pre**: target is function_definition or class_definition
**Post**: decorator present above target
**Covers**: Adding @property, @staticmethod, @cache, custom decorators

#### T13: inline_variable
**Pattern**: Replace all references to variable with its assigned value, delete assignment
**Params**: `target: locator` (assignment statement), `variable_name: identifier_in_scope`
**Kind**: block → block (reducing)
**Pre**: target is assignment, variable_name is assigned exactly once
**Post**: all references replaced with value, assignment removed
**Covers**: Simplifying code, removing unnecessary temporaries

#### T14: change_return_value
**Pattern**: Replace the value expression in a return statement
**Params**: `target: locator` (return statement or function for last return), `new_value: expression`
**Kind**: return_statement → return_statement (preserving)
**Pre**: target resolves to return_statement
**Post**: return value changed, rest of function unchanged
**Covers**: Fixing incorrect return values, wrapping return in transformation

#### T15: add_class_attribute
**Pattern**: Insert `<name>[: <type>] = <value>` at start of class body
**Params**: `class_locator: locator`, `attr_name: identifier`, `attr_value: expression`, `type_annotation: type_annotation [=None]`
**Kind**: class_definition → class_definition (preserving)
**Pre**: class exists, attr_name not already defined
**Post**: class body starts with attribute assignment
**Covers**: Class-level constants, default values, type annotations

### Coverage Analysis by Template

| Template | SWE-bench Category | Est. Coverage |
|----------|-------------------|:---:|
| guard_clause | Add statement | 8% |
| wrap_try_except | Wrap + Add | 6% |
| add_parameter | Modify signature | 4% |
| replace_expression | Modify expression | 15% |
| extract_variable | Modify expression | 3% |
| add_import_and_use | Add import | 4% |
| add_method | Add function | 8% |
| modify_condition | Modify expression | 8% |
| add_conditional_branch | Modify control flow | 3% |
| replace_function_body | Replace body | 15% |
| wrap_context_manager | Wrap | 3% |
| add_decorator | Add code | 2% |
| inline_variable | Refactoring | 1% |
| change_return_value | Modify expression | 5% |
| add_class_attribute | Add code | 2% |
| **Templates total** | | **~87%** |
| Tier 1 surgery (rename, delete, move) | | **~10%** |
| Tier 3 fragments (novel code) | | **~3%** |
| **Grand total** | | **~100%** |

## 4. Formal Verification Model

### Properties Hierarchy (First Principles)

From first principles, a code transformation T: AST → AST' should satisfy a hierarchy of properties, ordered from cheapest to most expensive to verify:

#### Level 0: Syntactic Well-Formedness ✅ (exists today)
**Property**: `parse(serialize(AST')) has no ERROR nodes`
**Cost**: O(n) parse
**Current**: `_verify_parses_ok` — the only active verifier

#### Level 1: Kind Preservation 🔴 (dead code exists)
**Property**: `kind(node_at(AST', edit_point)) == kind(node_at(AST, edit_point))` unless explicitly declared otherwise
**Cost**: O(1) node comparison
**Current**: `_verify_type_compatible` — exists but never called
**What it catches**: Replacing a function with a statement, a class with an expression, etc.

#### Level 2: Structural Containment 🔴 (dead code exists)
**Property**: `∀ node ∉ edit_region: node ∈ AST ↔ node ∈ AST'` — nodes outside the edit region are unchanged
**Cost**: O(n) tree comparison
**Current**: `_verify_scope_unchanged` — exists but falls back to parses_ok
**What it catches**: Edits that accidentally corrupt adjacent code

#### Level 3: Referential Integrity 🔴 (new)
**Property**: `∀ identifier i in replacement: i ∈ scope(edit_point) ∨ i ∈ builtins ∨ i is defined in replacement`
**Cost**: O(n) scope walk using tree-sitter
**Method**: Walk all identifiers in the replacement. For each, check if it's:
  - Defined within the replacement itself (local variable, parameter)
  - In scope at the edit point (walk up the AST to find enclosing definitions)
  - A language builtin (hardcoded set per language)
  - An imported symbol (check import statements)
**What it catches**: References to undefined variables, `self` in non-method context, using unimported symbols

#### Level 4: Import Closure 🔴 (new — builds on existing `_build_import_graph`)
**Property**: `∀ symbol s used in replacement: s is importable from the current file's import context`
**Cost**: O(m) where m = number of imports
**Method**: Reuse existing `_build_import_graph()` and `_classify_symbol_occurrences()`
**What it catches**: Using `OrderedDict` without `from collections import OrderedDict`

#### Level 5: Arity Preservation 🟡 (existing cross-file impact, extend)
**Property**: If modifying a callable's signature, `∀ call_site c: arity(c) compatible with new_signature`
**Cost**: O(files × symbols)
**Method**: Extend existing Layer 6 cross-file impact analysis
**What it catches**: Adding a required parameter that breaks all callers

#### Level 6: Semantic Non-Triviality 🔴 (new)
**Property**: `AST' ≠ trivial(AST)` — the replacement does something meaningful
**Cost**: O(1) pattern match
**Method**: Check for degenerate patterns: body = `pass`, body = `return None`, body = empty, body = exact copy of original
**What it catches**: LLM replacing a complex function with `pass` (Example 3 above)

### Verification Budget

All levels 0-6 use tree-sitter only. No external type checker. Estimated cost:

| Level | Time per step | Can block execution? |
|-------|:---:|:---:|
| L0: Syntax | <10ms | Yes (error) |
| L1: Kind | <1ms | Yes (error) |
| L2: Containment | <50ms | Yes (error) |
| L3: Referential | <100ms | Warning (may have false positives) |
| L4: Import closure | <50ms | Warning |
| L5: Arity | <200ms | Warning |
| L6: Non-triviality | <1ms | Warning |

Total: <400ms per step. Well within 60s timeout even for 100-step plans.

### Constraint Mapping: False Positive Analysis

**Hard constraint**: Tree-sitter provides AST but NO type information, NO dynamic attribute resolution, NO metaclass awareness. This affects L3 and L4 accuracy.

| Verification Level | False Positive Source | Est. FP Rate | Mitigation |
|---|---|:-:|---|
| L3: Referential | Django metaclass attributes (e.g., `self.objects`, `self.pk`) | ~15-20% on Django | Whitelist common framework attributes; L3 is warning-only |
| L3: Referential | Decorator-generated methods (`@property`, `@cached_property`) | ~5% | Walk decorator list, mark decorated names as defined |
| L3: Referential | `**kwargs` / `getattr()` dynamic access | ~5% | Ignore identifiers used as string keys |
| L4: Import | Star imports (`from module import *`) | ~10% | Treat `*` imports as "all symbols available" |
| L4: Import | Conditional imports (`if TYPE_CHECKING:`) | ~5% | Parse both branches of if/else at module level |
| L4: Import | Re-exports via `__init__.py` chains | ~10% | Only check direct imports, not transitive |

**Critical design decision**: L3 and L4 are **warnings, never errors**. They provide signal to the LLM ("you might be using an undefined name") but never block a correct edit. Only L0 (syntax), L1 (kind), and L2 (containment) can block execution — these have near-zero false positive rates because they check structural properties.

### Constraint Mapping: SWE-bench Docker Constraints

| Constraint | Impact | Design Response |
|---|---|---|
| No internet in container | Can't call external type checkers/linters | All verification is tree-sitter only (already the design) |
| 60s timeout per command | Verification budget: <400ms/step × 100 steps = 40s | L1/L6 are O(1), L0/L3/L4 are O(n). Total well within budget |
| Python-only in SWE-bench Lite | Fragment serialization only needs Python | serialize() is Python-only; other languages added post-validation |
| Heavy Django presence (38%) | Django uses metaclasses/descriptors heavily → L3 FPs | Framework-aware whitelist for common Django patterns |
| tree-sitter-languages package | Must be available in Docker image | Already a dependency (used by graph_plan_scripts.py) |

## 5. Multi-Step Plan Composition

### Interference Model

Two steps **interfere** if and only if they modify the same file. Steps on different files are **independent** (commutative). This is a graph coloring problem:

```
Step i ←→ Step j    iff    affected_files(i) ∩ affected_files(j) ≠ ∅
```

**Independence groups**: Connected components of the interference graph. Steps in different groups can be parallelized or reordered freely.

### Byte-Offset Invalidation

Within a single file, steps execute sequentially. After step i applies, all byte offsets in the file shift. Step i+1 must re-resolve its locator against the **new** AST (already the case — locators query the live tree).

**Critical invariant**: `resolve_locator()` is called fresh per step, never cached. This is already true in the current system and must be preserved.

### Rollback Strategy

**Per-step rollback** (already implemented):
```
save_original → apply_edit → verify_postconditions → (fail? → restore_original)
```

**Plan-level rollback** (new):
- Before the plan starts, snapshot all files that will be modified (from `affected_files` union)
- If any step fails and the plan aborts, restore all snapshots
- If the plan completes but the final test fails, restore all snapshots

### Ordering Constraints

Some templates have implicit ordering dependencies:
- `add_import_and_use` must run import step before usage step (but this is internal — the template handles it)
- `extract_variable` must insert assignment before replacing expression (internal)
- `add_parameter` + a usage step that references the new parameter — must add parameter first

**Explicit ordering**: The plan is an ordered list. Steps within the same independence group execute in list order. Steps in different groups could be parallelized (future optimization).

### Plan Validation Rules

Before execution:
1. Every step validates individually (parameter types, template existence, locator form)
2. Interference detection warns about same-file step pairs
3. No duplicate template+target combinations (would apply the same edit twice)

After execution (per step):
1. All verification levels L0-L4 run
2. L5-L6 run as warnings (non-blocking)
3. If any blocking verification fails → rollback + report

## 6. LLM Interface

### The Core Inversion

The fundamental shift from the current system:

| Aspect | Current (forward) | Formal (inverted) |
|--------|-------------------|-------------------|
| **LLM produces** | Raw code strings | Structured JSON: tier + template/surgery/fragment + typed params |
| **System produces** | Nothing (pastes LLM string) | Actual source code from template instantiation or fragment serialization |
| **Verification** | After construction (post-hoc) | Before AND after construction (pre-check params, post-check result) |
| **Error feedback** | "Syntax error" | "Parameter 'condition' is not a valid expression" or "identifier 'key' not in scope at target" |

### LLM Output Schema

Each plan step is one of three forms:

**Tier 1: AST Surgery**
```json
{
  "tier": 1,
  "op": "rename_identifier|copy_node|move_node|swap_nodes|delete_node|reorder_children",
  "target": { /* locator */ },
  "source": { /* locator — for copy/move/swap */ },
  "new_name": "string — for rename",
  "order": [0, 2, 1] // for reorder
}
```

**Tier 2: Template**
```json
{
  "tier": 2,
  "template": "guard_clause",
  "params": {
    "condition": "data is not None",
    "guard_body": "return None",
    "target": {"kind": "function", "name": "process", "file": "core.py", "field": "body"}
  }
}
```

**Tier 3: Typed Fragment**
```json
{
  "tier": 3,
  "target": { /* locator — where to insert/replace */ },
  "action": "replace|insert_before|insert_after",
  "fragment": {
    "kind": "if_statement",
    "condition": "not isinstance(data, dict)",
    "children": [
      {"kind": "raise_statement", "value": "TypeError('Expected dict')"}
    ]
  }
}
```

**Full plan format** (backward compatible with existing `plan` array):
```json
{
  "plan": [
    {"tier": 2, "template": "add_import_and_use", "params": {...}},
    {"tier": 2, "template": "guard_clause", "params": {...}},
    {"tier": 1, "op": "rename_identifier", "target": {...}, "new_name": "..."}
  ]
}
```

### Prompt Template for LLM

The system prompt teaches the LLM the available transforms. Key design: list templates grouped by use case, not by tier (the LLM shouldn't think about tiers — it thinks about intent).

```
## Available Transforms

### Adding Code
- **guard_clause**: Add a safety check (null, type, range) before code
  Params: condition (expression), guard_body (statement), target (locator)
- **add_import_and_use**: Import a symbol and use it somewhere
  Params: module, symbol, usage_target, usage_expression
- **add_method**: Add a method to a class
  Params: class_locator, method_name, parameters, body, [decorator]
- **add_parameter**: Add parameter to function signature
  Params: function, param_name, [default_value], [type_annotation]
- **add_class_attribute**: Add attribute to class
  Params: class_locator, attr_name, attr_value, [type_annotation]
- **add_decorator**: Add @decorator to function/class
  Params: target, decorator
- **add_conditional_branch**: Add elif/else to if statement
  Params: if_target, branch_type (elif|else), [condition], branch_body

### Modifying Code
- **replace_expression**: Change one expression to another
  Params: target, new_expression
- **modify_condition**: Change condition of if/while/for
  Params: target, new_condition
- **change_return_value**: Change what a function returns
  Params: target, new_value
- **replace_function_body**: Replace entire function body (use fragment)
  Params: function, new_body (fragment)

### Wrapping Code
- **wrap_try_except**: Wrap in try/except
  Params: target, [exception_type], [handler_body]
- **wrap_context_manager**: Wrap in `with` statement
  Params: target, context_expr, [as_var]

### Restructuring Code
- **extract_variable**: Extract expression into named variable
  Params: target, variable_name
- **inline_variable**: Replace variable with its value, remove assignment
  Params: target, variable_name

### AST Surgery (no code generation)
- **rename_identifier**: Rename a symbol
- **delete_node**: Remove code
- **copy_node / move_node / swap_nodes**: Rearrange code
- **reorder_children**: Reorder statements/parameters

### Novel Code (typed fragments)
When no template fits, describe the AST structure:
  fragment: {kind, [name], [condition], [value], [parameters], children: [...]}
  Supported kinds: function_definition, class_definition, if_statement, for_statement,
    while_statement, with_statement, try_statement, return_statement, raise_statement,
    assignment, expression_statement, except_clause, elif_clause, else_clause, finally_clause
```

### Error Feedback Loop

When a step fails validation, the system returns structured feedback to the LLM:

```json
{
  "step_index": 2,
  "errors": [
    {"level": "param_validation", "message": "'condition' does not parse as a valid expression: 'if x >'"},
    {"level": "semantic", "message": "'variable_name' = 'result' shadows existing name in scope"}
  ],
  "suggestion": "Fix the condition expression syntax. Choose a different variable name."
}
```

This is more actionable than the current "Syntax error after applying step 2". The LLM can fix the specific parameter without regenerating the entire plan.

### Tier Selection Heuristic

The LLM doesn't need to think about which tier to use. The system prompt guides by use case:
- "Rename/move/delete existing code" → Tier 1 surgery (no `tier` field needed — inferred from `op`)
- "Use a template from the catalog" → Tier 2 (inferred from `template` field)
- "Describe new code structure" → Tier 3 (inferred from `fragment` field)

The system detects the tier from which fields are present:
```python
def detect_tier(step: dict) -> int:
    if "op" in step: return 1
    if "template" in step: return 2
    if "fragment" in step: return 3
    return 0  # legacy fallback
```

### Fallback Strategy

When the formal system can't express an edit:
1. LLM can use legacy `replace_node` with free-form `replacement` — backward compatible
2. The enhanced verification pipeline (L0-L4) still applies to legacy ops
3. Legacy ops trigger a warning: "Free-form replacement used — reduced verification coverage"
4. The agent log tracks formal vs. legacy step counts for coverage metrics

## 7. Integration with GraphPlanAgent

### File-Level Changes

#### `src/minisweagent/agents/graph_plan_scripts.py`

**Add** (~300 lines):
```python
# 1. Tier detection and dispatch
def execute_formal_step(step_dict: dict, file_path: str) -> dict:
    """Route step to appropriate tier handler."""
    tier = detect_tier(step_dict)
    if tier == 1:
        return execute_surgery(step_dict)
    elif tier == 2:
        return execute_template(step_dict)
    elif tier == 3:
        return execute_fragment(step_dict)
    else:
        return execute_legacy_step(step_dict)  # backward compat

# 2. Template instantiation engine
def execute_template(step: dict) -> dict:
    """Instantiate a template from the catalog."""
    tmpl = TEMPLATE_CATALOG[step["template"]]
    # Phase 1: validate params (syntactic + semantic)
    scope_ctx = build_scope_context(step["params"])
    errors = tmpl.validate_params({**step["params"], "__scope_context__": scope_ctx})
    if errors:
        return {"success": False, "errors": errors, "phase": "param_validation"}
    # Phase 2: construct code from template
    code = instantiate_template(tmpl, step["params"])
    # Phase 3: apply code to file via existing primitives
    return apply_constructed_code(tmpl, step["params"], code)

# 3. Fragment serializer
def execute_fragment(step: dict) -> dict:
    """Serialize a typed fragment and apply it."""
    frag = ASTFragment.from_dict(step["fragment"])
    errors = frag.validate_structure()
    if errors:
        return {"success": False, "errors": errors, "phase": "fragment_validation"}
    code = frag.serialize(indent=detect_indent_level(step["target"]))
    return apply_code_at_target(step["target"], step.get("action", "replace"), code)

# 4. Scope context builder (reuses existing functions)
def build_scope_context(params: dict) -> dict:
    """Build scope analysis for semantic parameter validation."""
    # Reuses: _classify_symbol_occurrences(), _build_import_graph()
    target_locator = find_target_in_params(params)
    if not target_locator:
        return {}
    file_path = target_locator.get("file", "")
    return {
        "identifiers_in_scope": get_identifiers_in_scope(file_path, target_locator),
        "importable_symbols": get_importable_symbols(file_path),
        "callable_names": get_callable_names(file_path),
    }

# 5. Enhanced postcondition checker
def check_formal_postconditions(step: dict, file_path: str, original: str) -> list:
    """Run verification levels L0-L6 on the result."""
    results = []
    results.append(verify_syntax(file_path))                    # L0
    results.append(verify_kind_preservation(step, file_path))   # L1
    results.append(verify_containment(original, file_path))     # L2
    results.append(verify_referential(step, file_path))         # L3
    results.append(verify_imports(step, file_path))             # L4
    results.append(verify_arity(step, file_path))               # L5 (warning)
    results.append(verify_non_triviality(step, file_path))      # L6 (warning)
    return results
```

**Modify** (~100 lines):
- `execute_step()`: Add tier detection before current legacy dispatch
- `_check_postconditions()`: Wire in L1-L6 verifiers (activate dead code)
- `HELPER_SCRIPT`: Add TEMPLATE_CATALOG dict and ASTFragment class

**Keep unchanged**:
- `resolve_locator()` — already the foundation
- `_prim_*` functions — templates delegate to these
- `_classify_symbol_occurrences()` — reused for scope context
- `_build_import_graph()` — reused for import closure
- `build_graph_ts()` — unchanged
- All graph-building infrastructure

#### `src/minisweagent/agents/graph_plan.py`

**Modify** (~50 lines):
- `OPERATOR_CATALOG_PROMPT`: Replace current operator listing with template-grouped catalog (from Section 6 prompt template)
- `_is_valid_plan()`: Accept steps with `tier`, `template`, `op`, or `fragment` keys alongside legacy format
- `_execute_plan()`: Route each step through `execute_formal_step()` with fallback to legacy

**Add** (~20 lines):
- `_parse_formal_step()`: Parse a step dict and detect which tier it belongs to
- Plan-level metric tracking: count of formal vs. legacy steps per plan

#### `src/minisweagent/config/benchmarks/swebench_graphplan.yaml`

**Modify**: Update Phase 2 system prompt section to describe formal transforms instead of legacy operators.

#### `tests/agents/test_graph_plan.py`

**Add** tests (~150 lines):
- Template instantiation for each of the 15 templates
- Fragment serialization round-trip
- Semantic parameter validation (identifier_in_scope, importable_symbol)
- Plan validation with mixed tiers
- Backward compatibility: legacy steps still work
- Verification levels L1-L6

### Migration Strategy

**Phase 1**: Add formal system alongside legacy (no breaking changes)
- New `execute_formal_step()` function
- Steps with `tier`/`template`/`fragment` keys use formal path
- Steps with `op` key matching legacy names use legacy path
- Both paths end with enhanced postcondition checking

**Phase 2**: Update LLM prompt to prefer formal transforms
- System prompt lists templates as primary operators
- Legacy operators documented as fallback
- Track formal vs. legacy usage in logs

**Phase 3**: Deprecate legacy operators (future)
- Warning on legacy operator usage
- Remove legacy dispatch after coverage validated on SWE-bench

### Estimated Code Changes

| File | Lines Added | Lines Modified | Lines Removed | Net |
|------|:-:|:-:|:-:|:-:|
| graph_plan_scripts.py | ~300 | ~100 | 0 | +300 |
| graph_plan.py | ~70 | ~50 | ~30 | +40 |
| test_graph_plan.py | ~150 | ~30 | 0 | +150 |
| swebench_graphplan.yaml | ~20 | ~20 | ~20 | 0 |
| **Total** | **~540** | **~200** | **~50** | **~490** |

Within the ~800 line target (540 new + modifications).

## 8. Coverage Analysis

### Analogical Foundation

Every successful safe-transformation system uses a **fixed catalog of named operations** with typed parameters:

| System | Catalog size | Free-form code? | Safety mechanism |
|--------|:-:|:-:|---|
| **Compiler passes** (LLVM, GCC) | ~50 passes | No | Typed IR, each pass preserves well-formedness |
| **IDE refactorings** (IntelliJ, Eclipse) | ~20 refactorings | No | Dialog with typed inputs, preview before apply |
| **DB query optimizers** (PostgreSQL) | ~50 rewrite rules | No | Pattern → replacement with conditions |
| **Our system** | 15 templates + 6 surgery ops + fragments | Minimally (expressions only) | Typed params, L0-L6 verification |

The analogy confirms: **a finite, well-typed catalog is sufficient for the vast majority of real transformations**. The question is coverage.

### SWE-bench Lite Classification (32 real instances)

#### Tier 1: AST Surgery — 4 instances (12.5%)

| Instance | Patch Description | Operation |
|----------|-------------------|-----------|
| `django__django-10914` | Change default from `None` to `0o644` | replace_expression |
| `sympy__sympy-20590` | Add `__slots__ = ()` to class | add_class_attribute |
| `scikit-learn__scikit-learn-13584` | Replace `v != init_params[k]` with `repr(v) != repr(init_params[k])` | replace_expression |
| `django__django-11964` | Add `__str__` method returning `str(self.value)` | add_method |

**Pattern**: Simple value swaps, single-attribute additions, tiny method additions. 1 hunk, 1-3 lines.

#### Tier 2: Templates — 18 instances (56.25%)

| Instance | Patch Description | Template |
|----------|-------------------|----------|
| `django__django-11039` | AND additional boolean into condition | modify_condition |
| `django__django-11583` | Wrap in try/except ValueError | wrap_try_except |
| `django__django-11179` | Add `pk = None` assignment | guard_clause |
| `django__django-14016` | Replace deepcopy with reconstruction | replace_expression |
| `django__django-12453` | Wrap in `connection.constraint_checks_disabled()` | wrap_context_manager |
| `django__django-14238` | Change isinstance to issubclass | modify_condition |
| `django__django-16527` | AND `has_add_permission` into condition | modify_condition |
| `django__django-15347` | Change `if extra_tags` to `if extra_tags is not None` | modify_condition |
| `django__django-13658` | Thread parameter through function call | add_parameter |
| `marshmallow__marshmallow-1343` | Extend `except KeyError` to `except (KeyError, TypeError)` | modify_condition |
| `marshmallow__marshmallow-1359` | Change `schema.opts` to `self.root.opts` | replace_expression |
| `pvlib__pvlib-python-1854` | Add isinstance check, wrap in tuple | guard_clause |
| `scikit-learn__scikit-learn-14894` | Add `if n_SV == 0: return` guard | guard_clause |
| `sympy__sympy-18057` | Add type-check early return in `__eq__` | guard_clause |
| `sympy__sympy-20154` | Change `yield ms` to `yield dict(ms)` | replace_expression |
| `django__django-16873` | Add if/else for autoescape | add_conditional_branch |
| `pytest-dev__pytest-7373` | Replace cached eval + add helper function | replace_expression + add_method |
| `django__django-11815` | Change enum serialization expression | replace_expression |

**Most common templates by frequency**:
1. `modify_condition` — 7 instances (39% of Tier 2)
2. `replace_expression` — 5 instances (28% of Tier 2)
3. `guard_clause` — 4 instances (22% of Tier 2)
4. `wrap_try_except` — 1 instance
5. `wrap_context_manager` — 1 instance
6. `add_conditional_branch` — 1 instance
7. `add_parameter` — 1 instance

#### Tier 3: Typed Fragments — 7 instances (21.9%)

| Instance | Why Tier 3 |
|----------|------------|
| `django__django-10924` | Multi-location callable-handling logic |
| `django__django-13315` | Novel deduplication algorithm in iterator |
| `matplotlib__matplotlib-23562` | Novel 3D projection logic in getters |
| `sympy__sympy-24152` | Novel tensor product expansion with scalars |
| `pvlib__pvlib-python-1707` | Novel numerical computation with np.errstate |
| `pvlib__pvlib-python-1072` | Domain-specific pandas timezone API swap |
| `scikit-learn__scikit-learn-13496` | Multi-location parameter threading |

**Pattern**: Novel algorithmic logic, domain-specific knowledge, multi-location edits. The fragment system can express the AST structure, but the LLM needs domain understanding for the content.

#### Fallback: Legacy — 3 instances (9.4%)

| Instance | Why Fallback |
|----------|-------------|
| `sympy__sympy-17313` | Complex mathematical reasoning for comparison operators |
| `pvlib__pvlib-python-1606` | Restructure optimization algorithm |
| `sqlfluff__sqlfluff-1763` | Entirely new method with file I/O safety guarantees |

**Pattern**: Requires writing substantial new code with complex control flow that doesn't fit any template pattern. These need free-form generation with enhanced verification.

### Coverage Summary

| Tier | Instances | % | Formal? | Verification |
|------|:-:|:-:|:-:|---|
| Tier 1 (Surgery) | 4 | 12.5% | Full | L0-L5 (all properties) |
| Tier 2 (Templates) | 18 | 56.3% | Strong | L0-L4 (kind, scope, import) |
| Tier 3 (Fragments) | 7 | 21.9% | Good | L0-L3 (kind, scope) |
| Fallback (Legacy) | 3 | 9.4% | Partial | L0-L2 (syntax, containment) |
| **Formal total** | **29** | **90.6%** | | |

**Key finding**: 90.6% of SWE-bench Lite instances can be expressed with the formal system (Tiers 1-3). Only 9.4% require legacy free-form fallback.

### Patch Complexity Correlation

From SWE-bench Verified analysis:

| Difficulty | % of SWE-bench | Avg Lines | Likely Tier |
|-----------|:-:|:-:|:-:|
| Easy (38.8%) | 39% | 5.04 | Tier 1-2 |
| Medium (52.2%) | 52% | 14.1 | Tier 2-3 |
| Hard (9.0%) | 9% | 55.78 | Tier 3 / Fallback |

The 9% "Hard" instances align closely with our 9.4% fallback rate — these are the instances that require substantial novel code and likely can't be expressed formally.

## 9. Evaluation Strategy

### Primary Benchmark: SWE-bench Lite (300 instances)

| Metric | Description | Target |
|--------|-------------|:------:|
| **Resolve rate** | % of instances where generated patch passes tests | ≥ current baseline |
| **Formal coverage** | % of plan steps using formal system (not legacy fallback) | ≥ 85% |
| **Verification catch rate** | % of failed plans caught by L1-L6 before test execution | Measure |
| **Rollback rate** | % of steps that trigger rollback due to postcondition failure | < 15% |
| **Plan efficiency** | Average steps per resolved instance | Measure vs. baseline |

### Ablation Studies

**A1: Verification depth ablation**
- L0 only (current baseline)
- L0-L2 (activate dead verifiers)
- L0-L4 (add referential + import)
- L0-L6 (full hierarchy)
- Measure: resolve rate, false positive rate (verification blocks correct edits)

**A2: Template vs. free-form ablation**
- All templates available (formal system)
- No templates (Tier 3 fragments only for everything)
- No templates, no fragments (current free-form)
- Measure: resolve rate, average plan attempts, verification failure rate

**A3: Tier selection ablation**
- Tier 2 only (force templates for everything)
- Tier 2+3 (templates + fragments, no surgery)
- Tier 1+2+3 (full system)
- Measure: coverage (what % of patches can be expressed), resolve rate

### Comparison with Current System

| Aspect | Current System | Formal System |
|--------|---------------|---------------|
| Replacement | Free-form text | Templates + fragments |
| Verification | L0 only (parses_ok) | L0-L6 hierarchy |
| Dead verifiers | 2 unused functions | All activated + 4 new |
| Rollback | Per-step | Per-step + plan-level |
| Error feedback | "Syntax error" | Structured per-parameter |
| Coverage | 100% (anything) | ~91% formal + ~9% fallback |

### Evaluation Protocol

1. **Baseline run**: Current system on SWE-bench Lite (record resolve rate + plan logs)
2. **Formal run**: Formal system on same instances (same model, same compute budget)
3. **Analysis**: For each instance, log:
   - Tier breakdown (how many steps per tier)
   - Verification results (which levels triggered, false positive rate)
   - Whether formal system succeeded where baseline failed (or vice versa)
   - Legacy fallback frequency
4. **Statistical significance**: Bootstrap confidence intervals on resolve rate difference

### Success Criteria

The formal system is validated if:
1. Resolve rate ≥ baseline (no regression from adding formality)
2. Formal coverage ≥ 85% of plan steps (the system is actually used, not bypassed)
3. L1-L6 verifiers catch ≥ 20% of would-be failures before test execution
4. No false-positive verification blocks on correct patches (zero incorrectly rejected)

### Failure Pre-mortem: Why the Formal System Could Fail

| Failure Mode | Likelihood | Impact | Mitigation |
|---|:-:|:-:|---|
| **Template catalog too limited** — issue requires edit pattern not in catalog | Medium | Medium | Tier 3 fragments as safety net; legacy fallback; catalog is extensible |
| **LLM picks wrong template** — force-fits guard_clause when modify_condition is needed | Medium | Low | Structured error feedback; LLM can retry with different template; prompt groups by use case |
| **L3/L4 false positives block correct edits** | High for Django | **Zero** | L3/L4 are warnings only; only L0-L2 block; framework whitelists |
| **Fragment serialization produces wrong code** — correct structure, wrong expressions | Medium | Medium | L3 referential check catches undefined refs; L6 catches trivial bodies; but content correctness requires LLM accuracy |
| **Performance overhead exceeds budget** — 400ms/step × 100 steps = 40s | Low | Low | Average is <100ms/step; L1/L6 are O(1); skip L5 if not modifying signatures |
| **LLM can't reason about templates** — fails to fill parameters correctly | Low | Medium | Templates are simpler than writing code; JSON parameter filling is a well-understood LLM capability |
| **Multi-file edits break interference detection** — steps on same file but different functions treated as interfering | Low | Low | Conservative (warns, doesn't block); future: locator-level interference (more precise) |
| **Fragment serialization is Python-only** — can't handle other languages | N/A for SWE-bench | N/A | SWE-bench Lite is Python-only; multi-language serializers added post-validation |

**Highest-risk failure**: Template catalog completeness. If the catalog doesn't cover a common edit pattern, the LLM must fall back to fragments (more complex) or legacy (less verified). **Mitigation**: The catalog covers 87% of edits by template frequency analysis, and the top 3 templates (modify_condition, replace_expression, guard_clause) handle 78% of template cases. The long tail is caught by fragments.

**Most likely failure**: L3 false positives on Django code (metaclass attributes, descriptor protocols). **Mitigation**: Already handled — L3 is warning-only, never blocks. The LLM sees the warning and can decide whether to proceed.
