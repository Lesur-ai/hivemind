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
            "schema_version": 2, "source_sha": SOURCE, "source_digest": DIGEST,
            "plan_digest": value["plan_digest"], "index": shard["index"],
            "mode": value["mode"], "runtime": value["runtime"],
            "collection_digest": value["collection_digest"], "run_id": value["run_id"],
            "scope": value["scope"], "full_collection_digest": value["full_collection_digest"],
            "selection_policy_digest": value["selection_policy_digest"],
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
    unweighted = make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST, mode="plain")
    assert {key: field for key, field in value.items() if key not in {"shards", "plan_digest"}} == {
        key: field for key, field in unweighted.items() if key not in {"shards", "plan_digest"}
    }
    assert value["shards"] != unweighted["shards"]
    assert value["plan_digest"] != unweighted["plan_digest"]
    assert verify_results(unweighted, results(unweighted), SOURCE, DIGEST) == 4.0
    with pytest.raises(ShardError, match="mismatched plan_digest"):
        verify_results(unweighted, results(value), SOURCE, DIGEST)

    from scripts import ci_pytest_shards as runner

    # #1007 (port of #910): reuse the complete-inventory oracle for
    # heterogeneous internal cohorts. The second equal-cost item must use the
    # other fast bin, rather than the next empty slow bin: placement considers
    # the projected finishing load.
    costs = dict.fromkeys(NODES, 10)
    weighted = make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
                         mode="coverage", weights=costs, capacities=[2, 1, 1, 2])
    assert [row["nodeids"] for row in weighted["shards"]] == [
        [NODES[index] for index in indexes]
        for indexes in ([0, 2, 6, 8], [3, 9], [4, 10], [1, 5, 7, 11])
    ]
    assert weighted["nodeids"] == NODES and weighted["scope"] == "complete"
    assert set(weighted) == set(value), "capacity planning must retain schema v2"
    assert verify_results(weighted, results(weighted), SOURCE, DIGEST) == 4.0
    scaled = make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
                       mode="coverage", weights=costs, capacities=[20, 10, 10, 20])
    assert scaled == weighted, "only relative capacity may affect placement"
    default = make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
                        mode="coverage", weights=costs)
    equal = make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
                      mode="coverage", weights=costs, capacities=[1, 1, 1, 1])
    assert equal == default, "the existing default LPT assignment must stay exact"

    policy = {"schema_version": 1, "cohorts": [
        {"group": "org/internal-fast", "shards": 2, "capacity": 2},
        {"group": "org/internal-slow", "shards": 2, "capacity": 1},
    ]}
    capacities, matrix = runner.capacity_policy(policy, 4)
    assert capacities == [2, 2, 1, 1]
    assert matrix == [
        {"shard": index, "runner_group": "org/internal-fast" if index < 2 else "org/internal-slow"}
        for index in range(4)
    ]


