# Testing architecture

ActiveLoom separates short feedback loops from full regression coverage. The
Python suite includes unit tests, contract checks, and integration scenarios
that create Git repositories, run review controllers, and launch child
processes. Those scenarios remain valuable, but repeating every scenario during
each local review monopolizes a shared test host.

## Choose a lane

| Lane         | Selection                                                                | Budget through `scripts/run-tests.py` | Use                                        |
| ------------ | ------------------------------------------------------------------------ | ------------------------------------- | ------------------------------------------ |
| `fast`       | Unit and contract tests; excludes `regression` unless also marked `fast` | 120 seconds, two workers              | Default for iteration, review, and push CI |
| `focused`    | All cases in explicitly named files or node IDs                          | 120 seconds, two workers              | Verify a changed integration behavior      |
| `regression` | All tests, including every deferred scenario                             | 1,200 seconds, four workers locally   | Weekly CI or an explicit full checkpoint   |

```bash
# Routine Python gate, including all fast tests.
python3 scripts/run-tests.py fast

# A small integration regression for a changed controller behavior.
python3 scripts/run-tests.py focused tests/test_agent_loop.py::test_v3_clean_results_attest_and_converge

# Full regression is an explicit choice.
python3 scripts/run-tests.py regression
```

The runner uses the current Python interpreter and runs from the repository
root. Install pytest, pytest-xdist, and PyYAML in that interpreter's environment;
the pinned versions and mypy dependencies are in [CI](../.github/workflows/ci.yml).
Use the repository's `.nvmrc` runtime for tests that invoke Node. Static checks,
prompt rendering, CLI tests, and review-ledger package validation keep their
existing commands in CI and the review contract.

Bare `python3 -m pytest`, including old `pytest tests/ -n 4` commands, defaults
to the fast lane. The selector is independent of `-m` and `-k`: a marker or name
filter does not opt into regression. Direct pytest invocations do not have the
runner's wall-clock deadline. Use `--test-lane=focused` with explicit files, or
`--test-lane=regression`, when calling pytest directly for those lanes. An empty
selection fails with pytest's usual nonzero exit code; it is not validation.

The runner returns test failures unchanged. A timeout exits 124; cancellation
stops the owned process group on POSIX hosts. Tests that create detached sessions
must still own and clean those descendants. A deliberate timeout override goes
before the lane (`--timeout-seconds 180 fast`); investigate budget overruns before
increasing the limit. A missing dependency or failed fast check never triggers
an automatic full-suite retry.

## Maintain the boundary

- Add cheap unit and contract tests to the fast lane by default. Prefer calling
  functions directly with small fixtures over creating repositories or starting
  a controller for every assertion.
- Mark subprocess-heavy, timeout, lifecycle, and end-to-end scenarios
  `@pytest.mark.regression`. A predominantly integration module can declare
  `pytestmark = pytest.mark.regression` once.
- In a regression-marked module, use `@pytest.mark.fast` only for cheap tests
  with lightweight fixtures. This keeps parser, failure-recognition, path,
  configuration, and source-parity contracts in routine validation.
- When changing a deferred integration scenario, run its focused cases once
  against the final implementation and record the command and result. Broaden
  the selection when the change crosses boundaries; a full regression remains
  an explicit checkpoint.
- Review the runner's duration report when a new fast test creates a noticeable
  delay. Do not hide failures by moving tests between lanes. New slow scenarios
  should come with a smaller unit or contract check wherever a useful seam exists.

The initial separation used CI JUnit timings: agent-loop, review-controller,
and launcher scenarios dominated runtime, while the in-process sync engine,
renderer, configuration, and contract checks were inexpensive. Classification is
checked in as markers rather than learned automatically from a timing cache.
Acceptance tests that repeatedly launch Node, Git push scenarios, and repeated
repository-wide parity probes also belong to regression. Their cheap contract
and source-parity checks remain fast. Every deferred test remains in the full
regression collection.

## CI and review

`ci.yml` runs the fast Python lane and mypy in the existing required
`Python types + tests` check, including draft PRs. It has no Python regression
matrix. Review uses the same fast command through `.activeloom-review.json`;
the runner deduplicates the fallback command when a change includes an unmapped
path. One review owner runs validation; review lanes inspect the resulting
evidence rather than launching additional suites.

The `Focused integration tests` job closes the gap the fast lane leaves. For a
ready PR or a push to `main` it passes the changed paths to
`scripts/select-focused-tests.py` and runs the selected regression-marked
modules in the focused lane; a change that selects nothing runs no tests. The
script owns the path map. A new regression-marked module must be added to it,
with the source paths that should trigger it, or `tests/test_test_lanes.py`
fails. Local review does not run this selection, so the shared test host stays
on the fast lane.

`regression.yml` runs weekly on Sunday at 05:17 UTC and supports manual dispatch:

```bash
gh workflow run regression.yml --ref <branch>
```

All collection and execution commands explicitly select the regression lane.
The workflow retains four pytest-split shards with four xdist workers each,
requires all shards to succeed, verifies a complete and disjoint partition,
combines subprocess branch coverage, and enforces the existing 80% floors for
the sync engine, signed-commit helper, renderer, and parity lint. A failure stays
visible on that workflow and needs triage; a green fast check does not claim
that full regression passed. A scheduled failure opens an issue titled
"Weekly regression suite is failing", or comments on the open one. Before
preparing a release or advancing a distribution ref, require a successful run
at the exact commit:

```bash
scripts/require-regression-green.sh <commit-sha>
```

This repository's "unfiltered gating suite" means all tests selected by the
fast lane. It does not mean the separately scheduled regression suite. Validation
receipts and review attestations still refer to the exact head they checked.
Controllers pin the base revision's review contract, so existing checkpoints
may retain older commands until restarted through their documented recovery
flow; do not edit checkpoint state to replace their policy.
