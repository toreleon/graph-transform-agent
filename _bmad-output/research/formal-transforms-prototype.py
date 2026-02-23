"""
Formal Code Transformations Prototype
=====================================

Self-contained prototype demonstrating:
- Three-tier transformation system (AST surgery, templates, typed fragments)
- Formal verification hierarchy (6 levels beyond parses_ok)
- Template instantiation with parameter validation
- Multi-step plan composition with interference detection

Research prototype — Ralph Loop + BMAD Brainstorming
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ============================================================================
# TIER 0: Verification Property Hierarchy
# ============================================================================

class VerifyLevel(Enum):
    """Verification levels, ordered cheapest to most expensive."""
    SYNTAX = 0          # parses_ok (exists today)
    KIND = 1            # replacement node kind matches expected
    CONTAINMENT = 2     # AST outside edit region unchanged
    REFERENTIAL = 3     # all identifiers in replacement are in scope
    IMPORT_CLOSURE = 4  # all used symbols are importable
    ARITY = 5           # callable signatures compatible with call sites
    NON_TRIVIALITY = 6  # replacement is not degenerate (pass, None, empty)


@dataclass
class VerifyResult:
    """Result of a verification check."""
    level: VerifyLevel
    passed: bool
    message: str = ""
    is_error: bool = False  # True = blocks execution, False = warning only


def verify_kind_preservation(original_kind: str, new_kind: str) -> VerifyResult:
    """Level 1: Does the replacement preserve the AST node kind?"""
    if original_kind == new_kind:
        return VerifyResult(VerifyLevel.KIND, True)
    return VerifyResult(
        VerifyLevel.KIND, False,
        f"Kind changed from '{original_kind}' to '{new_kind}' without explicit declaration",
        is_error=True,
    )


def verify_containment(original_hashes: list, new_hashes: list) -> VerifyResult:
    """Level 2: Are nodes outside the edit region unchanged?"""
    if original_hashes == new_hashes:
        return VerifyResult(VerifyLevel.CONTAINMENT, True)
    return VerifyResult(
        VerifyLevel.CONTAINMENT, False,
        "AST nodes outside edit region were modified",
        is_error=True,
    )


def verify_referential_integrity(
    identifiers_used: set[str],
    identifiers_in_scope: set[str],
    builtins: set[str],
    defined_in_replacement: set[str],
) -> VerifyResult:
    """Level 3: Are all identifiers in the replacement resolvable?"""
    unresolved = identifiers_used - identifiers_in_scope - builtins - defined_in_replacement
    if not unresolved:
        return VerifyResult(VerifyLevel.REFERENTIAL, True)
    return VerifyResult(
        VerifyLevel.REFERENTIAL, False,
        f"Unresolved identifiers: {sorted(unresolved)}",
        is_error=False,  # warning — tree-sitter scope analysis may have false positives
    )


def verify_import_closure(
    symbols_used: set[str],
    imported_symbols: set[str],
    local_definitions: set[str],
    builtins: set[str],
) -> VerifyResult:
    """Level 4: Are all used symbols importable?"""
    unimported = symbols_used - imported_symbols - local_definitions - builtins
    if not unimported:
        return VerifyResult(VerifyLevel.IMPORT_CLOSURE, True)
    return VerifyResult(
        VerifyLevel.IMPORT_CLOSURE, False,
        f"Used but not imported: {sorted(unimported)}",
        is_error=False,
    )


TRIVIAL_BODIES = {"pass", "return None", "return", "...", "raise NotImplementedError"}


def verify_non_triviality(replacement_text: str) -> VerifyResult:
    """Level 6: Is the replacement non-degenerate?"""
    stripped = replacement_text.strip()
    if stripped in TRIVIAL_BODIES:
        return VerifyResult(
            VerifyLevel.NON_TRIVIALITY, False,
            f"Replacement is trivial: '{stripped}'",
            is_error=False,  # warning — might be intentional
        )
    return VerifyResult(VerifyLevel.NON_TRIVIALITY, True)


# ============================================================================
# TIER 1: AST Surgery — No Text Generation
# ============================================================================

class SurgeryOp(Enum):
    """Pure AST surgery operations. No new code generated."""
    COPY_NODE = "copy_node"
    MOVE_NODE = "move_node"
    SWAP_NODES = "swap_nodes"
    RENAME_IDENTIFIER = "rename_identifier"
    DELETE_NODE = "delete_node"
    REORDER_CHILDREN = "reorder_children"


@dataclass
class Locator:
    """Reference to an AST node. Same semantics as current resolve_locator()."""
    kind: str = ""
    name: str | None = None
    file: str = ""
    parent: Locator | None = None
    field: str | None = None
    nth_child: int | None = None
    index: int | None = None
    # S-expression mode
    sexp_query: str | None = None
    sexp_capture: str = "id"

    def to_dict(self) -> dict:
        d: dict[str, Any] = {}
        if self.sexp_query:
            d["type"] = "sexp"
            d["query"] = self.sexp_query
            d["capture"] = self.sexp_capture
        else:
            if self.kind:
                d["kind"] = self.kind
            if self.name is not None:
                d["name"] = self.name
        d["file"] = self.file
        if self.parent:
            d["parent"] = self.parent.to_dict()
        if self.field:
            d["field"] = self.field
        if self.nth_child is not None:
            d["nth_child"] = self.nth_child
        if self.index is not None:
            d["index"] = self.index
        return d


@dataclass
class ASTSurgery:
    """A pure AST surgery instruction — no text generation."""
    op: SurgeryOp
    target: Locator                  # primary target
    source: Locator | None = None    # for copy/move/swap
    new_name: str | None = None      # for rename_identifier
    order: list[int] | None = None   # for reorder_children

    def validate(self) -> list[str]:
        """Validate parameters are consistent with the operation."""
        errors = []
        if self.op in (SurgeryOp.COPY_NODE, SurgeryOp.MOVE_NODE, SurgeryOp.SWAP_NODES):
            if self.source is None:
                errors.append(f"{self.op.value} requires 'source' locator")
        if self.op == SurgeryOp.RENAME_IDENTIFIER:
            if not self.new_name:
                errors.append("rename_identifier requires 'new_name'")
            elif not self.new_name.isidentifier():
                errors.append(f"'{self.new_name}' is not a valid identifier")
        if self.op == SurgeryOp.REORDER_CHILDREN:
            if not self.order:
                errors.append("reorder_children requires 'order' list")
        return errors

    @property
    def verifiable_properties(self) -> list[VerifyLevel]:
        """Which properties can be verified for this operation."""
        # Surgery operations are fully verifiable — they only move existing code
        return [
            VerifyLevel.SYNTAX,
            VerifyLevel.KIND,
            VerifyLevel.CONTAINMENT,
            VerifyLevel.REFERENTIAL,     # references unchanged (just moved)
            VerifyLevel.IMPORT_CLOSURE,  # imports unchanged (just moved)
        ]


# ============================================================================
# TIER 2: Parameterized Templates — Constrained Text Generation
# ============================================================================

class ParamKind(Enum):
    """Types for template parameters. Constrains what the LLM can provide.

    Two levels:
    - Syntactic types (IDENTIFIER, EXPRESSION, STATEMENT) — validated by parsing
    - Semantic types (IDENTIFIER_IN_SCOPE, IMPORTABLE_SYMBOL, CALLABLE_NAME) — validated
      against the live AST scope/import analysis
    """
    # --- Syntactic types (parse-time validation) ---
    IDENTIFIER = "identifier"       # valid identifier string (e.g., variable name)
    EXPRESSION = "expression"       # must parse as expression via tree-sitter
    STATEMENT = "statement"         # must parse as statement
    TYPE_ANNOTATION = "type"        # type annotation string
    STRING_LITERAL = "string"       # string literal value
    INTEGER_LITERAL = "integer"     # integer value
    LOCATOR = "locator"             # reference to existing AST node
    IDENTIFIER_LIST = "id_list"     # list of identifiers (e.g., parameter names)
    FRAGMENT = "fragment"           # Tier 3 typed AST fragment
    ENUM = "enum"                   # one of a fixed set of string values

    # --- Semantic types (scope/import-time validation) ---
    IDENTIFIER_IN_SCOPE = "id_in_scope"       # identifier that exists in scope at target
    IMPORTABLE_SYMBOL = "importable_symbol"    # module.symbol that can be imported
    CALLABLE_NAME = "callable_name"            # resolves to a function/method definition


@dataclass
class TemplateParam:
    """A typed parameter slot in a template."""
    name: str
    kind: ParamKind
    required: bool = True
    default: Any = None
    description: str = ""


@dataclass
class TransformTemplate:
    """A parameterized transformation template.

    The LLM fills in typed slots. The system constructs code from the template.
    The system verifies all parameters against their types.
    """
    name: str
    description: str
    params: list[TemplateParam]
    input_kind: str          # expected AST kind at the target location
    output_kind: str         # produced AST kind (usually same as input)
    preconditions: list[str] # human-readable precondition descriptions
    postconditions: list[str]  # human-readable postcondition descriptions

    def validate_params(self, values: dict[str, Any]) -> list[str]:
        """Validate provided parameter values against type constraints.

        Two-phase validation:
        1. Syntactic: Does the value have the right shape? (cheap, always)
        2. Semantic: Is the identifier in scope? Is the symbol importable? (requires AST context)

        Phase 2 requires a scope_context dict with keys:
          - identifiers_in_scope: set[str]
          - importable_symbols: set[str]
          - callable_names: set[str]
        This is passed via values["__scope_context__"] if present.
        """
        errors = []
        scope_ctx = values.get("__scope_context__", {})

        for param in self.params:
            if param.name not in values:
                if param.required and param.default is None:
                    errors.append(f"Missing required parameter: {param.name}")
                continue

            val = values[param.name]

            # --- Syntactic validation ---
            if param.kind == ParamKind.IDENTIFIER:
                if not isinstance(val, str) or not val.isidentifier():
                    errors.append(f"'{param.name}' must be a valid identifier, got: {val!r}")
            elif param.kind == ParamKind.EXPRESSION:
                if not isinstance(val, str) or not val.strip():
                    errors.append(f"'{param.name}' must be a non-empty expression string")
                # In production: parse with tree-sitter via _syntax_check_content()
            elif param.kind == ParamKind.STATEMENT:
                if not isinstance(val, str) or not val.strip():
                    errors.append(f"'{param.name}' must be a non-empty statement string")
            elif param.kind == ParamKind.INTEGER_LITERAL:
                if not isinstance(val, int):
                    errors.append(f"'{param.name}' must be an integer, got: {type(val).__name__}")
            elif param.kind == ParamKind.LOCATOR:
                if not isinstance(val, (dict, Locator)):
                    errors.append(f"'{param.name}' must be a locator dict or Locator object")
            elif param.kind == ParamKind.IDENTIFIER_LIST:
                if not isinstance(val, list) or not all(
                    isinstance(x, str) and x.isidentifier() for x in val
                ):
                    errors.append(f"'{param.name}' must be a list of valid identifiers")
            elif param.kind == ParamKind.FRAGMENT:
                if not isinstance(val, (dict, ASTFragment)):
                    errors.append(f"'{param.name}' must be an ASTFragment or fragment dict")
            elif param.kind == ParamKind.ENUM:
                allowed = param.description.split("|") if param.description else []
                if val not in allowed:
                    errors.append(f"'{param.name}' must be one of {allowed}, got: {val!r}")

            # --- Semantic validation (requires scope context) ---
            elif param.kind == ParamKind.IDENTIFIER_IN_SCOPE:
                if not isinstance(val, str) or not val.isidentifier():
                    errors.append(f"'{param.name}' must be a valid identifier, got: {val!r}")
                elif scope_ctx and val not in scope_ctx.get("identifiers_in_scope", set()):
                    errors.append(f"'{param.name}': '{val}' not found in scope at target location")
            elif param.kind == ParamKind.IMPORTABLE_SYMBOL:
                if not isinstance(val, str):
                    errors.append(f"'{param.name}' must be a string, got: {type(val).__name__}")
                elif scope_ctx and val not in scope_ctx.get("importable_symbols", set()):
                    errors.append(f"'{param.name}': '{val}' not importable in current context")
            elif param.kind == ParamKind.CALLABLE_NAME:
                if not isinstance(val, str) or not val.isidentifier():
                    errors.append(f"'{param.name}' must be a valid identifier, got: {val!r}")
                elif scope_ctx and val not in scope_ctx.get("callable_names", set()):
                    errors.append(f"'{param.name}': '{val}' does not resolve to a callable")

        return errors

    @property
    def is_kind_preserving(self) -> bool:
        return self.input_kind == self.output_kind


# ============================================================================
# TIER 2: Concrete Template Catalog
# ============================================================================

TEMPLATE_CATALOG: dict[str, TransformTemplate] = {}


def _register(t: TransformTemplate) -> TransformTemplate:
    TEMPLATE_CATALOG[t.name] = t
    return t


# --- Template 1: Guard Clause ---
_register(TransformTemplate(
    name="guard_clause",
    description="Add a guard clause (early return/raise) before existing code",
    params=[
        TemplateParam("condition", ParamKind.EXPRESSION, description="Boolean expression for the guard"),
        TemplateParam("guard_body", ParamKind.STATEMENT, description="Statement to execute if guard triggers (e.g., 'return None', 'raise ValueError()')"),
        TemplateParam("target", ParamKind.LOCATOR, description="Locator for the statement(s) to guard"),
    ],
    input_kind="block",       # operates on a block/body
    output_kind="block",      # produces a block with guard prepended
    preconditions=["target must resolve to at least one statement"],
    postconditions=[
        "result has an if_statement as first child",
        "original statements are preserved after the guard",
        "guard condition parses as valid expression",
    ],
))

# --- Template 2: Wrap Try-Except ---
_register(TransformTemplate(
    name="wrap_try_except",
    description="Wrap statement(s) in a try/except block",
    params=[
        TemplateParam("target", ParamKind.LOCATOR, description="Locator for the statement(s) to wrap"),
        TemplateParam("exception_type", ParamKind.EXPRESSION, default="Exception",
                      description="Exception class to catch"),
        TemplateParam("handler_body", ParamKind.STATEMENT, default="pass",
                      description="Handler body statement"),
    ],
    input_kind="statement",
    output_kind="statement",
    preconditions=["target must resolve to at least one statement"],
    postconditions=[
        "result is a try_statement",
        "original code is inside the try block",
        "except clause catches the specified exception type",
    ],
))

# --- Template 3: Add Parameter ---
_register(TransformTemplate(
    name="add_parameter",
    description="Add a parameter to a function/method signature",
    params=[
        TemplateParam("function", ParamKind.LOCATOR, description="Locator for the function"),
        TemplateParam("param_name", ParamKind.IDENTIFIER, description="Name of the new parameter"),
        TemplateParam("default_value", ParamKind.EXPRESSION, required=False,
                      description="Default value (makes param optional)"),
        TemplateParam("type_annotation", ParamKind.TYPE_ANNOTATION, required=False,
                      description="Type annotation for the parameter"),
    ],
    input_kind="function_definition",
    output_kind="function_definition",
    preconditions=[
        "function must exist",
        "param_name must not already exist in the parameter list",
    ],
    postconditions=[
        "function signature contains new parameter",
        "parameter has specified default if provided",
        "function body is unchanged",
    ],
))

# --- Template 4: Replace Expression ---
_register(TransformTemplate(
    name="replace_expression",
    description="Replace one expression with another (both must parse as expressions)",
    params=[
        TemplateParam("target", ParamKind.LOCATOR, description="Locator for the expression to replace"),
        TemplateParam("new_expression", ParamKind.EXPRESSION, description="Replacement expression"),
    ],
    input_kind="expression",
    output_kind="expression",
    preconditions=["target must resolve to an expression node"],
    postconditions=[
        "new_expression parses as a valid expression",
        "surrounding code is unchanged",
    ],
))

# --- Template 5: Extract Variable ---
_register(TransformTemplate(
    name="extract_variable",
    description="Extract an expression into a named variable, inserted before the containing statement",
    params=[
        TemplateParam("target", ParamKind.LOCATOR, description="Locator for the expression to extract"),
        TemplateParam("variable_name", ParamKind.IDENTIFIER, description="Name for the new variable"),
    ],
    input_kind="expression",
    output_kind="expression",   # the expression is replaced with the variable reference
    preconditions=[
        "target must resolve to an expression node",
        "variable_name must not shadow existing name in scope",
    ],
    postconditions=[
        "assignment statement inserted before containing statement",
        "original expression replaced with variable_name",
        "variable_name is in scope at usage point",
    ],
))

# --- Template 6: Add Import and Use ---
_register(TransformTemplate(
    name="add_import_and_use",
    description="Import a symbol and use it at a specific location",
    params=[
        TemplateParam("module", ParamKind.EXPRESSION, description="Module to import from"),
        TemplateParam("symbol", ParamKind.IDENTIFIER, description="Symbol to import"),
        TemplateParam("usage_target", ParamKind.LOCATOR, description="Where to use the imported symbol"),
        TemplateParam("usage_expression", ParamKind.EXPRESSION,
                      description="Expression using the symbol (must contain symbol name)"),
    ],
    input_kind="expression",
    output_kind="expression",
    preconditions=[
        "module is a valid module path",
        "symbol is not already imported",
        "usage_expression contains symbol name",
    ],
    postconditions=[
        "import statement added to file",
        "symbol is now in scope",
        "usage_expression replaced at target",
    ],
))

# --- Template 7: Add Method to Class ---
_register(TransformTemplate(
    name="add_method",
    description="Add a method to a class with typed signature",
    params=[
        TemplateParam("class_locator", ParamKind.LOCATOR, description="Locator for the class"),
        TemplateParam("method_name", ParamKind.IDENTIFIER, description="Method name"),
        TemplateParam("parameters", ParamKind.IDENTIFIER_LIST, default=["self"],
                      description="Parameter names (first should be 'self' for instance methods)"),
        TemplateParam("body", ParamKind.STATEMENT, description="Method body statements"),
        TemplateParam("decorator", ParamKind.EXPRESSION, required=False,
                      description="Decorator (e.g., 'staticmethod', 'property')"),
    ],
    input_kind="class_definition",
    output_kind="class_definition",
    preconditions=[
        "class must exist",
        "method_name must not already exist in class",
    ],
    postconditions=[
        "class contains new method with specified name",
        "method has correct parameter list",
        "method body matches specification",
    ],
))


# --- Template 8: Modify Condition ---
_register(TransformTemplate(
    name="modify_condition",
    description="Replace the condition of an if/while/for statement",
    params=[
        TemplateParam("target", ParamKind.LOCATOR, description="Locator for the if/while/for statement"),
        TemplateParam("new_condition", ParamKind.EXPRESSION, description="New boolean expression"),
    ],
    input_kind="compound_statement",
    output_kind="compound_statement",
    preconditions=["target must resolve to if_statement, while_statement, or for_statement"],
    postconditions=[
        "condition field replaced with new_condition",
        "statement body is unchanged",
    ],
))

# --- Template 9: Add Conditional Branch ---
_register(TransformTemplate(
    name="add_conditional_branch",
    description="Add elif/else clause to existing if statement",
    params=[
        TemplateParam("if_target", ParamKind.LOCATOR, description="Locator for the if_statement"),
        TemplateParam("branch_type", ParamKind.ENUM, description="elif|else"),
        TemplateParam("condition", ParamKind.EXPRESSION, required=False,
                      description="Condition (required for elif, ignored for else)"),
        TemplateParam("branch_body", ParamKind.STATEMENT, description="Body of the new branch"),
    ],
    input_kind="if_statement",
    output_kind="if_statement",
    preconditions=[
        "if_target must be an if_statement",
        "elif requires condition parameter",
    ],
    postconditions=[
        "if statement has new branch appended",
        "original branches are preserved",
    ],
))

# --- Template 10: Replace Function Body ---
_register(TransformTemplate(
    name="replace_function_body",
    description="Replace entire function body with new code (Tier 3 fragment for body)",
    params=[
        TemplateParam("function", ParamKind.LOCATOR, description="Locator for the function"),
        TemplateParam("new_body", ParamKind.FRAGMENT, description="New body as typed AST fragment"),
    ],
    input_kind="function_definition",
    output_kind="function_definition",
    preconditions=["function must exist"],
    postconditions=[
        "function retains name and signature",
        "body replaced with fragment",
        "new body is non-trivial (L6 check)",
    ],
))

# --- Template 11: Wrap Context Manager ---
_register(TransformTemplate(
    name="wrap_context_manager",
    description="Wrap statement(s) in a `with` context manager",
    params=[
        TemplateParam("target", ParamKind.LOCATOR, description="Locator for the statement(s) to wrap"),
        TemplateParam("context_expr", ParamKind.EXPRESSION, description="Context manager expression"),
        TemplateParam("as_var", ParamKind.IDENTIFIER, required=False,
                      description="Variable to bind (the `as` clause)"),
    ],
    input_kind="statement",
    output_kind="statement",
    preconditions=["target must resolve to at least one statement"],
    postconditions=[
        "result is a with_statement",
        "original code is inside the with block",
    ],
))

# --- Template 12: Add Decorator ---
_register(TransformTemplate(
    name="add_decorator",
    description="Add a decorator above a function/method/class definition",
    params=[
        TemplateParam("target", ParamKind.LOCATOR, description="Locator for the function/method/class"),
        TemplateParam("decorator", ParamKind.EXPRESSION, description="Decorator expression (without @)"),
    ],
    input_kind="definition",
    output_kind="definition",
    preconditions=["target must be a function_definition or class_definition"],
    postconditions=[
        "decorator line present above target",
        "target definition unchanged",
    ],
))

# --- Template 13: Inline Variable ---
_register(TransformTemplate(
    name="inline_variable",
    description="Replace all references to a variable with its assigned value, delete assignment",
    params=[
        TemplateParam("target", ParamKind.LOCATOR, description="Locator for the assignment statement"),
        TemplateParam("variable_name", ParamKind.IDENTIFIER, description="Variable to inline"),
    ],
    input_kind="block",
    output_kind="block",
    preconditions=[
        "target is an assignment statement",
        "variable_name is assigned exactly once in scope",
    ],
    postconditions=[
        "all references to variable_name replaced with assigned value",
        "assignment statement removed",
    ],
))

# --- Template 14: Change Return Value ---
_register(TransformTemplate(
    name="change_return_value",
    description="Replace the value expression in a return statement",
    params=[
        TemplateParam("target", ParamKind.LOCATOR, description="Locator for the return statement"),
        TemplateParam("new_value", ParamKind.EXPRESSION, description="New return value expression"),
    ],
    input_kind="return_statement",
    output_kind="return_statement",
    preconditions=["target must resolve to a return_statement"],
    postconditions=[
        "return value replaced",
        "surrounding code unchanged",
    ],
))

# --- Template 15: Add Class Attribute ---
_register(TransformTemplate(
    name="add_class_attribute",
    description="Insert a class-level attribute assignment at the start of class body",
    params=[
        TemplateParam("class_locator", ParamKind.LOCATOR, description="Locator for the class"),
        TemplateParam("attr_name", ParamKind.IDENTIFIER, description="Attribute name"),
        TemplateParam("attr_value", ParamKind.EXPRESSION, description="Attribute value"),
        TemplateParam("type_annotation", ParamKind.TYPE_ANNOTATION, required=False,
                      description="Type annotation for the attribute"),
    ],
    input_kind="class_definition",
    output_kind="class_definition",
    preconditions=[
        "class must exist",
        "attr_name must not already be defined in class scope",
    ],
    postconditions=[
        "class body starts with attribute assignment",
        "attribute has specified value",
    ],
))


# ============================================================================
# TIER 3: Typed AST Fragments — Minimal Free-Form
# ============================================================================

@dataclass
class ASTFragment:
    """A structured description of code to generate.

    Instead of writing code as a string, the LLM describes the AST structure.
    The system serializes it to code with correct syntax and indentation.

    Serialization rules (Python):
      - function_definition → def {name}({params}): {body}
      - if_statement → if {condition}: {consequence} [elif/else]
      - return_statement → return {value}
      - raise_statement → raise {value}
      - assignment → {target} = {value}
      - expression_statement → {expression}
      - for_statement → for {target} in {iterable}: {body}
      - while_statement → while {condition}: {body}
      - with_statement → with {context} [as {var}]: {body}
      - try_statement → try: {body} except {exc}: {handler}
      - class_definition → class {name}[({bases})]: {body}

    The LLM provides kind + properties (expressions as strings) + children.
    The system serializes to source with correct indentation.
    """
    kind: str                            # AST node kind (function_definition, if_statement, etc.)
    properties: dict[str, Any] = field(default_factory=dict)
    children: list[ASTFragment] = field(default_factory=list)

    # Common properties accessed via convenience fields:
    @property
    def name(self) -> str | None:
        return self.properties.get("name")

    @property
    def condition(self) -> str | None:
        return self.properties.get("condition")

    @property
    def value(self) -> str | None:
        return self.properties.get("value")

    @classmethod
    def from_dict(cls, d: dict) -> ASTFragment:
        """Parse a fragment dict from LLM JSON output.

        The LLM provides a flat dict with 'kind' and other properties.
        Properties like 'name', 'condition', 'value' are extracted.
        'children' is recursively parsed.

        Example input:
          {"kind": "if_statement", "condition": "x > 0",
           "children": [{"kind": "return_statement", "value": "x"}]}
        """
        kind = d.get("kind", "")
        children_raw = d.get("children", [])
        children = [cls.from_dict(c) for c in children_raw]

        # Everything except 'kind' and 'children' goes into properties
        properties = {k: v for k, v in d.items() if k not in ("kind", "children")}

        return cls(kind=kind, properties=properties, children=children)

    def validate_structure(self) -> list[str]:
        """Validate the fragment's structural consistency."""
        errors = []

        # Check kind is non-empty
        if not self.kind:
            errors.append("Fragment kind must be non-empty")

        # Validate kind-specific required properties
        required = FRAGMENT_REQUIRED_PROPERTIES.get(self.kind, [])
        for prop in required:
            if prop not in self.properties:
                errors.append(f"'{self.kind}' requires property '{prop}'")

        # Validate children kinds are valid for parent
        allowed_children = FRAGMENT_ALLOWED_CHILDREN.get(self.kind)
        if allowed_children is not None:
            for i, child in enumerate(self.children):
                if child.kind not in allowed_children:
                    errors.append(
                        f"'{self.kind}' cannot contain child of kind '{child.kind}' "
                        f"(allowed: {allowed_children})"
                    )

        # Recursive validation
        for i, child in enumerate(self.children):
            child_errors = child.validate_structure()
            errors.extend(f"child[{i}]: {e}" for e in child_errors)

        return errors

    def serialize(self, indent: int = 0) -> str:
        """Serialize the fragment to source code (Python).

        This is the core of the system: the LLM describes structure,
        the system produces syntactically correct code.
        """
        pad = "    " * indent
        inner = "    " * (indent + 1)

        if self.kind == "function_definition":
            params = ", ".join(self.properties.get("parameters", []))
            decorator = self.properties.get("decorator")
            ret_type = self.properties.get("return_type")
            sig = f"def {self.name}({params})"
            if ret_type:
                sig += f" -> {ret_type}"
            lines = []
            if decorator:
                lines.append(f"{pad}@{decorator}")
            lines.append(f"{pad}{sig}:")
            if self.children:
                for child in self.children:
                    lines.append(child.serialize(indent + 1))
            else:
                lines.append(f"{inner}pass")
            return "\n".join(lines)

        elif self.kind == "if_statement":
            lines = [f"{pad}if {self.condition}:"]
            for child in self.children:
                lines.append(child.serialize(indent + 1))
            if not self.children:
                lines.append(f"{inner}pass")
            # elif/else handled as separate fragments appended after
            return "\n".join(lines)

        elif self.kind == "elif_clause":
            lines = [f"{pad}elif {self.condition}:"]
            for child in self.children:
                lines.append(child.serialize(indent + 1))
            return "\n".join(lines)

        elif self.kind == "else_clause":
            lines = [f"{pad}else:"]
            for child in self.children:
                lines.append(child.serialize(indent + 1))
            return "\n".join(lines)

        elif self.kind == "return_statement":
            val = self.value or ""
            return f"{pad}return {val}".rstrip()

        elif self.kind == "raise_statement":
            val = self.value or ""
            return f"{pad}raise {val}".rstrip()

        elif self.kind == "assignment":
            target = self.properties.get("target", "_")
            val = self.value or "None"
            type_ann = self.properties.get("type_annotation")
            if type_ann:
                return f"{pad}{target}: {type_ann} = {val}"
            return f"{pad}{target} = {val}"

        elif self.kind == "expression_statement":
            return f"{pad}{self.properties.get('expression', 'pass')}"

        elif self.kind == "for_statement":
            target = self.properties.get("target", "_")
            iterable = self.properties.get("iterable", "[]")
            lines = [f"{pad}for {target} in {iterable}:"]
            for child in self.children:
                lines.append(child.serialize(indent + 1))
            if not self.children:
                lines.append(f"{inner}pass")
            return "\n".join(lines)

        elif self.kind == "while_statement":
            lines = [f"{pad}while {self.condition}:"]
            for child in self.children:
                lines.append(child.serialize(indent + 1))
            if not self.children:
                lines.append(f"{inner}pass")
            return "\n".join(lines)

        elif self.kind == "with_statement":
            ctx = self.properties.get("context", "ctx")
            as_var = self.properties.get("as_var")
            header = f"with {ctx}"
            if as_var:
                header += f" as {as_var}"
            lines = [f"{pad}{header}:"]
            for child in self.children:
                lines.append(child.serialize(indent + 1))
            if not self.children:
                lines.append(f"{inner}pass")
            return "\n".join(lines)

        elif self.kind == "try_statement":
            lines = [f"{pad}try:"]
            # try body = children without except/else/finally kinds
            body_children = [c for c in self.children if c.kind not in
                             ("except_clause", "else_clause", "finally_clause")]
            exc_children = [c for c in self.children if c.kind == "except_clause"]
            else_children = [c for c in self.children if c.kind == "else_clause"]
            fin_children = [c for c in self.children if c.kind == "finally_clause"]
            for child in body_children:
                lines.append(child.serialize(indent + 1))
            if not body_children:
                lines.append(f"{inner}pass")
            for exc in exc_children:
                lines.append(exc.serialize(indent))
            for el in else_children:
                lines.append(el.serialize(indent))
            for fin in fin_children:
                lines.append(fin.serialize(indent))
            return "\n".join(lines)

        elif self.kind == "except_clause":
            exc_type = self.properties.get("exception_type", "Exception")
            exc_var = self.properties.get("exception_var")
            header = f"except {exc_type}"
            if exc_var:
                header += f" as {exc_var}"
            lines = [f"{pad}{header}:"]
            for child in self.children:
                lines.append(child.serialize(indent + 1))
            if not self.children:
                lines.append(f"{inner}pass")
            return "\n".join(lines)

        elif self.kind == "finally_clause":
            lines = [f"{pad}finally:"]
            for child in self.children:
                lines.append(child.serialize(indent + 1))
            if not self.children:
                lines.append(f"{inner}pass")
            return "\n".join(lines)

        elif self.kind == "class_definition":
            bases = ", ".join(self.properties.get("bases", []))
            header = f"class {self.name}"
            if bases:
                header += f"({bases})"
            lines = [f"{pad}{header}:"]
            for child in self.children:
                lines.append(child.serialize(indent + 1))
            if not self.children:
                lines.append(f"{inner}pass")
            return "\n".join(lines)

        else:
            # Fallback: expression or unknown kind
            expr = self.properties.get("expression", self.value or "pass")
            return f"{pad}{expr}"


