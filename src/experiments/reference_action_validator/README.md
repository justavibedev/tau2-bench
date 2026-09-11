# Offline reference-action validator (local prototype)

This experiment independently reproduces [tau2-bench issue #499](https://github.com/sierra-research/tau2-bench/issues/499), reported by brayans7, and prototypes an authoring-time check. It replays reference actions against fresh environments and reports exceptions without changing the evaluator, task data, or published scores. It has not been submitted upstream or accepted by maintainers.

Upstream source inspected: `2174a603f6d014ef94473ffa95957f6ce27100db` (September 10, 2026). The initial replay through the registry and the prototype through direct domain constructors independently produced the same 18 failure locations and messages.

## Results

| Domain | Tasks inspected | References replayed | Actions replayed | Tasks with exceptions | Action exceptions | No reference actions |
|---|---:|---:|---:|---:|---:|---:|
| airline | 50 | 43 | 142 | 0 | 0 | 7 |
| retail | 114 | 112 | 550 | 15 | 18 | 2 |
| telecom | 114 | 114 | 516 | 0 | 0 | 0 |
| Total | 278 | 269 | 1,208 | 15 | 18 | 9 |

The nine tasks without reference actions are explicitly skipped, not validated. Retail failures occur in tasks 2, 3, 4, 35, 37, 38, 39, 46, 47, 54, 55, 64, 67, 68, and 105. See `results/report.json` for every inspected task and each observed error. `results/initial_reproduction.json` contains the initial independent registry replay. `results/provenance.json` contains source/data SHA-256 hashes, Python version, installed package versions and the validator hash.

### Task 105

The two replacement products cost $101.12 and $109.58; each returned item cost $94.80. The $21.10 difference exceeds the sole gift card's $17 balance. The reference exchange raises before modifying the DB. Both the half-duplex and full-duplex environment evaluators therefore give an empty trajectory `db_match=true`, `db_reward=1.0`. The raw result is in `results/task105.json`.

This is only the DB component. We did not invoke the NL-assertion judge, estimate overall benchmark impact, or label a real agent run successful. A separate in-memory control with sufficient funds executes the exchange; this proves the reported exception's cause, not which dataset repair maintainers should choose. No file is modified by that control.

## Run

Run from the repository root with Python 3.12 and the project's dev dependencies. The validated environment reused the existing `../tau_bench/.venv`; exact installed versions are in the provenance file. The project core currently imports some optional voice modules, so that environment also contains websockets, scipy and soundfile. No live model keys are needed. No new dependencies were added to the repository.

```bash
PYTHONPATH=src ../tau_bench/.venv/bin/python \
  src/experiments/reference_action_validator/validate.py \
  --output-dir /tmp/tau-reference-report

PYTHONPATH=src ../tau_bench/.venv/bin/python \
  src/experiments/reference_action_validator/validate.py \
  --domains airline --output-dir /tmp/tau-airline-report

PYTHONPATH=src ../tau_bench/.venv/bin/python \
  src/experiments/reference_action_validator/reproduce_task105.py \
  --output /tmp/tau-task105.json

PYTHONPATH=src LITELLM_LOCAL_MODEL_COST_MAP=True ../tau_bench/.venv/bin/python \
  -m pytest src/experiments/reference_action_validator/test_validate.py -q
```

With your own prepared environment, replace `../tau_bench/.venv/bin/python` with its Python executable. `PYTHONPATH=src` must point to this checkout; both command-line scripts verify the imported source. The validator pins `TAU2_DATA_DIR` to this checkout's bundled data. Put outputs outside `src/tau2` and `data/tau2/domains` so writing reports cannot alter the inspected inputs.

`--domains` supports airline, retail and telecom; `--split` supports base (default), train and test. The recorded results use base. This is a standalone experiment, not an installed `tau2 check-data` option.

Exit codes:

- **0:** All selected replayed references completed without exceptions. Skips remain visible.
- **1:** At least one reference action raised; validation otherwise completed. The full recorded run intentionally exits 1.
- **2:** Validation is incomplete: task-loading/setup error, blocked network attempt, input mutation, or no replayable references. Argument parsing errors also exit 2. An uncaught process/output I/O failure produces a nonzero process error and must not be interpreted as a completed validation report.

## Method and boundaries

- Each task gets a fresh environment with a copied initial state. Initialization data, initialization actions and message history are replayed with strict state restoration, matching the existing reference setup.
- Actions run sequentially with their original assistant/user requestor. Like the current evaluator, the diagnostic continues after exceptions; later errors carry `after_prior_failure=true` because they may be consequences of earlier failure. A failing action may partially mutate its environment; the prototype does not roll it back.
- Python socket connections and DNS are blocked during domain import/replay. The local LiteLLM cost map is selected before import. Both final commands recorded zero network attempts. This guard is an execution check for the supported bundled domains, not a security sandbox for arbitrary plugins or subprocesses.
- Core Python files, domain input files and dependency files are hashed before and after replay. Recorded inputs were unchanged. Reports contain public synthetic benchmark identifiers, not candidate or employer-private data.
- Successful replay means only that calls did not raise. It does not prove task feasibility, correct policies, valid gold outcomes, or accurate scores. Error-shaped return values are not generically interpreted as exceptions. Initialization replay retains the upstream behavior of skipping read/history calls where applicable.
- Banking knowledge, voice runs, alternate external datasets, and tau2-bench-Verified are outside this prototype's tested scope. No reference task is rewritten, no exception is suppressed in the benchmark, and no scoring default changes.

## Validation and proposed maintainer discussion

17 tests cover continued replay, cascading-error labels, setup/loader failures, missing references, isolation, state/requestor forwarding, exit codes, network blocking, actual retail regressions, both task-105 environment evaluators, and successful/failed CLI runs. Repository lint/format checks pass. Raw test output is in the local work record; a full live-API core suite was not needed for these isolated experiment files and is not claimed green.

A possible follow-up is an opt-in `tau2 check-data --replay-reference-actions` mode. Before integrating it, ask maintainers whether to continue or stop at the first exception, how intentionally failing references should be represented, which domains/splits are supported, and whether an existing failure baseline should gate CI. Data repairs and score/version changes should be reviewed separately. Credit the issue reporter and avoid duplicating any new patch they may open.

AI assistance: developed and checked with OpenAI Codex. Local research only; no upstream endorsement is implied.
