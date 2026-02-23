# Ralph Loop: Implement Formal Code Transformations

## Objective

Implement the formal code transformations system designed in `_bmad-output/research/formal-transforms-design.md` with prototype code in `_bmad-output/research/formal-transforms-prototype.py`. This replaces free-form text replacements with a three-tier system (AST surgery, parameterized templates, typed fragments) and activates/extends the verification hierarchy from L0-only to L0-L6.

## Research Documents (READ THESE FIRST)

1. **`_bmad-output/research/formal-transforms-design.md`** — Full 9-section design document with problem analysis, three-tier architecture, 15-template catalog, verification hierarchy, LLM interface schema, integration plan, and SWE-bench coverage analysis (90.6% formal coverage)
2. **`_bmad-output/research/formal-transforms-prototype.py`** — Working prototype (~1400 lines) demonstrating all data structures, templates, fragment serialization, plan composition, and verification functions
3. **`_bmad-output/research/formal-transforms-iteration-log.md`** — 5-iteration research log with design decisions and rationale

## Current Codebase (UNDERSTAND THESE)

- **`src/minisweagent/agents/graph_plan_scripts.py`** (~2564 lines) — HELPER_SCRIPT constant containing the execution engine. Contains:
  - `resolve_locator()` — Core AST node finder (KEEP UNCHANGED)
  - `_prim_replace_node()`, `_prim_insert_before()`, etc. — Primitive mutators (KEEP, templates delegate to these)
  - `_verify_type_compatible()` — L1 kind preservation (EXISTS BUT NEVER CALLED — ACTIVATE)
  - `_verify_scope_unchanged()` — L2 containment (EXISTS BUT FALLS BACK TO parses_ok — FIX)
  - `_classify_symbol_occurrences()` — Symbol scope analysis (REUSE for L3/L4)
  - `_build_import_graph()` — Cross-file import analysis (REUSE for L4)
  - `execute_dsl_steps()` — Step execution with variable resolution
  - `expand_composed_operator()` — Template expansion infrastructure
  - `BUILTIN_COMPOSED_OPS` — Existing composed operators (add_method, add_import, add_class_attribute)
- **`src/minisweagent/agents/graph_plan.py`** (~870 lines) — Agent orchestration with:
  - `OPERATOR_CATALOG_PROMPT` — LLM-facing operator documentation (UPDATE with template catalog)
  - `_generate_plan()`, `_verify_plan()`, `_execute_plan()` — Plan lifecycle (MODIFY for tier routing)
- **`src/minisweagent/config/benchmarks/swebench_graphplan.yaml`** (~215 lines) — Benchmark config with system prompt (UPDATE Phase 2 section)
- **`tests/agents/test_graph_plan.py`** (~1904 lines) — Comprehensive tests (ADD formal transform tests)

## Implementation Tasks (Ordered by Priority)

### Task 1: Add Formal Transform Infrastructure to HELPER_SCRIPT

**File**: `src/minisweagent/agents/graph_plan_scripts.py` (inside the HELPER_SCRIPT string)

Add the following to the helper script, after the existing verification functions and before the `execute_dsl_steps()` function:

**1a. Tier detection function:**
```python
def detect_tier(step: dict) -> int:
    SURGERY_OPS = {"rename_identifier", "copy_node", "move_node",
                   "swap_nodes", "delete_node", "reorder_children"}
    if "op" in step and step["op"] in SURGERY_OPS:
        return 1
    if "template" in step:
        return 2
    if "fragment" in step:
        return 3
    return 0  # legacy fallback
```

**1b. Template catalog (TEMPLATE_CATALOG dict):**
Implement the 15 templates from Section 3 of the design doc. Each template is a dict with:
- `name`, `description`, `params` (list of typed param specs), `input_kind`, `output_kind`
- Validation function for typed parameters (identifier, expression, statement, locator checks)

The templates are: guard_clause, wrap_try_except, add_parameter, replace_expression, extract_variable, add_import_and_use, add_method, modify_condition, add_conditional_branch, replace_function_body, wrap_context_manager, add_decorator, inline_variable, change_return_value, add_class_attribute.

Use the prototype's `TransformTemplate`, `TemplateParam`, `ParamKind` as reference, but adapt to work within the embedded script context (no dataclasses — use plain dicts/functions).

