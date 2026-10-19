#!/usr/bin/env python3
"""Run bounded, disjoint pytest shards and verify SHA-bound suite or sentinel evidence."""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import heapq
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


MAX_TEST_SECONDS = 1680
COVERAGE_SOURCE = "src/live_mem,src/hivemind_inference,scripts"
PLAIN_SELECTION_POLICY = Path(".github/ci-plain-sentinels.json")
COLLECTOR = """
import faulthandler, json, os, re
from pathlib import Path
_diagnostics = os.fdopen(os.dup(2), 'w', buffering=1)
_timer_active = True
_reported = set()
_limited = False
faulthandler.dump_traceback_later(60, file=_diagnostics, repeat=False)
print('[ci collect] phase start', file=_diagnostics, flush=True)
_deselected = 0
def _stop_timer():
    global _timer_active
    if _timer_active:
        faulthandler.cancel_dump_traceback_later()
        _timer_active = False
def pytest_collectstart(collector):
    global _limited
    path = getattr(collector, 'path', None)
    if path is None or Path(path).suffix != '.py':
        return
    name = re.sub('[^A-Za-z0-9_.-]', '_', Path(path).name)[:160]
    if name in _reported:
        return
    if len(_reported) >= 400:
        if not _limited:
            print('[ci collect] phase progress-limit', file=_diagnostics, flush=True)
            _limited = True
        return
    _reported.add(name)
    print('[ci collect] module ' + name, file=_diagnostics, flush=True)
def pytest_deselected(items):
    global _deselected
    _deselected += len(items)
def pytest_collection_finish(session):
    _stop_timer()
    print('[ci collect] phase complete', file=_diagnostics, flush=True)
    Path(os.environ["CI_SHARD_COLLECTION"]).write_text(json.dumps({
        "nodeids": [item.nodeid for item in session.items],
        "deselected": _deselected,
    }), encoding="utf-8")
def pytest_unconfigure(config):
    _stop_timer()
    _diagnostics.close()
"""


class ShardError(ValueError):
    """Incomplete, stale or invalid evidence cannot produce a green check."""


def require(condition, message):
    if not condition:
        raise ShardError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def regular(path):
    require(not path.is_symlink() and stat.S_ISREG(path.stat().st_mode), f"not a regular evidence/source file: {path.name}")
    return path


def file_digest(path):
    return hashlib.sha256(regular(path).read_bytes()).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    regular(path)
    require(path.stat().st_size <= 25_000_000, "oversized shard JSON")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def runtime():
    machine = platform.machine().lower()
    return {
        "implementation": sys.implementation.name,
        "python": list(sys.version_info[:3]),
        "machine": "arm64" if machine in {"arm64", "aarch64"} else "x64" if machine in {"x86_64", "amd64"} else machine,
    }


def source_digest(root):
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    require(any(paths), "source checkout has no tracked files")
    rows = []
    for name in sorted(filter(None, paths)):
        path = PurePosixPath(name)
        require(not path.is_absolute() and ".." not in path.parts, "unsafe tracked path")
        file = regular(root / name)
        rows.append((name, ("100755" if file.stat().st_mode & stat.S_IXUSR else "100644"), file_digest(file)))
    return digest(rows)


def _nodeids(nodes):
    require(isinstance(nodes, list) and bool(nodes), "empty or invalid collection")
    require(all(isinstance(node, str) for node in nodes), "invalid node ID")
    require(len(set(nodes)) == len(nodes), "duplicate collected case")
    for node in nodes:
        file, separator, function = node.partition("::")
        path = PurePosixPath(file)
        require(separator and function and file.startswith("tests/") and file.endswith(".py"), "invalid test node ID")
        require(not path.is_absolute() and ".." not in path.parts and "\\" not in file, "unsafe test path")
        require(not any(ord(char) < 32 for char in node), "control character in node ID")


def number(value, maximum=None):
    require(not isinstance(value, bool) and isinstance(value, (int, float)), "invalid numeric evidence")
    require(math.isfinite(value) and value >= 0, "non-finite or negative evidence")
    require(maximum is None or value <= maximum, "test budget exceeded")
    return value