# Fragment validation rules
FRAGMENT_REQUIRED_PROPERTIES: dict[str, list[str]] = {
    "function_definition": ["name"],
    "class_definition": ["name"],
    "if_statement": ["condition"],
    "elif_clause": ["condition"],
    "while_statement": ["condition"],
    "for_statement": ["target", "iterable"],
    "with_statement": ["context"],
    "assignment": ["target", "value"],
    "return_statement": [],  # bare `return` is valid
    "raise_statement": [],   # bare `raise` is valid
    "except_clause": [],     # bare `except:` is valid
}

# What children kinds are valid inside each parent (None = any allowed)
FRAGMENT_ALLOWED_CHILDREN: dict[str, list[str] | None] = {
    "function_definition": None,   # any statement in body
    "class_definition": None,      # any definition in body
    "if_statement": None,          # any statement in body
    "elif_clause": None,
    "else_clause": None,
    "for_statement": None,
    "while_statement": None,
    "with_statement": None,
    "try_statement": None,
    "except_clause": None,
    "finally_clause": None,
    "return_statement": [],   # leaf — no children
    "raise_statement": [],    # leaf — no children
    "assignment": [],         # leaf
    "expression_statement": [],  # leaf
}


# ============================================================================
# TRANSFORMATION STEP: Unified Representation
# ============================================================================

