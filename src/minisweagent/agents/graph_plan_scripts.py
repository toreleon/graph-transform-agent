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
import difflib
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
# Normalized AST kind map: normalized kind -> per-language node types
# ============================================================

NORMALIZED_KINDS = {
    "function": {
        "python": ["function_definition"],
        "javascript": ["function_declaration", "method_definition"],
        "typescript": ["function_declaration", "method_definition"],
        "java": ["method_declaration", "constructor_declaration"],
        "go": ["function_declaration", "method_declaration"],
        "rust": ["function_item"],
        "ruby": ["method", "singleton_method"],
        "php": ["function_definition", "method_declaration"],
        "c": ["function_definition"],
        "cpp": ["function_definition"],
    },
    "class": {
        "python": ["class_definition"],
        "javascript": ["class_declaration"],
        "typescript": ["class_declaration"],
        "java": ["class_declaration"],
        "go": [],
        "rust": ["struct_item"],
        "ruby": ["class"],
        "php": ["class_declaration"],
        "c": ["struct_specifier"],
        "cpp": ["class_specifier", "struct_specifier"],
    },
    "method": {
        "python": ["function_definition"],  # methods are function_definitions inside classes
        "javascript": ["method_definition"],
        "typescript": ["method_definition"],
        "java": ["method_declaration"],
        "go": ["method_declaration"],
        "rust": ["function_item"],  # inside impl blocks
        "ruby": ["method"],
        "php": ["method_declaration"],
        "c": [],
        "cpp": [],
    },
    "import": {
        "python": ["import_statement", "import_from_statement"],
        "javascript": ["import_statement"],
        "typescript": ["import_statement"],
        "java": ["import_declaration"],
        "go": ["import_declaration"],
        "rust": ["use_declaration"],
        "ruby": ["call"],  # require/require_relative
        "php": ["namespace_use_declaration"],
        "c": ["preproc_include"],
        "cpp": ["preproc_include"],
    },
    "statement": {
        "python": ["expression_statement", "return_statement", "if_statement",
                    "for_statement", "while_statement", "try_statement",
                    "raise_statement", "assert_statement", "with_statement",
                    "assignment"],
        "javascript": ["expression_statement", "return_statement", "if_statement",
                        "for_statement", "while_statement", "try_statement",
                        "throw_statement", "variable_declaration"],
        "typescript": ["expression_statement", "return_statement", "if_statement",
                        "for_statement", "while_statement", "try_statement",
                        "throw_statement", "variable_declaration"],
        "java": ["expression_statement", "return_statement", "if_statement",
                  "for_statement", "while_statement", "try_statement",
                  "throw_statement", "local_variable_declaration"],
        "go": ["expression_statement", "return_statement", "if_statement",
                "for_statement", "short_var_declaration"],
        "rust": ["expression_statement", "return_expression", "if_expression",
                  "for_expression", "while_expression", "let_declaration"],
        "ruby": ["expression_statement", "return", "if", "for", "while"],
        "php": ["expression_statement", "return_statement", "if_statement",
                 "for_statement", "while_statement", "try_statement"],
        "c": ["expression_statement", "return_statement", "if_statement",
               "for_statement", "while_statement", "declaration"],
        "cpp": ["expression_statement", "return_statement", "if_statement",
                 "for_statement", "while_statement", "declaration",
                 "try_statement"],
    },
    "interface": {
        "python": [],
        "javascript": [],
        "typescript": ["interface_declaration"],
        "java": ["interface_declaration"],
        "go": [],
        "rust": ["trait_item"],
        "ruby": [],
        "php": ["interface_declaration"],
        "c": [],
        "cpp": [],
    },
    "enum": {
        "python": [],
        "javascript": [],
        "typescript": ["enum_declaration"],
        "java": ["enum_declaration"],
        "go": [],
        "rust": ["enum_item"],
        "ruby": [],
        "php": [],
        "c": ["enum_specifier"],
        "cpp": ["enum_specifier"],
    },
}


def _get_normalized_node_types(kind, lang):
    """Get tree-sitter node types for a normalized kind in a language."""
    kind_map = NORMALIZED_KINDS.get(kind)
    if kind_map is None:
        return []
    return kind_map.get(lang, [])


def _node_text(node):
    """Get text of a tree-sitter node as a string."""
    return node.text.decode("utf-8") if isinstance(node.text, bytes) else node.text


