"""Unit tests for GraphPlanAgent."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from minisweagent.agents.graph_plan import GraphPlanAgent, GraphPlanAgentConfig, READY_TO_PLAN_PATTERN
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import (
    DeterministicToolcallModel,
    make_toolcall_output,
)


# --- Fixtures ---


@pytest.fixture
def graphplan_config():
    """Load toolcall agent config from config/mini.yaml and add graphplan fields."""
    config_path = Path("src/minisweagent/config/mini.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)
    agent_config = config["agent"]
    # Remove interactive-only fields
    agent_config.pop("mode", None)
    # Add graphplan-specific fields
    agent_config["max_explore_steps"] = 30
    agent_config["max_plan_revisions"] = 3
    return agent_config


def make_tc_outputs(outputs_spec: list[tuple[str, list[dict]]]) -> list[dict]:
    """Create toolcall output dicts from (content, actions) tuples."""
    outputs = []
    for i, (content, actions) in enumerate(outputs_spec):
        tc_actions = []
        tool_calls = []
        for j, action in enumerate(actions):
            tool_call_id = f"call_{i}_{j}"
            tc_actions.append({"command": action["command"], "tool_call_id": tool_call_id})
            tool_calls.append(
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": "bash", "arguments": f'{{"command": "{action["command"]}"}}'},
                }
            )
        outputs.append(make_toolcall_output(content, tool_calls, tc_actions))
    return outputs


# --- Tests ---


def test_graphplan_config():
    """Test GraphPlanAgentConfig has the expected fields."""
    config = GraphPlanAgentConfig(
        system_template="test",
        instance_template="{{task}}",
        max_explore_steps=20,
        max_plan_revisions=5,
    )
    assert config.max_explore_steps == 20
    assert config.max_plan_revisions == 5
    assert config.step_limit == 0  # default


def test_graphplan_config_defaults():
    """Test GraphPlanAgentConfig default values."""
    config = GraphPlanAgentConfig(
        system_template="test",
        instance_template="{{task}}",
    )
    assert config.max_explore_steps == 30
    assert config.max_plan_revisions == 3


def test_ready_to_plan_pattern():
    """Test READY_TO_PLAN regex pattern matching."""
    # Standard format
    text = 'I found the files. READY_TO_PLAN: ["src/foo.py", "src/bar.py"]'
    match = READY_TO_PLAN_PATTERN.search(text)
    assert match is not None
    assert '"src/foo.py"' in match.group(1)

    # Single file
    text = 'READY_TO_PLAN: ["query.py"]'
    match = READY_TO_PLAN_PATTERN.search(text)
    assert match is not None

    # No match
    text = "I need to keep exploring"
    match = READY_TO_PLAN_PATTERN.search(text)
    assert match is None


def test_parse_file_list(graphplan_config):
    """Test _parse_file_list extracts files from READY_TO_PLAN marker."""
    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=[]),
        env=LocalEnvironment(),
        **graphplan_config,
    )

    # JSON format
    files = agent._parse_file_list('READY_TO_PLAN: ["src/foo.py", "src/bar.py"]')
    assert files == ["src/foo.py", "src/bar.py"]

    # Single file
    files = agent._parse_file_list('READY_TO_PLAN: ["query.py"]')
    assert files == ["query.py"]

    # No match
    files = agent._parse_file_list("no marker here")
    assert files == []


def test_extract_plan_json(graphplan_config):
    """Test _extract_plan_json parses JSON from LLM responses."""
    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=[]),
        env=LocalEnvironment(),
        **graphplan_config,
    )

    # Standard ```json``` format
    response = {"content": 'Here is the plan:\n```json\n[{"op": "replace_code", "params": {"file": "a.py"}}]\n```'}
    result = agent._extract_plan_json(response)
    assert '"replace_code"' in result

    # Raw JSON array
    response = {"content": 'Plan: [{"op": "add_import", "params": {"file": "b.py"}}]'}
    result = agent._extract_plan_json(response)
    assert '"add_import"' in result

    # No JSON
    response = {"content": "I'm not sure what to do"}
    result = agent._extract_plan_json(response)
    assert result == "[]"

    # List content format
    response = {"content": [{"text": '```json\n[{"op": "delete_lines"}]\n```'}]}
    result = agent._extract_plan_json(response)
    assert '"delete_lines"' in result


def test_fallback_on_no_files(graphplan_config):
    """Test graceful degradation when exploration doesn't find files."""
    outputs = make_tc_outputs([
        # Explore steps that don't produce READY_TO_PLAN
        ("Looking around", [{"command": "echo 'exploring'"}]),
        ("Still exploring", [{"command": "echo 'still looking'"}]),
        # Fallback step loop submits
        (
            "Let me submit",
            [{"command": "echo 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'\necho 'fallback submission'"}],
        ),
    ])
    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=outputs),
        env=LocalEnvironment(),
        **{**graphplan_config, "max_explore_steps": 2},
    )

    info = agent.run("Fix the bug")
    assert info["exit_status"] == "Submitted"
    assert info["submission"] == "fallback submission\n"