def select_sentinels(nodes, policy):
    """Resolve whole definitions from an unfiltered current collection."""
    _nodeids(nodes)
    require(isinstance(policy, dict) and set(policy) == {"schema_version", "definitions"}
            and type(policy["schema_version"]) is int and policy["schema_version"] == 1,
            "invalid sentinel policy schema")
    definitions = policy["definitions"]
    require(isinstance(definitions, list) and bool(definitions), "empty sentinel policy")
    selected = set()
    available = {node.split("[", 1)[0] for node in nodes}
    for row in definitions:
        require(isinstance(row, dict) and set(row) == {"definition_id", "reason"}, "invalid sentinel definition")
        definition = row["definition_id"]
        _nodeids([definition])
        require("[" not in definition and "]" not in definition, "sentinel policy must select a whole definition")
        require(isinstance(row["reason"], str) and bool(row["reason"].strip()), "missing sentinel reason")
        require(definition not in selected, "duplicate sentinel definition")
        require(definition in available, f"missing sentinel definition: {definition}")
        selected.add(definition)
    return [node for node in nodes if node.split("[", 1)[0] in selected]


def read_selection_policy(root, path=None):
    canonical = root / PLAIN_SELECTION_POLICY
    requested = root / path if path is not None and not path.is_absolute() else path
    require(requested is None or requested.resolve() == canonical.resolve(), "selection policy must use the canonical tracked path")
    policy = read_json(canonical)
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", str(PLAIN_SELECTION_POLICY)],
                             cwd=root, text=True, capture_output=True)
    require(tracked.returncode == 0, "sentinel policy must be tracked")
    return policy


def capacity_policy(value, count):
    """Expand a strict internal cohort policy into capacities and matrix rows."""
    require(type(count) is int and 1 <= count <= 64, "invalid capacity policy shard count")
    require(isinstance(value, dict) and set(value) == {"schema_version", "cohorts"}
            and type(value["schema_version"]) is int and value["schema_version"] == 1,
            "invalid capacity policy schema")
    cohorts = value["cohorts"]
    require(isinstance(cohorts, list) and 1 <= len(cohorts) <= count, "invalid capacity policy cohorts")
    groups = set()
    capacities, matrix = [], []
    for cohort in cohorts:
        require(isinstance(cohort, dict) and set(cohort) == {"group", "shards", "capacity"},
                "invalid capacity policy cohort")
        group, shards, capacity = cohort["group"], cohort["shards"], cohort["capacity"]
        require(isinstance(group, str) and re.fullmatch(r"org/[A-Za-z0-9][A-Za-z0-9_.-]*", group)
                and group not in groups, "invalid or duplicate capacity policy cohort group")
        require(type(shards) is int and 1 <= shards <= 64 and len(capacities) + shards <= count,
                "invalid capacity policy cohort shard count")
        require(not isinstance(capacity, bool) and isinstance(capacity, (int, float))
                and 0 < capacity <= sys.float_info.max and math.isfinite(capacity),
                "invalid capacity policy cohort capacity")
        groups.add(group)
        for _ in range(shards):
            matrix.append({"shard": len(capacities), "runner_group": group})
            capacities.append(capacity)
    require(len(capacities) == count, "capacity policy cohort counts do not match shard count")
    return capacities, matrix


