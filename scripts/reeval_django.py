#!/usr/bin/env python3
"""Re-evaluate Django instances using the corrected test runner.

This script loads existing trajectory files, extracts the submission patch,
and re-runs test evaluation using the fixed _run_single_test that properly
handles Django's runtests.py.
"""

import json
import sys
import time
from pathlib import Path

from datasets import load_dataset
from rich.console import Console

from minisweagent.config import get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.run.benchmarks.swebench import (
    evaluate_submission,
    get_sb_environment,
    update_preds_file,
)
from minisweagent.utils.log import logger

console = Console(highlight=False)


def main():
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "output/swebench_verified_graphplan_20260224_053428"
    )
    config_file = sys.argv[2] if len(sys.argv) > 2 else "swebench_graphplan.yaml"

    console.print(f"[bold]Re-evaluating Django instances in {output_dir}[/bold]\n")

    # Load config
    from minisweagent.config import builtin_config_dir
    config_path = builtin_config_dir / "benchmarks" / config_file
    config = get_config_from_spec(str(config_path))

    # Load dataset to get instance metadata (test lists, docker images, etc.)
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    instance_map = {row["instance_id"]: dict(row) for row in ds}

    # Find Django trajectory files
    django_dirs = sorted(
        d for d in output_dir.iterdir()
        if d.is_dir() and d.name.startswith("django__django-")
    )

    console.print(f"Found {len(django_dirs)} Django instances to re-evaluate\n")

    resolved = 0
    partial = 0
    failed = 0
    errors = 0

    for i, inst_dir in enumerate(django_dirs):
        instance_id = inst_dir.name
        traj_path = inst_dir / f"{instance_id}.traj.json"

        if not traj_path.exists():
            console.print(f"[yellow]SKIP[/yellow] {instance_id} - no trajectory file")
            continue

        traj = json.loads(traj_path.read_text())
        submission = traj.get("info", {}).get("submission", "")

        if not submission:
            console.print(f"[yellow]SKIP[/yellow] {instance_id} - no submission")
            continue

        instance = instance_map.get(instance_id)
        if not instance:
            console.print(f"[yellow]SKIP[/yellow] {instance_id} - not in dataset")
            continue

        console.print(f"\n[bold cyan]({i+1}/{len(django_dirs)}) Re-evaluating {instance_id}[/bold cyan]")

        env = None
        try:
            env = get_sb_environment(config, instance)
            test_results = evaluate_submission(env, instance, submission)

            if test_results:
                # Update trajectory file with new test results
                traj["info"]["test_results"] = test_results
                traj_path.write_text(json.dumps(traj, indent=2))

                if test_results["all_passed"]:
                    resolved += 1
                elif test_results["f2p_passed"] > 0:
                    partial += 1
                else:
                    failed += 1
            else:
                errors += 1
        except Exception as e:
            console.print(f"[red]ERROR[/red] {instance_id}: {e}")
            errors += 1
        finally:
            if env is not None:
                try:
                    env.close()
                except Exception:
                    pass

    console.print(f"\n[bold]{'='*60}[/bold]")
    console.print(f"[bold]Re-evaluation complete![/bold]")
    console.print(f"  Resolved: [green]{resolved}[/green]")
    console.print(f"  Partial:  [yellow]{partial}[/yellow]")
    console.print(f"  Failed:   [red]{failed}[/red]")
    console.print(f"  Errors:   [red]{errors}[/red]")
    console.print(f"  Total:    {len(django_dirs)}")


if __name__ == "__main__":
    main()
