"""Helper script that runs INSIDE the SWE-bench Docker container.

Contains the HELPER_SCRIPT constant as a Python string. The script
uses tree-sitter-languages for parsing all supported languages
(Python, JS, TS, Java, Go, Rust, Ruby, PHP, C, C++).
"""

HELPER_SCRIPT = r'''#!/usr/bin/env python3
"""GraphPlan helper - runs inside the SWE-bench Docker container.
Uses tree-sitter-languages for multi-language code parsing.

Commands:
    build_graph file1.py file2.py ...
    verify_plan '<plan_json>' '<graph_json>'
    execute_step '<step_json>'
"""
import json
import os
import re
import sys


# ============================================================
# Multi-language support: extension map + tree-sitter detection
# ============================================================

LANG_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
}


def detect_language(filepath):
    """Return language string from file extension, or None if unsupported."""
    _, ext = os.path.splitext(filepath)
    return LANG_MAP.get(ext.lower())


_treesitter_available = None  # lazy cache


def _check_treesitter():
    """Check if tree_sitter_languages is importable. Caches result."""
    global _treesitter_available
    if _treesitter_available is None:
        try:
            import tree_sitter_languages  # noqa: F401
            _treesitter_available = True
        except ImportError:
            _treesitter_available = False
    return _treesitter_available


# ============================================================
# Tree-sitter S-expression queries per language
# ============================================================

LANGUAGE_QUERIES = {
    "python": {
        "symbols": """
            (class_definition name: (identifier) @def) @class_node
            (function_definition name: (identifier) @def) @func_node
        """,
        "imports": """
            (import_statement) @import
            (import_from_statement) @import
        """,
    },
    "javascript": {
        "symbols": """
            (class_declaration name: (identifier) @def) @class_node
            (function_declaration name: (identifier) @def) @func_node
            (method_definition name: (property_identifier) @def) @method_node
            (export_statement declaration: (function_declaration name: (identifier) @def)) @export_func
            (export_statement declaration: (class_declaration name: (identifier) @def)) @export_class
        """,
        "imports": """
            (import_statement) @import
            (call_expression function: (identifier) @func (#eq? @func "require")) @require
        """,
    },
    "typescript": {
        "symbols": """
            (class_declaration name: (type_identifier) @def) @class_node
            (function_declaration name: (identifier) @def) @func_node
            (method_definition name: (property_identifier) @def) @method_node
            (interface_declaration name: (type_identifier) @def) @iface_node
            (enum_declaration name: (identifier) @def) @enum_node
            (type_alias_declaration name: (type_identifier) @def) @type_node
        """,
        "imports": """
            (import_statement) @import
        """,
    },
    "java": {
        "symbols": """
            (class_declaration name: (identifier) @def) @class_node
            (method_declaration name: (identifier) @def) @method_node
            (interface_declaration name: (identifier) @def) @iface_node
            (enum_declaration name: (identifier) @def) @enum_node
            (constructor_declaration name: (identifier) @def) @ctor_node
        """,
        "imports": """
            (import_declaration) @import
        """,
    },
    "go": {
        "symbols": """
            (function_declaration name: (identifier) @def) @func_node
            (method_declaration name: (field_identifier) @def) @method_node
            (type_declaration (type_spec name: (type_identifier) @def)) @type_node
        """,
        "imports": """
            (import_declaration) @import
        """,
    },
    "rust": {
        "symbols": """
            (function_item name: (identifier) @def) @func_node
            (struct_item name: (type_identifier) @def) @struct_node
            (enum_item name: (type_identifier) @def) @enum_node
            (trait_item name: (type_identifier) @def) @trait_node
            (impl_item) @impl_node
        """,
        "imports": """
            (use_declaration) @import
        """,
    },
    "ruby": {
        "symbols": """
            (class name: (constant) @def) @class_node
            (method name: (identifier) @def) @method_node
            (module name: (constant) @def) @module_node
            (singleton_method name: (identifier) @def) @smethod_node
        """,
        "imports": """
            (call method: (identifier) @func (#match? @func "^(require|require_relative|include|extend)$")) @import
        """,
    },
    "php": {
        "symbols": """
            (class_declaration name: (name) @def) @class_node
            (function_definition name: (name) @def) @func_node
            (method_declaration name: (name) @def) @method_node
            (interface_declaration name: (name) @def) @iface_node
            (trait_declaration name: (name) @def) @trait_node
        """,
        "imports": """
            (namespace_use_declaration) @import
        """,
    },
    "c": {
        "symbols": """
            (function_definition declarator: (function_declarator declarator: (identifier) @def)) @func_node
            (struct_specifier name: (type_identifier) @def) @struct_node
            (enum_specifier name: (type_identifier) @def) @enum_node
            (type_definition declarator: (type_identifier) @def) @typedef_node
        """,
        "imports": """
            (preproc_include) @import
        """,
    },
    "cpp": {
        "symbols": """
            (function_definition declarator: (function_declarator declarator: (identifier) @def)) @func_node
            (function_definition declarator: (function_declarator declarator: (qualified_identifier) @def)) @qual_func_node
            (class_specifier name: (type_identifier) @def) @class_node
            (struct_specifier name: (type_identifier) @def) @struct_node
            (enum_specifier name: (type_identifier) @def) @enum_node
            (namespace_definition name: (identifier) @def) @ns_node
        """,
        "imports": """
            (preproc_include) @import
        """,
    },
}

# Line-kind node types per language (tree-sitter node type -> normalized kind)
LINE_KIND_MAP = {
    "python": {
        "if_statement": "if_statement", "for_statement": "for_statement",
        "while_statement": "while_statement", "return_statement": "return_statement",
        "raise_statement": "raise_statement", "try_statement": "try_statement",
    },
    "javascript": {
        "if_statement": "if_statement", "for_statement": "for_statement",
        "for_in_statement": "for_statement", "while_statement": "while_statement",
        "return_statement": "return_statement", "try_statement": "try_statement",
        "throw_statement": "raise_statement",
    },
    "typescript": {
        "if_statement": "if_statement", "for_statement": "for_statement",
        "for_in_statement": "for_statement", "while_statement": "while_statement",
        "return_statement": "return_statement", "try_statement": "try_statement",
        "throw_statement": "raise_statement",
    },
    "java": {
        "if_statement": "if_statement", "for_statement": "for_statement",
        "enhanced_for_statement": "for_statement", "while_statement": "while_statement",
        "return_statement": "return_statement", "try_statement": "try_statement",
        "throw_statement": "raise_statement",
    },
    "go": {
        "if_statement": "if_statement", "for_statement": "for_statement",
        "return_statement": "return_statement",
    },
    "rust": {
        "if_expression": "if_statement", "for_expression": "for_statement",
        "while_expression": "while_statement", "return_expression": "return_statement",
    },
    "ruby": {
        "if": "if_statement", "for": "for_statement",
        "while": "while_statement", "return": "return_statement",
        "begin": "try_statement",
    },
    "php": {
        "if_statement": "if_statement", "for_statement": "for_statement",
        "foreach_statement": "for_statement", "while_statement": "while_statement",
        "return_statement": "return_statement", "try_statement": "try_statement",
        "throw_expression": "raise_statement",
    },
    "c": {
        "if_statement": "if_statement", "for_statement": "for_statement",
        "while_statement": "while_statement", "return_statement": "return_statement",
    },
    "cpp": {
        "if_statement": "if_statement", "for_statement": "for_statement",
        "for_range_loop": "for_statement", "while_statement": "while_statement",
        "return_statement": "return_statement", "try_statement": "try_statement",
        "throw_statement": "raise_statement",
    },
}


# ============================================================
# build_graph: Parse files, extract symbols + imports
# ============================================================

def _node_type_to_kind(node_type, lang):
    """Map a tree-sitter capture tag to a normalized symbol kind."""
    tag = node_type.lower()
    if "class" in tag or "struct" in tag or "trait" in tag or "iface" in tag:
        return "class"
    if "enum" in tag or "type" in tag or "ns" in tag or "module" in tag:
        return "type"
    # everything else (func, method, ctor, impl, smethod) -> function
    return "function"


def _get_captures_list(captures, tag):
    """Handle tree-sitter API version differences for query.captures().

    Newer versions return dict[str, list[Node]], older return list[(Node, str)].
    """
    if isinstance(captures, dict):
        return captures.get(tag, [])
    return [n for n, name in captures if name == tag]


def _extract_symbols_from_captures(captures, fp, lang, result):
    """Extract symbols from tree-sitter query captures into result."""
    defs = _get_captures_list(captures, "def")
    # Build a map from def node -> parent capture tag for kind detection
    # We need to inspect captures to find the tag associated with each def
    parent_tags = {}
    if isinstance(captures, dict):
        for tag, nodes in captures.items():
            if tag == "def":
                continue
            for node in nodes:
                # Associate parent tag with def nodes that are children
                for def_node in defs:
                    if (def_node.start_point[0] >= node.start_point[0]
                            and def_node.end_point[0] <= node.end_point[0]):
                        parent_tags[id(def_node)] = tag
    else:
        # List of (node, tag) tuples
        non_def = [(n, t) for n, t in captures if t != "def"]
        for def_node in defs:
            for parent_node, parent_tag in non_def:
                if (def_node.start_point[0] >= parent_node.start_point[0]
                        and def_node.end_point[0] <= parent_node.end_point[0]):
                    parent_tags[id(def_node)] = parent_tag
                    break

    for def_node in defs:
        name = def_node.text.decode("utf-8") if isinstance(def_node.text, bytes) else def_node.text
        tag = parent_tags.get(id(def_node), "func_node")
        kind = _node_type_to_kind(tag, lang)
        # Find the parent node for line range
        parent_node = def_node
        if isinstance(captures, dict):
            for t, nodes in captures.items():
                if t == "def":
                    continue
                for n in nodes:
                    if (def_node.start_point[0] >= n.start_point[0]
                            and def_node.end_point[0] <= n.end_point[0]):
                        parent_node = n
                        break
        else:
            for n, t in captures:
                if t != "def" and (def_node.start_point[0] >= n.start_point[0]
                        and def_node.end_point[0] <= n.end_point[0]):
                    parent_node = n
                    break

        result["symbols"].append({
            "name": name,
            "kind": kind,
            "file": fp,
            "start_line": parent_node.start_point[0] + 1,  # tree-sitter is 0-indexed
            "end_line": parent_node.end_point[0] + 1,
        })


def _parse_import_text(text, lang):
    """Parse import node text into (module, symbol) tuple."""
    text = text.strip()
    if lang == "python":
        # from os.path import join  or  import os
        if text.startswith("from "):
            m = re.match(r'from\s+(\S+)\s+import\s+(.+)', text)
            if m:
                return (m.group(1), m.group(2).strip())
        elif text.startswith("import "):
            m = re.match(r'import\s+(\S+)', text)
            if m:
                return (m.group(1), None)
        return (text, None)
    elif lang in ("javascript", "typescript"):
        # import X from 'module' or require('module')
        m = re.search(r"""(?:from\s+['"](.+?)['"]|require\s*\(\s*['"](.+?)['"]\s*\))""", text)
        module = m.group(1) or m.group(2) if m else text
        return (module, None)
    elif lang == "java":
        # import com.example.Foo;
        m = re.match(r'import\s+(?:static\s+)?(.+?)\s*;', text)
        if m:
            parts = m.group(1).rsplit(".", 1)
            if len(parts) == 2:
                return (parts[0], parts[1])
            return (parts[0], None)
        return (text, None)
    elif lang == "go":
        # import "fmt" or import ( "fmt" )
        m = re.search(r'"(.+?)"', text)
        return (m.group(1), None) if m else (text, None)
    elif lang == "rust":
        # use std::io::Read;
        m = re.match(r'use\s+(.+?)\s*;', text)
        if m:
            path = m.group(1)
            parts = path.rsplit("::", 1)
            if len(parts) == 2:
                return (parts[0], parts[1])
            return (path, None)
        return (text, None)
    elif lang == "ruby":
        # require 'foo' or require_relative 'foo'
        m = re.search(r"""(?:require(?:_relative)?)\s+['"](.+?)['"]""", text)
        return (m.group(1), None) if m else (text, None)
    elif lang == "php":
        # use Foo\Bar\Baz;
        m = re.match(r'use\s+(.+?)\s*;', text)
        if m:
            path = m.group(1)
            parts = path.rsplit("\\", 1)
            if len(parts) == 2:
                return (parts[0], parts[1])
            return (path, None)
        return (text, None)
    elif lang in ("c", "cpp"):
        # #include <foo.h> or #include "foo.h"
        m = re.search(r'#include\s*[<"](.+?)[>"]', text)
        return (m.group(1), None) if m else (text, None)
    return (text, None)


def _extract_imports_from_captures(captures, fp, lang, result):
    """Extract imports from tree-sitter query captures into result."""
    imports = _get_captures_list(captures, "import")
    for node in imports:
        text = node.text.decode("utf-8") if isinstance(node.text, bytes) else node.text
        module, symbol = _parse_import_text(text, lang)
        result["imports"].append({
            "file": fp,
            "module": module,
            "symbol": symbol,
            "line": node.start_point[0] + 1,
        })


def _walk_for_line_kinds(node, kind_map, file_line_kinds):
    """Recursively walk tree-sitter tree to collect line-level constructs."""
    node_type = node.type
    if node_type in kind_map:
        file_line_kinds[str(node.start_point[0] + 1)] = kind_map[node_type]
    for child in node.children:
        _walk_for_line_kinds(child, kind_map, file_line_kinds)


def build_graph_ts(file_paths):
    """Parse files with tree-sitter, build graph."""
    import tree_sitter_languages

    result = {"symbols": [], "imports": [], "line_kinds": {}, "errors": []}

    for fp in file_paths:
        lang = detect_language(fp)
        if lang is None:
            result["errors"].append(f"Unsupported file type: {fp}")
            continue

        try:
            source = open(fp, "rb").read()
        except (FileNotFoundError, PermissionError) as e:
            result["errors"].append(f"Cannot read {fp}: {e}")
            continue

        try:
            parser = tree_sitter_languages.get_parser(lang)
        except Exception as e:
            result["errors"].append(f"Cannot get parser for {lang} ({fp}): {e}")
            continue

        try:
            tree = parser.parse(source)
        except Exception as e:
            result["errors"].append(f"Parse failed for {fp}: {e}")
            continue

        root = tree.root_node

        queries = LANGUAGE_QUERIES.get(lang)
        if queries:
            try:
                ts_lang = tree_sitter_languages.get_language(lang)
            except Exception as e:
                result["errors"].append(f"Cannot get language {lang}: {e}")
                continue

            # Extract symbols
            if queries.get("symbols"):
                try:
                    query = ts_lang.query(queries["symbols"])
                    captures = query.captures(root)
                    _extract_symbols_from_captures(captures, fp, lang, result)
                except Exception as e:
                    result["errors"].append(f"Symbol query failed for {fp} ({lang}): {e}")

            # Extract imports
            if queries.get("imports"):
                try:
                    query = ts_lang.query(queries["imports"])
                    captures = query.captures(root)
                    _extract_imports_from_captures(captures, fp, lang, result)
                except Exception as e:
                    result["errors"].append(f"Import query failed for {fp} ({lang}): {e}")

        # Line kinds
        kind_map = LINE_KIND_MAP.get(lang, {})
        if kind_map:
            file_line_kinds = {}
            try:
                _walk_for_line_kinds(root, kind_map, file_line_kinds)
            except Exception as e:
                result["errors"].append(f"Line kinds walk failed for {fp}: {e}")
            if file_line_kinds:
                result["line_kinds"][fp] = file_line_kinds

    print(json.dumps(result))


def build_graph(file_paths):
    """Parse files and extract symbols + imports + line info.

    Uses tree-sitter for all supported languages. If tree-sitter is not
    available, returns an empty graph with an error message.
    """
    if _check_treesitter():
        build_graph_ts(file_paths)
    else:
        # Try importing to get the actual error message
        err_msg = "tree-sitter-languages not available"
        try:
            import tree_sitter_languages  # noqa: F401
        except ImportError as e:
            err_msg = f"tree-sitter-languages import failed: {e}"
        except Exception as e:
            err_msg = f"tree-sitter-languages error: {e}"
        print(json.dumps({"symbols": [], "imports": [], "line_kinds": {}, "errors": [err_msg]}))


# ============================================================
# verify_plan: Check plan preconditions against graph
# ============================================================

VALID_OPS = {
    "replace_code", "insert_code", "delete_lines", "add_method",
    "add_import", "modify_function_signature", "rename_symbol",
    "wrap_block", "add_class_attribute", "replace_function_body",
}

REQUIRED_PARAMS = {
    "replace_code": ["file", "pattern", "replacement"],
    "insert_code": ["file", "anchor_line", "position", "code"],
    "delete_lines": ["file", "start_line", "end_line"],
    "add_method": ["file", "class_name", "method_code"],
    "add_import": ["file", "import_statement"],
    "modify_function_signature": ["file", "func_name", "old_signature", "new_signature"],
    "rename_symbol": ["file", "old_name", "new_name"],
    "wrap_block": ["file", "start_line", "end_line", "before_code", "after_code"],
    "add_class_attribute": ["file", "class_name", "attribute_code"],
    "replace_function_body": ["file", "func_name", "new_body"],
}


def verify_plan(plan_json, graph_json):
    """Verify plan preconditions against graph."""
    plan = json.loads(plan_json)
    graph = json.loads(graph_json)
    errors = []

    symbols = graph.get("symbols", [])
    line_kinds = graph.get("line_kinds", {})

    for i, step in enumerate(plan):
        op = step.get("op", "")
        params = step.get("params", {})

        # Check valid operator
        if op not in VALID_OPS:
            errors.append(f"Step {i}: Unknown operator '{op}'")
            continue

        # Check required params
        for param_name in REQUIRED_PARAMS.get(op, []):
            if param_name not in params:
                errors.append(f"Step {i}: Missing parameter '{param_name}' for {op}")

        file_path = params.get("file", "")

        # Check file exists
        if file_path and file_path != "all" and not os.path.isfile(file_path):
            errors.append(f"Step {i}: File '{file_path}' does not exist")
            continue

        # Operator-specific checks
        if op in ("add_method", "add_class_attribute"):
            class_name = params.get("class_name", "")
            found = any(
                s["kind"] == "class" and s["name"] == class_name and s["file"] == file_path
                for s in symbols
            )
            if not found:
                errors.append(f"Step {i} ({op}): Class '{class_name}' not found in {file_path}")

        elif op in ("modify_function_signature", "replace_function_body"):
            func_name = params.get("func_name", "")
            found = any(
                s["kind"] == "function" and s["name"] == func_name and s["file"] == file_path
                for s in symbols
            )
            if not found:
                errors.append(f"Step {i} ({op}): Function '{func_name}' not found in {file_path}")

        elif op in ("insert_code",):
            anchor = params.get("anchor_line", 0)
            if file_path and os.path.isfile(file_path):
                num_lines = sum(1 for _ in open(file_path))
                if anchor < 1 or anchor > num_lines:
                    errors.append(f"Step {i} ({op}): anchor_line {anchor} out of range (1-{num_lines})")

        elif op in ("delete_lines", "wrap_block"):
            start = params.get("start_line", 0)
            end = params.get("end_line", 0)
            if start > end:
                errors.append(f"Step {i} ({op}): start_line ({start}) > end_line ({end})")

    print(json.dumps({"passed": len(errors) == 0, "errors": errors}))


# ============================================================
# Tree-sitter node finders for AST-dependent operators
# ============================================================

def _find_class_node_ts(filepath, class_name):
    """Find class boundaries using tree-sitter.

    Returns (start_line, end_line, body_start_line) with 1-indexed lines,
    or None if not found.
    """
    lang = detect_language(filepath)
    if not lang or not _check_treesitter():
        return None

    import tree_sitter_languages
    try:
        parser = tree_sitter_languages.get_parser(lang)
        source = open(filepath, "rb").read()
        tree = parser.parse(source)
    except Exception:
        return None

    def _search(node):
        # Look for class/struct/interface definitions/declarations
        if node.type in ("class_definition", "class_declaration", "class_specifier",
                         "struct_specifier", "interface_declaration", "trait_item"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8") if isinstance(name_node.text, bytes) else name_node.text
                if name == class_name:
                    start = node.start_point[0] + 1
                    end = node.end_point[0] + 1
                    # Find body node
                    body_node = node.child_by_field_name("body")
                    body_start = body_node.start_point[0] + 1 if body_node else start
                    return (start, end, body_start)
        for child in node.children:
            result = _search(child)
            if result:
                return result
        return None

    return _search(tree.root_node)


def _find_python_docstring_end_ts(filepath, class_name):
    """Find the end line of a class docstring in Python using tree-sitter.

    Returns 1-indexed line number (insert AFTER this line), or None if no docstring.
    """
    if not _check_treesitter():
        return None

    import tree_sitter_languages
    try:
        parser = tree_sitter_languages.get_parser("python")
        source = open(filepath, "rb").read()
        tree = parser.parse(source)
    except Exception:
        return None

    def _search(node):
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8") if isinstance(name_node.text, bytes) else name_node.text
                if name == class_name:
                    body = node.child_by_field_name("body")
                    if body and body.child_count > 0:
                        first = body.children[0]
                        if (first.type == "expression_statement"
                                and first.child_count > 0
                                and first.children[0].type == "string"):
                            return first.end_point[0] + 1  # 1-indexed
                    return None
        for child in node.children:
            result = _search(child)
            if result is not None:
                return result
        return None

    return _search(tree.root_node)


def _find_function_node_ts(filepath, func_name):
    """Find function boundaries using tree-sitter.

    Returns (start_line, end_line, body_start_line, body_end_line) with 1-indexed lines,
    or None if not found.
    """
    lang = detect_language(filepath)
    if not lang or not _check_treesitter():
        return None

    import tree_sitter_languages
    try:
        parser = tree_sitter_languages.get_parser(lang)
        source = open(filepath, "rb").read()
        tree = parser.parse(source)
    except Exception:
        return None

    def _search(node):
        if node.type in ("function_declaration", "function_definition", "method_declaration",
                         "method_definition", "function_item", "constructor_declaration"):
            name_node = node.child_by_field_name("name")
            if not name_node:
                # Try declarator for C/C++
                decl = node.child_by_field_name("declarator")
                if decl:
                    name_node = decl.child_by_field_name("declarator")
            if name_node:
                name = name_node.text.decode("utf-8") if isinstance(name_node.text, bytes) else name_node.text
                if name == func_name:
                    start = node.start_point[0] + 1
                    end = node.end_point[0] + 1
                    body_node = node.child_by_field_name("body")
                    if body_node:
                        body_start = body_node.start_point[0] + 1
                        body_end = body_node.end_point[0] + 1
                    else:
                        body_start = start
                        body_end = end
                    return (start, end, body_start, body_end)
        for child in node.children:
            result = _search(child)
            if result:
                return result
        return None

    return _search(tree.root_node)


# ============================================================
# Syntax check: language-aware post-edit validation
# ============================================================

def _has_error_nodes(node):
    """Check if a tree-sitter parse tree contains ERROR nodes."""
    if node.type == "ERROR":
        return True
    for child in node.children:
        if _has_error_nodes(child):
            return True
    return False


def _syntax_check(filepath):
    """Check syntax of a file. Returns (ok, error_message).

    Uses tree-sitter to parse and check for ERROR nodes.
    Unknown language or no tree-sitter -> skip (return success).
    """
    lang = detect_language(filepath)
    if lang is None or not _check_treesitter():
        return (True, None)

    import tree_sitter_languages
    try:
        parser = tree_sitter_languages.get_parser(lang)
        source = open(filepath, "rb").read()
        tree = parser.parse(source)
        if _has_error_nodes(tree.root_node):
            return (False, f"Parse error detected in {filepath} after edit")
        return (True, None)
    except Exception:
        return (True, None)  # Can't check, assume ok


# ============================================================
# execute_step: Apply a single plan step (file modification)
# ============================================================

def _read_file(path):
    with open(path) as f:
        return f.read()


def _write_file(path, content):
    with open(path, "w") as f:
        f.write(content)


def _read_lines(path):
    with open(path) as f:
        return f.readlines()


def _write_lines(path, lines):
    with open(path, "w") as f:
        f.writelines(lines)


def execute_step(step_json):
    """Execute a single plan step (file modification)."""
    step = json.loads(step_json)
    op = step["op"]
    params = step["params"]
    file_path = params.get("file", "")

    try:
        if op == "replace_code":
            _exec_replace_code(params)
        elif op == "insert_code":
            _exec_insert_code(params)
        elif op == "delete_lines":
            _exec_delete_lines(params)
        elif op == "add_method":
            _exec_add_method(params)
        elif op == "add_import":
            _exec_add_import(params)
        elif op == "modify_function_signature":
            _exec_modify_function_signature(params)
        elif op == "rename_symbol":
            _exec_rename_symbol(params)
        elif op == "wrap_block":
            _exec_wrap_block(params)
        elif op == "add_class_attribute":
            _exec_add_class_attribute(params)
        elif op == "replace_function_body":
            _exec_replace_function_body(params)
        else:
            print(json.dumps({"success": False, "error": f"Unknown operator: {op}"}))
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)

    # Post-check: language-aware syntax validation
    check_file = file_path if file_path != "all" else None
    if check_file:
        ok, err = _syntax_check(check_file)
        if ok:
            print(json.dumps({"success": True}))
        else:
            print(json.dumps({"success": False, "error": err}))
            sys.exit(1)
    else:
        print(json.dumps({"success": True}))


# --- Operator implementations ---

def _exec_replace_code(params):
    """Replace a code pattern with new code (string-based)."""
    content = _read_file(params["file"])
    pattern = params["pattern"]
    replacement = params["replacement"]
    if pattern not in content:
        raise ValueError(f"Pattern not found in {params['file']}: {pattern[:80]}")
    content = content.replace(pattern, replacement, 1)
    _write_file(params["file"], content)


def _exec_insert_code(params):
    """Insert code before or after a specific line."""
    lines = _read_lines(params["file"])
    anchor = params["anchor_line"]  # 1-indexed
    position = params.get("position", "after")
    code = params["code"]
    if not code.endswith("\n"):
        code += "\n"

    idx = anchor - 1  # convert to 0-indexed
    if idx < 0 or idx >= len(lines):
        raise ValueError(f"anchor_line {anchor} out of range (1-{len(lines)})")

    if position == "before":
        lines.insert(idx, code)
    else:  # after
        lines.insert(idx + 1, code)

    _write_lines(params["file"], lines)


def _exec_delete_lines(params):
    """Delete lines from start_line to end_line (inclusive, 1-indexed)."""
    lines = _read_lines(params["file"])
    start = params["start_line"] - 1  # convert to 0-indexed
    end = params["end_line"]  # inclusive, so this is the slice end
    if start < 0 or end > len(lines):
        raise ValueError(f"Line range {params['start_line']}-{params['end_line']} out of bounds (1-{len(lines)})")
    del lines[start:end]
    _write_lines(params["file"], lines)


def _exec_add_method(params):
    """Add a new method to an existing class."""
    fp = params["file"]
    class_name = params["class_name"]
    method_code = params["method_code"]
    if not method_code.endswith("\n"):
        method_code += "\n"

    lang = detect_language(fp)
    ts_info = _find_class_node_ts(fp, class_name)
    if ts_info is None:
        raise ValueError(f"Class '{class_name}' not found in {fp}")

    start_line, end_line, body_start = ts_info
    lines = _read_lines(fp)

    if lang == "python":
        # Indentation-based: insert after the last line of the class
        insert_idx = end_line  # end_line is 1-indexed, so this is 0-indexed pos after
        lines.insert(insert_idx, "\n" + method_code)
    else:
        # Brace-based: find closing } and insert before it
        insert_idx = end_line - 1  # 0-indexed
        while insert_idx > 0 and '}' not in lines[insert_idx]:
            insert_idx -= 1
        lines.insert(insert_idx, "\n" + method_code)

    _write_lines(fp, lines)


def _exec_add_import(params):
    """Add an import statement after existing imports."""
    lines = _read_lines(params["file"])
    stmt = params["import_statement"]
    if not stmt.endswith("\n"):
        stmt += "\n"

    # Find last import line
    last_import_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            last_import_idx = i

    if last_import_idx >= 0:
        lines.insert(last_import_idx + 1, stmt)
    else:
        # No imports found, insert at top (after any docstring/comments)
        insert_at = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith('"""') and not stripped.startswith("'" + "''"):
                insert_at = i
                break
        lines.insert(insert_at, stmt)

    _write_lines(params["file"], lines)


def _exec_modify_function_signature(params):
    """Change a function's signature line."""
    content = _read_file(params["file"])
    old_sig = params["old_signature"]
    new_sig = params["new_signature"]
    if old_sig not in content:
        raise ValueError(f"Old signature not found: {old_sig}")
    content = content.replace(old_sig, new_sig, 1)
    _write_file(params["file"], content)


def _exec_rename_symbol(params):
    """Rename a variable/function/class and update all references."""
    file_path = params["file"]
    old_name = params["old_name"]
    new_name = params["new_name"]

    # word-boundary replacement
    pattern = r'\b' + re.escape(old_name) + r'\b'

    if file_path == "all":
        # This would need a file list - for safety, just error
        raise ValueError("rename_symbol with file='all' requires explicit file listing")

    content = _read_file(file_path)
    content = re.sub(pattern, new_name, content)
    _write_file(file_path, content)


def _exec_wrap_block(params):
    """Wrap lines in a block structure (try/except, if/else, with, etc.)."""
    lines = _read_lines(params["file"])
    start = params["start_line"] - 1  # 0-indexed
    end = params["end_line"]  # inclusive end, so slice is start:end
    before_code = params["before_code"]
    after_code = params["after_code"]

    if not before_code.endswith("\n"):
        before_code += "\n"
    if not after_code.endswith("\n"):
        after_code += "\n"

    # Indent the wrapped lines by 4 spaces
    wrapped = []
    for line in lines[start:end]:
        wrapped.append("    " + line if line.strip() else line)

    new_section = [before_code] + wrapped + [after_code]
    lines[start:end] = new_section
    _write_lines(params["file"], lines)


def _exec_add_class_attribute(params):
    """Add a class-level attribute after the class definition line."""
    fp = params["file"]
    class_name = params["class_name"]
    attr_code = params["attribute_code"]
    if not attr_code.endswith("\n"):
        attr_code += "\n"

    lang = detect_language(fp)
    ts_info = _find_class_node_ts(fp, class_name)
    if ts_info is None:
        raise ValueError(f"Class '{class_name}' not found in {fp}")

    start_line, end_line, body_start = ts_info
    lines = _read_lines(fp)

    if lang == "python":
        # Insert after class def line, skipping past any docstring
        insert_line = _find_python_docstring_end_ts(fp, class_name)
        if insert_line is None:
            # No docstring — insert at body_start (before first body statement)
            insert_line = body_start - 1  # convert to 0-indexed insert position
        lines.insert(insert_line, attr_code)
    else:
        # Brace-based: insert right after the opening brace
        lines.insert(body_start, attr_code)

    _write_lines(fp, lines)


def _exec_replace_function_body(params):
    """Replace the entire body of a function."""
    fp = params["file"]
    func_name = params["func_name"]
    new_body = params["new_body"]
    if not new_body.endswith("\n"):
        new_body += "\n"

    lang = detect_language(fp)
    ts_info = _find_function_node_ts(fp, func_name)
    if ts_info is None:
        raise ValueError(f"Function '{func_name}' not found in {fp}")

    start_line, end_line, body_start, body_end = ts_info
    lines = _read_lines(fp)

    if lang == "python":
        # Indentation-based: body is from body_start to body_end (both 1-indexed)
        # For Python tree-sitter, body is the block node containing all statements
        lines[body_start - 1:body_end] = [new_body]
    else:
        # Brace-based: replace content between { and }
        brace_open_idx = body_start - 1  # 0-indexed
        brace_close_idx = body_end - 1  # 0-indexed

        if brace_open_idx == brace_close_idx:
            # Single-line body like { return x; }
            indent = "    "
            lines[brace_open_idx] = "{\n" + indent + new_body + "}\n"
        else:
            lines[brace_open_idx + 1:brace_close_idx] = [new_body]

    _write_lines(fp, lines)


# ============================================================
# Main dispatch
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: graphplan_helper.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "build_graph":
        build_graph(sys.argv[2:])
    elif cmd == "verify_plan":
        if len(sys.argv) < 4:
            print("Usage: graphplan_helper.py verify_plan '<plan_json>' '<graph_json>'", file=sys.stderr)
            sys.exit(1)
        verify_plan(sys.argv[2], sys.argv[3])
    elif cmd == "execute_step":
        if len(sys.argv) < 3:
            print("Usage: graphplan_helper.py execute_step '<step_json>'", file=sys.stderr)
            sys.exit(1)
        execute_step(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
'''
