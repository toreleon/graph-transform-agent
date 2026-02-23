"""GraphPlan agent: operator-based graph transform planning for code edits.

Instead of free-form text patches, the agent:
1. Explores the codebase to find relevant files (normal step loop)
2. Builds a code graph (AST + symbol table) from relevant files
3. LLM selects edit operators from a 10-operator catalog and outputs a JSON plan
4. Plan is verified against the code graph before execution
5. Operators execute via a Python helper script inside the container
6. Fallback: if planning fails, degrades to normal DefaultAgent step loop
"""

import json
import logging
import re
import shlex
import subprocess
import tempfile

from rich.console import Console
from rich.rule import Rule

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.agents.graph_plan_scripts import HELPER_SCRIPT
from minisweagent.exceptions import InterruptAgentFlow
from minisweagent.models.utils.content_string import get_content_string

logger = logging.getLogger("minisweagent.graph_plan")
_console = Console(highlight=False)

OPERATOR_CATALOG_PROMPT = """## Available Edit Operators

Use **formal transforms** (preferred), **AST-node primitives**, or **legacy operators** (backward compatible).

All locator-based operators use **locators** to find targets structurally via tree-sitter:
```json
{"kind": "function", "name": "defer", "file": "query.py", "parent": {"kind": "class", "name": "QuerySet"}}
```
Locator fields: `kind` (function/class/method/import/statement), `name`, `file`, `parent` (nested), `field` (body/parameters/condition), `nth_child`, `index`

### Formal Transforms (Preferred)

#### Adding Code
- **guard_clause**: Add a safety check before code
  `{"template": "guard_clause", "params": {"condition": "data is not None", "guard_body": "return None", "target": {locator}}}`
- **add_import_and_use**: Import a symbol and use it
  `{"template": "add_import_and_use", "params": {"module": "collections", "symbol": "OrderedDict", "usage_target": {locator}, "usage_expression": "OrderedDict()"}}`
- **add_method**: Add a method to a class
  `{"template": "add_method", "params": {"class_locator": {locator}, "method_name": "validate", "parameters": ["self", "data"], "body": "return True"}}`
- **add_parameter**: Add parameter to function
  `{"template": "add_parameter", "params": {"function": {locator}, "param_name": "timeout", "default_value": "None"}}`
- **add_class_attribute**: Add attribute to class
  `{"template": "add_class_attribute", "params": {"class_locator": {locator}, "attr_name": "__slots__", "attr_value": "()"}}`
- **add_decorator**: Add @decorator to function/class
  `{"template": "add_decorator", "params": {"target": {locator}, "decorator": "cache"}}`
- **add_conditional_branch**: Add elif/else to if statement
  `{"template": "add_conditional_branch", "params": {"if_target": {locator}, "branch_type": "elif", "condition": "x < 0", "branch_body": "return -1"}}`

#### Modifying Code
- **replace_expression**: Change one expression to another
  `{"template": "replace_expression", "params": {"target": {locator}, "new_expression": "repr(v) != repr(init_params[k])"}}`
- **modify_condition**: Change condition of if/while/for
  `{"template": "modify_condition", "params": {"target": {locator}, "new_condition": "x > 0 and y is not None"}}`
- **change_return_value**: Change what a function returns
  `{"template": "change_return_value", "params": {"target": {locator}, "new_value": "dict(ms)"}}`
- **replace_function_body**: Replace entire function body (use fragment)
  `{"template": "replace_function_body", "params": {"function": {locator}, "new_body": {fragment}}}`

#### Wrapping Code
- **wrap_try_except**: Wrap in try/except
  `{"template": "wrap_try_except", "params": {"target": {locator}, "exception_type": "ValueError", "handler_body": "return default"}}`
- **wrap_context_manager**: Wrap in `with` statement
  `{"template": "wrap_context_manager", "params": {"target": {locator}, "context_expr": "open(path)", "as_var": "f"}}`

#### Restructuring Code
- **extract_variable**: Extract expression into named variable
  `{"template": "extract_variable", "params": {"target": {locator}, "variable_name": "result"}}`
- **inline_variable**: Replace variable with its value, remove assignment
  `{"template": "inline_variable", "params": {"target": {locator}, "variable_name": "temp"}}`

#### AST Surgery (no code generation)
- **rename_identifier**: `{"op": "rename_identifier", "target": {locator}, "new_name": "new_func"}`
- **delete_node**: `{"op": "delete_node", "target": {locator}}`
- **copy_node**: `{"op": "copy_node", "target": {locator}, "source": {locator}}`
- **move_node**: `{"op": "move_node", "target": {locator}, "source": {locator}}`
- **swap_nodes**: `{"op": "swap_nodes", "target": {locator}, "source": {locator}}`

#### Novel Code (typed fragments)
When no template fits, describe AST structure:
```json
{"fragment": {"kind": "if_statement", "condition": "not isinstance(data, dict)",
  "children": [{"kind": "raise_statement", "value": "TypeError('Expected dict')"}]},
 "target": {locator}, "action": "replace"}
```
Supported kinds: function_definition, class_definition, if_statement, elif_clause, else_clause, for_statement, while_statement, with_statement, try_statement, except_clause, finally_clause, return_statement, raise_statement, assignment, expression_statement

### AST-Node Primitives
  `replace_node` - `{"op": "replace_node", "params": {"locator": {...}, "replacement": "new code"}}`
  `insert_before_node` / `insert_after_node` - `{"op": "...", "params": {"locator": {...}, "code": "new code"}}`
  `delete_node` - `{"op": "delete_node", "params": {"locator": {...}}}`
  `wrap_node` - `{"op": "wrap_node", "params": {"locator": {...}, "before": "try:", "after": "except: pass"}}`
  `replace_all_matching` - `{"op": "replace_all_matching", "params": {"locator": {...}, "replacement": "new"}}`

### Legacy Operators (still supported)
  `replace_code(file, pattern, replacement)`, `insert_code(file, anchor_line, position, code)`,
  `delete_lines(file, start_line, end_line)`, `rename_symbol(file, old_name, new_name)`,
  `wrap_block(file, start_line, end_line, before_code, after_code)`,
  `replace_function_body(file, func_name, new_body)`

Output your plan as JSON: `[{"template": "...", "params": {...}}, ...]` or with custom operators: `{"define_operators": [...], "plan": [...]}`"""

