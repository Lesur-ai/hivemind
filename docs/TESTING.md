# Testing Hivemind

Hivemind keeps one nominal pytest suite and separate first-class runners for
browser and manual end-to-end checks. The categories below are selection and
evidence tools, not a target for the number of tests.

## Pytest taxonomy

Every collected Python test receives exactly one primary marker from its
repository path. The ordered rules live in `tests/test_quality_policy.py` and
the marker declarations live in `pyproject.toml`.

| Marker | Meaning | Example command |
| --- | --- | --- |
| `unit` | Isolated behavior without a dedicated runtime dependency | `uv run pytest -m unit -q` |
| `integration` | Cross-component, subprocess, runtime, Docker, or smoke behavior | `uv run pytest -m integration -q` |
| `contract` | Documentation, ADR, policy, workflow, public-surface, and release contracts | `uv run pytest -m contract -q` |
| `security_protocol` | Protocol, authorization, recovery, routing, and fail-closed safety | `uv run pytest -m security_protocol -q` |
| `e2e` | Collected Python end-to-end scenarios | `uv run pytest -m e2e -q` |

The orthogonal `slow` and `optional` markers do not replace a primary marker:

- `slow` is exhaustive opt-in exploration. Run the current property suite with
  `HIVEMIND_PROPERTY_SLOW=1 uv run pytest -m slow -q`.
- `optional` identifies an explicit external binary or service proof that may
  skip on an unsupported local host. Run discovery with
  `uv run pytest -m optional --collect-only -q`, then provide the environment
  named by each skip reason. The current optional proofs use
  `HIVEMIND_TEST_CADDY_BIN` and `HIVEMIND_QDRANT_TEST_URL`.

The default `uv run pytest tests/` remains the complete nominal Python suite.
Platform-specific shipped behavior is not called optional merely because it
skips on another platform.

## Dedicated and manual runners

Dedicated suites are explicit in `DEDICATED_SUITES` inside
`tests/test_quality_policy.py`:

| Category | Distribution | Discovery | Execution |
| --- | --- | --- | --- |
| Playwright E2E | Private and public | `cd tests/e2e && npx playwright test --list` | `cd tests/e2e && npm ci && npx playwright test` |
| Reviewer-tooling unit suite | Private only | `rg --files scripts -g '*.test.js'` | Run each discovered file with `node`; Private CI pins all four commands explicitly |
| Embedded-credential Docker integration | Private and public | `scripts/verify_embedded_secret_docker.sh` plus its container helper | `bash scripts/verify_embedded_secret_docker.sh`; the dedicated Private CI job is required by the image build |
| Manual recipe | Private and public | `uv run python scripts/test_recette.py --list` | `uv run python scripts/test_recette.py --suite <name>` after the documented stack setup |

Each manifest entry declares its `private`/`public` distribution scope. The
private contract verifies every private path and CI command; the copied public
contract verifies only runners actually shipped in the sanitized tree. A
private-only suite is never required to leak into the public export merely to
satisfy the shared policy test.

`pyproject.toml` restricts pytest discovery to `tests/`. A new
`scripts/test_*.py` file must be added either to the manual-suite manifest or
to the repository-quality tooling allowlist; otherwise the taxonomy contract
fails. A manual or dedicated runner must never be wrapped by a collected pytest
test.

The protected provider-certification workflow is an operator-authorized paid
proof, not a nominal test suite. Its manual dispatch contract, fixed cost
ceiling, and redacted artifact path remain governed by
`.github/workflows/provider-certification.yml`; TQ-7 does not make it a PR
dependency.

## One active runner per process tree

The first runner exports `HIVEMIND_ACTIVE_TEST_RUNNER` to child processes.
Complete nested pytest selection, an unrelated Playwright process, the
embedded-secret Docker proof, and the manual recipe refuse to start when that
variable already names an active runner. Playwright recognizes only the owner
process that claimed its runner and the direct workers it created. A focused
nested pytest file or node ID remains available for process-isolation proofs.

Examples:

- refused below pytest: `pytest`, `pytest tests/`, `npx playwright test`,
  `bash scripts/verify_embedded_secret_docker.sh`, and
  `python scripts/test_recette.py`;
- allowed below pytest: `pytest tests/test_config.py` or one `file.py::nodeid`.

This guard prevents a nominal pytest case from silently launching an entire
second suite. It does not merge distinct CI jobs: the Python 3.11, native
Python 3.14.6 arm64, Playwright, Docker, and public-stage jobs remain separate
because they prove different environments or deliverables.

## Fast correctness, focused coverage, and exhaustive trend evidence (TQ-12)