@dataclass
class TransformStep:
    """A single step in a transformation plan.

    Can be one of three tiers:
    - Tier 1: AST surgery (op field set, no template/fragment)
    - Tier 2: Template instantiation (template field set)
    - Tier 3: Typed fragment insertion (fragment field set)
    """
    # Exactly one of these should be set:
    surgery: ASTSurgery | None = None
    template: str | None = None          # template name from TEMPLATE_CATALOG
    template_params: dict[str, Any] = field(default_factory=dict)
    fragment: ASTFragment | None = None
    fragment_target: Locator | None = None  # where to insert the fragment

    def validate(self) -> list[str]:
        """Validate the step is well-formed."""
        errors = []
        tiers_set = sum([
            self.surgery is not None,
            self.template is not None,
            self.fragment is not None,
        ])
        if tiers_set != 1:
            errors.append(f"Exactly one tier must be set, got {tiers_set}")
            return errors

        if self.surgery:
            errors.extend(self.surgery.validate())

        if self.template:
            if self.template not in TEMPLATE_CATALOG:
                errors.append(f"Unknown template: {self.template}")
            else:
                tmpl = TEMPLATE_CATALOG[self.template]
                errors.extend(tmpl.validate_params(self.template_params))

        if self.fragment:
            errors.extend(self.fragment.validate_structure())
            if self.fragment_target is None:
                errors.append("Fragment requires 'fragment_target' locator")

        return errors

    @property
    def tier(self) -> int:
        if self.surgery:
            return 1
        if self.template:
            return 2
        return 3

    @property
    def affected_files(self) -> set[str]:
        """Files that this step modifies."""
        files = set()
        if self.surgery:
            files.add(self.surgery.target.file)
            if self.surgery.source:
                files.add(self.surgery.source.file)
        if self.template:
            for v in self.template_params.values():
                if isinstance(v, dict) and "file" in v:
                    files.add(v["file"])
                elif isinstance(v, Locator):
                    files.add(v.file)
        if self.fragment_target:
            files.add(self.fragment_target.file)
        return files