@pytest.mark.parametrize("mutate", [
    lambda value: value["shards"][1]["nodeids"].append(NODES[0]),
    lambda value: value["shards"][1]["nodeids"].pop(),
    lambda value: value["shards"][1].update(index=0),
    lambda value: value.update(source_sha="main"),
    lambda value: value.update(max_test_seconds=3600),
    lambda value: value.update(max_test_seconds=840),
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
    (lambda rows: rows[0].update(wall_seconds=1681), "test budget exceeded"),
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


def _attempt_rows(value):
    rows = results(value)
    for row in rows:
        row["run_attempt"] = 1
    paths = [f"result-python314-{row['index']}/result.json" for row in rows]
    return rows, paths


def _rerun_duplicate(rows, paths, index, position=None):
    stale = deepcopy(rows[index])
    stale.update(run_attempt=2, pytest_return_code=1)
    if position is None:
        rows.append(stale)
        paths.append(f"result-python314-{index}-attempt-2/result.json")
    else:
        rows[position] = stale
        paths[position] = f"result-python314-{index}-attempt-2/result.json"


# #1061: a red result or a second result for one shard names the shard, the
# workflow attempt that produced it and its file, instead of a bare reason.
# The previous reason stays the message prefix or suffix the cases above match.
@pytest.mark.parametrize(("mutate", "message"), [
    (lambda rows, paths: rows[2].update(pytest_return_code=1),
     r"^shard 2 \(attempt 1, result-python314-2/result\.json\): pytest did not succeed \(return code 1\)$"),
    (lambda rows, paths: (rows[2].update(pytest_return_code=1), rows[2].pop("run_attempt")),
     r"^shard 2 \(attempt unknown, result-python314-2/result\.json\): pytest did not succeed"),
    (lambda rows, paths: rows[3]["cases"][1].update(outcome="error"),
     r"^shard 3 \(attempt 1, result-python314-3/result\.json\): failed/error case: "
     r"tests/test_example\.py::test_case\[\d+\]$"),
    (lambda rows, paths: _rerun_duplicate(rows, paths, 1),
     r"^missing or extra shard: shard 1 has 2 results: shard 1 \(attempt 1, result-python314-1/result\.json\), "
     r"shard 1 \(attempt 2, result-python314-1-attempt-2/result\.json\)$"),
    (lambda rows, paths: _rerun_duplicate(rows, paths, 1, position=3),
     r"^invalid or duplicated result index: shard 1 has 2 results: .*attempt 2, result-python314-1-attempt-2/result\.json\)"
     r"; missing shard 3$"),
    (lambda rows, paths: (rows.pop(), paths.pop()), r"^missing or extra shard: missing shard 3$"),
])
def test_failed_or_duplicated_result_names_the_shard_attempt_and_file(mutate, message):
    value = plan()
    rows, paths = _attempt_rows(value)
    assert verify_results(value, rows, SOURCE, DIGEST, paths=paths) == 4.0
    mutate(rows, paths)
    with pytest.raises(ShardError, match=message):
        verify_results(value, rows, SOURCE, DIGEST, paths=paths)
    with pytest.raises(ShardError):
        verify_results(value, rows, SOURCE, DIGEST)


def test_result_files_must_pair_one_to_one_with_results():
    value = plan()
    rows, paths = _attempt_rows(value)
    with pytest.raises(ShardError, match="result files do not match results"):
        verify_results(value, rows, SOURCE, DIGEST, paths=paths[:-1])


def test_registered_plain_witnesses_use_the_profile_policy_not_candidate_copy(tmp_path,monkeypatch):
    from scripts import ci_pytest_shards as runner
    nodes=['tests/test_one.py::test_a','tests/test_two.py::test_b']
    approved={'schema_version':1,'definitions':[{'definition_id':nodes[0],'reason':'line witness'}]}
    candidate={'schema_version':1,'definitions':[{'definition_id':nodes[1],'reason':'different line'}]}
    value=runner.make_plan(nodes,1,source_sha=SOURCE,source_digest=DIGEST,mode='plain',selection_policy=approved)
    monkeypatch.setattr(runner,'collect',lambda root:nodes)
    monkeypatch.setattr(runner,'read_selection_policy',lambda root:candidate)
    runner.verify_collection(tmp_path,value,selection_policy=approved)
    with pytest.raises(runner.ShardError,match='sentinel policy changed'):
        runner.verify_collection(tmp_path,value,selection_policy=candidate)
    with pytest.raises(runner.ShardError,match='sentinel policy changed'):
        runner.verify_collection(tmp_path,value)


def test_a_case_that_cannot_fit_the_test_budget_is_rejected():
    with pytest.raises(ShardError, match="budget"):
        make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST, mode="plain", weights={NODES[0]: 1681})
    # #1007 (port of #910): capacities change placement, never the
    # evidence/runtime budget.
    with pytest.raises(ShardError, match="budget"):
        make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
                  mode="coverage", weights={NODES[0]: 1681}, capacities=[100, 1, 1, 1])
    for capacities in ([1, 1, 1], [1, 1, 1, 1, 1], [1, 1, 1, 0],
                       [1, 1, 1, "1"], (1, 1, 1, 1), []):
        with pytest.raises(ShardError, match="capacit"):
            make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
                      mode="coverage", capacities=capacities)
    with pytest.raises(ShardError, match="coverage"):
        make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
                  mode="plain", capacities=[1, 1, 1, 1])

    from scripts import ci_pytest_shards as runner

    valid = {"schema_version": 1, "cohorts": [
        {"group": "org/internal-fast", "shards": 2, "capacity": 2},
        {"group": "org/internal-slow", "shards": 2, "capacity": 1},
    ]}
    invalid = [None, [], {**valid, "extra": True}, {**valid, "schema_version": True},
               {**valid, "schema_version": 2}, {**valid, "cohorts": []}]
    for field, bad_values in (
        ("group", ["internal-fast", "org/../paid", "org/", "org/a/b", "org/a b", 1]),
        ("shards", [True, 0, -1, 65, 2.0, "2"]),
        ("capacity", [True, 0, -1, float("nan"), float("inf"), "1"]),
    ):
        for bad in bad_values:
            mutant = deepcopy(valid)
            mutant["cohorts"][0][field] = bad
            invalid.append(mutant)
    for mutant in invalid:
        with pytest.raises(ShardError, match="capacity policy|cohort"):
            runner.capacity_policy(mutant, 4)
    duplicate = deepcopy(valid)
    duplicate["cohorts"][1]["group"] = duplicate["cohorts"][0]["group"]
    extra = deepcopy(valid)
    extra["cohorts"][0]["runner"] = "unverified"
    for mutant, count in ((duplicate, 4), (extra, 4), (valid, 3),
                          (valid, True), (valid, 0), (valid, 65)):
        with pytest.raises(ShardError, match="capacity policy|cohort"):
            runner.capacity_policy(mutant, count)


