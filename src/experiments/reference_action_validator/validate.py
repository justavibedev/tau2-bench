"""Offline reference replay prototype. Does not invoke or change scoring."""

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
DOMAINS = ("airline", "retail", "telecom")


@contextmanager
def offline():
    """Block Python socket network access, including import-time metadata fetches."""
    attempts = []

    def deny(*args, **kwargs):
        attempts.append("network access attempted")
        raise RuntimeError("Reference validation must run offline")

    with (
        patch.object(socket.socket, "connect", deny),
        patch.object(socket.socket, "connect_ex", deny),
        patch.object(socket, "create_connection", deny),
        patch.object(socket, "getaddrinfo", deny),
    ):
        yield attempts


def validate_task(domain, task, constructor):
    """Replay one reference on a fresh environment using evaluator ordering.

    Continue after action exceptions to expose every observed failure, like the
    existing evaluator. Later failures may be consequences, not independent bugs.
    """
    result = {
        "domain": domain,
        "task_id": task.id,
        "status": "skipped_no_reference",
        "reference_actions": 0,
        "attempted_actions": 0,
        "failures": [],
    }
    criteria = task.evaluation_criteria
    if criteria is None or not criteria.actions:
        return result
    actions = criteria.actions
    result["reference_actions"] = len(actions)
    try:
        env = constructor()
        state = deepcopy(task.initial_state)
        env.set_state(
            initialization_data=state.initialization_data if state else None,
            initialization_actions=state.initialization_actions if state else None,
            message_history=(state.message_history if state else None) or [],
            strict=True,
        )
    except Exception as exc:
        result.update(
            status="setup_error",
            setup_error={"type": type(exc).__name__, "message": str(exc)},
        )
        return result

    for index, action in enumerate(actions):
        result["attempted_actions"] += 1
        try:
            env.make_tool_call(
                tool_name=action.name,
                requestor=action.requestor,
                **deepcopy(action.arguments),
            )
        except Exception as exc:
            result["failures"].append(
                {
                    "action_index": index,
                    "action_id": action.action_id,
                    "tool": action.name,
                    "requestor": action.requestor,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "after_prior_failure": bool(result["failures"]),
                }
            )
    result["status"] = "failed" if result["failures"] else "passed"
    return result


def summarize(results):
    """Count incomplete validation separately from broken reference actions."""
    return {
        "tasks": len(results),
        "replayed_tasks": sum(r["status"] in ("passed", "failed") for r in results),
        "passed_tasks": sum(r["status"] == "passed" for r in results),
        "failed_tasks": sum(r["status"] == "failed" for r in results),
        "setup_errors": sum(r["status"] == "setup_error" for r in results),
        "skipped_no_reference": sum(
            r["status"] == "skipped_no_reference" for r in results
        ),
        "attempted_actions": sum(r["attempted_actions"] for r in results),
        "failed_actions": sum(len(r["failures"]) for r in results),
    }


def exit_status(report):
    """0: checked references passed; 1: broken references; 2: incomplete run."""
    summary = report["summary"]
    if (
        report["runtime_errors"]
        or summary["setup_errors"]
        or not summary["replayed_tasks"]
    ):
        return 2
    return 1 if summary["failed_actions"] else 0


def input_hashes():
    """Hash core source and the three domains' inputs, excluding build debris."""
    paths = list((ROOT / "src/tau2").rglob("*.py"))
    for domain in DOMAINS:
        paths.extend(
            p for p in (ROOT / "data/tau2/domains" / domain).rglob("*") if p.is_file()
        )
    paths.extend([ROOT / "pyproject.toml", ROOT / "uv.lock"])
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
        if "__pycache__" not in p.parts
    }


def run(domains, split):
    """Run the supported bundled domains; optional/network domains are excluded."""
    report = {"domains": {}, "runtime_errors": [], "task_split": split}
    all_results = []
    for domain in domains:
        try:
            module = importlib.import_module(f"tau2.domains.{domain}.environment")
            tasks = module.get_tasks(task_split_name=split)
            if not tasks:
                raise ValueError("Selected split contains no tasks")
            results = [validate_task(domain, t, module.get_environment) for t in tasks]
            report["domains"][domain] = {
                "summary": summarize(results),
                "tasks": results,
            }
            all_results.extend(results)
        except Exception as exc:
            report["runtime_errors"].append(
                {"domain": domain, "type": type(exc).__name__, "message": str(exc)}
            )
    report["summary"] = summarize(all_results)
    return report


def main(argv=None):
    """Write machine-readable results and provenance; fail on replay errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domains", nargs="+", choices=DOMAINS, default=list(DOMAINS))
    parser.add_argument("--split", choices=("base", "train", "test"), default="base")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    # Ensure the bundled local dataset is used, not an ambient data override.
    os.environ["TAU2_DATA_DIR"] = str(ROOT / "data")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    before = input_hashes()
    with offline() as attempts:
        from loguru import logger

        logger.remove()
        logger.add(sys.stderr, level="ERROR")
        import tau2

        if Path(tau2.__file__).resolve().parents[2] != ROOT:
            raise RuntimeError("Set PYTHONPATH=src to use this checkout's tau2")
        report = run(list(dict.fromkeys(args.domains)), args.split)
    if attempts:
        report["runtime_errors"].append({"blocked_network_attempts": len(attempts)})
    after = input_hashes()
    if before != after:
        report["runtime_errors"].append({"error": "Core source or dataset changed"})
    report["network_attempts"] = len(attempts)
    report["core_inputs_unchanged"] = before == after
    report["exit_code"] = exit_status(report)
    provenance = {
        "checkout_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "python": sys.version,
        "core_input_sha256": before,
        "validator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "packages": sorted(
            f"{d.metadata['Name']}=={d.version}"
            for d in importlib.metadata.distributions()
        ),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("report.json", report), ("provenance.json", provenance)):
        (args.output_dir / name).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n"
        )
    print(json.dumps({"summary": report["summary"], "exit_code": report["exit_code"]}))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