TQ-12 (#350) split what used to be one combined job into three tiers, because
whole-repository branch-coverage instrumentation was the dominant cost on the
critical path every PR waited on:

1. **Fast correctness** — the private `Tests` job runs a plain, uninstrumented
   `pytest tests/ -q --strict-markers --durations=50` on native
   GitHub-hosted arm64 with an in-job CPython 3.11/architecture assertion. No
   coverage, no policy report; this is the required PR gate and the fastest of
   the three. The complete suite, flags, Node-backed harnesses, and fail-closed
   public-export structural proof remain unchanged.
2. **Focused guarded coverage** — the parallel `Coverage floors (focused,
   TQ-12)` job runs the TQ-7 quality runner
   (`scripts/test_quality_ci.py --coverage-mode=focused`), enforcing the eight
   critical-surface floors and three required mutation families below. Also
   PR-blocking (wired into `required_ci`/`build` the same way
   `test_python314_arm64`/`public_tree` already were), but off the `Tests`
   job's own timing. Its coverage command is byte-identical to what the
   combined job used to run — an `--include`-based narrower trace scope was
   tried during implementation and reverted: `coverage.py` silently ignores
   `--include` whenever `--source` is also set, and dropping `--source`
   loses the directory-walk that makes an entirely-unexecuted critical file
   still fail its floor instead of silently vanishing from the measured
   denominator.
3. **Exhaustive trend evidence** — `.github/workflows/exhaustive-coverage.yml`
   runs the identical coverage command with `--coverage-mode=exhaustive` on a
   weekly schedule, on manual `workflow_dispatch` (optionally against an exact
   `source_sha`, usable as one of the checks an operator cites in an RC's
   `RC-VALIDATION` comment — see `docs/WORKFLOW_GIT_EPIC.md`), and never on
   `pull_request`/`push`. It additionally reports `overall_coverage`: a
   repo-wide aggregate across every measured file, not just the eight
   groups — informational only, never merge-blocking, never a floor.

Both coverage-mode runs publish to the job log and GitHub step summary:

- collected cases, authored functions, parameter expansions, and outcomes;
- cases/functions/expansions per primary marker;
- cases/functions/expansions and outcomes for `slow` and `optional` markers;
- wall-clock duration, runtime by file, and the 50 slowest cases;
- targeted branch coverage and its floor per critical surface;
- collected case counts for the required mutation families; and
- exhaustive mode only: the repo-wide `overall_coverage` trend summary.

The CI invocation uses pytest's quiet progress output rather than one line per
case. JUnit still carries every stable case identity and duration, while the
analyzer prints the 50 slowest cases and the complete policy report at the end.
This keeps the report and runtime annotation below the GitHub step-log limit as
the suite grows.

Other nominal Python jobs (including the fast `Tests` job above) use
`--durations=50 --strict-markers` so architecture or public-runner variance
stays visible without applying a foreign hard budget.

The canonical private policy's `reference` is derived from the exact
private-x64 focused-coverage run on TQ-12 #350 PR head `d110635`: 587.395
seconds at 5,704 collected cases (`docs/testing/test-quality-policy.json`).
This field was named
`nominal_wall_seconds` through TQ-7 but was always compared against a
coverage-INSTRUMENTED `wall_seconds` — reusing an uninstrumented TQ-0 baseline
as the budget for an instrumented run made healthy `coverage_floors` runs
alert far more often than intended (the exact mismatch behind "489.5s versus
a 317.7s warning-only budget" at TQ-12's own baseline measurement). The field
is renamed `reference_wall_seconds` and recalibrated so its name matches what
it has always been compared against. The effective reference never drops
below that value; when the executed suite grows, it scales by
`collected_cases / 5704` before the variance multiplier is applied. This keeps
the reference immutable without enforcing a smaller-suite absolute time
against a larger workload. The heterogeneous private x64 fleet and the
exhaustive-coverage workflow share an explicit alert-only profile:

- above the workload-adjusted reference by 50%: visible alert, job remains
  green (`focused` mode) and emits a GitHub Actions warning annotation.
  Exhaustive mode records `wall_seconds` for visibility but never evaluates a
  budget against it (`runtime.budget_evaluated: false`) — it is a
  schedule/manual trend job, not a PR gate with a "healthy run" budget to
  violate.

For an illustrative 8,000-case head, the effective reference is 823.836
seconds and the alert is 1,235.754 seconds. The report publishes the reference,
case scale, effective reference, threshold, and enforcement mode so
recalibration cannot be silent. Test failures, targeted coverage floors, and
mutation evidence remain hard failures independently of runtime alerts, and
identically so in both coverage modes.

The alert is an investigation trigger, not authorization to delete tests. A
sustained alert is resolved by interleaved same-host measurements, as TQ-4 did,
or by a reviewed profile recalibration with recorded evidence. Never raise or
remove a threshold merely to make a run green. Two same-workload candidate
runs on different self-hosted x64 groups once measured 308.981 and 455.831
seconds under the pre-TQ-12 combined job, a 47.5% spread; a single-run wall
clock is therefore evidence to alert on, not a sound cross-runner
hard-failure oracle.

## Fail-closed heavyweight PR routing (TQ-13)

TQ-13 (#351) lets the private CI avoid only environment-specific work when a
repository-owned classifier has complete, versioned evidence. `Tests`,
`Dependency audit (pip-audit)`, and `Coverage floors` always run for pull
requests and manual dispatches. The classifier may selectively skip only the
native Python 3.14.6 arm64 suite, Admin Playwright suite, embedded-secret Docker
proof, and staged public-tree check.

For a documentation-only change, the minimal safe PR matrix is therefore the
classifier, those three always-on gates, the protected Compose validation in
`Build Docker Image`, and `Required CI`; the four routed environment jobs can
be skipped. UI/static paths select Playwright/public-tree, while public-stage
paths select public-tree only; embedded runtime paths select
arm64/public-tree/Docker; platform and dependency paths select every routed job.
Selection is the union when paths overlap.

This is intentionally not an impact inference service. The CI log prints a
machine-readable classifier state, reason, and selected checks. Unknown paths,
renames with incomplete old-path data, a shallow or inconsistent checkout,
partial/malformed GitHub API responses (including the API's file-list cap),
invalid output, or any classifier error select all four checks. The first pull
request introducing the classifier also selects all four because the base copy
is unavailable. On normal runs, the classifier/aggregate-validator script comes
from the base-SHA copy rather than trusting a candidate script edit; the PR-head
workflow and its contract tests remain subject to protected review and branch
protection.
`Build Docker Image` and `Required CI` accept a skipped routed job only when its
exact successful classifier evidence says `false`; a missing, cancelled, failed,
or mismatched result is a hard failure.

The introducing PR's green run therefore proves the conservative bootstrap
fallback, not the normal base-script runtime. The first deliberately
documentation-only PR after TQ-13 merges is the live checkpoint: inspect a
`classified`/`documentation-only` output with all four selectors `false`, the
four expected skips, and successful normal Build and `Required CI` validators.
Any failure remains merge-blocking and must be corrected or rerun rather than
treated as a selective skip.

## Targeted coverage expectations

Coverage is branch-aware and enforced per critical surface. There is no
misleading repository-wide floor: exhaustive mode's `overall_coverage`
(TQ-12, #350) is a repo-wide trend number for schedule/manual/RC visibility,
never a gate, and never rounds down into a floor here.

| Surface | Minimum |
| --- | ---: |
| Protected certification budget | 76.0% |
| Hivemind state | 92.0% |
| Mesh | 83.0% |
| Permissions | 78.0% |
| Protected certification manifest and provenance | 77.0% |
| Recovery | 76.0% |
| MCP surface | 63.0% |
| Release audit | 83.0% |

These floors are rounded down from the TQ-0 measurements and must not be
lowered by hand. A legitimate authority or file-group change requires a new
clean measurement, rationale, focused tests, and review.

The two P13-4 groups were added from the exact 2026-08-03 full-suite
branch-aware measurement: 76.02% for the certification ledger and 77.46% for
the manifest/provenance reader, rounded down independently to 76% and 77%.

## Mutation evidence

The nominal JUnit report must contain every case in three fail-closed mutation
families consolidated by the Test Quality EPIC, and every required case must
pass rather than skip:

| Guard family | Required cases | Focused local command |
| --- | ---: | --- |
| Alias registration: missing source, existing canonical, collision, missing callable | 4 | `uv run pytest tests/test_mcp_tool_surface.py::test_alias_registration_fails_closed -q` |
| ADR registry: missing/duplicate/illegal/drift mutations | 7 | `uv run pytest tests/test_adr_registry.py::test_registry_validation_mutations_fail_closed -q` |
| Accepted architecture critical-clause weakening | 9 | `uv run pytest tests/test_architecture_contracts.py::test_critical_contract_mutations_are_detected -q` |

Each family changes the guarded input or branch and asserts the validator goes
RED. Merely retaining a test name is insufficient: CI binds file, function,
and minimum parameterized case count.

## Parameter matrices

A new parameter row is justified only when it exercises at least one distinct
branch, protocol state, input class, platform contract, failure message, or
mutation. Give rows stable descriptive IDs and state the unique behavior in
the test or nearby table. Do not multiply examples that reach the same branch
and assertion.

Before expanding a large matrix:

1. compare the authored-function and parameter-expansion counts;
2. prove the new row goes RED when its unique guard is removed or weakened;
3. check the file and suite duration evidence; and
4. use a dedicated optional/manual suite instead of making unsupported hosts
   emulate an external platform.

## Reproducible full baseline

Maintainers retain a private TQ-0 measurement procedure that performs
collection, one nominal timing run, and a separate coverage run for reviewable
before/after evidence. It is intentionally heavier than the one-pass CI runner
and is used only at test-quality checkpoints, never inside a collected test.
Public contributors can reproduce the relevant suite or marker category with
the commands above and should include the observed duration in their PR.