def make_plan(nodes, count, *, source_sha, source_digest, mode, weights=None, git_head=None, selection_policy=None,
              capacities=None):
    _nodeids(nodes)
    require(mode in {"plain", "coverage"}, "invalid shard mode")
    full_collection_digest = digest(nodes)
    if selection_policy is not None:
        require(mode == "plain", "sentinel selection is allowed only in plain mode")
        nodes = select_sentinels(nodes, selection_policy)
    require(type(count) is int and 1 <= count <= min(64, len(nodes)), "invalid shard count")
    if capacities is not None:
        require(mode == "coverage", "relative capacities are allowed only in coverage mode")
        require(isinstance(capacities, list) and len(capacities) == count, "invalid shard capacities")
        require(all(not isinstance(capacity, bool) and isinstance(capacity, (int, float))
                    and 0 < capacity <= sys.float_info.max and math.isfinite(capacity)
                    for capacity in capacities), "invalid shard capacity")
    weights = weights or {}
    default = 0.1 if mode == "plain" else 0.2
    costs = {node: number(weights.get(node, default), MAX_TEST_SECONDS) for node in nodes}
    buckets = [[] for _ in range(count)]
    positions = {node: index for index, node in enumerate(nodes)}
    ordered = sorted(nodes, key=lambda item: (-costs[item], positions[item]))
    if capacities is None or len(set(capacities)) == 1:
        # Preserve the original default LPT path exactly for plain/public/ARM
        # and for explicitly uniform coverage capacity.
        loads = [(0.0, index) for index in range(count)]
        heapq.heapify(loads)
        for node in ordered:
            load, index = heapq.heappop(loads)
            buckets[index].append(node)
            heapq.heappush(loads, (load + costs[node], index))
        estimates = {index: seconds for seconds, index in loads}
    else:
        loads = [0.0] * count
        for node in ordered:
            index = min(range(count), key=lambda index: ((loads[index] + costs[node]) / capacities[index], index))
            buckets[index].append(node)
            loads[index] += costs[node]
        estimates = dict(enumerate(loads))
    value = {
        "schema_version": 2, "source_sha": source_sha, "source_digest": source_digest,
        "git_head": git_head or source_sha, "mode": mode, "runtime": runtime(),
        "scope": "plain-sentinels" if selection_policy is not None else "complete",
        "full_collection_digest": full_collection_digest,
        "selection_policy_digest": digest(selection_policy) if selection_policy is not None else None,
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "max_test_seconds": MAX_TEST_SECONDS,
        "collection_digest": digest(nodes), "nodeids": nodes,
        "shards": [
            {"index": index, "nodeids": sorted(bucket, key=positions.__getitem__),
             "estimated_seconds": round(estimates[index], 3)}
            for index, bucket in enumerate(buckets)
        ],
    }
    value["plan_digest"] = digest(value)
    validate_plan(value)
    return value


def validate_plan(value):
    require(isinstance(value, dict), "invalid plan")
    expected = {"schema_version", "source_sha", "source_digest", "git_head", "mode", "scope", "runtime", "run_id", "max_test_seconds", "collection_digest", "full_collection_digest", "selection_policy_digest", "nodeids", "shards", "plan_digest"}
    require(set(value) == expected and type(value["schema_version"]) is int and value["schema_version"] == 2, "invalid plan schema")
    for name, length in (("source_sha", 40), ("git_head", 40), ("source_digest", 64), ("plan_digest", 64), ("collection_digest", 64), ("full_collection_digest", 64)):
        require(isinstance(value[name], str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value[name]), f"invalid {name}")
    require(value["mode"] in {"plain", "coverage"} and value["max_test_seconds"] == MAX_TEST_SECONDS, "invalid plan budget/mode")
    require(value["scope"] in {"complete", "plain-sentinels"}, "invalid plan scope")
    if value["scope"] == "complete":
        require(value["selection_policy_digest"] is None, "complete scope cannot use a selection policy")
        require(value["full_collection_digest"] == value["collection_digest"], "incomplete full collection")
    else:
        require(value["mode"] == "plain", "sentinel scope requires plain mode")
        require(isinstance(value["selection_policy_digest"], str) and re.fullmatch("[0-9a-f]{64}", value["selection_policy_digest"]), "invalid selection_policy_digest")
    require(isinstance(value["run_id"], str) and (value["run_id"] == "local" or re.fullmatch("[1-9][0-9]*", value["run_id"])), "invalid run identity")
    require(isinstance(value["runtime"], dict) and set(value["runtime"]) == {"implementation", "python", "machine"}, "invalid runtime")
    actual_runtime = value["runtime"]
    require(actual_runtime["implementation"] == "cpython", "unsupported interpreter")
    require(actual_runtime["machine"] in {"arm64", "x64"}, "unsupported architecture")
    version = actual_runtime["python"]
    require(isinstance(version, list) and len(version) == 3 and all(type(part) is int and part >= 0 for part in version), "invalid Python version")
    _nodeids(value["nodeids"])
    require(value["collection_digest"] == digest(value["nodeids"]), "changed collection")
    shards = value["shards"]
    require(isinstance(shards, list) and 1 <= len(shards) <= 64, "invalid shards")
    union = []
    for index, shard in enumerate(shards):
        require(isinstance(shard, dict) and set(shard) == {"index", "nodeids", "estimated_seconds"}, "invalid shard")
        require(type(shard["index"]) is int and shard["index"] == index, "invalid shard index")
        _nodeids(shard["nodeids"])
        number(shard["estimated_seconds"], MAX_TEST_SECONDS)
        union.extend(shard["nodeids"])
    require(len(union) == len(set(union)) and set(union) == set(value["nodeids"]), "shards do not form an exact disjoint collection")
    unsigned = {key: item for key, item in value.items() if key != "plan_digest"}
    require(value["plan_digest"] == digest(unsigned), "changed plan digest")