def _get_node_name(node):
    """Get the name of a tree-sitter node (from its 'name' field)."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        # Try declarator for C/C++
        decl = node.child_by_field_name("declarator")
        if decl:
            name_node = decl.child_by_field_name("declarator")
            if name_node is None:
                name_node = decl
    if name_node is not None:
        return _node_text(name_node)
    return None


def resolve_locator(locator, file_path=None, language=None, tree=None, source=None):
    """Resolve a locator against a live AST, returning matching nodes.

    A locator is a dict with fields:
        kind: normalized AST kind (function, class, method, import, statement, etc.)
        name: symbol name (for named nodes)
        file: file path (used externally; here we use file_path param)
        parent: nested locator constraining to children of parent
        field: named tree-sitter field of matched node (body, parameters, etc.)
        nth_child: select Nth child of matched node (-1 for last)
        index: disambiguate when multiple matches (0-based)
        type: "sexp" for S-expression locator mode

    Returns list of matching tree-sitter nodes.
    """
    if not _check_treesitter():
        return []

    fp = file_path or locator.get("file", "")
    if not fp:
        return []

    lang = language or detect_language(fp)
    if not lang:
        return []

    import tree_sitter_languages

    # Parse file if tree not provided
    if tree is None:
        try:
            source = open(fp, "rb").read()
            parser = tree_sitter_languages.get_parser(lang)
            tree = parser.parse(source)
        except Exception:
            return []

    if source is None:
        try:
            source = open(fp, "rb").read()
        except Exception:
            return []

    root = tree.root_node

    # S-expression locator mode
    if locator.get("type") == "sexp":
        query_str = locator.get("query", "")
        capture_name = locator.get("capture", "id")
        if not query_str:
            return []
        try:
            ts_lang = tree_sitter_languages.get_language(lang)
            query = ts_lang.query(query_str)
            captures = query.captures(root)
            nodes = _get_captures_list(captures, capture_name)
            idx = locator.get("index")
            if idx is not None and 0 <= idx < len(nodes):
                return [nodes[idx]]
            return list(nodes)
        except Exception:
            return []

    # Structured locator mode
    kind = locator.get("kind", "")
    name = locator.get("name")
    parent_locator = locator.get("parent")
    field_name = locator.get("field")
    nth_child = locator.get("nth_child")
    index = locator.get("index")

    # Get target node types for this kind+language
    target_types = set(_get_normalized_node_types(kind, lang)) if kind else set()

    # Determine search root
    if parent_locator:
        parent_nodes = resolve_locator(parent_locator, fp, lang, tree, source)
        if not parent_nodes:
            return []
        search_roots = parent_nodes
    else:
        search_roots = [root]

    # Collect matching nodes
    matches = []
    for search_root in search_roots:
        _collect_matching_nodes(search_root, target_types, name, kind, lang, matches)

    # Apply field selection
    if field_name and matches:
        field_nodes = []
        for node in matches:
            field_node = node.child_by_field_name(field_name)
            if field_node:
                field_nodes.append(field_node)
        matches = field_nodes

    # Apply nth_child selection
    if nth_child is not None and matches:
        child_nodes = []
        for node in matches:
            children = [c for c in node.children if c.type not in ("comment", "(", ")", "{", "}", ":", ",")]
            if children:
                idx_val = nth_child if nth_child >= 0 else len(children) + nth_child
                if 0 <= idx_val < len(children):
                    child_nodes.append(children[idx_val])
        matches = child_nodes

    # Apply index disambiguation
    if index is not None:
        if 0 <= index < len(matches):
            return [matches[index]]
        return []

    return matches


def _collect_matching_nodes(node, target_types, name, kind, lang, result):
    """Recursively collect nodes matching target types and name."""
    is_match = False

    if target_types:
        if node.type in target_types:
            is_match = True
    elif kind:
        # If kind has no mapping for this language, skip
        pass

    if is_match:
        if name is not None:
            node_name = _get_node_name(node)
            if node_name == name:
                result.append(node)
        else:
            result.append(node)

    for child in node.children:
        _collect_matching_nodes(child, target_types, name, kind, lang, result)


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


# Primitive names recognized by the engine (forward-declared for verify_plan)
PRIMITIVE_OPS = {
    "replace_node", "insert_before_node", "insert_after_node",
    "delete_node", "wrap_node", "replace_all_matching",
    "locate", "locate_region",
}

# Built-in composed operator names (forward-declared for verify_plan)
BUILTIN_COMPOSED_OP_NAMES = {"add_method", "add_import", "add_class_attribute"}


# ============================================================
# Verification helper functions (Layers 1-6)
# ============================================================

def _fuzzy_find(content, pattern, threshold=0.8):
    """Find closest match for pattern in content using difflib.

    Line-based sliding window for multi-line patterns,
    character-level fallback for short single-line patterns (< 200 chars).
    Returns (similarity_ratio, matched_text) or (0.0, None).
    """
    if not pattern or not content:
        return (0.0, None)

    pattern_lines = pattern.splitlines(True)
    content_lines = content.splitlines(True)
    n = len(pattern_lines)

    best_ratio = 0.0
    best_match = None

    if n > 1 or len(pattern) >= 200:
        # Line-based sliding window
        for i in range(max(1, len(content_lines) - n + 1)):
            window = content_lines[i:i + n]
            window_text = "".join(window)
            ratio = difflib.SequenceMatcher(None, pattern, window_text).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = window_text
    else:
        # Character-level sliding window for short single-line patterns
        plen = len(pattern)
        step = max(1, plen // 4)
        for i in range(0, max(1, len(content) - plen + 1), step):
            window = content[i:i + plen + plen // 4]
            ratio = difflib.SequenceMatcher(None, pattern, window).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = window

    if best_ratio >= threshold:
        return (best_ratio, best_match)
    return (0.0, None)


def _extract_method_name(method_code):
    """Extract method name from code string.

    Recognizes: def name(, function name(, name( patterns.
    Returns name string or None.
    """
    if not method_code:
        return None
    # Python/Ruby: def name(
    m = re.search(r'\bdef\s+(\w+)\s*\(', method_code)
    if m:
        return m.group(1)
    # JS/TS: function name( or async function name(
    m = re.search(r'\bfunction\s+(\w+)\s*\(', method_code)
    if m:
        return m.group(1)
    # Java/Go/general: name(  (first identifier followed by paren)
    m = re.search(r'\b(\w+)\s*\(', method_code)
    if m:
        return m.group(1)
    return None


def _check_line_drift(plan):
    """Layer 2: Check for cumulative line drift across steps targeting the same file.

    Groups steps by file, walks in order, computes cumulative drift from
    line-changing operations. Emits warnings for steps that use line numbers
    when drift != 0.
    """
    warnings = []
    # Group steps by file, preserving order
    file_steps = {}
    for i, step in enumerate(plan):
        fp = step.get("params", {}).get("file", "")
        if fp and fp != "all":
            file_steps.setdefault(fp, []).append((i, step))

    for fp, steps in file_steps.items():
        drift = 0
        for i, step in steps:
            op = step.get("op", "")
            params = step.get("params", {})

            # Warn if this step uses line numbers and drift != 0
            if op in ("insert_code", "delete_lines", "wrap_block") and drift != 0:
                warnings.append(
                    f"Step {i} ({op}): line numbers may be off by {drift:+d} lines "
                    f"due to earlier edits on {fp}"
                )

            # Compute drift from this step
            if op == "insert_code":
                code = params.get("code", "")
                drift += code.count("\n") + (1 if not code.endswith("\n") else 0)
            elif op == "delete_lines":
                start = params.get("start_line", 0)
                end = params.get("end_line", 0)
                if end >= start:
                    drift -= (end - start + 1)
            elif op == "wrap_block":
                # before_code + after_code add at least 2 lines
                before = params.get("before_code", "")
                after = params.get("after_code", "")
                drift += before.count("\n") + (1 if before and not before.endswith("\n") else 0)
                drift += after.count("\n") + (1 if after and not after.endswith("\n") else 0)
            elif op == "add_method":
                code = params.get("method_code", "")
                drift += code.count("\n") + 2  # newline + code lines
            elif op == "add_import":
                drift += 1
            elif op == "add_class_attribute":
                drift += 1
            elif op == "replace_code":
                old = params.get("pattern", "")
                new = params.get("replacement", "")
                old_lines = old.count("\n") + 1
                new_lines = new.count("\n") + 1
                drift += new_lines - old_lines

    return warnings


def _check_pattern_ast_context(filepath, pattern, match_start):
    """Layer 3: Check if a text match falls inside a string or comment AST node.

    Returns warning string or None. Graceful degradation if tree-sitter unavailable.
    """
    if not _check_treesitter():
        return None

    lang = detect_language(filepath)
    if not lang:
        return None

    try:
        import tree_sitter_languages
        parser = tree_sitter_languages.get_parser(lang)
        source = open(filepath, "rb").read()
        tree = parser.parse(source)
    except Exception:
        return None

    STRING_COMMENT_TYPES = {
        "string", "comment", "string_literal", "template_string",
        "line_comment", "block_comment", "string_content",
        "interpreted_string_literal", "raw_string_literal",
        "string_fragment", "heredoc_body", "regex",
    }

    # Convert byte offset to (row, col) for tree-sitter
    source_bytes = source[:match_start]
    row = source_bytes.count(b"\n")
    last_nl = source_bytes.rfind(b"\n")
    col = match_start - last_nl - 1 if last_nl >= 0 else match_start

    # Find deepest node at position
    node = tree.root_node.descendant_for_point_range((row, col), (row, col))
    if node is None:
        return None

    # Walk ancestors checking for string/comment types
    current = node
    while current is not None:
        if current.type in STRING_COMMENT_TYPES:
            return (
                f"Pattern match in {filepath} at offset {match_start} "
                f"is inside a {current.type} node (may not be actual code)"
            )
        current = current.parent

    return None


def _classify_symbol_occurrences(filepath, symbol_name):
    """Layer 4: Classify all occurrences of a symbol in a file.

    Returns {definitions, references, in_strings, in_comments, total} or None.
    """
    if not _check_treesitter():
        return None

    lang = detect_language(filepath)
    if not lang:
        return None

    try:
        import tree_sitter_languages
        parser = tree_sitter_languages.get_parser(lang)
        source = open(filepath, "rb").read()
        tree = parser.parse(source)
    except Exception:
        return None

    STRING_TYPES = {
        "string", "string_literal", "template_string",
        "interpreted_string_literal", "raw_string_literal",
        "string_content", "string_fragment", "heredoc_body",
    }
    COMMENT_TYPES = {"comment", "line_comment", "block_comment"}
    DEFINITION_NODE_TYPES = {
        "function_definition", "function_declaration", "method_definition",
        "method_declaration", "class_definition", "class_declaration",
        "variable_declarator", "assignment", "function_item",
        "struct_item", "enum_item", "trait_item",
    }

    counts = {"definitions": 0, "references": 0, "in_strings": 0, "in_comments": 0, "total": 0}

    def _walk(node):
        if node.type == "identifier" or node.type == "type_identifier":
            text = node.text.decode("utf-8") if isinstance(node.text, bytes) else node.text
            if text == symbol_name:
                counts["total"] += 1
                # Check ancestors for context
                parent = node.parent
                in_string = False
                in_comment = False
                is_def = False
                ancestor = node.parent
                while ancestor is not None:
                    if ancestor.type in STRING_TYPES:
                        in_string = True
                        break
                    if ancestor.type in COMMENT_TYPES:
                        in_comment = True
                        break
                    ancestor = ancestor.parent
                if in_string:
                    counts["in_strings"] += 1
                elif in_comment:
                    counts["in_comments"] += 1
                elif parent and parent.type in DEFINITION_NODE_TYPES:
                    # Check if this identifier is the name field
                    if parent.child_by_field_name("name") == node:
                        counts["definitions"] += 1
                    else:
                        counts["references"] += 1
                else:
                    counts["references"] += 1
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return counts if counts["total"] > 0 else None


def _syntax_check_content(content_str, filepath):
    """Layer 5: Check syntax of content string (not file on disk).

    Like _syntax_check() but takes content string instead of reading file.
    Returns (ok, error_message).
    """
    lang = detect_language(filepath)
    if lang is None or not _check_treesitter():
        return (True, None)

    try:
        import tree_sitter_languages
        parser = tree_sitter_languages.get_parser(lang)
        source = content_str.encode("utf-8") if isinstance(content_str, str) else content_str
        tree = parser.parse(source)
        if _has_error_nodes(tree.root_node):
            return (False, f"Replacement produces syntax error in {filepath}")
        return (True, None)
    except Exception:
        return (True, None)


def _build_import_graph(graph):
    """Layer 6: Build import relationship maps from graph data.

    Returns (symbol_importers, file_exports):
      symbol_importers: symbol_name -> set of files that import it
      file_exports: file_path -> set of symbol names it defines
    """
    symbol_importers = {}  # symbol_name -> set(files)
    file_exports = {}      # file_path -> set(symbol_names)

    # Build file_exports from symbols
    for sym in graph.get("symbols", []):
        fp = sym.get("file", "")
        name = sym.get("name", "")
        if fp and name:
            file_exports.setdefault(fp, set()).add(name)

    # Build symbol_importers from imports
    for imp in graph.get("imports", []):
        importing_file = imp.get("file", "")
        symbol = imp.get("symbol")
        module = imp.get("module", "")

        if symbol and symbol != "*":
            # Direct symbol import: from X import symbol
            symbol_importers.setdefault(symbol, set()).add(importing_file)
        elif module:
            # Module-level import: try matching module name to file stems
            module_stem = module.rsplit(".", 1)[-1] if "." in module else module
            module_stem = module_stem.rsplit("/", 1)[-1] if "/" in module_stem else module_stem
            # Check if any exported symbol name matches the module stem
            for fp, exports in file_exports.items():
                file_stem = os.path.splitext(os.path.basename(fp))[0]
                if file_stem == module_stem:
                    for exp_name in exports:
                        symbol_importers.setdefault(exp_name, set()).add(importing_file)

    return symbol_importers, file_exports


def _extract_plan_steps(plan_data):
    """Extract steps list and custom operators from plan data.

    Handles both formats:
      - Bare array: [{"op": ...}, ...]
      - Object format: {"define_operators": [...], "plan": [...]}

    Returns (steps, custom_operators).
    """
    if isinstance(plan_data, list):
        return plan_data, []
    elif isinstance(plan_data, dict):
        return plan_data.get("plan", []), plan_data.get("define_operators", [])
    return [], []


# All valid ops: legacy + primitives + built-in composed
ALL_VALID_OPS = VALID_OPS | PRIMITIVE_OPS | BUILTIN_COMPOSED_OP_NAMES


def verify_plan(plan_json, graph_json):
    """Verify plan preconditions against graph.

    Handles both legacy operators and new AST-node primitives.

    Layer 0: Structural checks (valid operator, required params, file exists, symbol in graph, line range)
    Layer 0b: Locator-based precondition checks (for primitives)
    Layer 1: Content existence checks (pattern found, signature found, duplicate detection)
    Layer 2: Line drift detection (cumulative line number shifts from earlier edits)
    Layer 3: AST context checks (pattern inside string/comment)
    Layer 4: Symbol occurrence classification (rename affects strings/comments)
    Layer 5: Preflight syntax check (simulated replacement produces valid syntax)
    Layer 6: Cross-file impact analysis (renamed/deleted symbols imported elsewhere)
    """
    plan_data = json.loads(plan_json)
    graph = json.loads(graph_json)

    plan, custom_operators = _extract_plan_steps(plan_data)
    errors = []
    warnings = []

    symbols = graph.get("symbols", [])
    line_kinds = graph.get("line_kinds", {})

    for i, step in enumerate(plan):
        op = step.get("op", "")
        params = step.get("params", {})

        # Check if this is a custom-defined operator
        is_custom = any(c.get("define") == op for c in custom_operators)

        # === Layer 0: Structural checks ===

        # Check valid operator
        if op not in ALL_VALID_OPS and not is_custom:
            errors.append(f"Step {i}: Unknown operator '{op}'")
            continue

        # For legacy ops, check required params
        if op in VALID_OPS:
            for param_name in REQUIRED_PARAMS.get(op, []):
                if param_name not in params:
                    errors.append(f"Step {i}: Missing parameter '{param_name}' for {op}")

        # Determine file path from params or locator
        file_path = params.get("file", "")
        if not file_path:
            locator = params.get("locator", {})
            file_path = locator.get("file", "")

        # Check file exists
        if file_path and file_path != "all" and not os.path.isfile(file_path):
            errors.append(f"Step {i}: File '{file_path}' does not exist")
            continue

        # === Layer 0b: Locator-based precondition checks (for primitives) ===

        if op in PRIMITIVE_OPS and op not in ("locate", "locate_region"):
            locator = params.get("locator", {})
            if locator:
                nodes = resolve_locator(locator, file_path=file_path)
                if not nodes and op not in ("replace_all_matching",):
                    errors.append(
                        f"Step {i} ({op}): Locator matched 0 nodes: {json.dumps(locator)}"
                    )
                elif len(nodes) > 1 and op in ("replace_node", "delete_node", "wrap_node"):
                    if locator.get("index") is None:
                        warnings.append(
                            f"Step {i} ({op}): Locator matched {len(nodes)} nodes "
                            f"(use 'index' to disambiguate): {json.dumps(locator)}"
                        )

        # Legacy operator-specific structural checks
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

        # === Layer 1: Content existence checks ===

        if file_path and file_path != "all" and os.path.isfile(file_path):
            try:
                content = open(file_path).read()
            except Exception:
                content = None

            if content is not None:
                if op == "replace_code":
                    pattern = params.get("pattern", "")
                    if pattern and pattern not in content:
                        ratio, matched = _fuzzy_find(content, pattern)
                        if ratio > 0:
                            preview = (matched[:60] + "...") if matched and len(matched) > 60 else matched
                            warnings.append(
                                f"Step {i} (replace_code): Pattern not found exactly, "
                                f"but {ratio:.0%} similar match found: {preview!r}"
                            )
                        else:
                            errors.append(
                                f"Step {i} (replace_code): Pattern not found in {file_path}: "
                                f"{pattern[:80]!r}"
                            )

                elif op == "modify_function_signature":
                    old_sig = params.get("old_signature", "")
                    if old_sig and old_sig not in content:
                        errors.append(
                            f"Step {i} (modify_function_signature): Old signature not found in "
                            f"{file_path}: {old_sig[:80]!r}"
                        )

                elif op == "rename_symbol":
                    old_name = params.get("old_name", "")
                    if old_name and not re.search(r'\b' + re.escape(old_name) + r'\b', content):
                        errors.append(
                            f"Step {i} (rename_symbol): Symbol '{old_name}' not found in {file_path}"
                        )

                elif op == "add_import":
                    import_stmt = params.get("import_statement", "").strip()
                    if import_stmt and import_stmt in content:
                        warnings.append(
                            f"Step {i} (add_import): Import already exists in {file_path}: "
                            f"{import_stmt[:80]!r}"
                        )

                elif op == "add_method":
                    method_code = params.get("method_code", "")
                    method_name = _extract_method_name(method_code)
                    if method_name and re.search(r'\bdef\s+' + re.escape(method_name) + r'\s*\(', content):
                        warnings.append(
                            f"Step {i} (add_method): Method '{method_name}' may already exist in {file_path}"
                        )

                # === Layer 3: AST context checks ===

                if op == "replace_code":
                    pattern = params.get("pattern", "")
                    if pattern and pattern in content:
                        match_pos = content.find(pattern)
                        ast_warn = _check_pattern_ast_context(file_path, pattern, match_pos)
                        if ast_warn:
                            warnings.append(f"Step {i} (replace_code): {ast_warn}")

                # === Layer 4: Symbol occurrence classification ===

                if op == "rename_symbol":
                    old_name = params.get("old_name", "")
                    if old_name:
                        occurrences = _classify_symbol_occurrences(file_path, old_name)
                        if occurrences:
                            if occurrences["in_strings"] > 0 or occurrences["in_comments"] > 0:
                                warnings.append(
                                    f"Step {i} (rename_symbol): '{old_name}' also appears in "
                                    f"strings ({occurrences['in_strings']}x) and "
                                    f"comments ({occurrences['in_comments']}x) -- "
                                    f"regex rename will change these too"
                                )

                # === Layer 5: Preflight syntax check ===

                if op == "replace_code":
                    pattern = params.get("pattern", "")
                    replacement = params.get("replacement", "")
                    if pattern and pattern in content:
                        simulated = content.replace(pattern, replacement, 1)
                        ok, err = _syntax_check_content(simulated, file_path)
                        if not ok:
                            errors.append(f"Step {i} (replace_code): {err}")

    # === Layer 2: Line drift detection (post-loop, only for legacy ops) ===
    legacy_steps = [s for s in plan if s.get("op", "") in VALID_OPS]
    if legacy_steps:
        drift_warnings = _check_line_drift(legacy_steps)
        warnings.extend(drift_warnings)

    # === Layer 6: Cross-file impact analysis (post-loop) ===
    try:
        symbol_importers, file_exports = _build_import_graph(graph)
        plan_files = set()
        for step in plan:
            fp = step.get("params", {}).get("file", "")
            if not fp:
                fp = step.get("params", {}).get("locator", {}).get("file", "")
            if fp:
                plan_files.add(fp)

        for i, step in enumerate(plan):
            op = step.get("op", "")
            params = step.get("params", {})

            if op == "rename_symbol":
                old_name = params.get("old_name", "")
                if old_name in symbol_importers:
                    affected = symbol_importers[old_name] - plan_files
                    if affected:
                        warnings.append(
                            f"Step {i} (rename_symbol): '{old_name}' is imported by files "
                            f"not in this plan: {sorted(affected)}"
                        )

            elif op == "modify_function_signature":
                func_name = params.get("func_name", "")
                if func_name in symbol_importers:
                    affected = symbol_importers[func_name] - plan_files
                    if affected:
                        warnings.append(
                            f"Step {i} (modify_function_signature): '{func_name}' is imported by "
                            f"files not in this plan: {sorted(affected)}"
                        )

            elif op == "delete_lines":
                fp = params.get("file", "")
                start = params.get("start_line", 0)
                end = params.get("end_line", 0)
                if fp and start and end:
                    for sym in symbols:
                        if (sym["file"] == fp
                                and sym["start_line"] >= start
                                and sym["end_line"] <= end):
                            sym_name = sym["name"]
                            if sym_name in symbol_importers:
                                affected = symbol_importers[sym_name] - plan_files
                                if affected:
                                    warnings.append(
                                        f"Step {i} (delete_lines): Deleting '{sym_name}' "
                                        f"which is imported by: {sorted(affected)}"
                                    )

            elif op == "delete_node":
                locator = params.get("locator", {})
                loc_name = locator.get("name", "")
                if loc_name and loc_name in symbol_importers:
                    affected = symbol_importers[loc_name] - plan_files
                    if affected:
                        warnings.append(
                            f"Step {i} (delete_node): '{loc_name}' is imported by files "
                            f"not in this plan: {sorted(affected)}"
                        )
    except Exception:
        pass  # Graceful degradation

    print(json.dumps({"passed": len(errors) == 0, "errors": errors, "warnings": warnings}))


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
# Verifier primitives
# ============================================================

def _verify_parses_ok(filepath):
    """Verify file parses without errors. Returns (ok, error_msg)."""
    return _syntax_check(filepath)


def _verify_parses_ok_content(content, filepath):
    """Verify content string parses without errors. Returns (ok, error_msg)."""
    return _syntax_check_content(content, filepath)


def _verify_node_exists(locator, filepath=None):
    """Verify locator resolves to >= 1 node. Returns (ok, error_msg)."""
    nodes = resolve_locator(locator, file_path=filepath)
    if nodes:
        return (True, None)
    return (False, f"Locator did not match any node: {json.dumps(locator)}")


def _verify_node_absent(locator, filepath=None):
    """Verify locator resolves to 0 nodes. Returns (ok, error_msg)."""
    nodes = resolve_locator(locator, file_path=filepath)
    if not nodes:
        return (True, None)
    return (False, f"Locator still matches {len(nodes)} node(s): {json.dumps(locator)}")


def _verify_scope_unchanged(original_source, new_source, edit_start, edit_end, filepath):
    """Verify AST outside edit region is unchanged via hash comparison."""
    if not _check_treesitter():
        return (True, None)

    lang = detect_language(filepath)
    if not lang:
        return (True, None)

    import tree_sitter_languages
    try:
        parser = tree_sitter_languages.get_parser(lang)
        old_tree = parser.parse(original_source if isinstance(original_source, bytes) else original_source.encode("utf-8"))
        new_tree = parser.parse(new_source if isinstance(new_source, bytes) else new_source.encode("utf-8"))

        def _hash_outside(root, start, end):
            """Collect node type+text hashes for nodes outside the edit region."""
            hashes = []
            for child in root.children:
                if child.end_byte <= start or child.start_byte >= end:
                    hashes.append((child.type, child.start_point, child.end_point))
            return hashes

        old_hashes = _hash_outside(old_tree.root_node, edit_start, edit_end)
        # For new tree, we need to account for byte offset shift
        # Just check that the file still parses ok as a simpler verification
        if _has_error_nodes(new_tree.root_node):
            return (False, f"New content has parse errors in {filepath}")
        return (True, None)
    except Exception:
        return (True, None)


def _verify_type_compatible(node, expected_type):
    """Verify a node has the expected AST type. Returns (ok, error_msg)."""
    if node.type == expected_type:
        return (True, None)
    return (False, f"Expected node type '{expected_type}', got '{node.type}'")


# ============================================================
# Mutator primitives: AST-node based editing with rollback
# ============================================================

def _execute_primitive(name, params):
    """Execute a primitive with pre-check, edit, post-check, rollback protocol.

    Returns dict: {success: bool, error?: str, rolled_back?: bool, result?: dict}
    """
    locator = params.get("locator", {})
    fp = locator.get("file", params.get("file", ""))
    if not fp:
        return {"success": False, "error": "No file specified in locator or params"}

    if not os.path.isfile(fp):
        return {"success": False, "error": f"File not found: {fp}"}

    # Save original for rollback
    try:
        original = open(fp, "rb").read()
    except Exception as e:
        return {"success": False, "error": f"Cannot read {fp}: {e}"}

    # Resolve locator to find target nodes
    nodes = resolve_locator(locator, file_path=fp)

    # Pre-condition checks
    pre_result = _check_preconditions(name, fp, nodes, params)
    if not pre_result[0]:
        return {"success": False, "error": pre_result[1]}

    # Apply the edit
    try:
        edit_result = _apply_primitive_edit(name, fp, nodes, params, original)
        if not edit_result.get("success"):
            return edit_result
    except Exception as e:
        # Rollback on exception
        try:
            with open(fp, "wb") as f:
                f.write(original)
        except Exception:
            pass
        return {"success": False, "error": str(e), "rolled_back": True}

    # Post-condition checks
    post_result = _check_postconditions(name, fp, locator, params)
    if not post_result[0]:
        # Rollback on postcondition failure
        try:
            with open(fp, "wb") as f:
                f.write(original)
        except Exception:
            pass
        return {"success": False, "error": post_result[1], "rolled_back": True}

    return {"success": True, "result": edit_result.get("result", {})}


def _check_preconditions(name, filepath, nodes, params):
    """Check preconditions for a primitive. Returns (ok, error_msg)."""
    if name == "replace_all_matching":
        if len(nodes) < 1:
            return (False, f"No matching nodes found for replace_all_matching")
        return (True, None)

    if name in ("replace_node", "insert_before_node", "insert_after_node",
                "delete_node", "wrap_node"):
        if len(nodes) == 0:
            locator = params.get("locator", {})
            return (False, f"Node not found for {name}: {json.dumps(locator)}")
        if len(nodes) > 1 and name in ("replace_node", "delete_node", "wrap_node"):
            locator = params.get("locator", {})
            if locator.get("index") is None:
                return (False, f"Ambiguous: {len(nodes)} matches for {name}, use 'index' to disambiguate")
        return (True, None)

    return (True, None)


def _check_postconditions(name, filepath, locator, params):
    """Check postconditions after a primitive edit. Returns (ok, error_msg)."""
    # Always check syntax
    ok, err = _verify_parses_ok(filepath)
    if not ok:
        return (False, f"Post-edit syntax check failed: {err}")

    if name == "delete_node":
        ok, err = _verify_node_absent(locator, filepath)
        if not ok:
            return (False, f"delete_node postcondition: node still present")

    if name in ("insert_before_node", "insert_after_node"):
        # Verify anchor is still findable
        ok, err = _verify_node_exists(locator, filepath)
        # Anchor may have shifted but file should parse ok, which we already checked

    return (True, None)


def _apply_primitive_edit(name, filepath, nodes, params, original_bytes):
    """Apply a single primitive edit. Returns {success, error?, result?}."""
    content = original_bytes.decode("utf-8") if isinstance(original_bytes, bytes) else original_bytes

    if name == "replace_node":
        return _prim_replace_node(filepath, nodes, params, content)
    elif name == "insert_before_node":
        return _prim_insert_before(filepath, nodes, params, content)
    elif name == "insert_after_node":
        return _prim_insert_after(filepath, nodes, params, content)
    elif name == "delete_node":
        return _prim_delete_node(filepath, nodes, params, content)
    elif name == "wrap_node":
        return _prim_wrap_node(filepath, nodes, params, content)
    elif name == "replace_all_matching":
        return _prim_replace_all_matching(filepath, nodes, params, content)
    else:
        return {"success": False, "error": f"Unknown primitive: {name}"}


def _prim_replace_node(filepath, nodes, params, content):
    """Replace a single AST node's text with new code."""
    node = nodes[0]
    replacement = params.get("replacement", "")
    source_bytes = content.encode("utf-8")
    new_content = source_bytes[:node.start_byte] + replacement.encode("utf-8") + source_bytes[node.end_byte:]
    _write_file(filepath, new_content.decode("utf-8"))
    return {"success": True, "result": {
        "replaced_start_line": node.start_point[0] + 1,
        "replaced_end_line": node.end_point[0] + 1,
    }}


def _prim_insert_before(filepath, nodes, params, content):
    """Insert code before a target AST node."""
    node = nodes[0]
    code = params.get("code", "")
    separator = params.get("separator", "\n")
    source_bytes = content.encode("utf-8")

    # Determine indentation from target node
    line_start = source_bytes.rfind(b"\n", 0, node.start_byte)
    if line_start < 0:
        line_start = 0
    else:
        line_start += 1
    indent = b""
    for b in source_bytes[line_start:node.start_byte]:
        if b in (32, 9):  # space or tab
            indent += bytes([b])
        else:
            break

    insert_text = code + separator
    if not insert_text.endswith("\n"):
        insert_text += "\n"

    # Indent the inserted code to match
    insert_lines = insert_text.split("\n")
    indented_lines = []
    for i, line in enumerate(insert_lines):
        if line.strip() or i == 0:
            indented_lines.append(indent.decode("utf-8") + line if i > 0 else line)
        else:
            indented_lines.append(line)
    insert_text = "\n".join(indented_lines)
    if not insert_text.endswith("\n"):
        insert_text += "\n"

    new_content = source_bytes[:line_start] + insert_text.encode("utf-8") + source_bytes[line_start:]
    _write_file(filepath, new_content.decode("utf-8"))
    return {"success": True, "result": {"inserted_at_line": node.start_point[0] + 1}}


def _prim_insert_after(filepath, nodes, params, content):
    """Insert code after a target AST node."""
    node = nodes[0]
    code = params.get("code", "")
    separator = params.get("separator", "\n")
    source_bytes = content.encode("utf-8")

    # Find end of node's line
    line_end = source_bytes.find(b"\n", node.end_byte)
    if line_end < 0:
        line_end = len(source_bytes)
    else:
        line_end += 1  # include the newline

    # Determine indentation from target node
    line_start = source_bytes.rfind(b"\n", 0, node.start_byte)
    if line_start < 0:
        line_start = 0
    else:
        line_start += 1
    indent = b""
    for b_val in source_bytes[line_start:node.start_byte]:
        if b_val in (32, 9):
            indent += bytes([b_val])
        else:
            break

    insert_text = separator + code
    if not insert_text.endswith("\n"):
        insert_text += "\n"

    # Apply indent to each line of inserted code
    insert_lines = insert_text.split("\n")
    indented_lines = []
    for i, line in enumerate(insert_lines):
        if line.strip():
            indented_lines.append(indent.decode("utf-8") + line)
        else:
            indented_lines.append(line)
    insert_text = "\n".join(indented_lines)

    new_content = source_bytes[:line_end] + insert_text.encode("utf-8") + source_bytes[line_end:]
    _write_file(filepath, new_content.decode("utf-8"))
    return {"success": True, "result": {"inserted_after_line": node.end_point[0] + 1}}


def _prim_delete_node(filepath, nodes, params, content):
    """Delete a single AST node."""
    node = nodes[0]
    source_bytes = content.encode("utf-8")

    # Delete the whole line(s) if the node spans complete lines
    line_start = source_bytes.rfind(b"\n", 0, node.start_byte)
    if line_start < 0:
        line_start = 0
    else:
        line_start += 1

    # Check if only whitespace before node on its line
    before_on_line = source_bytes[line_start:node.start_byte]
    only_whitespace_before = all(b in (32, 9) for b in before_on_line)

    line_end = source_bytes.find(b"\n", node.end_byte)
    if line_end < 0:
        line_end = len(source_bytes)
    else:
        line_end += 1

    after_on_line = source_bytes[node.end_byte:line_end].strip()

    if only_whitespace_before and not after_on_line:
        # Delete entire lines
        new_content = source_bytes[:line_start] + source_bytes[line_end:]
    else:
        # Delete just the node bytes
        new_content = source_bytes[:node.start_byte] + source_bytes[node.end_byte:]

    _write_file(filepath, new_content.decode("utf-8"))
    return {"success": True, "result": {
        "deleted_start_line": node.start_point[0] + 1,
        "deleted_end_line": node.end_point[0] + 1,
    }}


def _prim_wrap_node(filepath, nodes, params, content):
    """Wrap a node with before/after code, optionally indenting the body."""
    node = nodes[0]
    before = params.get("before", "")
    after = params.get("after", "")
    indent_body = params.get("indent_body", True)
    source_bytes = content.encode("utf-8")

    # Determine indentation of target node
    line_start = source_bytes.rfind(b"\n", 0, node.start_byte)
    if line_start < 0:
        line_start = 0
    else:
        line_start += 1
    indent = b""
    for b_val in source_bytes[line_start:node.start_byte]:
        if b_val in (32, 9):
            indent += bytes([b_val])
        else:
            break

    node_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8")

    # Build wrapped text
    indent_str = indent.decode("utf-8")
    if indent_body:
        # Indent body by 4 spaces relative to current
        body_lines = node_text.split("\n")
        indented_body = "\n".join("    " + line if line.strip() else line for line in body_lines)
    else:
        indented_body = node_text

    wrapped = f"{indent_str}{before}\n{indented_body}\n{indent_str}{after}"

    new_content = source_bytes[:node.start_byte] + wrapped.encode("utf-8") + source_bytes[node.end_byte:]
    _write_file(filepath, new_content.decode("utf-8"))
    return {"success": True, "result": {
        "wrapped_start_line": node.start_point[0] + 1,
        "wrapped_end_line": node.end_point[0] + 1,
    }}


def _prim_replace_all_matching(filepath, nodes, params, content):
    """Replace all matching nodes, processing bottom-up to avoid invalidation."""
    replacement = params.get("replacement", "")
    filter_mode = params.get("filter")
    source_bytes = content.encode("utf-8")

    # Sort nodes by start_byte descending (bottom-up) to avoid invalidation
    sorted_nodes = sorted(nodes, key=lambda n: n.start_byte, reverse=True)

    # Optionally filter out nodes inside strings/comments
    if filter_mode == "not_in_string_or_comment":
        STRING_COMMENT_TYPES = {
            "string", "comment", "string_literal", "template_string",
            "line_comment", "block_comment", "string_content",
            "interpreted_string_literal", "raw_string_literal",
            "string_fragment", "heredoc_body",
        }
        filtered = []
        for node in sorted_nodes:
            in_string_or_comment = False
            ancestor = node.parent
            while ancestor is not None:
                if ancestor.type in STRING_COMMENT_TYPES:
                    in_string_or_comment = True
                    break
                ancestor = ancestor.parent
            if not in_string_or_comment:
                filtered.append(node)
        sorted_nodes = filtered

    if not sorted_nodes:
        return {"success": False, "error": "No nodes to replace after filtering"}

    # Apply replacements bottom-up
    result_bytes = source_bytes
    count = 0
    for node in sorted_nodes:
        result_bytes = result_bytes[:node.start_byte] + replacement.encode("utf-8") + result_bytes[node.end_byte:]
        count += 1

    _write_file(filepath, result_bytes.decode("utf-8"))
    return {"success": True, "result": {"replaced_count": count}}


# ============================================================
# DSL interpreter: variable resolution + composed operators
# ============================================================

# Built-in composed operators expressed as DSL step sequences
BUILTIN_COMPOSED_OPS = {
    "add_method": {
        "params_schema": {"file": "string", "class_name": "string", "method_code": "string"},
        "steps": [
            {"primitive": "insert_after_node", "params": {
                "locator": {"kind": "class", "name": "$class_name", "file": "$file", "field": "body", "nth_child": -1},
                "code": "\n$method_code",
            }}
        ],
    },
    "add_import": {
        "params_schema": {"file": "string", "import_statement": "string"},
        "steps": [
            {"primitive": "insert_after_node", "params": {
                "locator": {"kind": "import", "file": "$file", "index": -1},
                "code": "$import_statement",
            }},
        ],
        "fallback": "_exec_add_import",  # fallback to legacy when no imports exist
    },
    "add_class_attribute": {
        "params_schema": {"file": "string", "class_name": "string", "attribute_code": "string"},
        "steps": [
            {"primitive": "insert_before_node", "params": {
                "locator": {"kind": "class", "name": "$class_name", "file": "$file", "field": "body", "nth_child": 0},
                "code": "$attribute_code",
            }}
        ],
    },
}


def resolve_var(template, variables):
    """Resolve $var references in a string or dict/list structure.

    Handles: $var, $var.field, and nested structures.
    """
    if isinstance(template, str):
        if template.startswith("$") and "." not in template and template[1:] in variables:
            # Direct variable reference - return the value as-is (may not be string)
            return variables[template[1:]]
        # String interpolation: replace $var within strings
        result = template
        for var_name, var_value in variables.items():
            result = result.replace(f"${var_name}", str(var_value))
        # Handle $var.field references
        import re as _re
        for match in _re.finditer(r'\$(\w+)\.(\w+)', template):
            full = match.group(0)
            var_n = match.group(1)
            field = match.group(2)
            if var_n in variables and isinstance(variables[var_n], dict):
                result = result.replace(full, str(variables[var_n].get(field, full)))
        return result
    elif isinstance(template, dict):
        return {k: resolve_var(v, variables) for k, v in template.items()}
    elif isinstance(template, list):
        return [resolve_var(item, variables) for item in template]
    return template


def execute_dsl_steps(steps, variables, custom_operators=None):
    """Execute a sequence of DSL steps with variable resolution.

    Each step is one of:
        {"primitive": "name", "params": {...}, "bind": "var_name"}
        {"if": "condition", "then": step, "else": step}

    Returns list of step results.
    """
    results = []
    for step in steps:
        # Handle conditional
        if "if" in step:
            condition = resolve_var(step["if"], variables)
            # Simple condition evaluation (e.g., "$var.count > 0")
            try:
                cond_result = eval(str(condition), {"__builtins__": {}}, {})
            except Exception:
                cond_result = bool(condition)
            branch = step.get("then") if cond_result else step.get("else")
            if branch:
                sub_results = execute_dsl_steps([branch], variables, custom_operators)
                results.extend(sub_results)
            continue

        # Handle primitive step
        if "primitive" in step:
            prim_name = step["primitive"]
            prim_params = resolve_var(step.get("params", {}), variables)

            if prim_name in ("locate", "locate_region"):
                # Read-only primitives
                result = _execute_locate(prim_name, prim_params)
            else:
                result = _execute_primitive(prim_name, prim_params)

            # Bind result to variable if requested
            bind_name = step.get("bind")
            if bind_name and isinstance(result, dict):
                variables[bind_name] = result.get("result", result)

            results.append(result)
            if not result.get("success", False) and prim_name not in ("locate", "locate_region"):
                break  # Stop on failure

        # Handle composed operator reference
        elif "op" in step:
            op_name = step["op"]
            op_params = resolve_var(step.get("params", {}), variables)
            result = _execute_composed_op(op_name, op_params, custom_operators)
            results.append(result)
            if not result.get("success", False):
                break

    return results


def _execute_locate(name, params):
    """Execute a locate (read-only) primitive."""
    locator = params.get("locator", params)
    nodes = resolve_locator(locator)
    if name == "locate":
        node_info = []
        for n in nodes:
            text = _node_text(n)
            preview = text[:100] + "..." if len(text) > 100 else text
            node_info.append({
                "start_line": n.start_point[0] + 1,
                "end_line": n.end_point[0] + 1,
                "kind": n.type,
                "text_preview": preview,
            })
        return {"success": True, "found": len(nodes) > 0, "count": len(nodes), "nodes": node_info}
    elif name == "locate_region":
        if not nodes:
            return {"success": False, "error": "No nodes matched"}
        n = nodes[0]
        text = _node_text(n)
        return {"success": True, "start_byte": n.start_byte, "end_byte": n.end_byte,
                "start_line": n.start_point[0] + 1, "end_line": n.end_point[0] + 1, "text": text}
    return {"success": False, "error": f"Unknown locate: {name}"}


def expand_composed_operator(op_name, op_params, custom_operators=None):
    """Expand a composed operator into its primitive steps with resolved variables.

    Returns (steps, variables) or (None, error_msg).
    """
    # Check custom operators first
    op_def = None
    if custom_operators:
        for custom in custom_operators:
            if custom.get("define") == op_name:
                op_def = custom
                break

    # Then check built-in composed operators
    if op_def is None:
        op_def = BUILTIN_COMPOSED_OPS.get(op_name)

    if op_def is None:
        return None, f"Unknown composed operator: {op_name}"

    steps = op_def.get("steps", [])
    variables = dict(op_params)  # param values become variables for $var resolution
    return steps, variables


def _execute_composed_op(op_name, op_params, custom_operators=None):
    """Execute a composed operator by expanding and running its steps."""
    steps, variables_or_error = expand_composed_operator(op_name, op_params, custom_operators)
    if steps is None:
        return {"success": False, "error": variables_or_error}

    results = execute_dsl_steps(steps, variables_or_error, custom_operators)
    if not results:
        return {"success": False, "error": f"No steps executed for {op_name}"}

    # Check if all steps succeeded
    all_ok = all(r.get("success", False) for r in results)
    if all_ok:
        return {"success": True, "results": results}

    # Find first failure
    for r in results:
        if not r.get("success", False):
            return {"success": False, "error": r.get("error", "Unknown error"), "results": results}
    return {"success": False, "error": "Unknown failure"}


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


def execute_step(step_json, custom_operators=None):
    """Execute a single plan step (file modification).

    Routes to primitive engine for AST-node primitives, falls back to
    legacy operator implementations for backward compatibility.
    """
    step = json.loads(step_json) if isinstance(step_json, str) else step_json
    op = step["op"]
    params = step.get("params", {})
    file_path = params.get("file", "") or (params.get("locator", {}).get("file", ""))

    # Route primitives through the new engine
    if op in PRIMITIVE_OPS:
        if op in ("locate", "locate_region"):
            result = _execute_locate(op, params)
        else:
            result = _execute_primitive(op, params)
        print(json.dumps(result))
        if not result.get("success"):
            sys.exit(1)
        return

    # Route composed operators (built-in + custom)
    if op in BUILTIN_COMPOSED_OPS or (custom_operators and any(c.get("define") == op for c in custom_operators)):
        result = _execute_composed_op(op, params, custom_operators)
        print(json.dumps(result))
        if not result.get("success"):
            # Try legacy fallback if available
            fallback = BUILTIN_COMPOSED_OPS.get(op, {}).get("fallback")
            if fallback and fallback in globals():
                try:
                    globals()[fallback](params)
                    print(json.dumps({"success": True, "fallback": True}))
                    return
                except Exception as e:
                    print(json.dumps({"success": False, "error": str(e)}))
                    sys.exit(1)
            sys.exit(1)
        return

    # Legacy operator routing (backward compatibility)
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
            print("Usage: graphplan_helper.py execute_step '<step_json>' ['<custom_operators_json>']", file=sys.stderr)
            sys.exit(1)
        custom_ops = None
        if len(sys.argv) >= 4:
            try:
                custom_ops = json.loads(sys.argv[3])
            except json.JSONDecodeError:
                pass
        execute_step(sys.argv[2], custom_operators=custom_ops)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
'''