@pytest.mark.parametrize("capacity", [True, -1, float("nan"), float("inf")])
def test_invalid_shard_capacity_fails_closed(capacity):
    # #1007 (port of #910): the capacity half of 1.7.0's
    # test_invalid_module_cost_fails_closed; capacity has no seconds ceiling.
    with pytest.raises(ShardError, match="capacit"):
        make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
                  mode="coverage", capacities=[1, 1, 1, capacity])




@pytest.mark.parametrize("nodes", [[], NODES + [NODES[0]], ["../secret.py::test_escape"], ["tests/test_example.py::test_case\n--ignore=tests"]])
def test_invalid_collection_cannot_make_a_plan(nodes):
    with pytest.raises(ShardError):
        make_plan(nodes, 4, source_sha=SOURCE, source_digest=DIGEST, mode="plain")


@pytest.mark.parametrize("data_only", [False, True])
def test_real_branch_coverage_shards_combine_and_retain_unexecuted_sources(tmp_path, monkeypatch, data_only):
    import os
    import subprocess
    import sys
    # coverage.combine changes coverage.files' process-global relative directory.
    # Keep the real inner coverage exercise outside the surrounding shard tracer.
    completed = subprocess.run(
        [sys.executable, "-c",
         "from pathlib import Path; import sys; "
         "from tests.test_ci_pytest_shards import _assert_real_branch_coverage_shards; "
         "_assert_real_branch_coverage_shards(Path(sys.argv[1]), sys.argv[2] == 'True')", str(tmp_path), str(data_only)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _assert_real_branch_coverage_shards(tmp_path, data_only=False):
    import json
    import subprocess
    import sys
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
    capacity_path = root / 'capacity.json'
    policy_bytes = json.dumps({'schema_version': 1, 'cohorts': [
        {'group': 'org/fixture-fast', 'shards': 1, 'capacity': 1.0},
        {'group': 'org/fixture-other', 'shards': 1, 'capacity': 0.75},
    ]}).encode()
    capacity_path.write_bytes(policy_bytes)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=ci', '-c', 'user.email=ci@invalid', 'commit', '-qm', 'fixture'], cwd=root, check=True)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    default = runner.make_plan(runner.collect(root), 2, source_sha=head, git_head=head,
                              source_digest=runner.source_digest(root), mode='coverage')
    # #1007 (port of #910): plan through the actual CLI with a capacity policy.
    plan_path = tmp_path / 'plan.json'
    command = [sys.executable, runner.__file__, 'plan', '--root', str(root),
               '--source-sha', head, '--mode', 'coverage', '--shards', '2',
               '--capacity-policy', str(capacity_path), '--output', str(plan_path)]
    planned = subprocess.run(command, cwd=root, text=True, capture_output=True)
    assert planned.returncode == 0, planned.stdout + planned.stderr
    value = json.loads(plan_path.read_text())
    # Two equal-cost cases land one per shard on either path.
    assert [row['nodeids'] for row in value['shards']] == [row['nodeids'] for row in default['shards']]
    # The actual CLI must consume the policy, rather than silently ignore it.
    capacity_path.write_text(json.dumps({'schema_version': 1, 'cohorts': []}))
    invalid = subprocess.run(command, cwd=root, text=True, capture_output=True)
    assert invalid.returncode == 2 and 'capacity policy' in invalid.stderr
    capacity_path.write_bytes(policy_bytes)
    evidence = tmp_path / 'results'
    for index in range(2):
        runner.run_shard(root, value, index, evidence / str(index))
    output = tmp_path / 'aggregate'
    aggregate = runner.aggregate
    if data_only:
        inventory = list(value['nodeids'])
        def refuse_candidate_execution(*args, **kwargs):
            raise AssertionError('trusted aggregate executed candidate collection')
        runner.collect = refuse_candidate_execution
        aggregate = lambda root, plan, results, output, head: runner.aggregate_collected(
            root, plan, results, output, head, inventory=inventory)
        with pytest.raises(runner.ShardError, match='independent collection'):
            runner.aggregate_collected(root, value, evidence, output, head, inventory=inventory[:1])
    aggregate(root, value, evidence, output, head)
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
        aggregate(root, value, evidence, tmp_path / 'corrupt-coverage', head)
    raw.write_bytes(original)
    xml = evidence / '0/pytest.xml'
    xml.write_bytes(xml.read_bytes() + b' ')
    with pytest.raises(runner.ShardError, match='corrupt JUnit'):
        aggregate(root, value, evidence, tmp_path / 'corrupt-junit', head)


def test_timeout_terminates_only_its_owned_process_group(tmp_path):
    import subprocess
    import sys
    import time
    from scripts.ci_pytest_shards import owned_run

    outsider = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        start = time.monotonic()
        script = 'import time; print("[ci collect] module test_safe.py", flush=True); print("PRIVATE_VALUE="+"x"*10000, flush=True); time.sleep(30)'
        with pytest.raises(ShardError, match='owned test process exceeded') as raised:
            owned_run([sys.executable, '-c', script], tmp_path, capture=True, seconds=0.5)
        assert time.monotonic() - start < 4
        assert outsider.poll() is None, 'An unrelated process must remain alive'
        assert '[ci collect] module test_safe.py' in str(raised.value)
        assert 'PRIVATE_VALUE' not in str(raised.value) and len(str(raised.value)) < 4300
    finally:
        outsider.terminate(); outsider.wait(timeout=3)


def test_complete_collection_retains_the_inherited_nested_runner_guard(tmp_path, monkeypatch):
    import io
    import json
    import sys
    from types import SimpleNamespace
    from pathlib import Path
    from scripts import ci_pytest_shards as runner
    # Collection is itself a complete-suite entrypoint. It must retain the
    # parent identity so the repository's configure hook refuses it before
    # recursively collecting or executing the suite below this pytest runner.
    original_owned_run = runner.owned_run
    def complete_collect(command, root, **kwargs):
        assert kwargs['seconds'] == 90 and kwargs['capture'] is True
        assert '--collect-only' in command and '--strict-markers' in command
        return original_owned_run(command, root, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(runner, 'owned_run', complete_collect)
        with pytest.raises(ShardError, match='refusing nested complete pytest'):
            runner.collect(Path(__file__).resolve().parents[1])
    # Exercise the collection plugin without starting another suite or waiting
    # sixty seconds. Its original stderr FD must survive pytest's own capture.
    events, streams = [], []
    def stream(fd, mode, buffering):
        assert (fd, mode, buffering) == (99, 'w', 1)
        result = io.StringIO(); streams.append(result); return result
    fake_os = SimpleNamespace(environ={'CI_SHARD_COLLECTION': str(tmp_path/'collection.json')},
                              dup=lambda fd: 99 if fd == 2 else pytest.fail('wrong diagnostic FD'), fdopen=stream)
    fake_fault = SimpleNamespace(dump_traceback_later=lambda seconds, **kw: events.append(('arm', seconds, kw)),
                                 cancel_dump_traceback_later=lambda: events.append(('cancel',)))
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, 'os', fake_os); patch.setitem(sys.modules, 'faulthandler', fake_fault)
        plugin = {}; exec(runner.COLLECTOR, plugin)
        assert events[0][0:2] == ('arm', 60)
        assert events[0][2] == {'file': streams[0], 'repeat': False}
        collector = SimpleNamespace(path=Path('/private/checkout/tests/test_safe.py'), nodeid='SECRET_PARAMETER')
        plugin['pytest_collectstart'](collector); plugin['pytest_collectstart'](collector)
        for index in range(450):
            plugin['pytest_collectstart'](SimpleNamespace(path=Path(f'test_progress_{index}.py')))
        plugin['pytest_collection_finish'](SimpleNamespace(items=[SimpleNamespace(nodeid=NODES[0])]))
        text = streams[0].getvalue()
        assert 'SECRET_PARAMETER' not in text and '/private/checkout' not in text
        assert text.count('[ci collect] module test_safe.py') == 1
        assert text.count('[ci collect] module ') == 400 and len(text) < 80000
        assert '[ci collect] phase complete' in text
        plugin['pytest_unconfigure'](None)
        assert streams[0].closed and events[-1] == ('cancel',)
        assert json.loads((tmp_path/'collection.json').read_text()) == {'nodeids': [NODES[0]], 'deselected': 0}
        plugin = {}; exec(runner.COLLECTOR, plugin)
        plugin['pytest_unconfigure'](None)
        assert streams[1].closed and events[-1] == ('cancel',), 'Configuration failure must cancel its timer too'


def test_missing_run_identity_cannot_default_to_local_success(monkeypatch):
    # Exercise the local fallback even when this test itself runs on Actions.
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    value = plan()
    rows = results(value)
    assert verify_results(value, rows, SOURCE, DIGEST) == 4.0
    rows[0].pop("run_id")
    with pytest.raises(ShardError, match="wrong workflow run"):
        verify_results(value, rows, SOURCE, DIGEST)


def sentinel_policy():
    return {"schema_version": 1, "definitions": [
        {"definition_id": "tests/test_example.py::test_case", "reason": "Real timing witness"},
    ]}


def test_sentinel_definition_expands_all_current_parameters_without_filtering_collection():
    from scripts.ci_pytest_shards import digest

    nodes = [*NODES, "tests/test_example.py::test_case[new[parameter]]",
             "tests/test_other.py::test_other"]
    value = make_plan(nodes, 2, source_sha=SOURCE, source_digest=DIGEST,
                      mode="plain", selection_policy=sentinel_policy())
    assert value["schema_version"] == 2 and value["scope"] == "plain-sentinels"
    assert value["nodeids"] == nodes[:-1]
    assert value["full_collection_digest"] == digest(nodes)
    assert value["selection_policy_digest"] == digest(sentinel_policy())
    assert value["collection_digest"] == digest(nodes[:-1])
    assert verify_results(value, results(value), SOURCE, DIGEST) == 2.0


@pytest.mark.parametrize("policy", [
    {},
    {"schema_version": True, "definitions": []},
    {"schema_version": 1, "definitions": []},
    {"schema_version": 1, "definitions": [*sentinel_policy()["definitions"],
                                           *sentinel_policy()["definitions"]]},
    {"schema_version": 1, "definitions": [{"definition_id": NODES[0], "reason": "Filtered parameter"}]},
    {"schema_version": 1, "definitions": [{"definition_id": "tests/test_example.py::test_missing", "reason": "Absent"}]},
    {"schema_version": 1, "definitions": [{"definition_id": "tests/test_example.py::test_case", "reason": " "}]},
    {"schema_version": 1, "definitions": [{"definition_id": "tests/test_example.py::test_case", "reason": "Valid", "nodeids": NODES}]},
])
def test_invalid_or_partial_sentinel_policy_fails_closed(policy):
    with pytest.raises(ShardError):
        make_plan(NODES, 2, source_sha=SOURCE, source_digest=DIGEST,
                  mode="plain", selection_policy=policy)


def test_coverage_cannot_select_sentinels_and_complete_is_the_default():
    from scripts.ci_pytest_shards import digest

    with pytest.raises(ShardError, match="plain"):
        make_plan(NODES, 2, source_sha=SOURCE, source_digest=DIGEST,
                  mode="coverage", selection_policy=sentinel_policy())
    for mode in ("plain", "coverage"):
        value = make_plan(NODES, 2, source_sha=SOURCE, source_digest=DIGEST, mode=mode)
        assert value["scope"] == "complete" and value["nodeids"] == NODES
        assert value["full_collection_digest"] == digest(NODES)
        assert value["selection_policy_digest"] is None


@pytest.mark.parametrize(("field", "replacement"), [
    ("scope", "complete"),
    ("full_collection_digest", "c" * 64),
    ("selection_policy_digest", "c" * 64),
])
def test_sentinel_result_cannot_change_scope_collection_or_policy(field, replacement):
    value = make_plan(NODES, 2, source_sha=SOURCE, source_digest=DIGEST,
                      mode="plain", selection_policy=sentinel_policy())
    rows = results(value)
    rows[0][field] = replacement
    with pytest.raises(ShardError, match=f"mismatched {field}"):
        verify_results(value, rows, SOURCE, DIGEST)


def test_complete_result_requires_an_explicit_null_selection_policy_digest():
    value = plan()
    rows = results(value)
    rows[0].pop("selection_policy_digest")
    with pytest.raises(ShardError, match="mismatched selection_policy_digest"):
        verify_results(value, rows, SOURCE, DIGEST)


def test_skipped_sentinel_is_not_successful_evidence():
    value = make_plan(NODES, 2, source_sha=SOURCE, source_digest=DIGEST,
                      mode="plain", selection_policy=sentinel_policy())
    rows = results(value)
    rows[0]["cases"][0]["outcome"] = "skipped"
    with pytest.raises(ShardError, match="sentinel.*passed"):
        verify_results(value, rows, SOURCE, DIGEST)
    complete = plan()
    rows = results(complete)
    rows[0]["cases"][0]["outcome"] = "skipped"
    assert verify_results(complete, rows, SOURCE, DIGEST) == 4.0


@pytest.mark.parametrize("fault", ["legacy_schema", "coverage_sentinels", "partial_complete", "complete_policy"])
def test_rehashed_scope_or_schema_cannot_weaken_the_plan_contract(fault):
    from scripts.ci_pytest_shards import digest

    value = make_plan(NODES, 2, source_sha=SOURCE, source_digest=DIGEST,
                      mode="plain", selection_policy=sentinel_policy())
    if fault == "legacy_schema":
        value["schema_version"] = 1
    elif fault == "coverage_sentinels":
        value["mode"] = "coverage"
    elif fault == "partial_complete":
        value.update(scope="complete", selection_policy_digest=None, full_collection_digest="c" * 64)
    else:
        value["scope"] = "complete"
    value["plan_digest"] = digest({key: item for key, item in value.items() if key != "plan_digest"})
    with pytest.raises(ShardError):
        validate_plan(value)


def _sentinel_checkout(tmp_path):
    import subprocess
    from scripts import ci_pytest_shards as runner

    root = tmp_path / "checkout"
    (root / "tests").mkdir(parents=True)
    (root / ".github").mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (root / "tests/test_example.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', [0, 1, 2], ids=['old', 'new', 'nested[brackets]'])\n"
        "def test_case(value):\n    assert value in (0, 1, 2)\n"
        "def test_other():\n    raise AssertionError('unselected case executed')\n"
    )
    policy_path = root / ".github/ci-plain-sentinels.json"
    runner.write_json(policy_path, sentinel_policy())
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=ci", "-c", "user.email=ci@invalid",
                    "commit", "-qm", "fixture"], cwd=root, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    nodes = runner.collect(root)
    value = runner.make_plan(nodes, 2, source_sha=head, source_digest=runner.source_digest(root),
                             mode="plain", selection_policy=sentinel_policy())
    return root, head, nodes, value