**1c. Template instantiation engine:**
```python
def execute_template(step, file_path, language, tree, source_bytes):
    """Instantiate a template, construct code, apply via existing primitives."""
    tmpl = TEMPLATE_CATALOG[step["template"]]
    params = step.get("params", {})
    # 1. Validate params against template type specs
    # 2. Resolve locators in params
    # 3. Construct code from template + params
    # 4. Apply via _prim_replace_node / _prim_insert_before / etc.
    # 5. Return result dict with success/errors
```

Each template constructs code differently:
- `guard_clause` → constructs `if {condition}:\n    {guard_body}` and inserts before target
- `wrap_try_except` → constructs `try:\n    {target_code}\nexcept {exc_type}:\n    {handler}` and replaces target
- `modify_condition` → finds condition child of if/while/for, replaces just the condition text
- `replace_expression` → replaces expression text at target locator
- etc. (see design doc Section 3 for all 15)

**1d. Fragment serializer (ASTFragment):**
Implement `serialize_fragment(fragment_dict, indent=0)` that converts a JSON AST description into Python source code. Support these kinds:
- function_definition, class_definition, if_statement, elif_clause, else_clause
- for_statement, while_statement, with_statement, try_statement
- except_clause, finally_clause, return_statement, raise_statement
- assignment, expression_statement

Use the prototype's `ASTFragment.serialize()` as reference. Also implement `validate_fragment(fragment_dict)` using `FRAGMENT_REQUIRED_PROPERTIES` and `FRAGMENT_ALLOWED_CHILDREN`.

**1e. Fragment execution:**
```python
def execute_fragment(step, file_path, language, tree, source_bytes):
    """Serialize a typed fragment and apply at target location."""
    fragment = step["fragment"]
    errors = validate_fragment(fragment)
    if errors:
        return {"success": False, "errors": errors}
    code = serialize_fragment(fragment, indent=detect_indent(step, tree, source_bytes))
    # Apply via _prim_replace_node or _prim_insert_before/after based on step["action"]
```

**1f. Activate and extend verification hierarchy:**
- **L1 (Kind preservation)**: Wire `_verify_type_compatible()` into `_check_postconditions()`. Call it after every edit, comparing original node kind with replacement node kind.
- **L2 (Containment)**: Fix `_verify_scope_unchanged()` to actually compare node hashes outside edit region instead of falling back to parses_ok.
- **L3 (Referential integrity)**: NEW — walk identifiers in replacement code, check each is in scope at edit point, a builtin, or defined within the replacement.
- **L4 (Import closure)**: NEW — check all symbols used in replacement are importable. Reuse `_build_import_graph()` and `_classify_symbol_occurrences()`.
- **L6 (Non-triviality)**: NEW — check replacement isn't degenerate (`pass`, `return None`, empty body, exact copy of original).
- **L3, L4, L6 are WARNINGS only** (never block execution). L0, L1, L2 are ERRORS (block execution).

**1g. Formal step execution dispatcher:**
```python
def execute_formal_step(step, file_path, language, tree, source_bytes):
    """Route to appropriate tier handler."""
    tier = detect_tier(step)
    if tier == 1:
        return execute_surgery(step, ...)  # delegate to existing _prim_* functions
    elif tier == 2:
        return execute_template(step, ...)
    elif tier == 3:
        return execute_fragment(step, ...)
    else:
        return None  # signal legacy fallback
```

Wire this into the existing `execute_step` command handler. If `execute_formal_step()` returns None, fall through to existing legacy dispatch.

### Task 2: Update LLM-Facing Prompt

**File**: `src/minisweagent/agents/graph_plan.py`

**2a. Update OPERATOR_CATALOG_PROMPT:**
Replace the current operator listing with the template-grouped catalog from design doc Section 6. Group by use case:
- Adding Code: guard_clause, add_import_and_use, add_method, add_parameter, add_class_attribute, add_decorator, add_conditional_branch
- Modifying Code: replace_expression, modify_condition, change_return_value, replace_function_body
- Wrapping Code: wrap_try_except, wrap_context_manager
- Restructuring: extract_variable, inline_variable
- AST Surgery: rename_identifier, delete_node, copy_node, move_node, swap_nodes, reorder_children
- Novel Code (fragments): fragment with kind/properties/children

Keep backward compatibility: document legacy operators as "Legacy (still supported)" section at the end.