def test_explore_to_plan_early_submit(graphplan_config):
    """Test that early submission during exploration works."""
    outputs = make_tc_outputs([
        (
            "This is trivial, submitting directly",
            [{"command": "echo 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'\necho 'early submit'"}],
        ),
    ])
    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=outputs),
        env=LocalEnvironment(),
        **graphplan_config,
    )

    info = agent.run("Simple fix")
    assert info["exit_status"] == "Submitted"
    assert info["submission"] == "early submit\n"


def test_helper_script_deployment(graphplan_config):
    """Test that helper script is deployed to the environment."""
    mock_env = MagicMock()
    # Functional check passes (returns "ok"), then script deployment succeeds
    mock_env.execute.return_value = {"output": "ok list", "returncode": 0, "exception_info": ""}
    mock_env.get_template_vars.return_value = {}
    mock_env.serialize.return_value = {}

    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=[]),
        env=mock_env,
        **graphplan_config,
    )

    agent._deploy_helper_scripts()
    assert agent._scripts_deployed is True
    # Verify env.execute was called with the helper script content
    all_commands = [c[0][0]["command"] for c in mock_env.execute.call_args_list]
    assert any("graphplan_helper.py" in c for c in all_commands)


def test_step_limit_during_explore(graphplan_config):
    """Test step limit is enforced during exploration phase."""
    outputs = make_tc_outputs([
        ("Step 1", [{"command": "echo 'step1'"}]),
    ])
    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=outputs),
        env=LocalEnvironment(),
        **{**graphplan_config, "step_limit": 1},
    )

    info = agent.run("Limited exploration")
    assert info["exit_status"] == "LimitsExceeded"


def test_agent_registered():
    """Test that graphplan agent is registered in the agent mapping."""
    from minisweagent.agents import _AGENT_MAPPING, get_agent_class

    assert "graphplan" in _AGENT_MAPPING
    cls = get_agent_class("graphplan")
    assert cls is GraphPlanAgent


def test_build_code_graph(graphplan_config):
    """Test _build_code_graph calls helper script and parses output."""
    graph_output = {
        "symbols": [
            {"name": "MyClass", "kind": "class", "file": "test.py", "start_line": 1, "end_line": 10},
            {"name": "my_func", "kind": "function", "file": "test.py", "start_line": 12, "end_line": 20},
        ],
        "imports": [
            {"file": "test.py", "module": "os", "symbol": None, "line": 1},
        ],
        "line_kinds": {},
    }

    mock_env = MagicMock()
    mock_env.execute.return_value = {
        "output": json.dumps(graph_output),
        "returncode": 0,
        "exception_info": "",
    }
    mock_env.get_template_vars.return_value = {}
    mock_env.serialize.return_value = {}

    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=[]),
        env=mock_env,
        **graphplan_config,
    )

    graph_json, graph_view = agent._build_code_graph(["test.py"])
    assert "MyClass" in graph_view
    assert "my_func" in graph_view
    assert "CLASS" in graph_view
    assert "FUNCTION" in graph_view


