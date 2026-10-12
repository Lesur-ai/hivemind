"""Evidence must prove that bounded CI shards ran the complete collected suite."""

from copy import deepcopy

import pytest

from scripts.ci_pytest_shards import ShardError, make_plan, validate_plan, verify_results


SOURCE = "a" * 40
DIGEST = "b" * 64
NODES = [f"tests/test_example.py::test_case[{index}]" for index in range(12)]


def plan():
    return make_plan(
        NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
        mode="plain", weights={NODES[0]: 300},
    )


def results(value):
    return [
        {
            "schema_version": 1, "source_sha": SOURCE, "source_digest": DIGEST,
            "plan_digest": value["plan_digest"], "index": shard["index"],
            "mode": value["mode"], "runtime": value["runtime"],
            "collection_digest": value["collection_digest"], "run_id": value["run_id"],
            "pytest_return_code": 0, "wall_seconds": 1.0,
            "cases": [
                {"nodeid": node, "outcome": "passed"} for node in shard["nodeids"]
            ],
        }
        for shard in value["shards"]
    ]


def test_balancing_is_disjoint_complete_and_keeps_original_relative_order():
    value = plan()
    validate_plan(value)
    assert value["shards"][0]["nodeids"] == [NODES[0]]
    assert sorted(node for shard in value["shards"] for node in shard["nodeids"]) == sorted(NODES)
    for shard in value["shards"]:
        assert shard["nodeids"] == sorted(shard["nodeids"], key=NODES.index)
    assert verify_results(value, results(value), SOURCE, DIGEST) == 4.0


@pytest.mark.parametrize("mutate", [
    lambda value: value["shards"][1]["nodeids"].append(NODES[0]),
    lambda value: value["shards"][1]["nodeids"].pop(),
    lambda value: value["shards"][1].update(index=0),
    lambda value: value.update(source_sha="main"),
    lambda value: value.update(max_test_seconds=3600),
])
def test_changed_or_incomplete_plan_fails_closed(mutate):
    value = plan()
    mutate(value)
    with pytest.raises(ShardError):
        validate_plan(value)


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda rows: rows.pop(), "missing or extra shard"),
    (lambda rows: rows.append(deepcopy(rows[0])), "missing or extra shard"),
    (lambda rows: rows[0].update(source_sha="c" * 40), "mismatched source_sha"),
    (lambda rows: rows[0].update(source_digest="c" * 64), "mismatched source_digest"),
    (lambda rows: rows[0].update(plan_digest="c" * 64), "mismatched plan_digest"),
    (lambda rows: rows[0].update(collection_digest="c" * 64), "mismatched collection_digest"),
    (lambda rows: rows[0].update(run_id="999999999999999999"), "wrong workflow run"),
    (lambda rows: rows[0].pop("run_id"), "wrong workflow run"),
    (lambda rows: rows[0].update(pytest_return_code=1), "pytest did not succeed"),
    (lambda rows: rows[0].update(wall_seconds=float("nan")), "non-finite or negative"),
    (lambda rows: rows[0].update(wall_seconds=421), "test budget exceeded"),
    (lambda rows: rows[0]["cases"].pop(), "partial or extra case"),
    (lambda rows: rows[0]["cases"].append({"nodeid": "tests/test_extra.py::test_extra", "outcome": "passed"}), "partial or extra case"),
    (lambda rows: rows[0]["cases"][0].update(outcome="failed"), "failed/error case"),
    (lambda rows: rows[0].update(index=True), "invalid or duplicated result index"),
])
def test_missing_duplicate_stale_failed_or_partial_result_fails_closed(mutate, message):
    value = plan()
    rows = results(value)
    assert verify_results(value, rows, SOURCE, DIGEST) == 4.0
    mutate(rows)
    with pytest.raises(ShardError, match=message):
        verify_results(value, rows, SOURCE, DIGEST)


def test_a_case_that_cannot_fit_the_test_budget_is_rejected():
    with pytest.raises(ShardError, match="budget"):
        make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST, mode="plain", weights={NODES[0]: 421})


@pytest.mark.parametrize("nodes", [[], NODES + [NODES[0]], ["../secret.py::test_escape"], ["tests/test_example.py::test_case\n--ignore=tests"]])
def test_invalid_collection_cannot_make_a_plan(nodes):
    with pytest.raises(ShardError):
        make_plan(nodes, 4, source_sha=SOURCE, source_digest=DIGEST, mode="plain")


