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

from rich.console import Console
from rich.rule import Rule

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.agents.graph_plan_scripts import HELPER_SCRIPT
from minisweagent.exceptions import InterruptAgentFlow
from minisweagent.models.utils.content_string import get_content_string

logger = logging.getLogger("minisweagent.graph_plan")
_console = Console(highlight=False)

OPERATOR_CATALOG_PROMPT = """## Available Edit Operators

### Tier 1 (Essential - use these first):
  `replace_code(file, pattern, replacement)` - Replace a code pattern with new code
    Example: {"op": "replace_code", "params": {"file": "query.py", "pattern": "if self.deferred:", "replacement": "if self.deferred and not self._select:"}}
  `insert_code(file, anchor_line, position, code)` - Insert code before or after a specific line
    Example: {"op": "insert_code", "params": {"file": "query.py", "anchor_line": 847, "position": "after", "code": "    self._clear_cache()"}}
  `delete_lines(file, start_line, end_line)` - Delete lines from start_line to end_line (inclusive)
    Example: {"op": "delete_lines", "params": {"file": "query.py", "start_line": 50, "end_line": 53}}
  `add_method(file, class_name, method_code)` - Add a new method to an existing class
    Example: {"op": "add_method", "params": {"file": "query.py", "class_name": "QuerySet", "method_code": "    def _clear_cache(self):\\n        self._cache = {}"}}
  `add_import(file, import_statement)` - Add an import statement to the top of a file
    Example: {"op": "add_import", "params": {"file": "query.py", "import_statement": "from collections import OrderedDict"}}
  `modify_function_signature(file, func_name, old_signature, new_signature)` - Change a function's parameter list
    Example: {"op": "modify_function_signature", "params": {"file": "query.py", "func_name": "defer", "old_signature": "def defer(self, *fields)", "new_signature": "def defer(self, *fields, clear_cache=True)"}}

### Tier 2 (Structural - for complex multi-step fixes):
  `rename_symbol(file, old_name, new_name)` - Rename a variable/function/class and update all references in scope
    Example: {"op": "rename_symbol", "params": {"file": "query.py", "old_name": "_deferred", "new_name": "_deferred_fields"}}
  `wrap_block(file, start_line, end_line, before_code, after_code)` - Wrap lines in a block structure (try/except, if/else, etc.)
    Example: {"op": "wrap_block", "params": {"file": "query.py", "start_line": 50, "end_line": 55, "before_code": "    try:", "after_code": "    except ValueError:\\n        pass"}}
  `add_class_attribute(file, class_name, attribute_code)` - Add a class-level attribute
    Example: {"op": "add_class_attribute", "params": {"file": "query.py", "class_name": "QuerySet", "attribute_code": "    _deferred_cache = None"}}
  `replace_function_body(file, func_name, new_body)` - Replace the entire body of a function
    Example: {"op": "replace_function_body", "params": {"file": "query.py", "func_name": "_clear_cache", "new_body": "        self._cache = {}\\n        self._result_cache = None"}}

Output your plan as JSON: [{"op": "name", "params": {...}}, ...]"""

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
        self._instance_data: dict = {}  # SWE-bench instance data for test evaluation
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

    def run(self, task="", *, instance: dict | None = None, **kwargs) -> dict:
        """Run the agent with phased approach: explore -> plan -> execute.

        Args:
            task: The problem statement / task description.
            instance: Optional SWE-bench instance dict with FAIL_TO_PASS,
                      PASS_TO_PASS, and test_patch fields for post-run evaluation.
        """
        if instance:
            self._instance_data = instance
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
        result = self.env.execute({"command": f"cat > /tmp/graphplan_helper.py << 'GRAPHPLAN_EOF'\n{HELPER_SCRIPT}\nGRAPHPLAN_EOF"})
        if result.get("returncode", -1) != 0:
            logger.warning(f"Failed to deploy helper script: {result.get('output', '')[:200]}")
        else:
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
        try:
            # Try JSON parse first
            files = json.loads("[" + inner + "]")
            return [f.strip() for f in files if isinstance(f, str) and f.strip()]
        except json.JSONDecodeError:
            # Fallback: split by comma, strip quotes
            parts = inner.split(",")
            return [p.strip().strip("'\"") for p in parts if p.strip().strip("'\"")]

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
    def _is_valid_plan(plan: list) -> bool:
        """Check that a parsed JSON array looks like a plan (list of op dicts)."""
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
        """Extract JSON plan array from raw text.

        Validates that the array contains plan step dicts (with "op" key)
        to avoid accidentally picking up file lists or other JSON arrays.
        """
        # Try ```json [...] ``` first
        match = PLAN_JSON_PATTERN.search(text)
        if match:
            candidate = match.group(1).strip()
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
            return verification.get("passed", False), verification.get("errors", [])
        except json.JSONDecodeError:
            return False, ["Failed to parse verification result"]

    def _execute_plan(self, plan_json: str) -> tuple[bool, list[str]]:
        """Execute each step of the plan. Returns (all_succeeded, errors)."""
        try:
            steps = json.loads(plan_json)
        except json.JSONDecodeError:
            return False, ["Invalid plan JSON"]

        errors = []
        for i, step in enumerate(steps):
            op = step.get("op", "?")
            target = step.get("params", {}).get("file", "?")
            _console.print(f"  [{i+1}/{len(steps)}] [cyan]{op}[/cyan] on [bold]{target}[/bold]", highlight=False)
            step_json = shlex.quote(json.dumps(step))
            result = self.env.execute({
                "command": f"python3 /tmp/graphplan_helper.py execute_step {step_json}"
            })
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
        """Print a post-run summary and verify the submission with SWE-bench tests."""
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

        # Run SWE-bench test evaluation if instance data is available
        if self._instance_data:
            self._run_swebench_tests(submission)
        else:
            _console.print(f"\n  [dim]SKIP  SWE-bench test eval (no instance data)[/dim]")

        if all_passed:
            _console.print(f"\n  [green bold]OK:[/green bold] Submission looks valid ({sub_bytes} bytes)")
        else:
            _console.print(f"\n  [yellow bold]WARN:[/yellow bold] Submission has format issues")

        return info

    def _run_swebench_tests(self, submission: str):
        """Run FAIL_TO_PASS and PASS_TO_PASS tests inside the container."""
        fail_to_pass_raw = self._instance_data.get("FAIL_TO_PASS", "[]")
        pass_to_pass_raw = self._instance_data.get("PASS_TO_PASS", "[]")

        try:
            fail_to_pass = json.loads(fail_to_pass_raw) if isinstance(fail_to_pass_raw, str) else fail_to_pass_raw
            pass_to_pass = json.loads(pass_to_pass_raw) if isinstance(pass_to_pass_raw, str) else pass_to_pass_raw
        except json.JSONDecodeError:
            _console.print(f"  [yellow]WARN[/yellow]  Could not parse test lists")
            return

        if not fail_to_pass:
            _console.print(f"  [dim]SKIP  No FAIL_TO_PASS tests in instance data[/dim]")
            return

        self._print_phase("TEST EVAL", f"{len(fail_to_pass)} fail_to_pass, {len(pass_to_pass)} pass_to_pass")

        try:
            # Reset to clean state and apply the patch
            _console.print(f"  [dim]Resetting to base commit and applying patch...[/dim]")
            self.env.execute({"command": "cd /testbed && git checkout -- . && git clean -fd"})
            self.env.execute({"command": f"cat > /tmp/eval_patch.diff << 'EVAL_EOF'\n{submission}\nEVAL_EOF"})
            apply_result = self.env.execute({"command": "cd /testbed && git apply /tmp/eval_patch.diff 2>&1"})
            if apply_result.get("returncode", -1) != 0:
                _console.print(f"  [red]FAIL[/red]  Could not apply patch: {apply_result.get('output', '')[:200]}")
                return

            # Apply test_patch if present (some instances need test changes)
            test_patch = self._instance_data.get("test_patch", "")
            if test_patch:
                self.env.execute({"command": f"cat > /tmp/test_patch.diff << 'TEST_EOF'\n{test_patch}\nTEST_EOF"})
                tp_result = self.env.execute({"command": "cd /testbed && git apply /tmp/test_patch.diff 2>&1"})
                if tp_result.get("returncode", -1) != 0:
                    _console.print(f"  [yellow]WARN[/yellow]  test_patch failed to apply (may already be present)")

            # Run FAIL_TO_PASS tests
            _console.print(f"\n  [bold]FAIL_TO_PASS tests ({len(fail_to_pass)}):[/bold]")
            f2p_passed = 0
            for test_id in fail_to_pass:
                result = self._run_single_test(test_id)
                if result:
                    f2p_passed += 1
                    _console.print(f"    [green]PASS[/green]  {test_id}")
                else:
                    _console.print(f"    [red]FAIL[/red]  {test_id}")

            # Run PASS_TO_PASS tests (sample if too many)
            p2p_passed = 0
            p2p_total = len(pass_to_pass)
            if p2p_total > 0:
                # Run all if <= 20, otherwise sample
                tests_to_run = pass_to_pass if p2p_total <= 20 else pass_to_pass[:20]
                _console.print(f"\n  [bold]PASS_TO_PASS tests ({len(tests_to_run)}/{p2p_total}):[/bold]")
                for test_id in tests_to_run:
                    result = self._run_single_test(test_id)
                    if result:
                        p2p_passed += 1
                        _console.print(f"    [green]PASS[/green]  {test_id}")
                    else:
                        _console.print(f"    [red]FAIL[/red]  {test_id}")
                p2p_total_run = len(tests_to_run)
            else:
                p2p_total_run = 0

            # Resolution status
            _console.print()
            f2p_pct = (f2p_passed / len(fail_to_pass) * 100) if fail_to_pass else 0
            p2p_pct = (p2p_passed / p2p_total_run * 100) if p2p_total_run > 0 else 100

            _console.print(f"  [bold]FAIL_TO_PASS:[/bold] {f2p_passed}/{len(fail_to_pass)} ({f2p_pct:.0f}%)")
            _console.print(f"  [bold]PASS_TO_PASS:[/bold] {p2p_passed}/{p2p_total_run} ({p2p_pct:.0f}%)")

            if f2p_pct == 100 and p2p_pct == 100:
                _console.print(f"\n  [green bold]RESOLVED[/green bold]  All tests pass!")
            elif f2p_pct > 0 and p2p_pct == 100:
                _console.print(f"\n  [yellow bold]PARTIAL[/yellow bold]  Some FAIL_TO_PASS tests still failing")
            else:
                _console.print(f"\n  [red bold]NOT RESOLVED[/red bold]  Tests failing")

        except Exception as e:
            _console.print(f"  [yellow]WARN[/yellow]  Test evaluation error: {e}")

    def _run_single_test(self, test_id: str, timeout: int = 120) -> bool:
        """Run a single test in the container. Returns True if passed."""
        # Handle both pytest-style (file::class::method) and unittest-style test IDs
        result = self.env.execute({
            "command": f"cd /testbed && python -m pytest -xvs {test_id} 2>&1 | tail -20",
            "timeout": timeout,
        })
        output = result.get("output", "")
        rc = result.get("returncode", -1)
        # pytest returns 0 on all pass, 1 on failures
        if rc == 0:
            return True
        # Also check output for "passed" in case returncode is unreliable
        if " passed" in output and " failed" not in output and " error" not in output.lower():
            return True
        return False