def test_extract_plan_json_from_text():
    """Test the static _extract_plan_json_from_text method."""
    # JSON in code block
    text = '```json\n[{"op": "replace_code", "params": {"file": "a.py"}}]\n```'
    result = GraphPlanAgent._extract_plan_json_from_text(text)
    assert '"replace_code"' in result

    # Raw valid JSON array
    text = 'Here is the plan: [{"op": "add_import", "params": {"file": "b.py"}}] done.'
    result = GraphPlanAgent._extract_plan_json_from_text(text)
    assert '"add_import"' in result

    # Invalid JSON array (not valid JSON between [ and ])
    text = "Some text [not valid json] more text"
    result = GraphPlanAgent._extract_plan_json_from_text(text)
    assert result == "[]"

    # Empty array should return "[]"
    text = "Plan: []"
    result = GraphPlanAgent._extract_plan_json_from_text(text)
    assert result == "[]"

    # No JSON at all
    text = "I don't know what to do"
    result = GraphPlanAgent._extract_plan_json_from_text(text)
    assert result == "[]"

    # File list should NOT be treated as a plan (no "op" key)
    text = 'READY_TO_PLAN: ["src/foo.py", "src/bar.py"]'
    result = GraphPlanAgent._extract_plan_json_from_text(text)
    assert result == "[]"

    # Array of non-dict items should NOT be treated as a plan
    text = '```json\n["file1.py", "file2.py"]\n```'
    result = GraphPlanAgent._extract_plan_json_from_text(text)
    assert result == "[]"


def test_read_plan_file(graphplan_config):
    """Test _read_plan_file reads and validates JSON from the environment."""
    mock_env = MagicMock()
    mock_env.get_template_vars.return_value = {}
    mock_env.serialize.return_value = {}

    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=[]),
        env=mock_env,
        **graphplan_config,
    )

    # Valid plan file
    mock_env.execute.return_value = {
        "output": '[{"op": "replace_code", "params": {"file": "a.py", "pattern": "x", "replacement": "y"}}]',
        "returncode": 0,
        "exception_info": "",
    }
    result = agent._read_plan_file()
    assert '"replace_code"' in result

    # Empty file
    mock_env.execute.return_value = {"output": "", "returncode": 0, "exception_info": ""}
    result = agent._read_plan_file()
    assert result == ""

    # File not found
    mock_env.execute.return_value = {"output": "", "returncode": 1, "exception_info": ""}
    result = agent._read_plan_file()
    assert result == ""

    # Invalid JSON
    mock_env.execute.return_value = {"output": "not json", "returncode": 0, "exception_info": ""}
    result = agent._read_plan_file()
    assert result == ""

    # Empty array (not a valid plan)
    mock_env.execute.return_value = {"output": "[]", "returncode": 0, "exception_info": ""}
    result = agent._read_plan_file()
    assert result == ""


def test_scan_messages_for_plan(graphplan_config):
    """Test _scan_messages_for_plan extracts plan from message history."""
    mock_env = MagicMock()
    mock_env.get_template_vars.return_value = {}
    mock_env.serialize.return_value = {}

    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=[]),
        env=mock_env,
        **graphplan_config,
    )

    # No messages - no plan
    agent.messages = []
    assert agent._scan_messages_for_plan() == ""

    # Message with plan in content
    agent.messages = [
        {"role": "assistant", "content": '```json\n[{"op": "add_import", "params": {"file": "x.py", "import_statement": "import os"}}]\n```'},
    ]
    result = agent._scan_messages_for_plan()
    assert '"add_import"' in result

    # Message with no plan
    agent.messages = [
        {"role": "assistant", "content": "I ran the command"},
        {"role": "tool", "output": "success"},
    ]
    assert agent._scan_messages_for_plan() == ""


