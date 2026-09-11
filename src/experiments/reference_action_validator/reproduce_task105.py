"""Demonstrate issue #499's DB-only consequence without an LLM judge."""

import argparse
import json
import os
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from experiments.reference_action_validator.validate import ROOT, input_hashes, offline


def reproduce():
    """Compare actual frozen task data and evaluator behavior with a control."""
    from tau2.domains.retail.environment import get_environment, get_tasks
    from tau2.evaluator.evaluator_env import (
        EnvironmentEvaluator,
        FullDuplexEnvironmentEvaluator,
    )

    task = next(t for t in get_tasks() if t.id == "105")
    assert len(task.evaluation_criteria.actions) == 1
    action = task.evaluation_criteria.actions[0]
    env = get_environment()

    def initialize(environment):
        state = deepcopy(task.initial_state)
        environment.set_state(
            initialization_data=state.initialization_data if state else None,
            initialization_actions=state.initialization_actions if state else None,
            message_history=(state.message_history if state else None) or [],
        )

    initialize(env)
    args = action.arguments
    order = env.tools.db.orders[args["order_id"]]
    user = env.tools.db.users[order.user_id]
    payment = user.payment_methods[args["payment_method_id"]]
    price_pairs = []
    for old_id, new_id in zip(args["item_ids"], args["new_item_ids"]):
        item = next(i for i in order.items if i.item_id == old_id)
        variant = env.tools.db.products[item.product_id].variants[new_id]
        price_pairs.append((Decimal(str(item.price)), Decimal(str(variant.price))))
    difference = sum(new - old for old, new in price_pairs)
    before = env.tools.db.model_dump_json()
    error = None
    try:
        env.make_tool_call(action.name, action.requestor, **args)
    except ValueError as exc:
        error = str(exc)
    unchanged = env.tools.db.model_dump_json() == before
    # Call only the existing environment evaluators. Do not run NL assertions.
    rewards = {}
    for cls in (EnvironmentEvaluator, FullDuplexEnvironmentEvaluator):
        reward = cls.calculate_reward(get_environment, task, [])
        rewards[cls.__name__] = {
            "db_match": reward.db_check.db_match,
            "db_reward": reward.db_check.db_reward,
        }
    # Counterfactual control in a separate in-memory environment, never a file edit.
    control = get_environment()
    initialize(control)
    control.tools.db.users[order.user_id].payment_methods[payment.id].balance = float(
        difference
    )
    control.make_tool_call(action.name, action.requestor, **args)
    return {
        "task_id": task.id,
        "action_id": action.action_id,
        "price_pairs": [[str(old), str(new)] for old, new in price_pairs],
        "required_difference": str(difference),
        "gift_card_balance": str(payment.balance),
        "payment_method_count": len(user.payment_methods),
        "reference_error": error,
        "db_bytes_unchanged_after_failure": unchanged,
        "empty_trajectory_environment_evaluation": rewards,
        "in_memory_funded_control_order_status": control.tools.db.orders[
            args["order_id"]
        ].status,
        "overall_reward_measured": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
        result = reproduce()
    result["network_attempts"] = len(attempts)
    result["core_inputs_unchanged"] = before == input_hashes()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if not attempts and result["core_inputs_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