def test_real_sentinel_workers_and_aggregate_preserve_full_collection(tmp_path, monkeypatch):
    import json
    import shutil
    from scripts import ci_pytest_shards as runner

    root, head, nodes, value = _sentinel_checkout(tmp_path)
    assert len(nodes) == 4 and len(value["nodeids"]) == 3
    evidence = tmp_path / "results"
    # #1061: each worker records the workflow attempt that produced its result.
    for attempt in ("0", "01", "x", ""):
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
        with pytest.raises(ShardError, match="invalid workflow run attempt"):
            runner.run_shard(root, value, 0, tmp_path / "refused")
        assert not (tmp_path / "refused").exists()
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    for index in range(2):
        runner.run_shard(root, value, index, evidence / str(index))
    assert [runner.read_json(evidence / str(index) / "result.json")["run_attempt"] for index in range(2)] == [2, 2]
    # A red result reaches the aggregate failure with its shard, attempt and file.
    red = tmp_path / "red-results"
    shutil.copytree(evidence, red)
    row = runner.read_json(red / "1/result.json")
    row["pytest_return_code"] = 1
    runner.write_json(red / "1/result.json", row)
    with pytest.raises(ShardError, match=r"^shard 1 \(attempt 2, 1/result\.json\): pytest did not succeed"):
        runner.aggregate(root, value, red, tmp_path / "red-aggregate", head)
    monkeypatch.delenv("GITHUB_RUN_ATTEMPT")
    runner.run_shard(root, value, 0, tmp_path / "local")
    assert runner.read_json(tmp_path / "local/result.json")["run_attempt"] is None
    output = tmp_path / "aggregate"
    runner.aggregate(root, value, evidence, output, head)
    summary = json.loads((output / "aggregate.json").read_text())
    assert summary["schema_version"] == 2 and summary["scope"] == "plain-sentinels"
    assert summary["cases"] == 3 and summary["full_collection_digest"] == runner.digest(nodes)
    assert {row["nodeid"] for row in runner.junit_cases(output / "pytest.xml", value["nodeids"])} == set(value["nodeids"])
    assert not (output / "coverage.json").exists()