# Need json import for test_build_code_graph
import json
import os
import tempfile
from unittest.mock import patch


# ============================================================
# Tree-sitter / multi-language support tests
# ============================================================


def _exec_helper_func(func_name, *args):
    """Execute a function from the HELPER_SCRIPT in an isolated namespace."""
    from minisweagent.agents.graph_plan_scripts import HELPER_SCRIPT
    ns = {"__name__": "__test__"}
    exec(HELPER_SCRIPT, ns)
    return ns[func_name](*args)


def _get_helper_ns():
    """Return the namespace from executing HELPER_SCRIPT."""
    from minisweagent.agents.graph_plan_scripts import HELPER_SCRIPT
    ns = {"__name__": "__test__"}
    exec(HELPER_SCRIPT, ns)
    return ns


def test_detect_language():
    """Verify extension -> language mapping for all supported extensions."""
    ns = _get_helper_ns()
    detect = ns["detect_language"]

    assert detect("foo.py") == "python"
    assert detect("app.js") == "javascript"
    assert detect("app.jsx") == "javascript"
    assert detect("app.ts") == "typescript"
    assert detect("app.tsx") == "typescript"
    assert detect("Main.java") == "java"
    assert detect("main.go") == "go"
    assert detect("lib.rs") == "rust"
    assert detect("app.rb") == "ruby"
    assert detect("index.php") == "php"
    assert detect("main.c") == "c"
    assert detect("util.h") == "c"
    assert detect("main.cpp") == "cpp"
    assert detect("main.cc") == "cpp"
    assert detect("main.cxx") == "cpp"
    assert detect("main.hpp") == "cpp"
    assert detect("main.hxx") == "cpp"
    assert detect("README.md") is None
    assert detect("Makefile") is None


def test_syntax_check_python():
    """Verify Python syntax check via tree-sitter."""
    ts_langs = pytest.importorskip("tree_sitter_languages")  # noqa: F841

    ns = _get_helper_ns()
    ns["_treesitter_available"] = True
    syntax_check = ns["_syntax_check"]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("def foo():\n    return 42\n")
        f.flush()
        ok, err = syntax_check(f.name)
        assert ok is True
        assert err is None
    os.unlink(f.name)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("def foo(\n")  # syntax error
        f.flush()
        ok, err = syntax_check(f.name)
        assert ok is False
        assert "Parse error" in err
    os.unlink(f.name)


def test_node_type_to_kind():
    """Verify node type -> kind mapping."""
    ns = _get_helper_ns()
    fn = ns["_node_type_to_kind"]

    assert fn("class_node", "javascript") == "class"
    assert fn("struct_node", "rust") == "class"
    assert fn("iface_node", "typescript") == "class"
    assert fn("trait_node", "rust") == "class"
    assert fn("func_node", "javascript") == "function"
    assert fn("method_node", "java") == "function"
    assert fn("ctor_node", "java") == "function"
    assert fn("enum_node", "java") == "type"
    assert fn("type_node", "go") == "type"
    assert fn("ns_node", "cpp") == "type"
    assert fn("module_node", "ruby") == "type"


def test_build_graph_ts_javascript():
    """Parse a JS file with tree-sitter, verify class/function extraction."""
    ts_langs = pytest.importorskip("tree_sitter_languages")  # noqa: F841

    ns = _get_helper_ns()
    # Reset treesitter cache to True since we confirmed import works
    ns["_treesitter_available"] = True

    js_code = """\
class MyWidget {
    constructor(name) {
        this.name = name;
    }

    render() {
        return '<div>' + this.name + '</div>';
    }
}

function createWidget(name) {
    return new MyWidget(name);
}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
        f.write(js_code)
        f.flush()
        js_path = f.name

    try:
        # Capture stdout from build_graph_ts
        import io
        from contextlib import redirect_stdout

        captured = io.StringIO()
        with redirect_stdout(captured):
            ns["build_graph_ts"]([js_path])

        output = captured.getvalue().strip()
        result = json.loads(output)

        names = [s["name"] for s in result["symbols"]]
        assert "MyWidget" in names
        assert "createWidget" in names

        kinds = {s["name"]: s["kind"] for s in result["symbols"]}
        assert kinds["MyWidget"] == "class"
        assert kinds["createWidget"] == "function"
    finally:
        os.unlink(js_path)


def test_build_graph_ts_java():
    """Parse a Java file with tree-sitter, verify class/method extraction."""
    ts_langs = pytest.importorskip("tree_sitter_languages")  # noqa: F841

    ns = _get_helper_ns()
    ns["_treesitter_available"] = True

    java_code = """\