def _shard_label(row, path):
    """Name a result by shard, workflow attempt and file for a failure (#1061)."""
    attempt = row.get("run_attempt")
    attempt = attempt if type(attempt) is int and attempt > 0 else "unknown"
    return f"shard {row.get('index')!r} (attempt {attempt}" + ("" if path is None else f", {path}") + ")"


def _result_inventory(rows, paths, count):
    """Describe duplicated and missing shard indexes; empty when exact."""
    found = {}
    for row, path in zip(rows, paths):
        if isinstance(row, dict) and type(row.get("index")) is int:
            found.setdefault(row["index"], []).append(_shard_label(row, path))
    details = [f"shard {index} has {len(labels)} results: {', '.join(labels)}"
               for index, labels in sorted(found.items()) if len(labels) > 1]
    missing = [str(index) for index in range(count) if index not in found]
    if missing:
        details.append(f"missing shard {', '.join(missing)}")
    return "; ".join(details)


def verify_results(plan, rows, expected_source, expected_digest, paths=None):
    validate_plan(plan)
    require(plan["source_sha"] == expected_source and plan["source_digest"] == expected_digest, "stale source plan")
    require(isinstance(rows, list), "missing or extra shard")
    paths = [None] * len(rows) if paths is None else list(paths)
    require(len(paths) == len(rows), "result files do not match results")
    inventory = _result_inventory(rows, paths, len(plan["shards"]))
    require(len(rows) == len(plan["shards"]), "missing or extra shard" + (f": {inventory}" if inventory else ""))
    seen = set()
    wall = 0.0
    for row, path in zip(rows, paths):
        require(isinstance(row, dict) and type(row.get("schema_version")) is int and row.get("schema_version") == 2, "invalid result schema")
        index = row.get("index")
        require(type(index) is int and 0 <= index < len(rows) and index not in seen,
                "invalid or duplicated result index" + (f": {inventory}" if inventory else ""))
        seen.add(index)
        label = _shard_label(row, path)
        for key in ("source_sha", "source_digest", "plan_digest", "mode", "scope", "runtime", "collection_digest", "full_collection_digest", "selection_policy_digest"):
            require(key in row and row[key] == plan[key], f"stale or mismatched {key}")
        require(row.get("run_id") == plan["run_id"], "wrong workflow run")
        code = row.get("pytest_return_code")
        require(type(code) is int and code == 0, f"{label}: pytest did not succeed (return code {code!r})")
        wall += number(row.get("wall_seconds"), MAX_TEST_SECONDS)
        cases = row.get("cases")
        require(isinstance(cases, list), "missing case evidence")
        ids = [case.get("nodeid") for case in cases if isinstance(case, dict)]
        require(len(ids) == len(cases) and len(set(ids)) == len(ids), "duplicated or invalid executed case")
        require(set(ids) == set(plan["shards"][index]["nodeids"]), "partial or extra case execution")
        failed = [str(case["nodeid"]) for case in cases if case.get("outcome") not in {"passed", "skipped"}]
        require(not failed, f"{label}: failed/error case: {', '.join(failed[:5])}" +
                (f" (+{len(failed) - 5} more)" if len(failed) > 5 else ""))
        if plan["scope"] == "plain-sentinels":
            require(all(case["outcome"] == "passed" for case in cases), f"{label}: every sentinel must have passed")
    return wall