def test_sentinel_policy_is_restricted_to_the_canonical_regular_tracked_file(tmp_path):
    from scripts import ci_pytest_shards as runner

    root, _, _, _ = _sentinel_checkout(tmp_path)
    assert runner.read_selection_policy(root, runner.PLAIN_SELECTION_POLICY) == sentinel_policy()
    other = root / ".github/other.json"
    runner.write_json(other, sentinel_policy())
    with pytest.raises(ShardError, match="canonical"):
        runner.read_selection_policy(root, other)
    canonical = root / runner.PLAIN_SELECTION_POLICY
    canonical.unlink()
    canonical.symlink_to(other)
    with pytest.raises(ShardError, match="regular"):
        runner.read_selection_policy(root)


@pytest.mark.parametrize("fault", ["full_collection", "selection", "policy", "missing_policy", "untracked_policy"])
def test_worker_and_aggregate_revalidate_the_full_inventory_and_policy(tmp_path, monkeypatch, fault):
    from scripts import ci_pytest_shards as runner

    root, head, nodes, value = _sentinel_checkout(tmp_path)
    if fault == "full_collection":
        monkeypatch.setattr(runner, "collect", lambda _: nodes + ["tests/test_other.py::test_new"])
        expected = "full collection"
    elif fault == "selection":
        # Even a rehashed plan cannot silently omit a new parameter.
        value["nodeids"].pop()
        value["shards"][0]["nodeids"].pop()
        value["collection_digest"] = runner.digest(value["nodeids"])
        value["plan_digest"] = runner.digest({k: v for k, v in value.items() if k != "plan_digest"})
        expected = "selection"
    elif fault == "policy":
        changed = sentinel_policy()
        changed["definitions"][0]["reason"] = "changed policy"
        runner.write_json(root / ".github/ci-plain-sentinels.json", changed)
        # Isolate the policy binding from the already-existing source guard.
        monkeypatch.setattr(runner, "source_digest", lambda _: value["source_digest"])
        expected = "policy"
    elif fault == "missing_policy":
        (root / ".github/ci-plain-sentinels.json").unlink()
        monkeypatch.setattr(runner, "source_digest", lambda _: value["source_digest"])
        expected = OSError
    else:
        import subprocess
        subprocess.run(["git", "rm", "--cached", ".github/ci-plain-sentinels.json"], cwd=root, check=True, capture_output=True)
        monkeypatch.setattr(runner, "source_digest", lambda _: value["source_digest"])
        expected = "tracked"
    for operation in (
        lambda: runner.run_shard(root, value, 0, tmp_path / "run"),
        lambda: runner.aggregate(root, value, tmp_path / "results", tmp_path / "aggregate", head),
    ):
        if expected is OSError:
            with pytest.raises(OSError):
                operation()
        else:
            with pytest.raises(ShardError, match=expected):
                operation()


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
        "parent=Path(sys.argv[1]); parent.mkdir(); proof(parent, None, False)\n"
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