**2b. Update plan validation:**
In `_generate_plan()` or `_is_valid_plan()`, accept steps with `template`, `op` (for surgery), or `fragment` keys alongside legacy format. Tier detection via `detect_tier()`.

**2c. Add formal step routing in `_execute_plan()`:**
For each step, try `execute_formal_step()` first. If it returns None (unrecognized tier), fall back to legacy execution.

### Task 3: Update Benchmark Config

**File**: `src/minisweagent/config/benchmarks/swebench_graphplan.yaml`

Update the Phase 2 system prompt section to describe the formal transforms. Mention templates as the preferred approach, fragments for novel code, and legacy as fallback. Keep the overall phase structure (EXPLORE → PLAN → VERIFY → EXECUTE → SUBMIT).

### Task 4: Add Tests

**File**: `tests/agents/test_graph_plan.py`

Add test classes/functions for:

**4a. Template validation tests:**
- Each of the 15 templates validates correct parameters
- Missing required parameters produce errors
- Invalid parameter types produce errors (e.g., non-identifier for IDENTIFIER param)
- Semantic validation with scope context

**4b. Fragment tests:**
- Serialization of each supported kind produces correct Python
- Round-trip: fragment dict → serialize → parse with tree-sitter → no errors
- Validation catches missing required properties
- Validation catches leaf nodes with children

**4c. Tier detection tests:**
- `{"op": "rename_identifier", ...}` → Tier 1
- `{"template": "guard_clause", ...}` → Tier 2
- `{"fragment": {...}, ...}` → Tier 3
- `{"op": "replace_node", ...}` → Tier 0 (legacy)

**4d. Verification hierarchy tests:**
- L1 kind preservation: same kind passes, different kind errors
- L2 containment: unchanged nodes pass, modified nodes error
- L3 referential: in-scope identifiers pass, out-of-scope warns
- L4 import closure: imported symbols pass, unimported warns
- L6 non-triviality: `pass` body warns, real code passes

**4e. Integration tests:**
- Mixed-tier plan (Tier 1 + Tier 2 + Tier 3 steps) validates and routes correctly
- Legacy steps still work alongside formal steps
- Template instantiation produces syntactically valid code (tree-sitter parse check)

### Task 5: Verify Everything Works

- Run `python -m pytest tests/agents/test_graph_plan.py -v` and ensure all tests pass (both new and existing)
- Run the prototype to verify the research code still works: `python _bmad-output/research/formal-transforms-prototype.py`
- Check that the HELPER_SCRIPT string in graph_plan_scripts.py is syntactically valid Python

## Implementation Constraints

1. **All code changes go inside HELPER_SCRIPT string** (graph_plan_scripts.py) for execution engine changes — this script is deployed to Docker containers
2. **Tree-sitter only** — no external type checkers, no language servers, no pip installs during execution
3. **Backward compatible** — existing plans with legacy operators must still work
4. **Python-first** — fragment serialization targets Python; other languages can be added later
5. **Performance budget** — verification must complete in <400ms per step (100-step plan = <40s, well within 60s timeout)
6. **L3/L4/L6 are warnings, never errors** — only L0 (syntax), L1 (kind), L2 (containment) can block execution

## Estimated Scope

| File | Lines Added | Lines Modified |
|------|:-:|:-:|
| graph_plan_scripts.py (HELPER_SCRIPT) | ~300 | ~100 |
| graph_plan.py | ~70 | ~50 |
| test_graph_plan.py | ~150 | ~30 |
| swebench_graphplan.yaml | ~20 | ~20 |
| **Total** | **~540** | **~200** |

## Success Criteria

The implementation is complete when:

1. All 15 templates in TEMPLATE_CATALOG are implemented with parameter validation
2. Fragment serializer produces valid Python for all 13 statement kinds
3. Tier detection correctly routes Tier 1/2/3 steps; legacy falls through
4. Verification levels L1-L6 are active (L1/L2 as errors, L3/L4/L6 as warnings)
5. All existing tests continue to pass (no regressions)
6. New tests cover template validation, fragment serialization, tier detection, and verification hierarchy
7. `pytest tests/agents/test_graph_plan.py` passes with 0 failures

When ALL criteria above are met, output:

<promise>FORMAL TRANSFORMS IMPLEMENTED</promise>
