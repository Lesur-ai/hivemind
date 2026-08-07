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

## CI timing and runtime policy

The primary private x64 job runs pytest exactly once under branch coverage via
the TQ-7 quality runner. It publishes to the job log and GitHub step summary:

- collected cases, authored functions, parameter expansions, and outcomes;
- cases/functions/expansions per primary marker;
- cases/functions/expansions and outcomes for `slow` and `optional` markers;
- wall-clock duration, runtime by file, and the 50 slowest cases;
- targeted branch coverage and its floor per critical surface; and
- collected case counts for the required mutation families.

The CI invocation uses pytest's quiet progress output rather than one line per
case. JUnit still carries every stable case identity and duration, while the
analyzer prints the 50 slowest cases and the complete policy report at the end.
This keeps the report and runtime annotation below the GitHub step-log limit as
the suite grows.

Other nominal Python jobs use `--durations=50 --strict-markers` so architecture
or public-runner variance stays visible without applying a foreign hard budget.

The canonical private policy is derived from the reviewed TQ-0 #284 baseline:
136.053 seconds at 3,590 collected cases. The effective reference never drops
below that value; when the executed suite grows, it scales by
`collected_cases / 3590` before the variance multiplier is applied. This
keeps the historical reference immutable without enforcing an arm64 absolute
time against a larger x64 workload. The heterogeneous private x64 fleet uses
an explicit alert-only profile:

- above the workload-adjusted reference by 50%: visible alert, job remains
  green and emits a GitHub Actions warning annotation.

For the 5,321-case integration head, the effective reference is 201.654
seconds and the alert is 302.481 seconds. The report publishes the historical
reference, case scale, effective reference, threshold, and enforcement mode so
recalibration cannot be silent. Test failures, targeted coverage floors, and
mutation evidence remain hard failures independently of runtime alerts.

The alert is an investigation trigger, not authorization to delete tests. A
sustained alert is resolved by interleaved same-host measurements, as TQ-4 did,
or by a reviewed profile recalibration with recorded evidence. Never raise or
remove a threshold merely to make a run green. Two same-workload candidate
runs on different self-hosted x64 groups measured 308.981 and 455.831 seconds,
a 47.5% spread; a single-run wall clock is therefore evidence to alert on, not
a sound cross-runner hard-failure oracle.

## Targeted coverage expectations

Coverage is branch-aware and enforced per critical surface. There is no
misleading repository-wide floor.

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