# ============================================================================
# PLAN: Ordered Sequence of Steps with Interference Detection
# ============================================================================

@dataclass
class TransformPlan:
    """A verified sequence of transformation steps."""
    steps: list[TransformStep]
    custom_operators: dict[str, Any] = field(default_factory=dict)  # LLM-defined composed ops

    def validate_all(self) -> list[str]:
        """Validate every step in the plan."""
        errors = []
        for i, step in enumerate(self.steps):
            step_errors = step.validate()
            errors.extend(f"Step {i}: {e}" for e in step_errors)
        return errors

    def detect_interference(self) -> list[str]:
        """Detect potential interference between steps.

        Two steps interfere if:
        1. They modify the same file
        2. One modifies a node that the other reads/targets
        """
        warnings = []
        for i, step_a in enumerate(self.steps):
            for j, step_b in enumerate(self.steps):
                if j <= i:
                    continue
                shared_files = step_a.affected_files & step_b.affected_files
                if shared_files:
                    warnings.append(
                        f"Steps {i} and {j} both modify {shared_files} — "
                        f"step {j} may see stale AST references"
                    )
        return warnings

    def classify_independence(self) -> list[set[int]]:
        """Group steps into independent sets (can be parallelized/reordered).

        Steps in the same group touch the same files. Steps in different groups
        are independent (commutative).
        """
        groups: list[set[int]] = []
        assigned: dict[int, int] = {}

        for i, step in enumerate(self.steps):
            files = step.affected_files
            merged_group: set[int] | None = None
            for j in range(i):
                if j in assigned and step.affected_files & self.steps[j].affected_files:
                    if merged_group is None:
                        merged_group = groups[assigned[j]]
                    else:
                        # Merge groups
                        other_group = groups[assigned[j]]
                        if other_group is not merged_group:
                            merged_group |= other_group
                            for idx in other_group:
                                assigned[idx] = groups.index(merged_group)
                            groups.remove(other_group)
            if merged_group is None:
                groups.append({i})
                assigned[i] = len(groups) - 1
            else:
                merged_group.add(i)
                assigned[i] = groups.index(merged_group)

        return groups


