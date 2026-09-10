"""Compare pinned tau2 revisions with deterministic checkpoint I/O failures.

The parent process uses only the standard library. Each isolated child imports
the real tau2 package from a git archive of its tested revision.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REVISIONS = {
    "baseline": "2174a603f6d014ef94473ffa95957f6ce27100db",
    "fixed": "ece0bc2c511a916a4fb399560041def8b8384340",
}


def sha256(path):
    """Return a file's SHA-256 digest."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def worker(source):
    """Measure six independent failures against real checkpoint code and models."""
    sys.path.insert(0, str(source / "src"))
    # Imports may emit optional-provider messages. Do not include paths or
    # incidental dependency chatter in the machine-readable observations.
    with contextlib.redirect_stdout(io.StringIO()):
        import multiprocessing
        from unittest.mock import patch

        from loguru import logger

        logger.remove()
        from tau2.data_model.simulation import (
            Info,
            Results,
            SimulationRun,
            TerminationReason,
            UserInfo,
        )
        from tau2.data_model.tasks import EvaluationCriteria, Task, UserScenario
        from tau2.environment.environment import EnvironmentInfo
        from tau2.runner import checkpoint

    assert Path(checkpoint.__file__).resolve().is_relative_to(source.resolve())
    observations = []
    for reuse_id in (False, True):
        for stage in ("mkstemp", "serialize", "replace"):
            with tempfile.TemporaryDirectory() as scratch:
                save_path = Path(scratch) / "results.json"
                results = Results(
                    timestamp="2026-01-01T00:00:00",
                    info=Info(
                        git_commit="synthetic-fixture",
                        num_trials=1,
                        max_steps=100,
                        max_errors=10,
                        user_info=UserInfo(implementation="user_simulator"),
                        agent_info={"implementation": "llm_agent"},
                        environment_info=EnvironmentInfo(
                            domain_name="mock", policy="synthetic policy"
                        ),
                    ),
                    tasks=[
                        Task(
                            id="task-0",
                            user_scenario=UserScenario(instructions="synthetic task"),
                            evaluation_criteria=EvaluationCriteria(),
                        )
                    ],
                    simulations=[],
                )
                results.save(save_path, format="dir")
                save_fn, replace_fn = checkpoint.create_checkpoint_fns(
                    save_path, multiprocessing.Lock()
                )
                original = SimulationRun(
                    id="original",
                    timestamp="2026-01-01T00:00:00",
                    task_id="task-0",
                    trial=0,
                    seed=42,
                    start_time="2026-01-01T00:00:00",
                    end_time="2026-01-01T00:01:00",
                    duration=60.0,
                    termination_reason=TerminationReason.USER_STOP,
                    messages=[],
                )
                save_fn(original)
                replacement = original.model_copy(
                    update={
                        "id": original.id if reuse_id else "replacement",
                        "duration": 120.0,
                    }
                )
                sims_dir = save_path.parent / "simulations"
                original_path = sims_dir / "original.json"
                original_hash = sha256(original_path)
                metadata_hash = sha256(save_path)
                target, attribute = {
                    "mkstemp": (checkpoint.tempfile, "mkstemp"),
                    "serialize": (SimulationRun, "model_dump_json"),
                    "replace": (checkpoint.os, "replace"),
                }[stage]
                caught = None
                with patch.object(
                    target, attribute, side_effect=OSError("injected I/O failure")
                ) as fault:
                    try:
                        replace_fn((0, "task-0", 42), replacement)
                    except OSError as exc:
                        caught = type(exc).__name__
                row = {
                    "failure_stage": stage,
                    "reuse_simulation_id": reuse_id,
                    "injected_error": caught,
                    "injection_calls": fault.call_count,
                    "original_sha256_before": original_hash,
                    "original_sha256_after": (
                        sha256(original_path) if original_path.exists() else None
                    ),
                    "metadata_sha256_before": metadata_hash,
                    "metadata_sha256_after": sha256(save_path),
                    "files_after_failure": sorted(p.name for p in sims_dir.iterdir()),
                    "reload_error": None,
                }
                try:
                    loaded = Results.load(save_path)
                    row["reload_equals_original"] = loaded.simulations == [original]
                except Exception as exc:
                    row["reload_error"] = type(exc).__name__
                    row["reload_equals_original"] = False
                # The patch is removed. The same checkpoint closure must recover.
                replace_fn((0, "task-0", 42), replacement)
                loaded = Results.load(save_path)
                row["retry_equals_replacement"] = loaded.simulations == [replacement]
                row["files_after_retry"] = sorted(p.name for p in sims_dir.iterdir())
                row["index_ids_after_retry"] = [
                    entry["id"]
                    for entry in json.loads(save_path.read_text())["simulation_index"]
                ]
                observations.append(row)
    print(json.dumps(observations, sort_keys=True))


def preserved(row):
    """Define the pre-retry checkpoint preservation invariant."""
    return (
        row["original_sha256_before"] == row["original_sha256_after"]
        and row["metadata_sha256_before"] == row["metadata_sha256_after"]
        and row["files_after_failure"] == ["original.json"]
        and row["reload_equals_original"]
    )


def run(output):
    """Export both source revisions, run isolated children, and save observations."""
    script = Path(__file__).resolve()
    repo = script.parents[3]
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "os_release": platform.release(),
        "runner_sha256": sha256(script),
        "requirements_sha256": sha256(script.with_name("requirements.txt")),
        "revisions": {},
    }
    for label, revision in REVISIONS.items():
        archive = subprocess.check_output(
            ["git", "archive", revision, "src", "data", "pyproject.toml", "uv.lock"],
            cwd=repo,
        )
        with tempfile.TemporaryDirectory() as scratch:
            source = Path(scratch)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(source, filter="data")
            env = os.environ.copy()
            env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
            # No API calls are made. Avoid loading user-specific dotenv/config.
            env["TAU2_DATA_DIR"] = str(source / "data")
            process = subprocess.run(
                [sys.executable, str(script), "--worker", str(source)],
                cwd=source,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if process.returncode:
                raise RuntimeError(
                    f"{label} worker exited {process.returncode}; "
                    "verify Python and requirements (no raw stderr published)"
                )
            rows = json.loads(process.stdout)
            (output / f"{label}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
            )
            for row in rows:
                assert row["injected_error"] == "OSError"
                assert row["injection_calls"] == 1
                assert row["retry_equals_replacement"]
                expected_id = (
                    "original" if row["reuse_simulation_id"] else "replacement"
                )
                assert row["index_ids_after_retry"] == [expected_id]
                assert row["files_after_retry"] == [f"{expected_id}.json"]
            count = sum(preserved(row) for row in rows)
            assert count == (6 if label == "fixed" else 0)
            manifest["revisions"][label] = {
                "commit": revision,
                "checkpoint_sha256": sha256(source / "src/tau2/runner/checkpoint.py"),
                "uv_lock_sha256": sha256(source / "uv.lock"),
                "cases": len(rows),
                "preserved_before_retry": count,
                "successful_retries": sum(
                    row["retry_equals_replacement"] for row in rows
                ),
            }
            print(f"{label}: preserved {count}/6; successful retries 6/6")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).with_name("results")
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
    else:
        run(args.output)