def test_real_branch_coverage_shards_combine_and_retain_unexecuted_sources(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    # coverage.combine changes coverage.files' process-global relative directory.
    # Keep the real inner coverage exercise outside the surrounding shard tracer.
    completed = subprocess.run(
        [sys.executable, "-c",
         "from pathlib import Path; import sys; "
         "from tests.test_ci_pytest_shards import _assert_real_branch_coverage_shards; "
         "_assert_real_branch_coverage_shards(Path(sys.argv[1]))", str(tmp_path)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _assert_real_branch_coverage_shards(tmp_path):
    import json
    import subprocess
    from scripts import ci_pytest_shards as runner

    root = tmp_path / 'checkout'
    root.mkdir()
    (root / 'tests').mkdir()
    source = root / 'src/live_mem'
    source.mkdir(parents=True)
    (source / '__init__.py').write_text('')
    (source / 'example.py').write_text('def choose(flag):\n    if flag:\n        return 1\n    return 0\n')
    (source / 'never_imported.py').write_text('def untouched():\n    return 42\n')
    (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    (root / 'tests/test_example.py').write_text(
        'from live_mem.example import choose\n'
        'def test_yes():\n    assert choose(True) == 1\n'
        'def test_no():\n    assert choose(False) == 0\n'
    )
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=ci', '-c', 'user.email=ci@invalid', 'commit', '-qm', 'fixture'], cwd=root, check=True)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    value = runner.make_plan(runner.collect(root), 2, source_sha=head, git_head=head,
                            source_digest=runner.source_digest(root), mode='coverage')
    evidence = tmp_path / 'results'
    for index in range(2):
        runner.run_shard(root, value, index, evidence / str(index))
    output = tmp_path / 'aggregate'
    runner.aggregate(root, value, evidence, output, head)
    data = json.loads((output / 'coverage.json').read_text())['files']
    assert data['src/live_mem/example.py']['summary']['percent_covered'] == 100
    assert data['src/live_mem/never_imported.py']['summary']['percent_covered'] == 0
    assert len(runner.junit_cases(output / 'pytest.xml', value['nodeids'])) == 2
    # Neither a modified raw coverage database nor an altered XML report can
    # reuse a successful shard's small result.json to produce a green gate.
    raw = evidence / '0/.coverage'
    original = raw.read_bytes()
    raw.write_bytes(original + b'corruption')
    with pytest.raises(runner.ShardError, match='corrupt coverage'):
        runner.aggregate(root, value, evidence, tmp_path / 'corrupt-coverage', head)
    raw.write_bytes(original)
    xml = evidence / '0/pytest.xml'
    xml.write_bytes(xml.read_bytes() + b' ')
    with pytest.raises(runner.ShardError, match='corrupt JUnit'):
        runner.aggregate(root, value, evidence, tmp_path / 'corrupt-junit', head)


def test_timeout_terminates_only_its_owned_process_group(tmp_path):
    import sys
    import time
    from scripts.ci_pytest_shards import owned_run

    start = time.monotonic()
    with pytest.raises(ShardError, match='owned test process exceeded'):
        owned_run([sys.executable, '-c', 'import time; time.sleep(30)'], tmp_path, seconds=0.05)
    assert time.monotonic() - start < 4


def test_complete_collection_retains_the_inherited_nested_runner_guard():
    from pathlib import Path
    from scripts.ci_pytest_shards import collect
    # Collection is itself a complete-suite entrypoint. It must retain the
    # parent identity so the repository's configure hook refuses it before
    # recursively collecting or executing the suite below this pytest runner.
    with pytest.raises(ShardError, match='refusing nested complete pytest'):
        collect(Path(__file__).resolve().parents[1])


def test_missing_run_identity_cannot_default_to_local_success(monkeypatch):
    # Exercise the local fallback even when this test itself runs on Actions.
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    value = plan()
    rows = results(value)
    assert verify_results(value, rows, SOURCE, DIGEST) == 4.0
    rows[0].pop("run_id")
    with pytest.raises(ShardError, match="wrong workflow run"):
        verify_results(value, rows, SOURCE, DIGEST)


def test_embedded_coverage_fixture_does_not_change_outer_coverage_paths(tmp_path):
    import coverage
    import os
    from pathlib import Path, PurePosixPath
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "outer.py"
    script.write_text(
        "from pathlib import Path; import sys\n"
        "from tests.test_ci_pytest_shards import test_real_branch_coverage_shards_combine_and_retain_unexecuted_sources as proof\n"
        "parent=Path(sys.argv[1]); parent.mkdir(); proof(parent, None)\n"
    )
    rc = tmp_path / "coverage.ini"
    rc.write_text("[run]\nbranch = True\nrelative_files = True\nsource = scripts,src/live_mem,src/hivemind_inference\n")
    data_path = tmp_path / ".coverage"
    completed = subprocess.run(
        [sys.executable, "-m", "coverage", "run", f"--rcfile={rc}", str(script), str(tmp_path / "fixture")],
        cwd=root, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), "COVERAGE_FILE": str(data_path)},
        text=True, capture_output=True, timeout=45,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    data = coverage.CoverageData(basename=str(data_path))
    data.read()
    assert data.has_arcs() and data.measured_files()
    assert "scripts/ci_pytest_shards.py" in data.measured_files()
    assert all(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts for name in data.measured_files())