# ============================================================================
# LLM OUTPUT SCHEMA: Parsing and Dispatch
# ============================================================================

# Surgery op names for Tier 1 detection
SURGERY_OPS = {"rename_identifier", "copy_node", "move_node",
               "swap_nodes", "delete_node", "reorder_children"}


def detect_tier(step: dict) -> int:
    """Detect which tier a step dict belongs to.

    Inferred from which keys are present — the LLM doesn't need to
    think about tiers, just use the right keys.
    """
    if "op" in step and step["op"] in SURGERY_OPS:
        return 1
    if "template" in step:
        return 2
    if "fragment" in step:
        return 3
    return 0  # legacy fallback


def parse_step(step_dict: dict) -> TransformStep:
    """Parse a JSON step dict from LLM output into a TransformStep.

    This is the core of the LLM interface — it translates the model's
    structured JSON into the internal representation.
    """
    tier = detect_tier(step_dict)

    if tier == 1:
        op_name = step_dict["op"]
        target = step_dict.get("target", {})
        return TransformStep(
            surgery=ASTSurgery(
                op=SurgeryOp(op_name),
                target=Locator(
                    kind=target.get("kind", ""),
                    name=target.get("name"),
                    file=target.get("file", ""),
                ),
                source=_parse_locator(step_dict.get("source")) if "source" in step_dict else None,
                new_name=step_dict.get("new_name"),
                order=step_dict.get("order"),
            )
        )

    elif tier == 2:
        return TransformStep(
            template=step_dict["template"],
            template_params=step_dict.get("params", {}),
        )

    elif tier == 3:
        fragment = ASTFragment.from_dict(step_dict["fragment"])
        target = step_dict.get("target", {})
        return TransformStep(
            fragment=fragment,
            fragment_target=Locator(
                kind=target.get("kind", ""),
                name=target.get("name"),
                file=target.get("file", ""),
            ) if target else None,
        )

    else:
        # Legacy fallback — create a step that will be handled by old dispatch
        return TransformStep(
            template="__legacy__",
            template_params=step_dict,
        )


