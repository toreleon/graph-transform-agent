#!/usr/bin/env python3

"""Run mini-SWE-agent on SWE-bench instances in batch mode."""
# Read this first: https://mini-swe-agent.com/latest/usage/swebench/  (usage docs)

import concurrent.futures
import json
import random
import re
import threading
import time
import traceback
from pathlib import Path

import typer
from jinja2 import StrictUndefined, Template
from rich.live import Live

from rich.console import Console

from minisweagent import Environment
from minisweagent.agents import get_agent_class
from minisweagent.agents.default import DefaultAgent
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager
from minisweagent.utils.log import add_file_handler, logger
from minisweagent.utils.serialize import UNSET, recursive_merge

_console = Console(highlight=False)

_HELP_TEXT = """Run mini-SWE-agent on SWEBench instances.

[not dim]
More information about the usage: [bold green]https://mini-swe-agent.com/latest/usage/swebench/[/bold green]
[/not dim]
"""

_CONFIG_SPEC_HELP_TEXT = """Path to config files, filenames, or key-value pairs.

[bold red]IMPORTANT:[/bold red] [red]If you set this option, the default config file will not be used.[/red]
So you need to explicitly set it e.g., with [bold green]-c swebench.yaml <other options>[/bold green]

Multiple configs will be recursively merged.

Examples:

[bold red]-c model.model_kwargs.temperature=0[/bold red] [red]You forgot to add the default config file! See above.[/red]

[bold green]-c swebench.yaml -c model.model_kwargs.temperature=0.5[/bold green]

[bold green]-c swebench.yaml -c agent.max_iterations=50[/bold green]
"""

DEFAULT_CONFIG_FILE = builtin_config_dir / "benchmarks" / "swebench.yaml"

DATASET_MAPPING = {
    "full": "princeton-nlp/SWE-Bench",
    "verified": "princeton-nlp/SWE-Bench_Verified",
    "lite": "princeton-nlp/SWE-Bench_Lite",
    "multimodal": "princeton-nlp/SWE-Bench_Multimodal",
    "multilingual": "swe-bench/SWE-Bench_Multilingual",
    "smith": "SWE-bench/SWE-smith",
    "_test": "klieret/swe-bench-dummy-test-dataset",
    "rebench": "nebius/SWE-rebench",
}

app = typer.Typer(rich_markup_mode="rich", add_completion=False)
_OUTPUT_FILE_LOCK = threading.Lock()


def _make_progress_tracking_class(base_class: type) -> type:
    """Create a progress-tracking subclass of any agent class."""

    class ProgressTrackingAgent(base_class):
        """Wrapper that provides progress updates for batch runs."""

        def __init__(self, *args, progress_manager: RunBatchProgressManager, instance_id: str = "", **kwargs):
            super().__init__(*args, **kwargs)
            self.progress_manager: RunBatchProgressManager = progress_manager
            self.instance_id = instance_id

        def step(self) -> dict:
            """Override step to provide progress updates."""
            self.progress_manager.update_instance_status(
                self.instance_id, f"Step {self.n_calls + 1:3d} (${self.cost:.2f})"
            )
            return super().step()

    ProgressTrackingAgent.__name__ = f"ProgressTracking{base_class.__name__}"
    ProgressTrackingAgent.__qualname__ = f"ProgressTracking{base_class.__name__}"
    return ProgressTrackingAgent


def get_swebench_docker_image_name(instance: dict) -> str:
    """Get the image name for a SWEBench instance."""
    image_name = instance.get("image_name", None) or instance.get("docker_image", None)
    if image_name is None:
        # Docker doesn't allow double underscore, so we replace them with a magic token
        iid = instance["instance_id"]
        id_docker_compatible = iid.replace("__", "_1776_")
        image_name = f"docker.io/swebench/sweb.eval.x86_64.{id_docker_compatible}:latest".lower()
    return image_name


def get_sb_environment(config: dict, instance: dict) -> Environment:
    env_config = config.setdefault("environment", {})
    env_config["environment_class"] = env_config.get("environment_class", "docker")
    image_name = get_swebench_docker_image_name(instance)
    if env_config["environment_class"] in ["docker", "swerex_modal"]:
        env_config["image"] = image_name
    elif env_config["environment_class"] in ["singularity", "contree"]:
        env_config["image"] = "docker://" + image_name

    env = get_environment(env_config)
    if startup_command := config.get("run", {}).get("env_startup_command"):
        startup_command = Template(startup_command, undefined=StrictUndefined).render(**instance)
        out = env.execute(startup_command)
        if out["returncode"] != 0:
            raise RuntimeError(f"Error executing startup command: {out}")
    return env