@pytest.mark.parametrize("seconds", [421, 840, 1680])
def test_doubled_budget_accepts_complete_shard_evidence_through_twenty_eight_minutes(seconds):
    value = make_plan(NODES, 4, source_sha=SOURCE, source_digest=DIGEST,
                      mode="plain", weights={NODES[0]: seconds})
    assert value["max_test_seconds"] == 1680
    validate_plan(value)
    rows = results(value)
    rows[0]["wall_seconds"] = seconds
    assert verify_results(value, rows, SOURCE, DIGEST) == seconds + 3


def test_default_owned_process_uses_the_doubled_deadline(tmp_path, monkeypatch):
    import signal
    import subprocess
    from scripts import ci_pytest_shards as runner

    class Process:
        returncode = 0

        def communicate(self, *, timeout):
            assert timeout == 1680
            return "complete", None

    def spawn(command, **kwargs):
        assert kwargs["start_new_session"] is True
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", spawn)
    assert runner.owned_run(["pytest"], tmp_path).returncode == 0
    for escalate in (False, True):
        signals, waits = [], []
        class TimedOut:
            pid = 12345
            def communicate(self, *, timeout):
                waits.append(timeout)
                if len(waits) == 1 or (escalate and len(waits) == 2):
                    raise subprocess.TimeoutExpired('owned', timeout)
                return ('[ci collect] module test_safe.py\n' * 500 +
                        '  File "/private/path/test_safe.py", line 7 in collect\n'
                        'PASSWORD=not-a-source-location\n'), None
        def timed_out(command, **kw):
            assert kw['start_new_session'] is True and kw['stdout'] == subprocess.PIPE
            return TimedOut()
        monkeypatch.setattr(runner.subprocess, 'Popen', timed_out)
        monkeypatch.setattr(runner.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
        with pytest.raises(ShardError) as raised:
            runner.owned_run(['pytest'], tmp_path, capture=True)
        assert waits == ([1680, 3, 3] if escalate else [1680, 3])
        assert signals == ([(12345, signal.SIGTERM), (12345, signal.SIGKILL)] if escalate else [(12345, signal.SIGTERM)])
        assert 'test_safe.py' in str(raised.value) and 'PASSWORD' not in str(raised.value)
        assert '/private/path' not in str(raised.value) and len(str(raised.value)) < 4300