def _parse_locator(d: dict | None) -> Locator | None:
    """Parse a locator dict into a Locator object."""
    if not d:
        return None
    return Locator(
        kind=d.get("kind", ""),
        name=d.get("name"),
        file=d.get("file", ""),
        field=d.get("field"),
        nth_child=d.get("nth_child"),
        index=d.get("index"),
    )


def parse_plan(plan_json: dict) -> TransformPlan:
    """Parse a full plan JSON from LLM output.

    Accepts two formats:
    1. {"plan": [...steps...]}  — new formal format
    2. [...steps...]            — bare array (backward compatible)
    """
    if isinstance(plan_json, list):
        step_dicts = plan_json
    else:
        step_dicts = plan_json.get("plan", [])

    steps = [parse_step(s) for s in step_dicts]
    return TransformPlan(steps=steps)


def format_validation_errors(step_index: int, errors: list[str]) -> dict:
    """Format validation errors for LLM feedback.

    Structured error feedback allows the LLM to fix specific parameters
    rather than regenerating the entire plan.
    """
    return {
        "step_index": step_index,
        "errors": [{"level": "validation", "message": e} for e in errors],
        "suggestion": f"Fix errors in step {step_index} parameters and resubmit.",
    }


def format_verify_results(step_index: int, results: list[VerifyResult]) -> dict:
    """Format verification results for LLM feedback."""
    failures = [r for r in results if not r.passed]
    if not failures:
        return {"step_index": step_index, "status": "verified"}
    return {
        "step_index": step_index,
        "status": "failed" if any(r.is_error for r in failures) else "warnings",
        "issues": [
            {
                "level": r.level.name,
                "severity": "error" if r.is_error else "warning",
                "message": r.message,
            }
            for r in failures
        ],
    }


# ============================================================================
# DEMO: Show the system in action
# ============================================================================

