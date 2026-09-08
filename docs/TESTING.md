# Testing Hivemind

This guide covers the tests shipped in a public Hivemind source snapshot and
the checks run by [Public CI](../.github/workflows/ci.yml). Use an isolated
development environment; the nominal Python suite needs no production
credentials or live provider account.

## Install and run the public checks

Python 3.11 or newer, `uv`, and Node.js 24 are required for the complete
nominal suite, including its dependency-free browser-script harnesses.

```bash
uv sync --locked --dev
uv run --no-sync pytest tests/ -q --strict-markers --durations=50
uv run python scripts/check_doc_links.py
git diff --check
```

Public CI runs the complete Python suite and documentation checks on Python
3.11 and native Python 3.14.6 arm64. It does not build container images, run
paid provider certification, or publish packages. Release image validation is
a separate maintainer step; container publication uses the tag-gated
[release workflow](../.github/workflows/release.yml).

Passing a focused selection is useful evidence for a change, not a substitute
for the complete applicable checks. Report the exact command, runtime, result,
and any skips when contributing.

## Pytest taxonomy

Every collected Python test receives exactly one primary marker from its
repository path. The ordered rules live in
[`tests/test_quality_policy.py`](../tests/test_quality_policy.py) and the marker
declarations live in [`pyproject.toml`](../pyproject.toml).

| Marker | Meaning | Example command |
| --- | --- | --- |
| `unit` | Isolated behavior without a dedicated runtime dependency | `uv run pytest -m unit -q` |
| `integration` | Cross-component, subprocess, runtime, Docker, or smoke behavior | `uv run pytest -m integration -q` |
| `contract` | Documentation, policy, workflow, and public-surface contracts | `uv run pytest -m contract -q` |
| `security_protocol` | Authorization, recovery, routing, and fail-closed protocol safety | `uv run pytest -m security_protocol -q` |
| `e2e` | Collected Python end-to-end scenarios | `uv run pytest -m e2e -q` |

The orthogonal `slow` and `optional` markers do not replace a primary marker:

- `slow` selects exhaustive opt-in exploration. Run the current property suite
  with `HIVEMIND_PROPERTY_SLOW=1 uv run pytest -m slow -q`.
- `optional` identifies a proof requiring an external binary or service.
  Discover it with `uv run pytest -m optional --collect-only -q`, then inspect
  each test's prerequisites before executing it. Current optional proofs use
  `HIVEMIND_TEST_CADDY_BIN` and `HIVEMIND_QDRANT_TEST_URL`; point them only at
  resources dedicated to testing.

The default `uv run pytest tests/` remains the complete nominal Python suite.
An environment-dependent skip is a recorded limitation, not evidence that the
skipped behavior passed.

## Dedicated and manual runners

These suites are separate from nominal pytest discovery. Their entry points
are listed in `DEDICATED_SUITES` inside
[`tests/test_quality_policy.py`](../tests/test_quality_policy.py).

| Suite | What it verifies | How to start |
| --- | --- | --- |
| Playwright E2E | The shipped admin UI with controlled API responses | Install and run as shown below |
| Embedded-credential Docker integration | Credential-volume handling in real containers | `bash scripts/verify_embedded_secret_docker.sh` |
| Manual server recipe | Selected operations against an explicitly configured test stack | `uv run python scripts/test_recette.py --list` |
| Async-ingestion demonstrator | A live example, with limitations described in the [CLI guide](../scripts/README.md#async-ingestion-demonstrator--test_async_ingest_e2epy) | `uv run python scripts/test_async_ingest_e2e.py --help` |

Playwright uses Chromium and serves the real static bundle with controlled
network responses; it does not need a running Hivemind deployment:

```bash
cd tests/e2e
npm ci
npx playwright install chromium
npx playwright test --list
npx playwright test
```

On Linux, Chromium may also require operating-system packages. Install the
browser's prerequisites in your disposable test environment. Keep the browser
sandbox enabled for ordinary local runs.

The Docker proof needs Docker and builds isolated test images/containers. The
manual server scripts can write or delete test data and spend provider budget:
read the [CLI and manual-test guide](../scripts/README.md), use a disposable
stack and dedicated credentials, and select the intended suite explicitly.
Never run them against an existing customer space. The async demonstrator's
success banner is not proof of rollback, cleanup, or complete data integrity.

`pyproject.toml` restricts pytest discovery to `tests/`. A new
`scripts/test_*.py` file must be registered in the dedicated/manual suite
manifest or tooling allowlist; otherwise the taxonomy contract fails. Do not
wrap an entire manual or dedicated runner inside a collected pytest test.

## One active runner per process tree

The nominal suite and guarded dedicated runners use
`HIVEMIND_ACTIVE_TEST_RUNNER` to prevent accidentally launching complete
nested suites. Full nested pytest selection, unrelated Playwright processes,
the embedded-credential Docker proof, and the manual server recipe refuse
nested execution. Playwright admits only its owning process and direct workers.
A focused nested pytest file or node ID remains available for process-isolation
tests.

- Refused below a guarded runner: `pytest`, `pytest tests/`,
  `npx playwright test`, `bash scripts/verify_embedded_secret_docker.sh`,
  and `python scripts/test_recette.py`.
- Permitted for a focused process-isolation proof: `pytest tests/test_config.py`
  or one `file.py::nodeid`.

The async-ingestion demonstrator does not currently enforce this runner guard.
Do not launch it from another test runner. A process-tree guard does not
coordinate independent shells or protect a live service from test traffic.

## Regression and mutation evidence

For a behavior change, add a focused regression that fails without the fix and
passes with it. Where feasible, prove a safety guard by removing or weakening
the guarded input or branch and showing that the test fails. Do not delete
cases, lower expectations, or count skipped cases as successful proofs.

For example, the shipped alias-registration guard exercises missing sources,
existing canonical names, collisions, and missing callables:

```bash
uv run pytest tests/test_mcp_tool_surface.py::test_alias_registration_fails_closed -q
```

A new parameter row should exercise a distinct branch, protocol state, input
class, platform contract, failure message, or mutation. Give rows stable,
descriptive IDs. Avoid multiplying examples that reach the same branch and
assertion.

Use `--durations=50` and repeat comparable runs in the same environment when
investigating regressions. Timings from different machines or provider-backed
runs are not interchangeable, and this snapshot makes no portable runtime or
coverage-percentage promise.
