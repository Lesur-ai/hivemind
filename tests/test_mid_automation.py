"""Automatic policy uses the existing compactor and archive worker offline."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from live_mem.config import Settings, get_settings
from live_mem.core import mid_automation
from live_mem.core.write_sink import DirectLocalWriteSink
from tests.test_mid_archive_projection import setup_case
from live_mem.core.consolidation_queue import ConsolidationQueueService
from live_mem.core.locks import LockManager


def test_default_policy_and_independent_env_opt_out(monkeypatch):
    monkeypatch.delenv('MID_AUTO_COMPACT', raising=False)
    monkeypatch.delenv('MID_AUTO_ARCHIVE', raising=False)
    settings = Settings(_env_file=None)
    assert settings.mid_auto_compact is True
    assert settings.mid_auto_archive is True
    monkeypatch.setenv('MID_AUTO_COMPACT', 'false')
    settings = Settings(_env_file=None)
    assert settings.mid_auto_compact is False and settings.mid_auto_archive is True
    monkeypatch.setenv('MID_AUTO_ARCHIVE', 'false')
    assert Settings(_env_file=None).mid_auto_archive is False


@pytest.fixture
def engine_case(monkeypatch):
    settings = SimpleNamespace(mid_auto_compact=True, mid_auto_archive=True, bank_file_max_size=35000)
    engine = SimpleNamespace(write_sink=object(), compact_bank=AsyncMock())
    registry = SimpleNamespace(mid_engine=AsyncMock(return_value=engine))
    monkeypatch.setattr(mid_automation, 'get_settings', lambda: settings)
    monkeypatch.setattr(mid_automation, 'get_engine_registry', lambda: registry)
    return settings, engine, registry


@pytest.mark.asyncio
async def test_disabled_compaction_does_not_resolve_or_mutate(engine_case):
    settings, engine, registry = engine_case
    settings.mid_auto_compact = False
    assert (await mid_automation.compact_after_consolidation('space'))['status'] == 'disabled'
    registry.mid_engine.assert_not_awaited()
    engine.compact_bank.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_route_never_calls_compactor(engine_case):
    _, engine, _ = engine_case
    result = await mid_automation.compact_after_consolidation('space')
    assert result['status'] == 'not_applicable'
    assert result['reason'] == 'direct_local_route_required'
    engine.compact_bank.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_compactor_called_once_with_timestamped_result(engine_case):
    _, engine, _ = engine_case
    engine.write_sink = DirectLocalWriteSink(storage=object())
    engine.compact_bank.return_value = {'status': 'ok', 'files_over_limit': 1, 'preimage_id': 'capture'}
    result = await mid_automation.compact_after_consolidation('space')
    engine.compact_bank.assert_awaited_once_with('space', dry_run=False)
    assert result['status'] == 'ok' and result['preimage_id'] == 'capture'
    assert result['started_at'].endswith('+00:00') and result['finished_at'].endswith('+00:00')


@pytest.mark.asyncio
async def test_unknown_compactor_failure_never_claims_unchanged_bank(engine_case):
    _, engine, _ = engine_case
    engine.write_sink = DirectLocalWriteSink(storage=object())
    engine.compact_bank.side_effect = RuntimeError('secret completion')
    result = await mid_automation.compact_after_consolidation('space')
    assert result['status'] == 'error' and result['recovery_required'] is True
    assert 'secret' not in str(result)


@pytest.mark.asyncio
async def test_archive_opt_out_retains_work_then_resumes(monkeypatch):
    storage, _, key, long, _, worker = await setup_case()
    original = await storage.get(key)
    monkeypatch.setattr(get_settings(), 'mid_auto_archive', False)
    await worker.run_space('archive-test')
    assert await storage.get(key) == original and not long.submissions
    monkeypatch.setattr(get_settings(), 'mid_auto_archive', True)
    await worker.run_space('archive-test')
    assert await storage.get(key) is None and len(long.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status,notes,expected', [('ok', 1, True), ('ok', 0, False), ('partial', 1, False), ('error', 0, False)])
async def test_queue_automatic_stage_keeps_lock_and_consolidation_outcome(monkeypatch, engine_case, status, notes, expected):
    from live_mem.core import consolidation_queue as module
    _, engine, _ = engine_case
    locks = LockManager()
    monkeypatch.setattr(module, 'get_lock_manager', lambda: locks)
    monkeypatch.setattr(module, 'get_consolidator', lambda: SimpleNamespace(consolidate=AsyncMock(
        return_value={'status': status, 'notes_processed': notes})))
    engine.write_sink = DirectLocalWriteSink(storage=object())
    async def compact(*args, **kwargs):
        assert locks.consolidation('space').locked()
        return {'status': 'error', 'failure_reason': 'compaction_prepare_failed'}
    engine.compact_bank.side_effect = compact
    queue = ConsolidationQueueService()
    submitted = await queue.enqueue('space', '', 'test')
    await queue._workers['space']
    job = await queue.get_job(submitted['job_id'])
    assert engine.compact_bank.await_count == int(expected)
    assert job['status'] == ('succeeded' if status == 'ok' else 'failed')
    if expected:
        assert job['result']['auto_compaction']['status'] == 'error'


@pytest.mark.asyncio
async def test_cancelled_maintenance_preserves_successful_consolidation(monkeypatch, engine_case):
    import asyncio
    from live_mem.core import consolidation_queue as module
    _, engine, _ = engine_case
    monkeypatch.setattr(module, 'get_consolidator', lambda: SimpleNamespace(consolidate=AsyncMock(
        return_value={'status': 'ok', 'batch_size': 2, 'notes_total': 2,
                      'notes_processed': 1, 'batches_total': 1, 'batches_completed': 1})))
    engine.write_sink = DirectLocalWriteSink(storage=object())
    engine.compact_bank.side_effect = asyncio.CancelledError()
    queue = ConsolidationQueueService()
    submitted = await queue.enqueue('space', '', 'test')
    await queue._workers['space']
    job = await queue.get_job(submitted['job_id'])
    assert job['status'] == 'succeeded'
    assert job['result']['auto_compaction']['status'] == 'cancelled'
    assert job['result']['auto_compaction']['recovery_required'] is True
    assert job['progress']['phase'] == 'done'
    assert job['progress']['batch_size'] == 2
    assert job['progress']['notes_total'] == 2
    assert job['progress']['notes_done'] == 1
    assert job['progress']['batches_total'] == 1
    assert job['progress']['batches_done'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('connected', [False, True])
async def test_status_reports_policy_and_backlog_even_when_unbound(monkeypatch, engine_case, connected):
    from live_mem.core import graph_bridge
    storage, _, _, _, _, _ = await setup_case()
    monkeypatch.setattr(graph_bridge, 'get_storage', lambda: storage)
    service = graph_bridge.GraphBridgeService()
    monkeypatch.setattr(service, '_connection_status', AsyncMock(
        return_value={'status': 'ok', 'connected': connected}))
    result = await service.status('archive-test')
    assert result['mid_automation']['compaction_enabled'] is True
    assert result['mid_automation']['archive_enabled'] is True
    assert result['mid_archive_projection']['pending'] == 1


@pytest.mark.asyncio
async def test_cancelled_transaction_preserves_existing_recovery_receipt(engine_case):
    import asyncio
    _, engine, _ = engine_case
    engine.write_sink = DirectLocalWriteSink(storage=object())
    cancelled = asyncio.CancelledError()
    cancelled.compaction_preimage_id = 'space/preimage'
    cancelled.compaction_rollback_failures = ({'filename': 'facts.md', 'error': 'compaction_rollback_readback_unverified'},)
    engine.compact_bank.side_effect = cancelled
    with pytest.raises(asyncio.CancelledError) as caught:
        await mid_automation.compact_after_consolidation('space')
    result = caught.value.mid_auto_compaction_result
    assert result['preimage_id'] == 'space/preimage'
    assert result['recovery_required'] is True
    assert result['failures'][0]['error'] == 'compaction_rollback_readback_unverified'


@pytest.mark.asyncio
async def test_auto_followup_reuses_real_compactor_with_retained_source_and_pending_capture(monkeypatch, engine_case):
    from live_mem.core import consolidator as module
    from live_mem.core.mid_archive import projection_status
    from tests.test_consolidator_compaction import CompactionStorage, make_service
    _, engine, _ = engine_case
    storage = CompactionStorage()
    await storage.put('space-a/bank/facts.md', 'f' * 120)
    service = make_service(max_size=100)
    service._plan_single_file_compaction = AsyncMock(return_value=(
        'c' * 60, {'status': 'ok', 'action': 'edit', 'operation_reasons': ('Remove repetition.',)}))
    sink = DirectLocalWriteSink(storage)
    engine.write_sink = sink
    engine.compact_bank = service.compact_bank
    monkeypatch.setattr(module, 'get_storage', lambda: storage)
    monkeypatch.setattr(module, 'assert_space_not_reserved', AsyncMock())
    with module._direct_local_compaction_authority(module._issue_direct_local_compaction_authority('space-a', sink)):
        result = await mid_automation.compact_after_consolidation('space-a')
    assert result['status'] == 'ok'
    assert await storage.get('space-a/bank/facts.md') == 'c' * 60
    assert await storage.get('_backups/' + result['preimage_id'] + '/bank/facts.md') == 'f' * 120
    assert (await projection_status(storage, 'space-a'))['pending'] == 1
    assert result['files'][0]['compacted_size'] == 60
