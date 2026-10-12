"""Standard bootstrap and compaction-to-edit regression witnesses."""

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json

import pytest

from live_mem.auth import context as auth_context
from live_mem.core import consolidator as normal
from live_mem.core import space as spaces
from live_mem.core.space import SpaceService
from live_mem.tools.space import register
from live_mem.core.write_sink import DirectLocalWriteSink
from live_mem.core.engines import EngineRegistry
from tests.test_lm2_11_access_roles import wired, _handler
from tests.test_consolidator_compaction import make_service, RecordingChat


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = Path.cwd()
STANDARD = ROOT / "RULES/live-mem.standard.memory.bank.md"
FILENAMES = {
    "projectbrief.md", "productContext.md", "systemPatterns.md",
    "techContext.md", "activeContext.md", "progress.md",
}


def test_standard_rules_fingerprint_matches_shipped_content():
    from live_mem.core.bank_template import STANDARD_RULES_SHA256
    assert hashlib.sha256(STANDARD.read_text(encoding="utf-8").encode("utf-8")).hexdigest() == STANDARD_RULES_SHA256


@pytest.mark.parametrize("configured", ["", "RULES/live-mem.standard.memory.bank.md"])
async def test_mcp_default_space_has_verified_standard_bank(wired, monkeypatch, configured):
    storage, _ = wired
    monkeypatch.chdir(RUN_ROOT)
    monkeypatch.setattr("live_mem.config.get_settings", lambda: SimpleNamespace(default_rules_file=configured))
    actor = {"type": "bootstrap", "permissions": ["admin"], "client_name": "admin"}
    token = auth_context.current_token_info.set(actor)
    try:
        result = await _handler(register, "space_create")("fresh", "Synthetic project")
    finally:
        auth_context.current_token_info.reset(token)
    assert result["status"] == "created"
    assert storage.objects["fresh/_rules.md"] == STANDARD.read_text()
    files = {key.removeprefix("fresh/bank/") for key in storage.objects if key.startswith("fresh/bank/") and not key.endswith(".keep")}
    assert files == FILENAMES
    for filename in files:
        body = storage.objects[f"fresh/bank/{filename}"]
        assert body.startswith("# ") and "\n## " in body
        assert storage.events.index(f"put:fresh/bank/{filename}") < storage.events.index("put:fresh/_meta.json")


async def test_default_seed_failure_retries_additively_without_commit(wired, monkeypatch):
    storage, _ = wired
    monkeypatch.chdir(RUN_ROOT)
    monkeypatch.setattr("live_mem.config.get_settings", lambda: SimpleNamespace(default_rules_file=""))
    key = "fresh/bank/progress.md"
    storage.fail_before_put.add(key)
    first = await SpaceService().create("fresh", "Synthetic", "", bootstrap_admin=True)
    assert first["status"] == "partial" and first["recovery"]["retry_safe"]
    assert "fresh/_meta.json" not in storage.objects
    before = dict(storage.objects)
    storage.fail_before_put.clear()
    second = await SpaceService().create("fresh", "Synthetic", "", bootstrap_admin=True)
    assert second["status"] == "created"
    assert all(storage.objects[k] == v for k, v in before.items())


async def test_explicit_rules_are_not_replaced_or_standard_seeded(wired):
    storage, _ = wired
    result = await SpaceService().create("custom", "Synthetic", "# Custom\nOnly index.md", bootstrap_admin=True)
    assert result["status"] == "created"
    assert storage.objects["custom/_rules.md"] == "# Custom\nOnly index.md"
    assert not any(k.endswith(".md") for k in storage.objects if k.startswith("custom/bank/"))


async def test_invalid_custom_default_path_does_not_mutate(wired, monkeypatch, tmp_path):
    storage, _ = wired
    monkeypatch.setattr("live_mem.config.get_settings", lambda: SimpleNamespace(default_rules_file=str(tmp_path / "missing.md")))
    result = await SpaceService().create("fresh", "Synthetic", "", bootstrap_admin=True)
    assert result["status"] == "error" and "not found" in result["message"]
    assert storage.objects == {}


async def test_custom_default_file_keeps_its_dynamic_structure(wired, monkeypatch, tmp_path):
    storage, _ = wired
    rules_path = tmp_path / "custom.md"
    rules_path.write_text("# Custom rules\nOnly maintain index.md.\n")
    monkeypatch.setattr("live_mem.config.get_settings", lambda: SimpleNamespace(default_rules_file=str(rules_path)))
    result = await SpaceService().create("custom", "Synthetic", "", bootstrap_admin=True)
    assert result["status"] == "created"
    assert storage.objects["custom/_rules.md"] == rules_path.read_text()
    assert not any(k.endswith(".md") for k in storage.objects if k.startswith("custom/bank/"))