def update_preds_file(output_path: Path, instance_id: str, model_name: str, result: str):
    """Update the output JSON file with results from a single instance."""
    with _OUTPUT_FILE_LOCK:
        output_data = {}
        if output_path.exists():
            output_data = json.loads(output_path.read_text())
        output_data[instance_id] = {
            "model_name_or_path": model_name,
            "instance_id": instance_id,
            "model_patch": result,
        }
        output_path.write_text(json.dumps(output_data, indent=2))


def remove_from_preds_file(output_path: Path, instance_id: str):
    """Remove an instance from the predictions file."""
    if not output_path.exists():
        return
    with _OUTPUT_FILE_LOCK:
        output_data = json.loads(output_path.read_text())
        if instance_id in output_data:
            del output_data[instance_id]
            output_path.write_text(json.dumps(output_data, indent=2))


def evaluate_submission(env: Environment, instance: dict, submission: str) -> dict | None:
    """Run FAIL_TO_PASS and PASS_TO_PASS tests after the agent has finished.

    This is a post-task evaluation only -- results are logged but never fed
    back to the agent.

    Returns a results dict or None if tests could not be run.
    """
    fail_to_pass_raw = instance.get("FAIL_TO_PASS", "[]")
    pass_to_pass_raw = instance.get("PASS_TO_PASS", "[]")

    try:
        fail_to_pass = json.loads(fail_to_pass_raw) if isinstance(fail_to_pass_raw, str) else fail_to_pass_raw
        pass_to_pass = json.loads(pass_to_pass_raw) if isinstance(pass_to_pass_raw, str) else pass_to_pass_raw
    except json.JSONDecodeError:
        logger.warning("Could not parse test lists for %s", instance.get("instance_id", "?"))
        return None

    if not fail_to_pass:
        logger.info("No FAIL_TO_PASS tests for %s, skipping evaluation", instance.get("instance_id", "?"))
        return None

    _console.print(f"\n  [bold cyan]POST-TASK TEST EVALUATION[/bold cyan]  "
                    f"({len(fail_to_pass)} fail_to_pass, {len(pass_to_pass)} pass_to_pass)")

    try:
        # Reset to clean state and apply the submission patch
        env.execute({"command": "cd /testbed && git checkout -- . && git clean -fd"})
        env.execute({"command": f"cat > /tmp/eval_patch.diff << 'EVAL_EOF'\n{submission}\nEVAL_EOF"})
        apply_result = env.execute({"command": "cd /testbed && git apply /tmp/eval_patch.diff 2>&1"})
        if apply_result.get("returncode", -1) != 0:
            _console.print(f"  [red]FAIL[/red]  Could not apply patch: {apply_result.get('output', '')[:200]}")
            return None

        # Apply test_patch if present (some instances need test file changes)
        test_patch = instance.get("test_patch", "")
        if test_patch:
            env.execute({"command": f"cat > /tmp/test_patch.diff << 'TEST_EOF'\n{test_patch}\nTEST_EOF"})
            tp_result = env.execute({"command": "cd /testbed && git apply /tmp/test_patch.diff 2>&1"})
            if tp_result.get("returncode", -1) != 0:
                _console.print(f"  [yellow]WARN[/yellow]  test_patch failed to apply (may already be present)")

        # Run FAIL_TO_PASS tests
        _console.print(f"\n  [bold]FAIL_TO_PASS tests ({len(fail_to_pass)}):[/bold]")
        f2p_passed = 0
        f2p_failed = []
        for test_id in fail_to_pass:
            passed = _run_single_test(env, test_id)
            if passed:
                f2p_passed += 1
                _console.print(f"    [green]PASS[/green]  {test_id}")
            else:
                f2p_failed.append(test_id)
                _console.print(f"    [red]FAIL[/red]  {test_id}")

        # Run PASS_TO_PASS tests
        p2p_passed = 0
        p2p_failed = []
        p2p_total = len(pass_to_pass)
        if p2p_total > 0:
            _console.print(f"\n  [bold]PASS_TO_PASS tests ({p2p_total}):[/bold]")
            for test_id in pass_to_pass:
                passed = _run_single_test(env, test_id)
                if passed:
                    p2p_passed += 1
                    _console.print(f"    [green]PASS[/green]  {test_id}")
                else:
                    p2p_failed.append(test_id)
                    _console.print(f"    [red]FAIL[/red]  {test_id}")

        # Summary
        f2p_pct = (f2p_passed / len(fail_to_pass) * 100) if fail_to_pass else 0
        p2p_pct = (p2p_passed / p2p_total * 100) if p2p_total > 0 else 100
        all_passed = f2p_pct == 100 and p2p_pct == 100

        _console.print()
        _console.print(f"  [bold]FAIL_TO_PASS:[/bold] {f2p_passed}/{len(fail_to_pass)} ({f2p_pct:.0f}%)")
        _console.print(f"  [bold]PASS_TO_PASS:[/bold] {p2p_passed}/{p2p_total} ({p2p_pct:.0f}%)")

        if all_passed:
            _console.print(f"\n  [green bold]RESOLVED[/green bold]  All tests pass!")
        elif f2p_pct > 0 and p2p_pct == 100:
            _console.print(f"\n  [yellow bold]PARTIAL[/yellow bold]  Some FAIL_TO_PASS tests still failing")
        else:
            _console.print(f"\n  [red bold]NOT RESOLVED[/red bold]  Tests failing")

        return {
            "all_passed": all_passed,
            "f2p_passed": f2p_passed,
            "f2p_total": len(fail_to_pass),
            "f2p_failed": f2p_failed,
            "p2p_passed": p2p_passed,
            "p2p_total": p2p_total,
            "p2p_failed": p2p_failed,
        }
    except Exception as e:
        logger.warning("Test evaluation error for %s: %s", instance.get("instance_id", "?"), e)
        return None


