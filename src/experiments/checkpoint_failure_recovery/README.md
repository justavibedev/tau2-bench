# Checkpoint recovery under failed trajectory replacement

This reproducible fault-injection experiment accompanies [tau2-bench PR #533](https://github.com/sierra-research/tau2-bench/pull/533). It tests whether a previously saved trajectory remains recoverable when writing its replacement fails. It uses synthetic records and makes no model calls or benchmark-score claims.

The experiment is published on a separate fork branch. It is not part of the upstream PR, and the PR remains unmerged as of September 10, 2026.

## Result

| Failure injected | Simulation ID | Baseline preserves original | Fix preserves original | Retry succeeds (both) |
|---|---|---|---|---|
| Temporary file creation | New | No | Yes | Yes |
| Serialization | New | No | Yes | Yes |
| Trajectory rename | New | No | Yes | Yes |
| Temporary file creation | Reused | No | Yes | Yes |
| Serialization | Reused | No | Yes | Yes |
| Trajectory rename | Reused | No | Yes | Yes |

The baseline deletes the previous trajectory before attempting the replacement write. All six injected failures therefore leave metadata referring to a missing trajectory. Reload raises `ValueError`. The fixed version writes the replacement first, preserving the original trajectory and metadata on these failures. It also avoids deleting the newly installed file when the replacement uses the same simulation ID.

The counts describe six deliberately selected, deterministic test cases. They are not estimates of production failure rates or statistical evidence about agent quality. Successful later retries in the baseline do not undo loss of the old trajectory during the failure window.

## Pinned inputs and recorded outputs

- Baseline: [`2174a603f6d014ef94473ffa95957f6ce27100db`](https://github.com/sierra-research/tau2-bench/commit/2174a603f6d014ef94473ffa95957f6ce27100db).
- Fix: [`ece0bc2c511a916a4fb399560041def8b8384340`](https://github.com/justavibedev/tau2-bench/commit/ece0bc2c511a916a4fb399560041def8b8384340).
- Python: **3.12.11**. Recorded host: Darwin, arm64; release and source hashes are in [`results/manifest.json`](results/manifest.json).
- Exact installed Python package versions: [`requirements.txt`](requirements.txt). This includes dev tools and optional packages needed by upstream's eager imports; it is an environment snapshot, not a minimal dependency list. Source code comes from the pinned Git commits, not an editable package path.
- Raw observations: [`results/baseline.jsonl`](results/baseline.jsonl) and [`results/fixed.jsonl`](results/fixed.jsonl). Each row records hashes before/after failure, directory contents, reload outcome, injected-error count, and the retry outcome.

The runner records SHA-256 hashes for itself, requirements, both checkpoint implementations, and each revision's `uv.lock`. Both source revisions have the same upstream dependency lock.

## Reproduce

The checkout must contain both pinned commit objects; use a normal clone, not a shallow download. Git, uv, and Python package installation access are needed for setup. The experiment itself runs offline without credentials.

```bash
git clone --branch experiment/checkpoint-failure-recovery https://github.com/justavibedev/tau2-bench.git
cd tau2-bench
uv venv --python 3.12.11 .venv-checkpoint
uv pip install --python .venv-checkpoint/bin/python -r src/experiments/checkpoint_failure_recovery/requirements.txt
.venv-checkpoint/bin/python src/experiments/checkpoint_failure_recovery/run.py --output checkpoint-reproduction
```

Expected terminal output:

```text
baseline: preserved 0/6; successful retries 6/6
fixed: preserved 6/6; successful retries 6/6
```

Compare the JSONL files with the recorded results. Host metadata in the manifest may differ. The runner exits unsuccessfully if a failure is not injected exactly once, a retry does not produce exactly one replacement trajectory/index entry, or the expected preservation counts differ.

The requirements were installed into a fresh Python 3.12.11 environment and the complete runner reproduced these results. Synthetic timestamps are fixed so the recorded JSONL hashes can be compared directly across reruns in that environment.

## Method

1. Export `src/`, `data/`, `pyproject.toml`, and `uv.lock` directly from each pinned commit with `git archive`. Run each revision in a fresh child process that imports its real tau2 source and models. Uncommitted checkout changes cannot affect the source under test.
2. For each of six cases, create a fresh directory-format checkpoint with one synthetic task and save one completed trajectory through `create_checkpoint_fns`.
3. Replace that trajectory, injecting one `OSError` at temporary-file creation, serialization, or the first `os.replace` (the trajectory installation). Cross these stages with a new versus reused simulation ID.
4. Remove the injected fault. Before retrying, compare the original trajectory and metadata hashes, check directory contents, and reload through `Results.load`.
5. Retry with the same checkpoint closure and verify that only the intended replacement trajectory and index entry remain.

Preservation requires **all** of the following: unchanged original trajectory bytes, unchanged metadata bytes, no extra/temporary files, and a loaded simulation equal to the original. Both revisions use the same synthetic fixture, package environment, failure stages, and inspection criteria.

The existing focused regression suite was also rerun against the fixed checkout: `pytest tests/test_results_format.py tests/test_checkpoint.py -q` reported **55 passed, 2 warnings** (upstream `audioop` deprecation and an unrecognized pytest asyncio config option). The previous full core run included live-API failures; no full-suite success is claimed here.

## Limits and a next improvement to discuss

This is a local filesystem test of handled exceptions before trajectory installation succeeds. It does **not** test power loss, `SIGKILL`, durability after reboot, metadata-write failure after a successful trajectory rename, unlink failure, cross-process contention, network filesystems, long-running evaluations, or benchmark scoring. Package versions are pinned; wheel hashes and an OS/container image are not. The recorded run was on macOS, not a cross-platform validation.

A useful next proposal for maintainers is a **recovery contract for directory checkpoints when trajectory and metadata updates disagree**. Start with targeted failure injection between trajectory rename, old-file removal, and metadata replacement, plus process-restart tests. Agree on which on-disk record is authoritative and how orphaned trajectories should be recovered before implementing a journal or changing the storage format. This is a proposed discussion scope, not an accepted roadmap item or a claim that PR #533 makes multi-file writes atomic.

The implementation and experiment were developed with AI coding assistance. The linked source, regression tests, pinned inputs, and raw observations provide the reviewable evidence.