async def test_unverified_seed_never_commits_and_conflicting_retry_is_refused(wired, monkeypatch):
    storage, _ = wired
    monkeypatch.chdir(RUN_ROOT)
    monkeypatch.setattr("live_mem.config.get_settings", lambda: SimpleNamespace(default_rules_file=""))
    original_put = storage.put
    async def corrupt_seed(key, content, content_type="text/plain"):
        await original_put(key, "conflicting bytes" if key == "fresh/bank/progress.md" else content, content_type)
    monkeypatch.setattr(storage, "put", corrupt_seed)
    first = await SpaceService().create("fresh", "Synthetic", "", bootstrap_admin=True)
    assert first["status"] == "partial"
    assert "fresh/_meta.json" not in storage.objects
    before = dict(storage.objects)
    monkeypatch.setattr(storage, "put", original_put)
    second = await SpaceService().create("fresh", "Synthetic", "", bootstrap_admin=True)
    assert second["status"] == "partial" and not second["recovery"]["retry_safe"]
    assert storage.objects == before


@pytest.mark.parametrize("operation", ["replace_section", "append_to_section", "prepend_to_section"])
@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_existing_h1_body_is_editable_without_changing_title_or_children(operation, eol):
    root = "# Student Index — Synthetic" + eol
    children = "## Untouched" + eol + "exact evidence" + eol + "# Other" + eol + "other body" + eol
    source = root + eol + "old row" + eol + children
    candidate, failures, _ = normal._normal_edit_candidate(source, [{
        "type": operation, "heading": root.strip(), "content": "new row",
        "reason": "Update index", "notes": [1],
    }], 0)
    assert failures == []
    assert candidate.startswith(root)
    assert candidate.endswith(children)
    assert "new row" in candidate
    assert ("old row" in candidate) == (operation != "replace_section")


@pytest.mark.parametrize("operation,heading,body", [
    ("delete_section", "# Root", "unused"),
    ("add_section", "# New", "new"),
    ("append_to_section", "# Missing", "new"),
    ("replace_section", "# Root", "# Injected\nbody"),
])
def test_h1_destructive_and_injected_operations_remain_refused(operation, heading, body):
    candidate, failures, _ = normal._normal_edit_candidate("# Root\n\nkept\n", [{
        "type": operation, "heading": heading, "content": body,
        "reason": "Invalid", "notes": [1],
    }], 0)
    assert candidate is None and failures


@pytest.mark.parametrize("source,body,reason", [
    ("# Root\nfirst\n# Root\nsecond\n", "addition", "ambiguous_or_missing_normal_target"),
    ("# Root\nintro\n### Child\nkept\n", "## New Parent\nnew", "normal_append_reparents_source"),
])
def test_h1_body_edit_cannot_choose_duplicate_or_reparent_child(source, body, reason):
    candidate, failures, _ = normal._normal_edit_candidate(source, [{
        "type": "append_to_section", "heading": "# Root", "content": body,
        "reason": "Invalid source or hierarchy", "notes": [1],
    }], 0)
    assert candidate is None and failures[0]["reason"] == reason


async def test_actual_compaction_body_can_be_edited_on_next_consolidation():
    service = make_service(max_size=100)
    service._complete_chat = RecordingChat("Inventory: 12 tablets; owner Morgan.")
    source = "# Inventory\n\n## Stock\n" + "Inventory: 12 tablets; owner Morgan. " * 20
    compacted, details = await service._plan_single_file_compaction("inventory.md", source, 100, "# Rules")
    assert details["status"] == "ok", details
    assert compacted.startswith("# Inventory\n") and len(compacted) < len(source)
    assert "## " not in compacted
    candidate, failures, _ = normal._normal_edit_candidate(compacted, [{
        "type": "append_to_section", "heading": "# Inventory",
        "content": "Loan register validated.", "reason": "New completed work", "notes": [1],
    }], 0)
    assert failures == []
    assert "12 tablets" in candidate and "Loan register validated." in candidate