def _collection_diagnostic_tail(stdout):
    # Collection emits parametrized node IDs and arbitrary import output too.
    # Keep only controlled progress and faulthandler source locations, no values.
    lines = deque(maxlen=64)
    for line in (stdout or "").splitlines():
        if len(line) > 1024:
            continue
        if re.fullmatch(r"\[ci collect\] (phase (start|complete|progress-limit)|module [A-Za-z0-9_.-]{1,160})", line):
            lines.append(line)
        else:
            frame = re.fullmatch(r'\s*File "([^"\n]+)", line ([0-9]+) in ([A-Za-z0-9_<>.-]+)', line)
            if frame:
                name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(frame[1]).name)[:160]
                lines.append(f"  File {name}, line {frame[2]} in {frame[3][:80]}")
            elif re.fullmatch(r"((Current thread|Thread) 0x[0-9a-f]+ \(most recent call first\):|Timeout \([0-9:]+\)!)", line):
                lines.append(line)
    return "\n".join(lines)[-4096:]


def owned_run(command, root, *, env=None, capture=False, seconds=MAX_TEST_SECONDS):
    require(os.name == "posix", "CI shard process ownership requires POSIX")
    process = subprocess.Popen(command, cwd=root, env=env, text=True, start_new_session=True,
                               stdout=subprocess.PIPE if capture else None,
                               stderr=subprocess.STDOUT if capture else None)
    try:
        stdout, _ = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, _ = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, _ = process.communicate(timeout=3)
        diagnostics = _collection_diagnostic_tail(stdout)
        raise ShardError(f"owned test process exceeded budget ({seconds}s)" +
                         ("\nSafe collection diagnostic tail:\n" + diagnostics if diagnostics else "")) from None
    return subprocess.CompletedProcess(command, process.returncode, stdout or "")


def collect(root):
    require(not os.environ.get("PYTEST_ADDOPTS", "").strip(), "external pytest selection is forbidden")
    with tempfile.TemporaryDirectory(prefix="ci-collection-") as folder:
        directory = Path(folder)
        (directory / "_ci_collection.py").write_text(COLLECTOR, encoding="utf-8")
        output = directory / "collection.json"
        env = {**os.environ, "CI_SHARD_COLLECTION": str(output),
               "PYTHONPATH": str(directory) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        result = owned_run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "--strict-markers", "-p", "_ci_collection"],
                           root, env=env, capture=True, seconds=90)
        require(result.returncode == 0, "complete pytest collection failed:\n" + result.stdout[-2000:])
        value = read_json(output)
        require(value["deselected"] == 0, "complete collection was filtered")
        _nodeids(value["nodeids"])
        return value["nodeids"]


def verify_collection(root, plan, *, selection_policy=None):
    nodes = collect(root)
    require(digest(nodes) == plan["full_collection_digest"], "full collection changed")
    if plan["scope"] == "plain-sentinels":
        policy = selection_policy if selection_policy is not None else read_selection_policy(root)
        require(digest(policy) == plan["selection_policy_digest"], "sentinel policy changed")
        nodes = select_sentinels(nodes, policy)
    require(nodes == plan["nodeids"], "collection selection changed")


def junit_cases(path, nodes):
    regular(path)
    require(path.stat().st_size <= 50_000_000, "oversized JUnit evidence")
    known = {}
    for node in nodes:
        file = node.split("::", 1)[0]
        stem = PurePosixPath(file).stem
        require(stem not in known or known[stem] == file, "ambiguous JUnit module")
        known[stem] = file
    result = []
    for case in ET.parse(path).getroot().iter("testcase"):
        parts = case.attrib.get("classname", "").split(".")
        matches = [index for index, part in enumerate(parts) if part in known]
        require(len(matches) == 1 and bool(case.attrib.get("name")), "unmappable JUnit case")
        index = matches[0]
        node = "::".join([known[parts[index]], *parts[index + 1:], case.attrib["name"]])
        outcome = "error" if case.find("error") is not None else "failed" if case.find("failure") is not None else "skipped" if case.find("skipped") is not None else "passed"
        result.append({"nodeid": node, "outcome": outcome})
    return result


def coverage_config(path):
    path.write_text(f"[run]\nbranch = True\nrelative_files = True\nsource = {COVERAGE_SOURCE}\n", encoding="utf-8")