def _parse_django_test_id(test_id: str) -> str | None:
    """Parse Django-style test ID like 'test_name (module.tests.ClassName)' into
    a dotted path suitable for runtests.py: 'module.tests.ClassName.test_name'.
    Returns None if test_id is not Django-style.
    """
    m = re.match(r'^(\S+)\s+\(([^)]+)\)$', test_id)
    if m:
        test_name, module_path = m.group(1), m.group(2)
        return f"{module_path}.{test_name}"
    return None


def _run_single_test(env: Environment, test_id: str, timeout: int = 120) -> bool:
    """Run a single test in the environment. Returns True if passed."""
    django_path = _parse_django_test_id(test_id)
    if django_path is not None:
        # Django uses its own test runner, not pytest
        command = f"cd /testbed && python tests/runtests.py --settings=test_sqlite --parallel 1 -v 2 {django_path} 2>&1 | tail -30"
    else:
        command = f"cd /testbed && python -m pytest -xvs {test_id} 2>&1 | tail -20"

    result = env.execute({
        "command": command,
        "timeout": timeout,
    })
    output = result.get("output", "")
    rc = result.get("returncode", -1)
    if rc == 0:
        return True
    # Fallback heuristic for pytest
    if django_path is None and " passed" in output and " failed" not in output and " error" not in output.lower():
        return True
    # Fallback heuristic for Django runner: "OK" at end means success
    if django_path is not None and re.search(r'\bOK\b', output) and 'FAILED' not in output:
        return True
    return False


def process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager: RunBatchProgressManager,
) -> None:
    """Process a single SWEBench instance."""
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    # avoid inconsistent state if something here fails and there's leftover previous files
    remove_from_preds_file(output_dir / "preds.json", instance_id)
    (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)
    model = get_model(config=config.get("model", {}))
    task = instance["problem_statement"]

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Pulling/starting environment")

    agent = None
    exit_status = None
    result = None
    extra_info = {}

    try:
        env = get_sb_environment(config, instance)
        agent_config = dict(config.get("agent", {}))
        agent_class_spec = agent_config.pop("agent_class", "default")
        base_class = get_agent_class(agent_class_spec)
        TrackedClass = _make_progress_tracking_class(base_class)
        agent = TrackedClass(
            model,
            env,
            progress_manager=progress_manager,
            instance_id=instance_id,
            **agent_config,
        )
        info = agent.run(task)
        exit_status = info.get("exit_status")
        result = info.get("submission")
        # Post-task test evaluation (agent is done, results are only logged)
        if result and exit_status == "Submitted":
            progress_manager.update_instance_status(instance_id, "Evaluating tests")
            test_results = evaluate_submission(env, instance, result)
            if test_results:
                extra_info["test_results"] = test_results
    except Exception as e:
        logger.error(f"Error processing instance {instance_id}: {e}", exc_info=True)
        exit_status, result = type(e).__name__, ""
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(e)}
    finally:
        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info(f"Saved trajectory to '{traj_path}'")
        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