async def test_create_consolidate_compact_consolidate_transaction_cycle(wired, monkeypatch):
    storage, _ = wired
    monkeypatch.chdir(RUN_ROOT)
    monkeypatch.setattr("live_mem.config.get_settings", lambda: SimpleNamespace(default_rules_file=""))
    assert (await SpaceService().create("fresh", "Synthetic", "", bootstrap_admin=True))["status"] == "created"
    monkeypatch.setattr(normal, "get_storage", lambda: storage)
    service = make_service(max_size=120)
    service._max_notes = 100
    service._batch_size = 2
    service._validation_enabled = False
    registry = EngineRegistry(storage=storage, consolidator=service)
    monkeypatch.setattr("live_mem.core.engines.get_engine_registry", lambda: registry)
    # Use the real initial and final routing checks over the fake durable store.
    del service._final_direct_local_compaction_sink
    phase = 0
    normal_calls = []
    async def chat(messages, output_budget, *, retry_policy="bounded", normal_json=False):
        if normal_json:
            normal_calls.append(messages)
            operation = {
                "type": "append_to_section", "heading": "## Current Focus" if phase == 0 else "# Active Context",
                "content": "Inventory: 12 tablets; owner Morgan. " * 20 if phase == 0 else "Loan register validated.",
                "reason": "Record synthetic work", "notes": [1],
            }
            text = json.dumps({"file_edits": [{"filename": "activeContext.md", "action": "edit", "operations": [operation]}], "discarded_notes": [], "synthesis": "Inventory work recorded."})
        else:
            text = "Inventory: 12 tablets; owner Morgan."
        return await RecordingChat(text)(messages, output_budget)
    service._complete_chat = chat
    note = "fresh/live/20261011T000000_bench_observation_1.md"
    storage.objects[note] = "Inventory: 12 tablets; owner Morgan."
    first = await service.consolidate("fresh", enforce_cooldown=False)
    assert first["status"] == "ok", first
    assert first["notes_deleted"] == 1 and note not in storage.objects
    assert "## Current Focus" in str(normal_calls[0])
    source = storage.objects["fresh/bank/activeContext.md"]
    with normal._direct_local_compaction_authority(normal._issue_direct_local_compaction_authority("fresh", DirectLocalWriteSink(storage))):
        compacted = await service.compact_bank("fresh", dry_run=False)
    assert compacted["status"] == "ok", compacted
    assert len(storage.objects["fresh/bank/activeContext.md"]) < len(source)
    assert any(k.startswith("_backups/fresh/") for k in storage.objects)
    phase = 1
    storage.objects[note] = "Loan register validated."
    second = await service.consolidate("fresh", enforce_cooldown=False)
    assert second["status"] == "ok", second
    assert second["notes_deleted"] == 1 and note not in storage.objects
    final = storage.objects["fresh/bank/activeContext.md"]
    assert final.startswith("# Active Context\n")
    assert "12 tablets" in final and "Loan register validated." in final


async def test_standard_rules_at_absolute_path_seed_from_another_cwd(wired, monkeypatch, tmp_path):
    storage, _ = wired
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('live_mem.config.get_settings', lambda: SimpleNamespace(default_rules_file=str(STANDARD)))
    assert (await SpaceService().create('fresh', 'Synthetic', bootstrap_admin=True))['status'] == 'created'
    assert {k.split('/')[-1] for k in storage.objects if k.startswith('fresh/bank/') and not k.endswith('.keep')} == FILENAMES