READY_TO_PLAN_PATTERN = re.compile(r"READY_TO_PLAN:\s*\[([^\]]*)\]", re.DOTALL)
PLAN_JSON_PATTERN = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


class GraphPlanAgentConfig(AgentConfig):
    max_explore_steps: int = 30
    """Maximum number of steps for the exploration phase."""
    max_plan_revisions: int = 3
    """Maximum number of plan revision attempts before fallback."""


class GraphPlanAgent(DefaultAgent):
    """Agent that uses operator-based graph transform planning for code edits."""

    def __init__(self, model, env, *, config_class=GraphPlanAgentConfig, **kwargs):
        super().__init__(model, env, config_class=config_class, **kwargs)
        self._scripts_deployed = False
        self._treesitter_installed = False
        self._current_phase = "init"
        self._plan_info: dict = {}  # Saved plan data for trajectory

    def serialize(self, *extra_dicts) -> dict:
        """Override to include plan data in the trajectory."""
        data = super().serialize(*extra_dicts)
        if self._plan_info:
            data["info"]["plan"] = self._plan_info
        return data

    def add_messages(self, *messages: dict) -> list[dict]:
        """Override to stream all messages to the terminal."""
        for msg in messages:
            role = msg.get("role") or msg.get("type", "unknown")
            content = get_content_string(msg)
            if not content:
                continue
            if role == "assistant":
                _console.print(
                    f"\n[red][bold]graphplan[/bold] (step [bold]{self.n_calls}[/bold], "
                    f"[bold]${self.cost:.2f}[/bold], phase=[bold]{self._current_phase}[/bold]):[/red]",
                    highlight=False,
                )
            elif role == "exit":
                _console.print(f"\n[bold magenta]Exit[/bold magenta]:", highlight=False)
            else:
                _console.print(f"\n[bold green]{role.capitalize()}[/bold green]:", highlight=False)
            # Truncate very long outputs for readability
            if len(content) > 3000:
                _console.print(content[:2000], highlight=False, markup=False)
                _console.print(f"\n... ({len(content) - 2000} chars truncated) ...\n", highlight=False)
                _console.print(content[-500:], highlight=False, markup=False)
            else:
                _console.print(content, highlight=False, markup=False)
        return super().add_messages(*messages)

    def _print_phase(self, phase: str, detail: str = ""):
        """Print a phase transition banner to the terminal."""
        self._current_phase = phase
        label = f" {phase} "
        if detail:
            label += f"- {detail} "
        _console.print(Rule(label, style="cyan bold"))

    def run(self, task="", **kwargs) -> dict:
        """Run the agent with phased approach: explore -> plan -> execute.

        Args:
            task: The problem statement / task description.
        """
        self.extra_template_vars |= {"task": task, **kwargs}
        self.messages = []
        self.add_messages(
            self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
            self.model.format_message(role="user", content=self._render_template(self.config.instance_template)),
        )

        self._deploy_helper_scripts()

        # Phase 1: Localize (normal step loop)
        self._print_phase("EXPLORE", "finding relevant files")
        try:
            plan_files = self._explore_phase()
        except InterruptAgentFlow as e:
            self.add_messages(*e.messages)
            self.save(self.config.output_path)
            return self._verify_submission(self.messages[-1].get("extra", {}))
        except Exception as e:
            self.handle_uncaught_exception(e)
            self.save(self.config.output_path)
            raise

        if self.messages[-1].get("role") == "exit":
            self.save(self.config.output_path)
            return self._verify_submission(self.messages[-1].get("extra", {}))

        if not plan_files:
            return self._verify_submission(self._fallback_step_loop())

        # Phase 2: Build graph + Generate plan + Verify + Execute
        try:
            self._plan_and_execute(plan_files, task)
        except InterruptAgentFlow as e:
            self.add_messages(*e.messages)
        except Exception as e:
            self.handle_uncaught_exception(e)
            raise
        finally:
            self.save(self.config.output_path)

        if self.messages[-1].get("role") == "exit":
            return self._verify_submission(self.messages[-1].get("extra", {}))

        # If plan execution didn't submit, fall back to step loop
        return self._verify_submission(self._fallback_step_loop())

    def _install_treesitter(self):
        """Install tree-sitter-languages in the container if not already present.

        Non-fatal: logs a warning on failure and continues (empty-graph mode).
        Only installs tree-sitter-languages (not tree-sitter separately) to avoid
        version conflicts — tree-sitter-languages bundles compatible tree-sitter.
        """
        if self._treesitter_installed:
            return
        # Functional check: can we actually parse a Python snippet?
        func_check = self.env.execute(
            {"command": (
                "python3 -c '"
                "from tree_sitter_languages import get_parser, get_language; "
                "p = get_parser(\"python\"); "
                "t = p.parse(b\"def f(): pass\"); "
                "q = get_language(\"python\").query(\"(function_definition) @f\"); "
                "c = q.captures(t.root_node); "
                "print(\"ok\", type(c).__name__)"
                "' 2>&1"
            )}
        )
        if func_check.get("returncode", -1) == 0 and "ok" in func_check.get("output", ""):
            self._treesitter_installed = True
            logger.info(f"tree-sitter-languages functional check passed: {func_check.get('output', '').strip()}")
            return
        logger.info("Installing tree-sitter-languages in container...")
        # Pin tree-sitter<0.21 for compatibility with tree-sitter-languages.
        # tree-sitter 0.21+ changed Language.__init__() from (path, name) to (ptr),
        # which breaks tree-sitter-languages' Cython bindings.
        install = self.env.execute(
            {"command": "pip install 'tree-sitter<0.21' tree-sitter-languages --quiet --disable-pip-version-check 2>&1"}
        )
        if install.get("returncode", -1) != 0:
            logger.warning(
                f"Failed to install tree-sitter-languages (non-fatal): "
                f"{install.get('output', '')[:200]}"
            )
            return
        # Verify installation with functional check
        verify = self.env.execute(
            {"command": (
                "python3 -c '"
                "from tree_sitter_languages import get_parser, get_language; "
                "p = get_parser(\"python\"); "
                "t = p.parse(b\"def f(): pass\"); "
                "q = get_language(\"python\").query(\"(function_definition) @f\"); "
                "c = q.captures(t.root_node); "
                "print(\"ok\", type(c).__name__, len(c) if isinstance(c, list) else sum(len(v) for v in c.values()))"
                "' 2>&1"
            )}
        )
        if verify.get("returncode", -1) == 0 and "ok" in verify.get("output", ""):
            self._treesitter_installed = True
            logger.info(f"tree-sitter-languages installed and verified: {verify.get('output', '').strip()}")
        else:
            logger.warning(
                f"tree-sitter-languages installed but functional check failed: "
                f"{verify.get('output', '')[:300]}"
            )

    def _deploy_helper_scripts(self):
        """Write the helper Python script to /tmp/graphplan_helper.py in the container."""
        if self._scripts_deployed:
            return
        self._install_treesitter()
        # Use docker cp to avoid 'argument list too long' error for large scripts.
        # The HELPER_SCRIPT can exceed ARG_MAX when passed via shell heredoc.
        container_id = getattr(self.env, "container_id", None)
        docker_exe = getattr(getattr(self.env, "config", None), "executable", "docker")
        if container_id:
            try:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                    f.write(HELPER_SCRIPT)
                    tmp_path = f.name
                subprocess.run(
                    [docker_exe, "cp", tmp_path, f"{container_id}:/tmp/graphplan_helper.py"],
                    capture_output=True, text=True, timeout=30, check=True,
                )
                import os
                os.unlink(tmp_path)
                self._scripts_deployed = True
                return
            except Exception as e:
                logger.warning(f"docker cp failed ({e}), falling back to chunked write")
        # Fallback: write in chunks via shell to stay under ARG_MAX
        chunk_size = 48000  # well under typical 128KB ARG_MAX
        for i in range(0, len(HELPER_SCRIPT), chunk_size):
            chunk = HELPER_SCRIPT[i:i + chunk_size].replace("'", "'\\''")
            op = ">" if i == 0 else ">>"
            result = self.env.execute({"command": f"printf '%s' '{chunk}' {op} /tmp/graphplan_helper.py"})
            if result.get("returncode", -1) != 0:
                logger.warning(f"Failed to deploy helper script chunk: {result.get('output', '')[:200]}")
                return
        self._scripts_deployed = True

    def _explore_phase(self) -> list[str]:
        """Run step() in a loop for exploration. Returns file list or empty.

        Catches InterruptAgentFlow (e.g., FormatError for no tool calls)
        so that exploration continues even when the LLM occasionally produces
        a response without tool calls.
        """
        # Track where messages start so we only scan new messages (not system/instance templates)
        msg_start_idx = len(self.messages)

        for step_num in range(self.config.max_explore_steps):
            try:
                self.step()
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
                # Check if this was a submission/exit
                if self.messages[-1].get("role") == "exit":
                    return []
                # Otherwise continue exploring (e.g., FormatError)

            if self.messages[-1].get("role") == "exit":
                return []

            # Check only NEW messages (added during step loop) for READY_TO_PLAN marker.
            # Skip system/user template messages which may contain examples of the marker.
            for msg in reversed(self.messages[msg_start_idx:]):
                # Only check assistant messages and tool/observation outputs
                role = msg.get("role", msg.get("type", ""))
                if role in ("system", "user"):
                    continue
                for text in self._extract_all_text(msg):
                    match = READY_TO_PLAN_PATTERN.search(text)
                    if match:
                        files = self._parse_file_list(match.group(0))
                        if files:
                            logger.info(f"READY_TO_PLAN detected at step {step_num} with files: {files}")
                            return files

        logger.warning(f"Exploration phase exhausted {self.config.max_explore_steps} steps without READY_TO_PLAN")
        return []

    @staticmethod
    def _extract_all_text(msg: dict) -> list[str]:
        """Extract all text fields from a message (content, output, etc.)."""
        texts = []
        for key in ("content", "output"):
            val = msg.get(key, "")
            if isinstance(val, str) and val:
                texts.append(val)
            elif isinstance(val, list):
                texts.append(" ".join(
                    item.get("text", "") for item in val if isinstance(item, dict)
                ))
        return texts

    def _parse_file_list(self, text: str) -> list[str]:
        """Extract file paths from READY_TO_PLAN: [...] marker."""
        match = READY_TO_PLAN_PATTERN.search(text)
        if not match:
            return []
        inner = match.group(1).strip()
        if not inner:
            return []
        # Strip escaped quotes that LLMs sometimes produce (e.g., \"file.py\")
        cleaned = inner.replace('\\"', '"').replace("\\'", "'")
        try:
            # Try JSON parse first
            files = json.loads("[" + cleaned + "]")
            return [f.strip() for f in files if isinstance(f, str) and f.strip()]
        except json.JSONDecodeError:
            # Fallback: split by comma, strip quotes and backslashes
            parts = cleaned.split(",")
            return [p.strip().strip("'\"\\") for p in parts if p.strip().strip("'\"\\")]

    def _build_code_graph(self, files: list[str]) -> tuple[str, str]:
        """Build code graph from files. Returns (graph_json, graph_view)."""
        file_args = " ".join(shlex.quote(f) for f in files)
        logger.info(f"Building code graph for files: {files}")

        result = self.env.execute({"command": f"python3 /tmp/graphplan_helper.py build_graph {file_args}"})

        if result.get("returncode", -1) != 0:
            logger.warning(f"Graph build failed (rc={result.get('returncode')}): {result.get('output', '')[:500]}")
            return "{}", ""

        graph_json = result.get("output", "").strip()
        logger.info(f"Graph JSON length: {len(graph_json)}")
        try:
            graph_data = json.loads(graph_json)
        except json.JSONDecodeError as e:
            logger.warning(f"Graph JSON parse failed: {e}, output: {graph_json[:200]}")
            return "{}", ""

        # Log any errors from the graph build
        graph_errors = graph_data.get("errors", [])
        if graph_errors:
            for err in graph_errors:
                logger.warning(f"Graph build error: {err}")
            _console.print(f"[yellow]Graph build had {len(graph_errors)} error(s):[/yellow]")
            for err in graph_errors[:5]:
                _console.print(f"  [yellow]- {err}[/yellow]")

        # Build a compact text view for the LLM
        view_lines = []
        for fp in sorted(set(s["file"] for s in graph_data.get("symbols", []))):
            view_lines.append(f"FILE: {fp}")
            file_imports = [i for i in graph_data.get("imports", []) if i["file"] == fp]
            for imp in file_imports:
                if imp.get("symbol"):
                    view_lines.append(f"  IMPORT: from {imp['module']} import {imp['symbol']} [line {imp['line']}]")
                else:
                    view_lines.append(f"  IMPORT: import {imp['module']} [line {imp['line']}]")
            file_syms = [s for s in graph_data.get("symbols", []) if s["file"] == fp]
            for sym in sorted(file_syms, key=lambda s: s["start_line"]):
                kind = sym["kind"].upper()
                view_lines.append(f"  {kind}: {sym['name']} (lines {sym['start_line']}-{sym['end_line']})")
            view_lines.append("")

        return graph_json, "\n".join(view_lines)

    def _generate_plan(self, graph_view: str, task: str) -> str:
        """Ask LLM to generate an edit plan via a step loop.

        The LLM must produce bash tool calls (required by LitellmModel),
        so we instruct it to write the plan to a file via bash, then read
        it back from the container.
        """
        # Clean up any stale plan file
        self.env.execute({"command": "rm -f /tmp/edit_plan.json"})

        plan_prompt = (
            f"Based on your exploration, here is the structural view of the relevant code:\n\n"
            f"```\n{graph_view}```\n\n"
            f"{OPERATOR_CATALOG_PROMPT}\n\n"
            f"Generate a plan to fix the issue.\n\n"
            f"IMPORTANT: You MUST write your plan as a JSON array to /tmp/edit_plan.json "
            f"using a bash command like:\n"
            f"```bash\n"
            f"cat > /tmp/edit_plan.json << 'PLAN_EOF'\n"
            f'[{{"op": "replace_code", "params": {{"file": "...", "pattern": "...", "replacement": "..."}}}}]\n'
            f"PLAN_EOF\n"
            f"```\n"
            f"The file MUST contain a valid JSON array of operator steps. "
            f"Do NOT just echo the plan — write it to /tmp/edit_plan.json."
        )

        self.add_messages(self.model.format_message(role="user", content=plan_prompt))

        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                self.step()
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
                if self.messages[-1].get("role") == "exit":
                    return "[]"
                continue

            if self.messages[-1].get("role") == "exit":
                return "[]"

            # Check if the plan file was written
            plan_json = self._read_plan_file()
            if plan_json:
                logger.info(f"Plan generated via file on attempt {attempt + 1}")
                return plan_json

            # Fallback: scan recent messages for plan JSON in content
            plan_json = self._scan_messages_for_plan()
            if plan_json:
                logger.info(f"Plan extracted from message content on attempt {attempt + 1}")
                return plan_json

            # Nudge the LLM to write the file
            if attempt < max_attempts - 1:
                nudge = (
                    "I don't see a valid JSON plan in /tmp/edit_plan.json yet. "
                    "Please write your complete JSON plan array to /tmp/edit_plan.json "
                    "using: cat > /tmp/edit_plan.json << 'PLAN_EOF'\n"
                    "[...your operator steps...]\n"
                    "PLAN_EOF"
                )
                self.add_messages(self.model.format_message(role="user", content=nudge))

        logger.warning("Failed to generate plan after max attempts")
        return "[]"

    @staticmethod
    def _is_valid_plan(plan) -> bool:
        """Check that parsed JSON looks like a plan.

        Accepts:
          - List of op dicts: [{"op": ..., "params": ...}, ...]
          - Object with plan key: {"define_operators": [...], "plan": [...]}
        """
        if isinstance(plan, dict):
            # Object format: {"define_operators": [...], "plan": [...]}
            plan_steps = plan.get("plan", [])
            if isinstance(plan_steps, list) and len(plan_steps) > 0:
                return isinstance(plan_steps[0], dict) and "op" in plan_steps[0]
            return False
        if not isinstance(plan, list) or len(plan) == 0:
            return False
        # At least the first element must be a dict with an "op" key
        return isinstance(plan[0], dict) and "op" in plan[0]

    def _read_plan_file(self) -> str:
        """Read and validate the plan file from the container."""
        result = self.env.execute({"command": "cat /tmp/edit_plan.json 2>/dev/null"})
        if result.get("returncode", -1) != 0:
            return ""
        content = result.get("output", "").strip()
        if not content:
            return ""
        try:
            plan = json.loads(content)
            if self._is_valid_plan(plan):
                return content
        except json.JSONDecodeError:
            pass
        return ""

    def _scan_messages_for_plan(self) -> str:
        """Scan recent messages for a JSON plan in text content."""
        for msg in reversed(self.messages[-6:]):
            for text in self._extract_all_text(msg):
                extracted = self._extract_plan_json_from_text(text)
                if extracted and extracted != "[]":
                    return extracted
        return ""

    def _extract_plan_json(self, response: dict) -> str:
        """Extract JSON plan from LLM response."""
        content = response.get("content", "")
        if isinstance(content, list):
            content = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
        return self._extract_plan_json_from_text(str(content))

    @staticmethod
    def _extract_plan_json_from_text(text: str) -> str:
        """Extract JSON plan from raw text.

        Accepts both array format [{"op": ...}] and object format
        {"define_operators": [...], "plan": [...]}.

        Validates that the result contains plan step dicts (with "op" key)
        to avoid accidentally picking up file lists or other JSON.
        """
        # Try ```json ... ``` first (handles both array and object)
        match = PLAN_JSON_PATTERN.search(text)
        if match:
            candidate = match.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if GraphPlanAgent._is_valid_plan(parsed):
                    return candidate
            except json.JSONDecodeError:
                pass

        # Try object format with ```json {...} ```
        obj_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if obj_match:
            candidate = obj_match.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if GraphPlanAgent._is_valid_plan(parsed):
                    return candidate
            except json.JSONDecodeError:
                pass

        # Fallback: find raw JSON array
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            candidate = text[start:end + 1]
            try:
                parsed = json.loads(candidate)
                if GraphPlanAgent._is_valid_plan(parsed):
                    return candidate
            except json.JSONDecodeError:
                pass

        # Fallback: find raw JSON object with "plan" key
        obj_start = text.find("{")
        obj_end = text.rfind("}")
        if obj_start >= 0 and obj_end > obj_start:
            candidate = text[obj_start:obj_end + 1]
            try:
                parsed = json.loads(candidate)
                if GraphPlanAgent._is_valid_plan(parsed):
                    return candidate
            except json.JSONDecodeError:
                pass

        return "[]"

    def _verify_plan(self, plan_json: str, graph_json: str) -> tuple[bool, list[str]]:
        """Verify plan preconditions against graph."""
        escaped_plan = shlex.quote(plan_json)
        escaped_graph = shlex.quote(graph_json)
        result = self.env.execute({
            "command": f"python3 /tmp/graphplan_helper.py verify_plan {escaped_plan} {escaped_graph}"
        })

        if result.get("returncode", -1) != 0:
            return False, [f"Verification script error: {result.get('output', '')[:200]}"]

        try:
            verification = json.loads(result.get("output", "").strip())
            warnings = verification.get("warnings", [])
            for warn in warnings:
                _console.print(f"  [yellow]WARNING: {warn}[/yellow]", highlight=False)
            return verification.get("passed", False), verification.get("errors", [])
        except json.JSONDecodeError:
            return False, ["Failed to parse verification result"]

    def _execute_plan(self, plan_json: str) -> tuple[bool, list[str]]:
        """Execute each step of the plan. Returns (all_succeeded, errors)."""
        try:
            plan_data = json.loads(plan_json)
        except json.JSONDecodeError:
            return False, ["Invalid plan JSON"]

        # Extract steps and custom operators from both formats
        if isinstance(plan_data, dict):
            steps = plan_data.get("plan", [])
            custom_operators = plan_data.get("define_operators", [])
        elif isinstance(plan_data, list):
            steps = plan_data
            custom_operators = []
        else:
            return False, ["Invalid plan format"]

        errors = []
        custom_ops_json = shlex.quote(json.dumps(custom_operators)) if custom_operators else ""

        for i, step in enumerate(steps):
            op = step.get("op", "?")
            target = step.get("params", {}).get("file", "?")
            if target == "?":
                target = step.get("params", {}).get("locator", {}).get("file", "?")
            _console.print(f"  [{i+1}/{len(steps)}] [cyan]{op}[/cyan] on [bold]{target}[/bold]", highlight=False)
            step_json = shlex.quote(json.dumps(step))
            cmd = f"python3 /tmp/graphplan_helper.py execute_step {step_json}"
            if custom_ops_json:
                cmd += f" {custom_ops_json}"
            result = self.env.execute({"command": cmd})
            if result.get("returncode", -1) != 0:
                error_msg = f"Step {i} ({op}) failed: {result.get('output', '')[:200]}"
                errors.append(error_msg)
                _console.print(f"    [red]FAILED: {result.get('output', '')[:200]}[/red]", highlight=False)
                break
            else:
                _console.print(f"    [green]OK[/green]", highlight=False)

        return len(errors) == 0, errors

    def _plan_and_execute(self, plan_files: list[str], task: str):
        """Phase 2: Build graph, generate plan, verify, execute."""
        self._print_phase("GRAPH", f"building AST for {len(plan_files)} file(s)")
        graph_json, graph_view = self._build_code_graph(plan_files)

        if not graph_view:
            _console.print("[yellow]Empty graph view, falling back to step loop[/yellow]")
            return

        _console.print(f"[green]Graph built: {len(graph_view)} chars[/green]")

        # Generate plan
        self._print_phase("PLAN", "generating edit plan")
        plan_json = self._generate_plan(graph_view, task)
        if plan_json == "[]":
            _console.print("[yellow]Empty plan generated, falling back[/yellow]")
            return

        _console.print(f"[green]Plan generated ({len(plan_json)} chars)[/green]")

        # Save plan info for trajectory
        try:
            plan_steps = json.loads(plan_json)
        except json.JSONDecodeError:
            plan_steps = []
        self._plan_info = {
            "plan_files": plan_files,
            "graph_view": graph_view,
            "plan_json": plan_json,
            "plan_steps": plan_steps,
        }

        # Verify + revision loop
        self._print_phase("VERIFY", "checking plan against graph")
        passed, errors = self._verify_plan(plan_json, graph_json)
        for attempt in range(self.config.max_plan_revisions):
            if passed:
                break
            _console.print(f"[yellow]Verification failed (attempt {attempt + 1}): {errors}[/yellow]")
            plan_json = self._revise_plan(plan_json, errors)
            passed, errors = self._verify_plan(plan_json, graph_json)

        if not passed:
            _console.print(f"[red]Plan verification failed after {self.config.max_plan_revisions} revisions[/red]")
            return

        _console.print("[green]Plan verified successfully[/green]")

        # Checkpoint before execution
        self.env.execute({"command": "git stash --include-untracked"})

        # Execute
        self._print_phase("EXECUTE", "applying operators")
        success, exec_errors = self._execute_plan(plan_json)

        if not success:
            _console.print(f"[yellow]Execution failed: {exec_errors}, attempting revision[/yellow]")
            self.env.execute({"command": "git stash pop"})
            plan_json = self._revise_plan(plan_json, exec_errors)
            passed, errors = self._verify_plan(plan_json, graph_json)
            if passed:
                self.env.execute({"command": "git stash --include-untracked"})
                success, exec_errors = self._execute_plan(plan_json)
                if not success:
                    _console.print(f"[red]Execution failed again: {exec_errors}[/red]")
                    self.env.execute({"command": "git stash pop"})
                    return
            else:
                _console.print("[red]Revised plan failed verification[/red]")
                return

        _console.print("[green]Plan executed successfully[/green]")

        # Plan executed successfully - now validate and submit
        self._print_phase("SUBMIT", "reviewing and submitting patch")
        self._validate_and_submit(plan_files)

    def _revise_plan(self, plan_json: str, errors: list[str]) -> str:
        """Ask LLM to revise the plan based on errors via a step loop."""
        self.env.execute({"command": "rm -f /tmp/edit_plan.json"})

        error_msg = "\n".join(f"- {e}" for e in errors)
        revision_prompt = (
            f"Your plan had errors:\n{error_msg}\n\n"
            f"Current plan:\n```json\n{plan_json}\n```\n\n"
            f"Revise the plan to fix these errors. Write the corrected JSON array "
            f"to /tmp/edit_plan.json using:\n"
            f"```bash\n"
            f"cat > /tmp/edit_plan.json << 'PLAN_EOF'\n"
            f"[...corrected steps...]\n"
            f"PLAN_EOF\n"
            f"```"
        )
        self.add_messages(self.model.format_message(role="user", content=revision_prompt))

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                self.step()
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
                if self.messages[-1].get("role") == "exit":
                    return "[]"
                continue

            if self.messages[-1].get("role") == "exit":
                return "[]"

            revised = self._read_plan_file()
            if revised:
                return revised

            revised = self._scan_messages_for_plan()
            if revised:
                return revised

        return "[]"

    def _validate_and_submit(self, plan_files: list[str]):
        """Ask LLM to create patch and submit."""
        file_list = " ".join(shlex.quote(f) for f in plan_files)
        submit_prompt = (
            f"The edit plan has been executed successfully on these files: {', '.join(plan_files)}\n\n"
            f"Now:\n"
            f"1. Review the changes with: git diff -- {file_list}\n"
            f"2. If the changes look correct, create patch and submit:\n"
            f"   git diff -- {file_list} > patch.txt\n"
            f"   Then verify patch.txt, then submit with:\n"
            f"   echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt\n\n"
            f"If the changes need adjustments, you can make additional edits before submitting."
        )
        self.add_messages(self.model.format_message(role="user", content=submit_prompt))
        # Continue with step loop to let LLM review and submit
        while True:
            try:
                self.step()
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
            except Exception as e:
                self.handle_uncaught_exception(e)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                break

    def _fallback_step_loop(self) -> dict:
        """Continue with the normal DefaultAgent step loop for graceful degradation."""
        self._print_phase("FALLBACK", "standard step loop")
        while True:
            try:
                self.step()
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
            except Exception as e:
                self.handle_uncaught_exception(e)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                break
        return self.messages[-1].get("extra", {})

    def _verify_submission(self, info: dict) -> dict:
        """Print a post-run summary and verify submission patch format."""
        self._print_phase("RESULT", "verifying submission")

        exit_status = info.get("exit_status", "unknown")
        submission = info.get("submission", "")
        sub_bytes = len(submission.encode("utf-8")) if submission else 0

        # Summary table
        _console.print(f"  [bold]Exit status:[/bold]  {exit_status}")
        _console.print(f"  [bold]API calls:[/bold]    {self.n_calls}")
        _console.print(f"  [bold]Total cost:[/bold]   ${self.cost:.4f}")
        _console.print(f"  [bold]Patch size:[/bold]   {sub_bytes} bytes")

        if exit_status != "Submitted":
            _console.print(f"\n  [red bold]FAIL:[/red bold] Agent did not submit (status: {exit_status})")
            return info

        if sub_bytes == 0:
            _console.print(f"\n  [red bold]FAIL:[/red bold] Submission is empty (0 bytes)")
            return info

        # Check patch format
        checks = []
        has_diff_header = submission.lstrip().startswith("diff --git")
        checks.append(("diff --git header", has_diff_header))

        has_minus = "--- a/" in submission or "--- /dev/null" in submission
        has_plus = "+++ b/" in submission or "+++ /dev/null" in submission
        checks.append(("--- a/ / +++ b/ markers", has_minus and has_plus))

        has_hunks = "@@" in submission
        checks.append(("@@ hunk markers", has_hunks))

        has_changes = any(line.startswith("+") and not line.startswith("+++") for line in submission.splitlines())
        checks.append(("contains additions (+)", has_changes))

        all_passed = True
        for label, passed in checks:
            icon = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
            _console.print(f"  {icon}  {label}")
            if not passed:
                all_passed = False

        # Try applying the patch in the container (dry-run)
        try:
            self.env.execute({"command": f"cat > /tmp/verify_patch.diff << 'VERIFY_EOF'\n{submission}\nVERIFY_EOF"})
            apply_result = self.env.execute({"command": "cd /testbed && git apply --check /tmp/verify_patch.diff 2>&1"})
            rc = apply_result.get("returncode", -1)
            output = apply_result.get("output", "").strip()
            if rc == 0:
                _console.print(f"  [green]PASS[/green]  git apply --check (patch applies cleanly)")
            else:
                _console.print(f"  [yellow]WARN[/yellow]  git apply --check failed: {output[:200]}")
        except Exception:
            _console.print(f"  [dim]SKIP[/dim]  git apply --check (environment unavailable)")

        # Show patch preview
        lines = submission.strip().splitlines()
        if len(lines) <= 30:
            _console.print(f"\n  [dim]Full patch ({len(lines)} lines):[/dim]")
            for line in lines:
                _console.print(f"  {line}", highlight=False, markup=False)
        else:
            _console.print(f"\n  [dim]Patch preview ({len(lines)} lines total):[/dim]")
            for line in lines[:10]:
                _console.print(f"  {line}", highlight=False, markup=False)
            _console.print(f"  [dim]... {len(lines) - 20} lines omitted ...[/dim]")
            for line in lines[-10:]:
                _console.print(f"  {line}", highlight=False, markup=False)

        if all_passed:
            _console.print(f"\n  [green bold]OK:[/green bold] Submission looks valid ({sub_bytes} bytes)")
        else:
            _console.print(f"\n  [yellow bold]WARN:[/yellow bold] Submission has format issues")

        return info