def demo():
    """Demonstrate the three-tier system."""

    print("=" * 60)
    print("FORMAL CODE TRANSFORMS — PROTOTYPE DEMO")
    print("=" * 60)

    # --- Tier 1: AST Surgery ---
    print("\n--- Tier 1: AST Surgery (no text generation) ---")
    rename = ASTSurgery(
        op=SurgeryOp.RENAME_IDENTIFIER,
        target=Locator(kind="function", name="old_name", file="utils.py"),
        new_name="new_name",
    )
    errors = rename.validate()
    print(f"Rename surgery: {rename.op.value} -> errors: {errors}")
    print(f"  Verifiable properties: {[v.name for v in rename.verifiable_properties]}")

    # --- Tier 2: Template ---
    print("\n--- Tier 2: Parameterized Template ---")
    guard_tmpl = TEMPLATE_CATALOG["guard_clause"]
    params = {
        "condition": "data is not None",
        "guard_body": "return None",
        "target": {"kind": "statement", "file": "core.py", "index": 0},
    }
    errors = guard_tmpl.validate_params(params)
    print(f"Guard clause template: errors: {errors}")
    print(f"  Kind preserving: {guard_tmpl.is_kind_preserving}")

    # --- Tier 2: New templates ---
    print("\n--- Tier 2: New Templates (T8-T15) ---")
    cond_tmpl = TEMPLATE_CATALOG["modify_condition"]
    errors = cond_tmpl.validate_params({
        "target": {"kind": "if_statement", "file": "core.py"},
        "new_condition": "x > 0 and y is not None",
    })
    print(f"modify_condition: errors: {errors}")

    wrap_ctx = TEMPLATE_CATALOG["wrap_context_manager"]
    errors = wrap_ctx.validate_params({
        "target": {"kind": "statement", "file": "io.py"},
        "context_expr": "open(path, 'r')",
        "as_var": "f",
    })
    print(f"wrap_context_manager: errors: {errors}")

    # --- Tier 3: Typed Fragment with Serialization ---
    print("\n--- Tier 3: Fragment Serialization ---")
    frag = ASTFragment(
        kind="function_definition",
        properties={"name": "validate_input", "parameters": ["self", "data"]},
        children=[
            ASTFragment(
                kind="if_statement",
                properties={"condition": "not isinstance(data, dict)"},
                children=[
                    ASTFragment(kind="raise_statement",
                                properties={"value": "TypeError('Expected dict')"})
                ],
            ),
            ASTFragment(
                kind="for_statement",
                properties={"target": "key", "iterable": "data"},
                children=[
                    ASTFragment(
                        kind="if_statement",
                        properties={"condition": "not key.startswith('_')"},
                        children=[
                            ASTFragment(kind="expression_statement",
                                        properties={"expression": "self._validate_key(key)"})
                        ],
                    ),
                ],
            ),
            ASTFragment(kind="return_statement", properties={"value": "data"}),
        ],
    )
    errors = frag.validate_structure()
    print(f"Fragment structure errors: {errors}")
    print(f"\nSerialized code:\n{frag.serialize(indent=1)}")

    # --- Try/except fragment ---
    print("\n--- Try/Except Fragment ---")
    try_frag = ASTFragment(
        kind="try_statement",
        children=[
            ASTFragment(kind="expression_statement",
                        properties={"expression": "result = parse_json(raw_data)"}),
            ASTFragment(
                kind="except_clause",
                properties={"exception_type": "json.JSONDecodeError", "exception_var": "e"},
                children=[
                    ASTFragment(kind="expression_statement",
                                properties={"expression": "logger.warning(f'Parse error: {e}')"}),
                    ASTFragment(kind="assignment",
                                properties={"target": "result", "value": "{}"}),
                ],
            ),
        ],
    )
    print(f"Serialized:\n{try_frag.serialize(indent=0)}")

    # --- Plan Composition ---
    print("\n--- Plan with Interference Detection ---")
    plan = TransformPlan(steps=[
        TransformStep(surgery=ASTSurgery(
            op=SurgeryOp.RENAME_IDENTIFIER,
            target=Locator(kind="function", name="old_func", file="utils.py"),
            new_name="new_func",
        )),
        TransformStep(template="guard_clause", template_params={
            "condition": "data is not None",
            "guard_body": "return None",
            "target": {"kind": "statement", "file": "core.py", "index": 0},
        }),
        TransformStep(template="wrap_try_except", template_params={
            "target": {"kind": "statement", "file": "core.py", "index": 1},
            "exception_type": "ValueError",
            "handler_body": "return default",
        }),
        TransformStep(template="add_decorator", template_params={
            "target": {"kind": "function", "name": "new_func", "file": "utils.py"},
            "decorator": "cache",
        }),
    ])

    errors = plan.validate_all()
    print(f"Plan validation errors: {errors}")
    interference = plan.detect_interference()
    print(f"Interference warnings: {interference}")
    groups = plan.classify_independence()
    print(f"Independent groups: {groups}")

    # --- Verification ---
    print("\n--- Verification Hierarchy ---")
    results = [
        verify_kind_preservation("function_definition", "function_definition"),
        verify_kind_preservation("function_definition", "expression_statement"),
        verify_referential_integrity(
            {"self", "data", "OrderedDict"},
            {"self", "data"},
            {"print", "len", "isinstance"},
            set(),
        ),
        verify_non_triviality("pass"),
        verify_non_triviality("return self._cache.get(key, default)"),
    ]
    for r in results:
        status = "PASS" if r.passed else ("ERROR" if r.is_error else "WARN")
        print(f"  [{status}] L{r.level.value} {r.level.name}: {r.message or 'OK'}")

    print(f"\nTemplate catalog: {len(TEMPLATE_CATALOG)} templates registered")
    for name, tmpl in TEMPLATE_CATALOG.items():
        print(f"  - {name}: {len(tmpl.params)} params, kind: {tmpl.input_kind} -> {tmpl.output_kind}")

    # --- LLM Output Parsing ---
    print("\n--- LLM Output Schema: Parsing ---")
    llm_plan = {
        "plan": [
            {
                "op": "rename_identifier",
                "target": {"kind": "function", "name": "old_func", "file": "utils.py"},
                "new_name": "new_func",
            },
            {
                "template": "guard_clause",
                "params": {
                    "condition": "data is not None",
                    "guard_body": "return None",
                    "target": {"kind": "function", "name": "process", "file": "core.py", "field": "body"},
                },
            },
            {
                "fragment": {
                    "kind": "if_statement",
                    "condition": "not isinstance(data, dict)",
                    "children": [
                        {"kind": "raise_statement", "value": "TypeError('Expected dict')"},
                    ],
                },
                "target": {"kind": "function", "name": "validate", "file": "forms.py", "field": "body"},
            },
        ]
    }

    parsed_plan = parse_plan(llm_plan)
    print(f"Parsed {len(parsed_plan.steps)} steps from LLM JSON")
    for i, step in enumerate(parsed_plan.steps):
        print(f"  Step {i}: Tier {step.tier}, errors: {step.validate()}")

    # Tier detection
    for step_dict in llm_plan["plan"]:
        tier = detect_tier(step_dict)
        print(f"  detect_tier({list(step_dict.keys())[:2]}) = Tier {tier}")

    # Error feedback formatting
    feedback = format_validation_errors(1, ["'condition' is not a valid expression: 'if x >'"])
    print(f"\nError feedback: {feedback}")

    # Verify result formatting
    verify_feedback = format_verify_results(0, [
        VerifyResult(VerifyLevel.KIND, True),
        VerifyResult(VerifyLevel.REFERENTIAL, False, "Unresolved: ['OrderedDict']", is_error=False),
    ])
    print(f"Verify feedback: {verify_feedback}")

    # --- Coverage Analysis ---
    print("\n--- SWE-bench Coverage Analysis (32 instances) ---")
    classification = {
        "tier1": [
            ("django__django-10914", "replace_expression", "Change default None → 0o644"),
            ("sympy__sympy-20590", "add_class_attribute", "Add __slots__ = ()"),
            ("scikit-learn__scikit-learn-13584", "replace_expression", "repr() comparison"),
            ("django__django-11964", "add_method", "Add __str__ method"),
        ],
        "tier2": [
            ("django__django-11039", "modify_condition", "AND additional boolean"),
            ("django__django-11583", "wrap_try_except", "Wrap in try/except ValueError"),
            ("django__django-11179", "guard_clause", "Add pk=None assignment"),
            ("django__django-14016", "replace_expression", "Replace deepcopy"),
            ("django__django-12453", "wrap_context_manager", "Wrap in constraint_checks_disabled()"),
            ("django__django-14238", "modify_condition", "isinstance → issubclass"),
            ("django__django-16527", "modify_condition", "AND has_add_permission"),
            ("django__django-15347", "modify_condition", "extra_tags is not None"),
            ("django__django-13658", "add_parameter", "Thread parameter"),
            ("marshmallow__marshmallow-1343", "modify_condition", "Extend except clause"),
            ("marshmallow__marshmallow-1359", "replace_expression", "schema → self.root"),
            ("pvlib__pvlib-python-1854", "guard_clause", "isinstance check"),
            ("scikit-learn__scikit-learn-14894", "guard_clause", "Early return on n_SV==0"),
            ("sympy__sympy-18057", "guard_clause", "Type-check in __eq__"),
            ("sympy__sympy-20154", "replace_expression", "yield dict(ms)"),
            ("django__django-16873", "add_conditional_branch", "Autoescape branching"),
            ("pytest-dev__pytest-7373", "replace_expression", "Replace cached eval"),
            ("django__django-11815", "replace_expression", "Enum serialization"),
        ],
        "tier3": [
            ("django__django-10924", "fragment", "Callable-handling logic"),
            ("django__django-13315", "fragment", "Deduplication algorithm"),
            ("matplotlib__matplotlib-23562", "fragment", "3D projection in getters"),
            ("sympy__sympy-24152", "fragment", "Tensor product expansion"),
            ("pvlib__pvlib-python-1707", "fragment", "Numerical computation"),
            ("pvlib__pvlib-python-1072", "fragment", "Timezone API swap"),
            ("scikit-learn__scikit-learn-13496", "fragment", "Multi-location param threading"),
        ],
        "fallback": [
            ("sympy__sympy-17313", "legacy", "Complex math reasoning"),
            ("pvlib__pvlib-python-1606", "legacy", "Algorithm restructure"),
            ("sqlfluff__sqlfluff-1763", "legacy", "New method with I/O safety"),
        ],
    }

    total = sum(len(v) for v in classification.values())
    for tier_name, instances in classification.items():
        pct = len(instances) / total * 100
        print(f"  {tier_name}: {len(instances)} instances ({pct:.1f}%)")

    # Template frequency analysis
    template_counts: dict[str, int] = {}
    for inst_id, tmpl, desc in classification["tier2"]:
        template_counts[tmpl] = template_counts.get(tmpl, 0) + 1
    print(f"\n  Most used templates:")
    for tmpl, count in sorted(template_counts.items(), key=lambda x: -x[1]):
        print(f"    {tmpl}: {count} instances")

    formal_pct = (len(classification["tier1"]) + len(classification["tier2"])
                  + len(classification["tier3"])) / total * 100
    print(f"\n  Formal coverage: {formal_pct:.1f}% ({total - len(classification['fallback'])}/{total})")

    # --- Constraint Mapping: Edge Cases ---
    print("\n--- Constraint Mapping: Edge Cases ---")

    # Edge case 1: Django metaclass attribute (L3 false positive)
    fp_result = verify_referential_integrity(
        identifiers_used={"self", "objects", "pk", "DoesNotExist"},  # Django model attrs
        identifiers_in_scope={"self"},  # tree-sitter only sees explicit defs
        builtins={"print", "len", "isinstance", "type", "str", "int"},
        defined_in_replacement=set(),
    )
    print(f"  Django metaclass FP: {fp_result.message}")
    print(f"    Blocks execution? {fp_result.is_error}  (should be False — warning only)")

    # Edge case 2: Star import makes scope analysis unreliable
    fp_import = verify_import_closure(
        symbols_used={"OrderedDict", "defaultdict", "Counter"},
        imported_symbols=set(),  # star import: `from collections import *`
        local_definitions=set(),
        builtins=set(),
    )
    print(f"  Star import FP: {fp_import.message}")
    print(f"    Blocks execution? {fp_import.is_error}  (should be False)")

    # Edge case 3: Fragment with missing required property
    bad_frag = ASTFragment(kind="if_statement", properties={}, children=[])
    errs = bad_frag.validate_structure()
    print(f"  Missing condition: {errs}")

    # Edge case 4: Leaf node with children (structural violation)
    bad_leaf = ASTFragment(kind="return_statement", properties={"value": "42"},
                           children=[ASTFragment(kind="expression_statement")])
    errs = bad_leaf.validate_structure()
    print(f"  Leaf with children: {errs}")

    # Edge case 5: Semantic param validation without scope context
    tmpl = TEMPLATE_CATALOG["inline_variable"]
    errs = tmpl.validate_params({
        "target": {"kind": "assignment", "file": "test.py"},
        "variable_name": "temp_var",
    })
    print(f"  No scope context: errors={errs}  (should be empty — no scope to check)")

    # --- Failure Pre-mortem: Performance Budget ---
    print("\n--- Performance Budget ---")
    import time
    t0 = time.perf_counter()
    for _ in range(1000):
        verify_kind_preservation("function_definition", "function_definition")
        verify_non_triviality("return self._cache.get(key, default)")
        verify_referential_integrity({"a", "b", "c"}, {"a", "b"}, {"print"}, {"c"})
    elapsed = time.perf_counter() - t0
    per_step = elapsed / 1000 * 1000  # ms
    print(f"  1000 verification cycles: {elapsed*1000:.1f}ms total, {per_step:.3f}ms/step")
    print(f"  100-step plan budget: {per_step * 100:.1f}ms (limit: 60000ms)")


if __name__ == "__main__":
    demo()