async def test_custom_rules_over_shipped_path_do_not_seed(wired, monkeypatch, tmp_path):
    storage, _ = wired
    custom = tmp_path / 'RULES/live-mem.standard.memory.bank.md'
    custom.parent.mkdir(); custom.write_text('# Custom\nOnly index.md.\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('live_mem.config.get_settings', lambda: SimpleNamespace(default_rules_file=''))
    assert (await SpaceService().create('fresh', 'Synthetic', bootstrap_admin=True))['status'] == 'created'
    assert not any(k.endswith('.md') for k in storage.objects if k.startswith('fresh/bank/'))


async def _new_mesh_target(monkeypatch):
    from tests.test_hivemind_state import FakeStorage
    from live_mem.core import tokens, locks
    storage = FakeStorage()
    monkeypatch.setattr(spaces, 'get_storage', lambda: storage)
    monkeypatch.setattr(tokens, 'get_storage', lambda: storage)
    monkeypatch.setattr(tokens, '_token_service', tokens.TokenService())
    monkeypatch.setattr(locks, '_lock_manager', locks.LockManager())
    monkeypatch.setattr('live_mem.config.get_settings', lambda: SimpleNamespace(default_rules_file=str(STANDARD)))
    assert (await SpaceService().create('target-space', 'New Mesh target', bootstrap_admin=True))['status'] == 'created'
    return storage


@pytest.mark.parametrize('pending', [False, True])
async def test_created_standard_target_imports_exact_source_without_surplus_templates(monkeypatch, pending):
    from live_mem.core.hivemind import BootstrapService, HiveNodeStatus
    from tests.test_hivemind_bootstrap import _seed_source, SOURCE, TARGET
    from tests.test_hivemind_pending_import import _seed_source_with_pending_peer
    storage = await _new_mesh_target(monkeypatch)
    keys = (await _seed_source_with_pending_peer(storage))[1] if pending else await _seed_source(storage)
    service = BootstrapService(storage)
    snapshot = await service.export_snapshot(SOURCE)
    result = await (service.import_pending_snapshot if pending else service.import_snapshot)(TARGET, snapshot, keys)
    expected = {p: content for p, content in snapshot.files.items() if p.startswith('bank/')}
    actual = {k.removeprefix(TARGET+'/'): v for k,v in storage.snapshot().items()
              if k.startswith(TARGET+'/bank/') and not k.endswith('/.keep')}
    assert actual == expected
    assert result.node_status == (HiveNodeStatus.UNSAFE if pending else HiveNodeStatus.HEALTHY)


@pytest.mark.parametrize('fault', ['edited','same_size_edit','missing','extra','live','synthesis','protocol','history','rules','invalid_counter'])
async def test_only_complete_pristine_standard_bank_is_a_blank_mesh_target(monkeypatch, fault):
    from live_mem.core.hivemind import BootstrapService, BootstrapError
    storage = await _new_mesh_target(monkeypatch)
    prefix = 'target-space/'
    if fault == 'edited': await storage.put(prefix+'bank/progress.md', '# Progress\nReal work')
    elif fault == 'same_size_edit':
        key = prefix+'bank/progress.md'
        await storage.put(key, (await storage.get(key)).replace('History', 'Fact123'))
    elif fault == 'missing': await storage.delete(prefix+'bank/progress.md')
    elif fault == 'extra': await storage.put(prefix+'bank/z-extra.md', 'Real work')
    elif fault == 'live': await storage.put(prefix+'live/note.md', 'Real work')
    elif fault == 'synthesis': await storage.put(prefix+'_synthesis.md', 'Real work')
    elif fault == 'protocol': await storage.put(prefix+'_hivemind/node_status.json', '{}')
    elif fault == 'history':
        meta = await storage.get_json(prefix+'_meta.json');meta['consolidation_count']=1
        await storage.put_json(prefix+'_meta.json', meta)
    elif fault == 'rules': await storage.put(prefix+'_rules.md', '# Custom')
    elif fault == 'invalid_counter':
        meta = await storage.get_json(prefix+'_meta.json'); meta['consolidation_count'] = False
        await storage.put_json(prefix+'_meta.json', meta)
    before = storage.snapshot()
    with pytest.raises(BootstrapError): await BootstrapService(storage)._assert_blank_target('target-space')
    assert storage.snapshot() == before


@pytest.mark.parametrize('fault', ['manifest','delete','silent_delete','drift'])
async def test_pristine_seed_import_failures_preserve_content_and_fail_closed(monkeypatch, fault):
    from live_mem.core.hivemind import BootstrapService, BootstrapError, HivemindStateStore, HiveNodeStatus
    from tests.test_hivemind_bootstrap import _seed_source, SOURCE, TARGET
    storage = await _new_mesh_target(monkeypatch)
    keys = await _seed_source(storage)
    service = BootstrapService(storage); snapshot = await service.export_snapshot(SOURCE)
    original_delete, original_put = storage.delete, storage.put
    deleted = []
    async def delete(key):
        deleted.append(key)
        if fault == 'delete': raise OSError('seed deletion failed')
        if fault == 'silent_delete': return None
        return await original_delete(key)
    async def put(key, value, **kwargs):
        result = await original_put(key,value,**kwargs)
        if fault == 'drift' and key == TARGET+'/_hivemind/node_status.json':
            await original_put(TARGET+'/bank/projectbrief.md', '# Project Brief\nNew fact')
        return result
    monkeypatch.setattr(storage,'delete',delete);monkeypatch.setattr(storage,'put',put)
    if fault == 'manifest': snapshot.files['bank/activeContext.md'] += 'tampered'
    before = storage.snapshot()
    with pytest.raises((BootstrapError,OSError)): await service.import_snapshot(TARGET,snapshot,keys)
    if fault == 'manifest': assert storage.snapshot() == before and not deleted
    else:
        health = await HivemindStateStore(storage=storage,space_id=TARGET).get_node_status()
        assert health.status == HiveNodeStatus.UNSAFE
        if fault == 'drift':
            assert not deleted
            assert await storage.get(TARGET+'/bank/projectbrief.md') == '# Project Brief\nNew fact'