def run_shard(root, plan, index, output, *, selection_policy=None):
    validate_plan(plan)
    require(plan["source_digest"] == source_digest(root), "changed source before shard")
    require(plan["git_head"] == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(), "wrong checkout")
    require(plan["runtime"] == runtime(), "wrong shard interpreter/architecture")
    require(plan["run_id"] == os.environ.get("GITHUB_RUN_ID", "local"), "wrong workflow run")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
    require(attempt is None or re.fullmatch(r"[1-9][0-9]{0,5}", attempt) is not None, "invalid workflow run attempt")
    verify_collection(root, plan, selection_policy=selection_policy)
    require(type(index) is int and 0 <= index < len(plan["shards"]), "invalid shard index")
    require(not output.exists(), "shard evidence directory must be fresh")
    output.mkdir(parents=True)
    arguments = output / "selection.txt"
    arguments.write_text("\n".join(plan["shards"][index]["nodeids"]) + "\n", encoding="utf-8")
    junit = output / "pytest.xml"
    env = dict(os.environ)
    command = [sys.executable]
    if plan["mode"] == "coverage":
        rc = output / "coverage.ini"
        coverage_config(rc)
        env.update(COVERAGE_RCFILE=str(rc), COVERAGE_FILE=str(output / ".coverage"))
        command += ["-m", "coverage", "run", "--branch", f"--source={COVERAGE_SOURCE}"]
    command += ["-m", "pytest", f"@{arguments}", "-q", "--strict-markers", "--durations=50", f"--junitxml={junit}"]
    started = time.monotonic()
    result = owned_run(command, root, env=env)
    wall = time.monotonic() - started
    require(source_digest(root) == plan["source_digest"], "source changed while tests ran")
    value = {
        "schema_version": 2, "source_sha": plan["source_sha"], "source_digest": plan["source_digest"],
        "plan_digest": plan["plan_digest"], "collection_digest": plan["collection_digest"],
        "scope": plan["scope"], "full_collection_digest": plan["full_collection_digest"],
        "selection_policy_digest": plan["selection_policy_digest"],
        "mode": plan["mode"], "runtime": runtime(), "run_id": plan["run_id"],
        "run_attempt": None if attempt is None else int(attempt), "index": index, "pytest_return_code": result.returncode, "wall_seconds": wall,
        "cases": junit_cases(junit, plan["nodeids"]), "junit_sha256": file_digest(junit),
        "coverage_sha256": file_digest(output / ".coverage") if plan["mode"] == "coverage" else None,
    }
    write_json(output / "result.json", value)
    require(result.returncode == 0, "shard pytest failed")
    if plan["scope"] == "plain-sentinels":
        require(all(case["outcome"] == "passed" for case in value["cases"]), "every sentinel must have passed")


@contextmanager
def working_directory(root):
    previous = Path.cwd()
    try:
        os.chdir(root)
        yield
    finally:
        os.chdir(previous)


def aggregate(root, plan, results, output, expected_source):
    validate_plan(plan)
    require(plan["git_head"] == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(), "wrong aggregate checkout")
    verify_collection(root, plan)
    return _aggregate_evidence(root, plan, results, output, expected_source)


def aggregate_collected(root, plan, results, output, expected_source, *, inventory,
                        mode='coverage', expected_git_head=None):
    """Trusted RC aggregate: inspect data, never import/collect candidate tests.

    inventory is the complete output of the approved isolated collection job,
    independently bound to this source/run by the caller's producer checks.
    """
    validate_plan(plan)
    _nodeids(inventory)
    require(mode in {'coverage','plain'} and plan["scope"] == "complete" and plan["mode"] == mode,
            "RC requires complete "+mode)
    require(inventory == plan["nodeids"] and digest(inventory) == plan["full_collection_digest"],
            "plan differs from independent collection")
    head=expected_source if expected_git_head is None else expected_git_head
    require(plan["git_head"] == head and head == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(), "wrong aggregate checkout")
    return _aggregate_evidence(root, plan, results, output, expected_source)