import java.util.List;

public class Calculator {
    private int result;

    public Calculator() {
        this.result = 0;
    }

    public int add(int a, int b) {
        return a + b;
    }
}
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".java", delete=False) as f:
        f.write(java_code)
        f.flush()
        java_path = f.name

    try:
        import io
        from contextlib import redirect_stdout

        captured = io.StringIO()
        with redirect_stdout(captured):
            ns["build_graph_ts"]([java_path])

        output = captured.getvalue().strip()
        result = json.loads(output)

        names = [s["name"] for s in result["symbols"]]
        assert "Calculator" in names
        assert "add" in names

        # Check imports
        modules = [i["module"] for i in result["imports"]]
        assert any("java.util" in m for m in modules)
    finally:
        os.unlink(java_path)


def test_install_treesitter_already_available(graphplan_config):
    """Verify _install_treesitter skips install when functional check passes."""
    mock_env = MagicMock()
    mock_env.get_template_vars.return_value = {}
    mock_env.serialize.return_value = {}
    # Functional check passes
    mock_env.execute.return_value = {"output": "ok list", "returncode": 0, "exception_info": ""}

    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=[]),
        env=mock_env,
        **graphplan_config,
    )

    agent._install_treesitter()
    assert agent._treesitter_installed is True
    # Only the functional check should have been called, not pip install
    calls = [c[0][0]["command"] for c in mock_env.execute.call_args_list]
    assert any("get_parser" in c for c in calls)
    assert not any("pip install" in c for c in calls)


def test_install_treesitter_not_available(graphplan_config):
    """Verify _install_treesitter runs pip install when not present."""
    mock_env = MagicMock()
    mock_env.get_template_vars.return_value = {}
    mock_env.serialize.return_value = {}

    # First call (functional check): fails. Second call (install): succeeds.
    # Third call (verification): succeeds.
    mock_env.execute.side_effect = [
        {"output": "ImportError", "returncode": 1, "exception_info": ""},
        {"output": "Successfully installed", "returncode": 0, "exception_info": ""},
        {"output": "ok list 1", "returncode": 0, "exception_info": ""},
    ]

    agent = GraphPlanAgent(
        model=DeterministicToolcallModel(outputs=[]),
        env=mock_env,
        **graphplan_config,
    )

    agent._install_treesitter()
    assert agent._treesitter_installed is True
    calls = [c[0][0]["command"] for c in mock_env.execute.call_args_list]
    assert any("pip install" in c for c in calls)


def test_build_graph_dispatch_python():
    """Verify build_graph uses tree-sitter for Python files."""
    ts_langs = pytest.importorskip("tree_sitter_languages")  # noqa: F841

    ns = _get_helper_ns()
    ns["_treesitter_available"] = True

    py_code = "class Foo:\n    pass\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(py_code)
        f.flush()
        py_path = f.name

    try:
        import io
        from contextlib import redirect_stdout

        captured = io.StringIO()
        with redirect_stdout(captured):
            ns["build_graph"]([py_path])

        output = captured.getvalue().strip()
        result = json.loads(output)

        names = [s["name"] for s in result["symbols"]]
        assert "Foo" in names
    finally:
        os.unlink(py_path)
