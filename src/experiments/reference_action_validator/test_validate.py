"""Offline behavior tests plus regressions against the actual retail fixtures."""

import json
import os
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from experiments.reference_action_validator.validate import (
    ROOT,
    exit_status,
    offline,
    run,
    summarize,
    validate_task,
)


def task(*names, initial_state=None):
    return SimpleNamespace(
        id="test",
        initial_state=initial_state,
        evaluation_criteria=SimpleNamespace(
            actions=[
                SimpleNamespace(
                    name=name, action_id=str(i), requestor="assistant", arguments={}
                )
                for i, name in enumerate(names)
            ]
        ),
    )


class RecordingEnvironment:
    def __init__(self):
        self.calls = []

    def set_state(self, **kwargs):
        self.state = kwargs

    def make_tool_call(self, tool_name, requestor, **kwargs):
        self.calls.append((tool_name, requestor, kwargs))
        if tool_name == "broken":
            raise ValueError("fixture failure")


def test_continues_replay_and_marks_failures_after_an_earlier_error():
    env = RecordingEnvironment()
    result = validate_task("test", task("broken", "ok", "broken"), lambda: env)
    assert [c[0] for c in env.calls] == ["broken", "ok", "broken"]
    assert result["status"] == "failed"
    assert result["attempted_actions"] == 3
    assert [f["action_index"] for f in result["failures"]] == [0, 2]
    assert [f["after_prior_failure"] for f in result["failures"]] == [False, True]


@pytest.mark.parametrize("stage", ["construction", "initialization"])
def test_setup_errors_are_not_reported_as_action_failures(stage):
    class BrokenEnvironment(RecordingEnvironment):
        def __init__(self):
            if stage == "construction":
                raise RuntimeError("setup unavailable")
            super().__init__()

        def set_state(self, **kwargs):
            raise RuntimeError("setup unavailable")

    result = validate_task("test", task("ok"), BrokenEnvironment)
    assert result["status"] == "setup_error"
    assert result["attempted_actions"] == 0
    assert result["failures"] == []
    assert exit_status({"summary": summarize([result]), "runtime_errors": []}) == 2


@pytest.mark.parametrize("missing_criteria", [False, True])
def test_no_reference_is_explicitly_skipped(missing_criteria):
    sample = task()
    if missing_criteria:
        sample.evaluation_criteria = None
    result = validate_task("test", sample, lambda: pytest.fail("must not construct"))
    assert result["status"] == "skipped_no_reference"


def test_state_and_requestor_forwarding_use_copies():
    state = SimpleNamespace(
        initialization_data={"value": 1},
        initialization_actions=["init"],
        message_history=["history"],
    )
    sample = task("ok", initial_state=state)
    sample.evaluation_criteria.actions[0].requestor = "user"
    env = RecordingEnvironment()
    result = validate_task("test", sample, lambda: env)
    assert result["status"] == "passed"
    assert env.state == {
        "initialization_data": {"value": 1},
        "initialization_actions": ["init"],
        "message_history": ["history"],
        "strict": True,
    }
    env.state["message_history"].append("mutation")
    assert state.message_history == ["history"]
    assert env.calls[0][1] == "user"


def test_each_task_gets_a_fresh_environment():
    instances = []

    def construct():
        instance = RecordingEnvironment()
        instances.append(instance)
        return instance

    for _ in range(2):
        validate_task("test", task("ok"), construct)
    assert len(instances) == 2
    assert instances[0] is not instances[1]
    assert all(len(e.calls) == 1 for e in instances)


@pytest.mark.parametrize(
    "kind,expected", [("clean", 0), ("broken", 1), ("setup", 2), ("empty", 2)]
)
def test_exit_status_distinguishes_invalid_references_and_incomplete_runs(
    kind, expected
):
    results = (
        []
        if kind == "empty"
        else [
            validate_task(
                "test",
                task("broken" if kind == "broken" else "ok"),
                RecordingEnvironment,
            )
        ]
    )
    report = {"summary": summarize(results), "runtime_errors": []}
    if kind == "setup":
        report["runtime_errors"] = [{"error": "unavailable domain"}]
    assert exit_status(report) == expected


def test_network_attempt_is_blocked_and_recorded():
    with offline() as attempts:
        with pytest.raises(RuntimeError, match="must run offline"):
            socket.create_connection(("example.com", 443))
    assert len(attempts) == 1


def test_actual_retail_regressions_and_control():
    with offline() as attempts:
        from tau2.domains.retail.environment import get_environment, get_tasks

        tasks = {t.id: t for t in get_tasks()}
        results = {
            i: validate_task("retail", tasks[i], get_environment)
            for i in ("0", "64", "105")
        }
    assert not attempts
    assert results["0"]["status"] == "passed"
    assert (
        results["64"]["failures"][0]["message"]
        == "Non-delivered order cannot be exchanged"
    )
    assert (
        results["64"]["attempted_actions"]
        > results["64"]["failures"][0]["action_index"] + 1
    )
    assert (
        results["105"]["failures"][0]["message"]
        == "Insufficient gift card balance to pay for the price difference"
    )


def test_task105_db_only_consequence_and_funded_control():
    from experiments.reference_action_validator.reproduce_task105 import reproduce

    with offline() as attempts:
        result = reproduce()
    assert not attempts
    assert result["required_difference"] == "21.10"
    assert result["gift_card_balance"] == "17.0"
    assert result["payment_method_count"] == 1
    assert result["db_bytes_unchanged_after_failure"]
    assert all(
        r["db_match"] and r["db_reward"] == 1.0
        for r in result["empty_trajectory_environment_evaluation"].values()
    )
    assert result["in_memory_funded_control_order_status"] == "exchange requested"


def test_task_loader_failure_cannot_produce_a_clean_report(monkeypatch):
    def fail_loader(**kwargs):
        raise ValueError("invalid task data")

    monkeypatch.setattr(
        "experiments.reference_action_validator.validate.importlib.import_module",
        lambda name: SimpleNamespace(get_tasks=fail_loader),
    )
    report = run(["airline"], "base")
    assert report["runtime_errors"][0]["message"] == "invalid task data"
    assert exit_status(report) == 2


@pytest.mark.parametrize("domain,expected_exit", [("airline", 0), ("retail", 1)])
def test_cli_reports_real_domain_results_and_exit_codes(
    tmp_path, domain, expected_exit
):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "src/experiments/reference_action_validator/validate.py"),
            "--domains",
            domain,
            "--output-dir",
            str(tmp_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == expected_exit, result.stderr
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["exit_code"] == expected_exit
    assert report["network_attempts"] == 0
    assert report["core_inputs_unchanged"]
    assert report["runtime_errors"] == []
    if domain == "retail":
        failed = [
            t for t in report["domains"][domain]["tasks"] if t["status"] == "failed"
        ]
        assert {t["task_id"] for t in failed} == {
            "2",
            "3",
            "4",
            "35",
            "37",
            "38",
            "39",
            "46",
            "47",
            "54",
            "55",
            "64",
            "67",
            "68",
            "105",
        }
        assert report["summary"]["failed_actions"] == 18
    else:
        assert report["summary"]["tasks"] == 50
        assert report["summary"]["failed_actions"] == 0