def _aggregate_evidence(root, plan, results, output, expected_source):
    require(plan["run_id"] == os.environ.get("GITHUB_RUN_ID", "local"), "wrong workflow run")
    paths = sorted(results.rglob("result.json"))
    rows = [read_json(path) for path in paths]
    wall = verify_results(plan, rows, expected_source, source_digest(root),
                          paths=[path.relative_to(results) for path in paths])
    require(not output.exists(), "aggregate evidence directory must be fresh")
    output.mkdir(parents=True)
    suites = ET.Element("testsuites")
    data_dirs = []
    for path, row in zip(paths, rows, strict=True):
        junit = path.parent / "pytest.xml"
        require(row.get("junit_sha256") == file_digest(junit), "corrupt JUnit evidence")
        require(row["cases"] == junit_cases(junit, plan["nodeids"]), "JUnit/result disagreement")
        xml = ET.parse(junit).getroot()
        require(xml.tag == "testsuites", "invalid JUnit root")
        suites.extend(deepcopy(list(xml)))
        if plan["mode"] == "coverage":
            import coverage
            data = path.parent / ".coverage"
            require(row.get("coverage_sha256") == file_digest(data), "corrupt coverage data")
            measured = coverage.CoverageData(basename=str(data))
            measured.read()
            require(measured.has_arcs() and bool(measured.measured_files()), "missing branch coverage")
            for file in measured.measured_files():
                name = PurePosixPath(file)
                require(not name.is_absolute() and ".." not in name.parts, "unsafe coverage path")
                require(any(file.startswith(prefix + "/") for prefix in COVERAGE_SOURCE.split(",")), "coverage outside declared source")
            directory = output / f"coverage-{row['index']}"
            directory.mkdir()
            (directory / f".coverage.{row['index']}").write_bytes(data.read_bytes())
            data_dirs.append(str(directory))
    ET.ElementTree(suites).write(output / "pytest.xml", encoding="utf-8", xml_declaration=True)
    if plan["mode"] == "coverage":
        import coverage
        rc = output / "coverage.ini"
        coverage_config(rc)
        cov = coverage.Coverage(data_file=str(output / ".coverage"), config_file=str(rc))
        with working_directory(root):
            cov.combine(data_paths=data_dirs, strict=True, keep=True)
            cov.save()
            cov.json_report(outfile=str(output / "coverage.json"))
    write_json(output / "aggregate.json", {"schema_version": 2, "source_sha": expected_source,
               "scope": plan["scope"], "full_collection_digest": plan["full_collection_digest"],
               "selection_policy_digest": plan["selection_policy_digest"],
               "shards": len(rows), "cases": len(plan["nodeids"]), "wall_seconds": wall})
    print(f"Verified {len(rows)} disjoint shards / {len(plan['nodeids'])} cases; sum of test-process time {wall:.2f}s")
    return wall


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("plan")
    prepare.add_argument("--root", type=Path, default=Path.cwd())
    prepare.add_argument("--source-sha", required=True)
    prepare.add_argument("--mode", choices=("plain", "coverage"), required=True)
    prepare.add_argument("--shards", type=int, required=True)
    prepare.add_argument("--weights", type=Path)
    prepare.add_argument("--selection-policy", type=Path)
    prepare.add_argument("--capacity-policy", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    for name in ("run", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--root", type=Path, default=Path.cwd())
        sub.add_argument("--plan", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
        if name == "run":
            sub.add_argument("--index", type=int, required=True)
            sub.add_argument("--selection-policy-evidence", type=Path)
        else:
            sub.add_argument("--results", type=Path, required=True)
            sub.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        if args.command == "plan":
            weights = read_json(args.weights).get(args.mode, {}) if args.weights else {}
            require(args.selection_policy is None or args.mode == "plain", "sentinel selection is allowed only in plain mode")
            policy = read_selection_policy(root, args.selection_policy) if args.selection_policy is not None else None
            require(args.capacity_policy is None or args.mode == "coverage", "capacity policy is allowed only in coverage mode")
            capacities = capacity_policy(read_json(args.capacity_policy), args.shards)[0] if args.capacity_policy is not None else None
            git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            value = make_plan(collect(root), args.shards, source_sha=args.source_sha, source_digest=source_digest(root), mode=args.mode, weights=weights, git_head=git_head, selection_policy=policy, capacities=capacities)
            write_json(args.output, value)
            print(f"Planned {len(value['nodeids'])} cases in {len(value['shards'])} disjoint shards; max estimated test time {max(row['estimated_seconds'] for row in value['shards']):.1f}s")
        elif args.command == "run":
            policy=read_json(args.selection_policy_evidence) if args.selection_policy_evidence else None
            run_shard(root, read_json(args.plan), args.index, args.output.resolve(),selection_policy=policy)
        else:
            aggregate(root, read_json(args.plan), args.results, args.output.resolve(), args.source_sha)
        return 0
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError) as error:
        print(f"CI shard evidence failed closed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