def filter_instances(
    instances: list[dict], *, filter_spec: str, slice_spec: str = "", shuffle: bool = False
) -> list[dict]:
    """Filter and slice a list of SWEBench instances."""
    if shuffle:
        instances = sorted(instances.copy(), key=lambda x: x["instance_id"])
        random.seed(42)
        random.shuffle(instances)
    before_filter = len(instances)
    instances = [instance for instance in instances if re.match(filter_spec, instance["instance_id"])]
    if (after_filter := len(instances)) != before_filter:
        logger.info(f"Instance filter: {before_filter} -> {after_filter} instances")
    if slice_spec:
        values = [int(x) if x else None for x in slice_spec.split(":")]
        instances = instances[slice(*values)]
        if (after_slice := len(instances)) != before_filter:
            logger.info(f"Instance slice: {before_filter} -> {after_slice} instances")
    return instances


# fmt: off
@app.command(help=_HELP_TEXT)
def main(
    subset: str = typer.Option("lite", "--subset", help="SWEBench subset to use or path to a dataset", rich_help_panel="Data selection"),
    split: str = typer.Option("dev", "--split", help="Dataset split", rich_help_panel="Data selection"),
    slice_spec: str = typer.Option("", "--slice", help="Slice specification (e.g., '0:5' for first 5 instances)", rich_help_panel="Data selection"),
    filter_spec: str = typer.Option("", "--filter", help="Filter instance IDs by regex", rich_help_panel="Data selection"),
    shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle instances", rich_help_panel="Data selection"),
    output: str = typer.Option("", "-o", "--output", help="Output directory", rich_help_panel="Basic"),
    workers: int = typer.Option(1, "-w", "--workers", help="Number of worker threads for parallel processing", rich_help_panel="Basic"),
    model: str | None = typer.Option(None, "-m", "--model", help="Model to use", rich_help_panel="Basic"),
    model_class: str | None = typer.Option(None, "--model-class", help="Model class to use (e.g., 'anthropic' or 'minisweagent.models.anthropic.AnthropicModel')", rich_help_panel="Advanced"),
    redo_existing: bool = typer.Option(False, "--redo-existing", help="Redo existing instances", rich_help_panel="Data selection"),
    config_spec: list[str] = typer.Option([str(DEFAULT_CONFIG_FILE)], "-c", "--config", help=_CONFIG_SPEC_HELP_TEXT, rich_help_panel="Basic"),
    environment_class: str | None = typer.Option(None, "--environment-class", help="Environment type to use. Recommended are docker or singularity", rich_help_panel="Advanced"),
) -> None:
    # fmt: on
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"Results will be saved to {output_path}")
    add_file_handler(output_path / "minisweagent.log")

    from datasets import load_dataset

    dataset_path = DATASET_MAPPING.get(subset, subset)
    logger.info(f"Loading dataset {dataset_path}, split {split}...")
    instances = list(load_dataset(dataset_path, split=split))

    instances = filter_instances(instances, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle)
    if not redo_existing and (output_path / "preds.json").exists():
        existing_instances = list(json.loads((output_path / "preds.json").read_text()).keys())
        logger.info(f"Skipping {len(existing_instances)} existing instances")
        instances = [instance for instance in instances if instance["instance_id"] not in existing_instances]
    logger.info(f"Running on {len(instances)} instances...")

    logger.info(f"Building agent config from specs: {config_spec}")
    configs = [get_config_from_spec(spec) for spec in config_spec]
    configs.append({
        "environment": {"environment_class": environment_class or UNSET},
        "model": {"model_name": model or UNSET, "model_class": model_class or UNSET},
    })
    config = recursive_merge(*configs)

    progress_manager = RunBatchProgressManager(len(instances), output_path / f"exit_statuses_{time.time()}.yaml")

    def process_futures(futures: dict[concurrent.futures.Future, str]):
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception as e:
                instance_id = futures[future]
                logger.error(f"Error in future for instance {instance_id}: {e}", exc_info=True)
                progress_manager.on_uncaught_exception(instance_id, e)

    with Live(progress_manager.render_group, refresh_per_second=4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_instance, instance, output_path, config, progress_manager): instance[
                    "instance_id"
                ]
                for instance in instances
            }
            try:
                process_futures(futures)
            except KeyboardInterrupt:
                logger.info("Cancelling all pending jobs. Press ^C again to exit immediately.")
                for future in futures:
                    if not future.running() and not future.done():
                        future.cancel()
                process_futures(futures)


if __name__ == "__main__":
    app()
