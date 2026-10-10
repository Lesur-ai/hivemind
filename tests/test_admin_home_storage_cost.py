"""Home SHORT cost characterization, not an HTTP/S3 performance benchmark.

The displayed limit does not bound storage work today. Exercise the real
list_and_get loop over the established offline storage seam so direct-dict
read fakes cannot hide its GETs. A future backend optimization may deliberately
change these measurements; the UI must not imply it already exists.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from live_mem.core.live import LiveService
from live_mem.core.storage import StorageService
from live_mem.core.space import SpaceService
from tests.test_hivemind_note_replication import (
    SPACE,
    _make_note,
    _note_md,
    _runtime,
    _seed_hivemind_space,
)
from tests.test_write_sink import WriteSinkFakeStorage


PREFIX = f"{SPACE}/live/"
BODY = "Synthetic note é — " + "x" * 80


@pytest.mark.asyncio
@pytest.mark.parametrize("include_counts", [False, True, None])
@pytest.mark.parametrize("allowed", [None, [], ["visible"]])
async def test_dashboard_metadata_inventory_io_and_legacy_default(include_counts, allowed):
    storage = AsyncMock()
    storage.list_prefixes.return_value = ["visible/", "private/", "_system/", "orphan/"]
    metadata = {"last_consolidation": "2026-09-30T08:00:00Z", "consolidation_count": 7,
                "total_notes_processed": 41, "description": "A real space"}
    storage.get_json.side_effect = lambda key: None if key == "orphan/_meta.json" else metadata
    storage.list_objects.return_value = [{"Key": "visible/live/.keep"}, {"Key": "visible/live/n.md"}]
    with patch("live_mem.core.space.get_storage", return_value=storage):
        options = {} if include_counts is None else {"include_counts": include_counts}
        result = await SpaceService().list_spaces(allowed_space_ids=allowed, **options)
    expected_ids = ["visible", "private"] if allowed is None else allowed
    assert [space["space_id"] for space in result["spaces"]] == expected_ids
    assert result["total"] == len(expected_ids)
    assert [call.args[0] for call in storage.get_json.await_args_list] == (
        ["visible/_meta.json", "private/_meta.json", "orphan/_meta.json"] if allowed is None
        else [f"{sid}/_meta.json" for sid in allowed])
    storage.list_prefixes.assert_awaited_once_with("")
    assert [call.args[0] for call in storage.list_objects.await_args_list] == (
        [] if include_counts is False else [f"{sid}/{tier}/" for sid in expected_ids for tier in ["live", "bank"]])
    for space in result["spaces"]:
        assert all(space[key] == metadata[key] for key in ["last_consolidation", "consolidation_count", "total_notes_processed"])
        if include_counts is False:
            assert "live_notes_count" not in space and "bank_files_count" not in space
        else:
            assert space["live_notes_count"] == space["bank_files_count"] == 1
    storage.put.assert_not_awaited()
    storage.delete.assert_not_awaited()


class MeasuredStorage(WriteSinkFakeStorage):
    # Use the production loop, not LiveFakeStorage's dictionary shortcut.
    list_and_get = StorageService.list_and_get

    def __init__(self) -> None:
        super().__init__()
        self.lists: list[tuple[str, int]] = []
        self.reads: list[tuple[str, int]] = []
        self.heads: list[str] = []

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        objects = await super().list_objects(prefix, max_keys)
        # The shared fake reports characters. S3 reports bytes.
        for item in objects:
            item["Size"] = len(self.objects[item["Key"]].encode("utf-8"))
        self.lists.append((prefix, len(objects)))
        return objects

    async def get(self, key: str) -> str | None:
        value = await super().get(key)
        self.reads.append((key, len(value.encode("utf-8")) if value is not None else 0))
        return value

    async def exists(self, key: str) -> bool:
        self.heads.append(key)
        return await super().exists(key)


def _seed_notes(storage: MeasuredStorage, count: int) -> list[str]:
    storage.objects[f"{SPACE}/_meta.json"] = "{}"
    storage.objects[PREFIX + ".keep"] = ""
    keys = []
    for index in range(count):
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)
        key = PREFIX + f"{timestamp:%Y%m%dT%H%M%S}_agent_observation_{index:08d}.md"
        storage.objects[key] = _note_md(
            agent="agent", category="observation", body=BODY,
            ts=timestamp.isoformat(), tags=[],
        )
        keys.append(key)
    return keys


async def _measure(storage: MeasuredStorage) -> tuple[dict, dict]:
    before = storage.snapshot()
    mutations = storage.put_calls, storage.delete_calls
    storage.lists.clear()
    storage.reads.clear()
    storage.heads.clear()
    with patch("live_mem.core.live.get_storage", return_value=storage):
        result = await LiveService().read_notes(SPACE, limit=20)
    assert result["status"] == "ok"
    assert storage.snapshot() == before
    assert (storage.put_calls, storage.delete_calls) == mutations
    assert storage.heads == [f"{SPACE}/_meta.json"]
    assert not any(key.endswith(".keep") for key, _ in storage.reads)
    note_reads = [(key, size) for key, size in storage.reads if key.startswith(PREFIX) and key.endswith(".md")]
    origin_reads = [(key, size) for key, size in storage.reads if key.startswith(PREFIX + "_origin/")]
    metrics = {
        "list_calls": len(storage.lists),
        "listed_objects": sum(count for _, count in storage.lists),
        "note_gets": len(note_reads),
        "note_bytes_read": sum(size for _, size in note_reads),
        "origin_gets": len(origin_reads),
        "control_gets": sum(not key.startswith(PREFIX) for key, _ in storage.reads),
        "all_gets": len(storage.reads),
        "all_bytes_read": sum(size for _, size in storage.reads),
        "returned_notes": len(result["notes"]),
        "returned_content_bytes": sum(len(note["content"].encode("utf-8")) for note in result["notes"]),
        "serialized_result_bytes": len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        "has_more": result["has_more"],
    }
    return result, metrics


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 20, 200, 2000])
async def test_home_latest_notes_limit_does_not_bound_prefix_reads(count: int) -> None:
    storage = MeasuredStorage()
    keys = _seed_notes(storage, count)
    result, metrics = await _measure(storage)

    assert storage.lists == [(PREFIX, count + 1)]  # Includes .keep, never GETs it.
    assert metrics["note_gets"] == count
    assert metrics["note_bytes_read"] == sum(len(storage.objects[key].encode("utf-8")) for key in keys)
    assert metrics["origin_gets"] == 0
    assert metrics["control_gets"] == 3  # Missing node, membership and node health.
    assert metrics["all_gets"] == count + 3
    assert metrics["all_bytes_read"] == metrics["note_bytes_read"]
    assert result["total"] == metrics["returned_notes"] == min(count, 20)
    assert result["has_more"] is (count > 20)
    assert [note["filename"] for note in result["notes"]] == [key.rsplit("/", 1)[1] for key in reversed(keys[-20:])]
    assert metrics["returned_content_bytes"] == min(count, 20) * len(BODY.encode("utf-8"))
    print("HOME_STORAGE_COST " + json.dumps({"case": f"local-{count}", **metrics}, sort_keys=True))


@pytest.mark.asyncio
async def test_home_hivemind_sidecar_reads_are_counted_but_never_displayed() -> None:
    storage = MeasuredStorage()
    _seed_notes(storage, 1)
    await _seed_hivemind_space(storage)
    peer = _make_note()
    await _runtime(storage).replicate_inbound(
        note=peer, event_id="home-cost-seed", event_ts="2026-01-01T00:00:01+00:00",
    )
    result, metrics = await _measure(storage)

    assert storage.lists == [(PREFIX, 4)]  # Two notes, their one sidecar, .keep.
    assert metrics["note_gets"] == metrics["returned_notes"] == 2
    # Full-prefix loading GETs the sidecar once, then provenance looks up both
    # returned notes (one existing sidecar again and one absent local sidecar).
    assert metrics["origin_gets"] == 3
    assert metrics["control_gets"] == 6
    assert metrics["all_gets"] == 11
    assert result["has_more"] is False
    assert all(note["filename"].endswith(".md") for note in result["notes"])
    assert {note["provenance"]["is_local"] for note in result["notes"]} == {True, False}
    print("HOME_STORAGE_COST " + json.dumps({"case": "hivemind-2", **metrics}, sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('credential,allowed', [
    (None, None),
    ({'permissions': ['read'], 'allowed_resources': []}, []),
    ({'permissions': ['read'], 'allowed_resources': ['visible']}, ['visible']),
    ({'permissions': ['admin'], 'allowed_resources': []}, None),
])
@pytest.mark.parametrize('options,counts', [({}, True), ({'include_counts': False}, False)])
async def test_dashboard_space_list_tool_keeps_auth_filter_and_default(credential, allowed, options, counts):
    from mcp.server.mcpserver import MCPServer
    from live_mem.tools.space import register
    mcp = MCPServer('dashboard-metadata-test')
    register(mcp)
    handler = mcp._tool_manager.get_tool('space_list')
    assert handler.parameters['properties']['include_counts']['default'] is True
    service = AsyncMock()
    service.list_spaces.return_value = {'status': 'ok', 'spaces': []}
    with patch('live_mem.auth.context._get_effective_token_info', return_value=credential), \
         patch('live_mem.core.space.get_space_service', return_value=service):
        result = await handler.fn(**options)
    if credential is None:
        assert result['status'] == 'error' and 'Authentication' in result['message']
        service.list_spaces.assert_not_awaited()
    else:
        assert result['status'] == 'ok'
        service.list_spaces.assert_awaited_once_with(allowed_space_ids=allowed, include_counts=counts)


@pytest.mark.asyncio
@pytest.mark.parametrize('old,new,counts,allowed', [
    ('if include_counts:', 'if True:', False, ['visible']),
    ('if allowed_space_ids is not None and sid not in allowed_space_ids:', 'if False:', False, ['visible']),
    ('include_counts: bool = True', 'include_counts: bool = False', None, ['visible']),
])
async def test_dashboard_metadata_io_and_permission_guards_are_mutation_proven(monkeypatch, old, new, counts, allowed):
    import inspect
    import textwrap
    import live_mem.core.space as space_module
    source = textwrap.dedent(inspect.getsource(SpaceService.list_spaces))
    assert source.count(old) == 1
    namespace = dict(vars(space_module))
    namespace['get_storage'] = lambda: space_module.get_storage()  # Preserve the offline patch seam.
    exec(compile(source.replace(old, new), '<dashboard-metadata-mutant>', 'exec'), namespace)
    monkeypatch.setattr(SpaceService, 'list_spaces', namespace['list_spaces'])
    with pytest.raises(AssertionError):
        await test_dashboard_metadata_inventory_io_and_legacy_default(counts, allowed)
