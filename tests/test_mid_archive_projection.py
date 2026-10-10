"""Offline F13 recovery and lifecycle proofs on the real storage/engine seams."""
import asyncio
import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from live_mem.core.engines import EngineRegistry
from live_mem.core.locks import LockManager
from live_mem.core.mid_archive import prepare_mid_archive, projection_status
from live_mem.core.mid_archive_projection import MidArchiveProjector
from tests.test_write_sink import WriteSinkFakeStorage

SPACE = 'archive-test'
PREIMAGE = SPACE + '/2026-09-16T10-00-00-' + 'a' * 32
TEXT = '# Historical MID\nThe launch password is not a password; launch was deferred.\n'


class LongDouble:
    """Matches LongEngine document and Graph queue response envelopes."""
    def __init__(self):
        self.documents = {}
        self.jobs = {}
        self.submissions = []
        self.fail = False
        self.lose_ack = False
        self.complete_immediately = True

    async def get_document(self, space_id, *, source_path):
        if self.fail:
            raise ConnectionError('secret provider payload')
        document = self.documents.get((space_id, source_path))
        return {'status': 'ok', 'document': dict(document)} if document else {
            'status': 'error', 'message': f"Document '{source_path}' not found"}

    async def ingest_async(self, space_id, *, documents, options=None):
        self.submissions.append((space_id, documents, options))
        if self.fail:
            raise ConnectionError('secret provider payload')
        doc = documents[0]
        jid = 'ing_' + str(len(self.submissions))
        self.jobs[jid] = {'status': 'running', 'job_id': jid,
                          'source_path': doc['source_path'], 'sha256': doc['sha256']}
        if self.complete_immediately:
            self.documents[(space_id, doc['source_path'])] = {
                'source_path': doc['source_path'], 'sha256': doc['sha256'],
                'ingestion_status': 'succeeded'}
            self.jobs[jid]['status'] = 'succeeded'
        if self.lose_ack:
            raise ConnectionError('lost acknowledgement')
        return {'status': 'ok', 'items': [{'status': self.jobs[jid]['status'],
                 'source_path': doc['source_path'], 'job_id': jid}]}

    async def ingest_status(self, space_id, job_id):
        return self.jobs.get(job_id, {'status': 'not_found'})

    async def prepare_archive_ingest(self, space_id, *, preimage_id):
        # These regression cases exercise an already-calibrated destination.
        # Initial crafting/replay is covered by test_mid_archive_auto_projection.
        return {'status': 'ok', 'ontology_mode': 'frozen'}

    async def get_archive_document(self, space_id, *, source_path):
        return await self.get_document(space_id, source_path=source_path)

    async def ingest_archive(self, space_id, *, documents, automatic):
        assert automatic is False
        return await self.ingest_async(space_id, documents=documents)

    async def archive_ingest_status(self, space_id, job_id):
        return await self.ingest_status(space_id, job_id)


async def setup_case():
    storage = WriteSinkFakeStorage()
    await storage.put_json(SPACE + '/_meta.json', {
        'space_id': SPACE, 'created_at': '2026-09-16T09:00:00+00:00',
        'graph_memory': {'binding': 'explicit', 'url': 'https://example.org/mcp',
                         'memory_id': 'memory-test', 'ontology': 'general', 'token': 'secret'}})
    await storage.put('_backups/' + PREIMAGE + '/bank/nested/progress.md', TEXT)
    docs = [{'bank_path': 'nested/progress.md', 'sha256': hashlib.sha256(TEXT.encode()).hexdigest(),
             'size_bytes': len(TEXT.encode())}]
    key = await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    long = LongDouble()
    now = [2_000_000_000.0]
    projector = MidArchiveProjector(storage=storage, long_engine=long,
        registry=EngineRegistry(storage=storage), locks=LockManager(), clock=lambda: now[0])
    return storage, docs, key, long, now, projector


@pytest.mark.asyncio
async def test_capture_verified_and_secret_free_then_projection_leaves_source():
    storage, docs, key, long, now, worker = await setup_case()
    raw = await storage.get(key)
    assert 'secret' not in raw and 'https://' not in raw and TEXT not in raw
    assert await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs) == key
    await worker.run_once()
    assert await storage.get(key) is None
    assert await storage.get('_backups/' + PREIMAGE + '/bank/nested/progress.md') == TEXT
    assert len(long.submissions) == 1
    sent = long.submissions[0][1][0]
    assert sent['metadata']['provenance'] == 'mid_archive'
    assert sent['metadata']['preimage_id'] == PREIMAGE
    assert sent['source_path'] != 'nested/progress.md'
    assert (await projection_status(storage, SPACE))['pending'] == 0


@pytest.mark.asyncio
async def test_outage_backoff_survives_restart_without_replaying_success():
    storage, docs, key, long, now, worker = await setup_case()
    long.fail = True
    await worker.run_space(SPACE)
    assert not long.submissions
    status = await projection_status(storage, SPACE)
    assert status['pending'] == 1 and status['error'] == 'projection_unavailable'
    assert 'secret' not in str(status)
    long.fail = False
    restarted = MidArchiveProjector(storage=storage, long_engine=long,
        registry=EngineRegistry(storage=storage), locks=LockManager(), clock=lambda: now[0])
    await restarted.run_space(SPACE)
    assert not long.submissions
    now[0] += 4000
    await restarted.run_space(SPACE)
    assert len(long.submissions) == 1 and await storage.get(key) is None


@pytest.mark.asyncio
async def test_lost_ack_verifies_document_before_any_resubmit():
    storage, docs, key, long, now, worker = await setup_case()
    long.lose_ack = True
    await worker.run_space(SPACE)
    assert await storage.get(key) is not None
    now[0] += 4000
    await worker.run_space(SPACE)
    assert len(long.submissions) == 1 and await storage.get(key) is None


@pytest.mark.asyncio
async def test_running_job_polled_then_vanished_job_can_resume():
    storage, docs, key, long, now, worker = await setup_case()
    long.complete_immediately = False
    await worker.run_space(SPACE)
    now[0] += 60
    await worker.run_space(SPACE)
    assert len(long.submissions) == 1 and await storage.get(key) is not None
    long.jobs.clear()
    long.complete_immediately = True
    now[0] += 60
    await worker.run_space(SPACE)
    assert len(long.submissions) == 2 and await storage.get(key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['archive', 'identity', 'binding', 'cross_space', 'source_path'])
async def test_tamper_and_changed_identity_never_reach_long(change):
    storage, docs, key, long, now, worker = await setup_case()
    if change == 'archive':
        await storage.put('_backups/' + PREIMAGE + '/bank/nested/progress.md', 'altered')
    elif change in ('identity', 'binding'):
        meta = await storage.get_json(SPACE + '/_meta.json')
        if change == 'identity':
            meta['created_at'] = '2026-09-16T09:01:00+00:00'
        else:
            meta['graph_memory']['memory_id'] = 'someone-else'
        await storage.put_json(SPACE + '/_meta.json', meta)
    else:
        record = await storage.get_json(key)
        if change == 'cross_space':
            record['preimage_id'] = 'another/' + PREIMAGE.split('/', 1)[1]
        else:
            record['documents'][0]['bank_path'] = '../_meta.json'
        await storage.put_json(key, record)
    await worker.run_space(SPACE)
    assert long.submissions == [] and await storage.get(key) is not None


@pytest.mark.asyncio
async def test_deleted_space_or_pending_not_resurrected():
    storage, docs, key, long, now, worker = await setup_case()
    await storage.delete(SPACE + '/_meta.json')
    await worker.run_space(SPACE)
    assert not long.submissions
    await storage.delete(key)
    await worker.run_space(SPACE)
    assert await storage.get(key) is None


@pytest.mark.asyncio
async def test_route_refusal_retains_work():
    storage, docs, key, long, now, worker = await setup_case()
    worker.registry.resolve_sink = AsyncMock(side_effect=RuntimeError('shared route'))
    await worker.run_space(SPACE)
    assert not long.submissions and await storage.get(key) is not None


@pytest.mark.asyncio
async def test_prepare_readback_failure_never_returns_success():
    storage, docs, key, long, now, worker = await setup_case()
    await storage.delete(key)
    original = storage.put
    async def corrupt(key, content, content_type='text/plain'):
        await original(key, '{}' if '_mid_archive_pending/' in key else content, content_type)
    storage.put = corrupt
    with pytest.raises(ValueError):
        await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)


@pytest.mark.asyncio
async def test_terminal_job_is_not_document_success_proof():
    storage, docs, key, long, now, worker = await setup_case()
    long.complete_immediately = False
    await worker.run_space(SPACE)
    long.jobs['ing_1']['status'] = 'succeeded'
    now[0] += 60
    await worker.run_space(SPACE)
    assert await storage.get(key) is not None


@pytest.mark.asyncio
async def test_token_rotation_does_not_change_destination():
    storage, docs, key, long, now, worker = await setup_case()
    meta = await storage.get_json(SPACE + '/_meta.json')
    meta['graph_memory']['token'] = 'rotated'
    await storage.put_json(SPACE + '/_meta.json', meta)
    await worker.run_space(SPACE)
    assert len(long.submissions) == 1 and await storage.get(key) is None


@pytest.mark.asyncio
async def test_remote_failed_jobs_backoff_preserved_until_document_proof():
    storage, docs, key, long, now, worker = await setup_case()
    long.complete_immediately = False
    await worker.run_space(SPACE)
    long.jobs['ing_1']['status'] = 'failed'
    now[0] += 30
    await worker.run_space(SPACE)
    record = await storage.get_json(key)
    assert record['failures'] == 1 and record['next_attempt_at'] == now[0] + 30
    await worker.run_space(SPACE)
    assert len(long.submissions) == 1
    now[0] += 30
    await worker.run_space(SPACE)
    assert (await storage.get_json(key))['failures'] == 1
    long.jobs['ing_2']['status'] = 'failed'
    now[0] += 30
    await worker.run_space(SPACE)
    record = await storage.get_json(key)
    assert record['failures'] == 2 and record['next_attempt_at'] == now[0] + 60
    assert len(long.submissions) == 2


@pytest.mark.asyncio
async def test_unbound_capture_is_prepared_and_pinned_before_lost_ack():
    from live_mem.core import mid_archive
    from live_mem.core.memory_id import derive_memory_id
    from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL
    storage, docs, key, long, now, worker = await setup_case()
    meta = await storage.get_json(SPACE + '/_meta.json')
    meta.pop('graph_memory')
    await storage.put_json(SPACE + '/_meta.json', meta)
    await storage.delete(key)
    await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    assert (await storage.get_json(key))['binding_sha256'] is None
    events = []
    async def prepare(space_id):
        events.append('prepare')
        assert not long.submissions
        meta['graph_memory'] = {'url': 'http://configured-later:8002/mcp',
            'token': EMBEDDED_TOKEN_SENTINEL, 'memory_id': derive_memory_id(SPACE), 'ontology': 'general'}
        await storage.put_json(SPACE + '/_meta.json', meta)
        return {'status': 'ok'}
    long.prepare_ingest = AsyncMock(side_effect=prepare)
    original_read, original_submit = long.get_document, long.ingest_async
    async def read(*args, **kwargs):
        assert (await storage.get_json(key))['binding_sha256'] == mid_archive.space_identity(meta, SPACE, archive=True)[1]
        events.append('read')
        return await original_read(*args, **kwargs)
    async def submit(*args, **kwargs):
        assert (await storage.get_json(key))['binding_sha256'] == mid_archive.space_identity(meta, SPACE, archive=True)[1]
        events.append('submit')
        return await original_submit(*args, **kwargs)
    long.get_document, long.ingest_async = read, submit
    long.lose_ack = True
    await worker.run_space(SPACE)
    assert events[:3] == ['prepare', 'read', 'submit']
    assert len(long.submissions) == 1 and await storage.get(key) is not None
    now[0] += 4000
    await worker.run_space(SPACE)
    assert len(long.submissions) == 1 and await storage.get(key) is None
    long.prepare_ingest.assert_awaited_once_with(SPACE)


@pytest.mark.asyncio
async def test_partial_capture_restart_only_submits_unfinished_source():
    storage, docs, key, long, now, worker = await setup_case()
    await storage.delete(key)
    await storage.put('_backups/' + PREIMAGE + '/bank/other.md', TEXT)
    docs.append({**docs[0], 'bank_path': 'other.md'})
    await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    original = long.ingest_async
    async def fail_second_once(space_id, **kwargs):
        if len(long.submissions) == 1:
            long.ingest_async = original
            raise ConnectionError('temporary')
        return await original(space_id, **kwargs)
    long.ingest_async = fail_second_once
    await worker.run_space(SPACE)
    assert len(long.submissions) == 1 and await storage.get(key) is not None
    now[0] += 4000
    restarted = MidArchiveProjector(storage=storage, long_engine=long,
        registry=EngineRegistry(storage=storage), locks=LockManager(), clock=lambda: now[0])
    await restarted.run_space(SPACE)
    assert len(long.submissions) == 2 and await storage.get(key) is None
    assert long.submissions[0][1][0]['source_path'] != long.submissions[1][1][0]['source_path']


@pytest.mark.asyncio
async def test_stale_inventory_after_delete_and_recreate_does_not_resurrect_work():
    storage, docs, key, long, now, worker = await setup_case()
    original = storage.list_objects
    async def deleted_after_listing(prefix, *args, **kwargs):
        result = await original(prefix, *args, **kwargs)
        await storage.delete(key)
        meta = await storage.get_json(SPACE + '/_meta.json')
        meta['created_at'] = '2026-09-17T09:00:00+00:00'
        await storage.put_json(SPACE + '/_meta.json', meta)
        return result
    storage.list_objects = deleted_after_listing
    await worker.run_space(SPACE)
    assert not long.submissions and await storage.get(key) is None


@pytest.mark.asyncio
async def test_lifecycle_lock_serializes_projection_with_space_deletion():
    storage, docs, key, long, now, worker = await setup_case()
    entered, release, deleted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = long.get_document
    async def paused_read(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    long.get_document = paused_read
    async def delete_space():
        async with worker.locks.space_lifecycle(SPACE):
            await storage.delete(key)
            await storage.delete(SPACE + '/_meta.json')
            deleted.set()
    projection = asyncio.create_task(worker.run_space(SPACE))
    await entered.wait()
    deletion = asyncio.create_task(delete_space())
    await asyncio.sleep(0)
    assert not deleted.is_set()
    release.set()
    await asyncio.gather(projection, deletion)
    assert deleted.is_set() and await storage.get(key) is None


@pytest.mark.asyncio
async def test_empty_archive_is_not_falsely_marked_projected():
    storage, docs, key, long, now, worker = await setup_case()
    await storage.delete(key)
    await storage.put('_backups/' + PREIMAGE + '/bank/nested/progress.md', '')
    docs[0].update(size_bytes=0, sha256=hashlib.sha256(b'').hexdigest())
    await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    await worker.run_space(SPACE)
    assert not long.submissions and await storage.get(key) is not None
    assert (await projection_status(storage, SPACE))['error'] == 'document_unverified'


@pytest.mark.asyncio
async def test_provider_error_is_not_mistaken_for_document_absence():
    storage, docs, key, long, now, worker = await setup_case()
    long.get_document = AsyncMock(return_value={'status': 'error', 'message': 'Memory not found'})
    await worker.run_space(SPACE)
    assert not long.submissions and await storage.get(key) is not None


@pytest.mark.asyncio
async def test_process_startup_is_singleton_and_shutdown_cancels_worker(monkeypatch):
    from live_mem.core import mid_archive_projection as module
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = []
    class WaitingWorker:
        async def run_once(self):
            calls.append('run')
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    monkeypatch.setattr(module, 'MidArchiveProjector', WaitingWorker)
    await module.stop_mid_archive_projector()
    try:
        await module.start_mid_archive_projector()
        task = module._projector_task
        await entered.wait()
        await module.start_mid_archive_projector()
        assert module._projector_task is task and calls == ['run']
    finally:
        await module.stop_mid_archive_projector()
    assert cancelled.is_set() and module._projector_task is None and task.done()


def test_capture_helper_has_no_graph_or_engine_import():
    from live_mem.core import mid_archive
    tree = ast.parse(Path(mid_archive.__file__).read_text())
    imported = [n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imported += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any(any(part in name for part in ('graph', 'engines', 'projection')) for name in imported)


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_stage', ['prepare', 'read', 'poll', 'submission', 'proof'])
async def test_long_timeout_releases_lifecycle_with_durable_pending(monkeypatch, blocked_stage):
    from live_mem.core import mid_archive_projection as module
    storage, docs, key, long, now, worker = await setup_case()
    long.complete_immediately = False
    if blocked_stage == 'prepare':
        meta = await storage.get_json(SPACE + '/_meta.json')
        meta.pop('graph_memory')
        await storage.put_json(SPACE + '/_meta.json', meta)
        await storage.delete(key)
        await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    if blocked_stage == 'poll':
        await worker.run_space(SPACE)
        now[0] += 60
    monkeypatch.setattr(module, '_LONG_TIMEOUT_SECONDS', 0.03, raising=False)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    original_read = long.get_document
    reads = 0
    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if blocked_stage == 'read' or (blocked_stage == 'proof' and reads == 2):
            return await blocked()
        return await original_read(*args, **kwargs)
    long.get_document = read
    if blocked_stage == 'prepare':
        long.prepare_ingest = blocked
    elif blocked_stage == 'poll':
        long.ingest_status = blocked
    elif blocked_stage == 'submission':
        long.ingest_async = blocked
    async def lifecycle_operation():
        async with worker.locks.space_lifecycle(SPACE):
            return await storage.get_json(key)
    projection = asyncio.create_task(worker.run_space(SPACE))
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        pending = await asyncio.wait_for(lifecycle_operation(), 0.5)
        await projection
        assert cancelled.is_set()
        assert pending['error'] == 'projection_unavailable' and pending['failures'] == 1
        assert pending['next_attempt_at'] == now[0] + 30
        assert await storage.get('_backups/' + PREIMAGE + '/bank/nested/progress.md') == TEXT
    finally:
        projection.cancel()
        await asyncio.gather(projection, return_exceptions=True)


@pytest.mark.asyncio
async def test_long_calls_share_one_monotonic_capture_deadline(monkeypatch):
    from live_mem.core import mid_archive_projection as module
    storage, docs, key, long, now, worker = await setup_case()
    ticks, reads = [0.0], []
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: ticks[0]))
    monkeypatch.setattr(module, '_LONG_TIMEOUT_SECONDS', 60.0, raising=False)
    original_read, original_submit = long.get_document, long.ingest_async
    async def read(*args, **kwargs):
        reads.append('read')
        ticks[0] += 20
        return await original_read(*args, **kwargs)
    async def submit(*args, **kwargs):
        ticks[0] += 45
        return await original_submit(*args, **kwargs)
    long.get_document, long.ingest_async = read, submit
    await worker.run_space(SPACE)
    pending = await storage.get_json(key)
    assert pending is not None and pending['error'] == 'projection_unavailable'
    assert reads == ['read']  # No fresh 60-second allowance for the proof call.
    assert len(long.submissions) == 1 and pending['jobs']


@pytest.mark.asyncio
async def test_long_deadline_never_cancels_pending_storage_write(monkeypatch):
    from live_mem.core import mid_archive_projection as module
    storage, docs, key, long, now, worker = await setup_case()
    monkeypatch.setattr(module, '_LONG_TIMEOUT_SECONDS', 0.02, raising=False)
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_put = storage.put_json
    calls = []
    async def slow_put(target, record):
        if target == key and not calls:
            calls.append(target)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        await original_put(target, record)
    storage.put_json = slow_put
    projection = asyncio.create_task(worker.run_space(SPACE))
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        await asyncio.sleep(0.04)
        assert not cancelled.is_set() and not projection.done()
        release.set()
        await asyncio.wait_for(projection, 0.5)
        pending = await storage.get_json(key)
        assert pending is not None and pending['error'] == 'projection_unavailable'
        assert not cancelled.is_set()
    finally:
        release.set()
        projection.cancel()
        await asyncio.gather(projection, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_does_not_recompute_passive_status():
    storage, docs, key, long, now, worker = await setup_case()
    await storage.delete(key)
    original = storage.list_objects
    storage.list_objects = AsyncMock(wraps=original)
    await worker.run_space(SPACE)
    storage.list_objects.assert_awaited_once_with(SPACE + '/_mid_archive_pending/')


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_record', [False, True])
async def test_capture_deadline_resumes_after_durable_source_acknowledgements(monkeypatch, legacy_record):
    from collections import Counter
    from live_mem.core import mid_archive_projection as module
    storage, docs, key, long, now, worker = await setup_case()
    await storage.delete(key)
    for index in range(1, 5):
        name = f'next-{index}.md'
        await storage.put('_backups/' + PREIMAGE + '/bank/' + name, TEXT)
        docs.append({**docs[0], 'bank_path': name})
    await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    if legacy_record:
        record = await storage.get_json(key)
        record.pop('completed', None)
        await storage.put_json(key, record)
    ticks, reads, acknowledged = [0.0], Counter(), []
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: ticks[0]))
    original_read, original_submit = long.get_document, long.ingest_async
    async def read(space_id, *, source_path):
        ticks[0] += 10
        reads[source_path] += 1
        return await original_read(space_id, source_path=source_path)
    async def submit(*args, **kwargs):
        ticks[0] += 10
        return await original_submit(*args, **kwargs)
    long.get_document, long.ingest_async = read, submit
    for _ in range(3):
        # A fresh worker demonstrates that acknowledgements are persisted.
        restarted = MidArchiveProjector(storage, long, worker.registry, worker.locks, clock=lambda: now[0])
        await restarted.run_space(SPACE)
        pending = await storage.get_json(key)
        acknowledged.append(len(pending.get('completed', [])) if pending else None)
        now[0] += 4000
    assert await storage.get(key) is None
    assert acknowledged == [2, 4, None]
    assert len(long.submissions) == 5 and sorted(reads.values()) == [2] * 5
    for document in docs:
        assert await storage.get('_backups/' + PREIMAGE + '/bank/' + document['bank_path']) == TEXT


@pytest.mark.asyncio
async def test_existing_document_proof_is_acknowledged_once_across_capture_deadlines(monkeypatch):
    from collections import Counter
    from live_mem.core import mid_archive, mid_archive_projection as module
    storage, docs, key, long, now, worker = await setup_case()
    await storage.delete(key)
    for index in range(1, 5):
        name = f'next-{index}.md'
        await storage.put('_backups/' + PREIMAGE + '/bank/' + name, TEXT)
        docs.append({**docs[0], 'bank_path': name})
    await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    record = await storage.get_json(key)
    for document in docs:
        path = mid_archive.source_path(record, document)
        long.documents[SPACE, path] = {'source_path': path, 'sha256': document['sha256'],
                                       'ingestion_status': 'succeeded'}
    ticks, reads = [0.0], Counter()
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: ticks[0]))
    original_read = long.get_document
    async def read(space_id, *, source_path):
        ticks[0] += 20
        reads[source_path] += 1
        return await original_read(space_id, source_path=source_path)
    long.get_document = read
    for _ in range(2):
        await worker.run_space(SPACE)
        now[0] += 4000
    assert await storage.get(key) is None
    assert not long.submissions and sorted(reads.values()) == [1] * 5


@pytest.mark.asyncio
@pytest.mark.parametrize('completed', ['not-a-list', ['foreign/source.md'], [None]])
async def test_invalid_completed_sources_cannot_bypass_projection(completed):
    storage, docs, key, long, now, worker = await setup_case()
    record = await storage.get_json(key)
    record['completed'] = completed
    await storage.put_json(key, record)
    await worker.run_space(SPACE)
    assert await storage.get(key) is not None and not long.submissions
    assert (await projection_status(storage, SPACE))['error'] == 'invalid_record'


@pytest.mark.asyncio
@pytest.mark.parametrize('repaired_state', ['binding', 'missing_document'])
async def test_refused_capture_resumes_after_origin_or_document_is_repaired(repaired_state):
    storage, docs, key, long, now, worker = await setup_case()
    if repaired_state == 'binding':
        original_meta = await storage.get_json(SPACE + '/_meta.json')
        changed = {**original_meta, 'graph_memory': {**original_meta['graph_memory'], 'memory_id': 'changed'}}
        await storage.put_json(SPACE + '/_meta.json', changed)
        await worker.run_space(SPACE)
        assert not long.submissions and (await storage.get_json(key))['error'] == 'identity_changed'
        await storage.put_json(SPACE + '/_meta.json', original_meta)
    else:
        long.complete_immediately = False
        await worker.run_space(SPACE)
        long.jobs['ing_1']['status'] = 'succeeded'
        now[0] += 60
        await worker.run_space(SPACE)
        assert len(long.submissions) == 1
        assert (await storage.get_json(key))['error'] == 'document_unverified'
        document = long.submissions[0][1][0]
        long.documents[SPACE, document['source_path']] = {
            'source_path': document['source_path'], 'sha256': document['sha256'],
            'ingestion_status': 'succeeded'}
    assert await storage.get(key) is not None
    now[0] += 4000
    await worker.run_space(SPACE)
    assert await storage.get(key) is None and len(long.submissions) == 1
    assert await storage.get('_backups/' + PREIMAGE + '/bank/nested/progress.md') == TEXT



@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['error', 'missing_binding', 'recreated'])
async def test_unassigned_capture_is_not_pinned_after_failed_preparation(failure):
    storage, docs, key, long, now, worker = await setup_case()
    meta = await storage.get_json(SPACE + '/_meta.json')
    meta.pop('graph_memory')
    await storage.put_json(SPACE + '/_meta.json', meta)
    await storage.delete(key)
    await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    assert (await storage.get_json(key))['binding_sha256'] is None
    async def prepare(space_id):
        if failure == 'recreated':
            await storage.put_json(SPACE + '/_meta.json', {**meta, 'created_at': '2026-09-17T00:00:00+00:00'})
        return {'status': 'error' if failure == 'error' else 'ok'}
    long.prepare_ingest = AsyncMock(side_effect=prepare)
    await worker.run_space(SPACE)
    record = await storage.get_json(key)
    assert record['binding_sha256'] is None and record['error']
    assert not long.submissions


@pytest.mark.asyncio
async def test_capture_without_long_can_activate_later_and_reject_rebinding_after_lost_ack(monkeypatch):
    from live_mem.core import mid_archive
    from live_mem.config import get_settings
    storage, docs, key, long, now, worker = await setup_case()
    meta = await storage.get_json(SPACE + '/_meta.json')
    destination = meta.pop('graph_memory')
    monkeypatch.setattr(get_settings(), 'long_embedded_url', '')
    await storage.put_json(SPACE + '/_meta.json', meta)
    await storage.delete(key)
    await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    assert (await storage.get_json(key))['binding_sha256'] is None
    long.prepare_ingest = AsyncMock(return_value={'status': 'error'})
    await worker.run_space(SPACE)
    assert not long.submissions and (await storage.get_json(key))['binding_sha256'] is None
    monkeypatch.setattr(get_settings(), 'long_embedded_url', 'http://now-enabled:8002/')
    async def prepare(space_id):
        await storage.put_json(SPACE + '/_meta.json', {**meta, 'graph_memory': destination})
        return {'status': 'ok'}
    long.prepare_ingest = AsyncMock(side_effect=prepare)
    long.lose_ack = True
    now[0] += 4000
    await worker.run_space(SPACE)
    assert len(long.submissions) == 1
    pinned = (await storage.get_json(key))['binding_sha256']
    assert pinned == mid_archive.space_identity({**meta, 'graph_memory': destination}, SPACE, archive=True)[1]
    await storage.put_json(SPACE + '/_meta.json', {**meta, 'graph_memory': {**destination, 'memory_id': 'elsewhere'}})
    now[0] += 4000
    await worker.run_space(SPACE)
    assert len(long.submissions) == 1
    assert (await storage.get_json(key))['binding_sha256'] == pinned
    assert (await storage.get_json(key))['error'] == 'identity_changed'
    await storage.put_json(SPACE + '/_meta.json', {**meta, 'graph_memory': destination})
    now[0] += 4000
    await worker.run_space(SPACE)
    assert await storage.get(key) is None and len(long.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://example.org', 'https://example.org/',
                                  'https://example.org/sse/', ' HTTPS://EXAMPLE.ORG/mcp/ '])
async def test_cosmetic_persisted_url_changes_do_not_block_projection(url):
    storage, docs, key, long, now, worker = await setup_case()
    meta = await storage.get_json(SPACE + '/_meta.json')
    meta['graph_memory']['url'] = url
    await storage.put_json(SPACE + '/_meta.json', meta)
    await worker.run_space(SPACE)
    assert await storage.get(key) is None and len(long.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('original_url,changed_url', [
    ('https://example.org/TenantA/mcp', 'https://example.org/tenanta/mcp'),
    ('https://example.org/mcp?tenant=A', 'https://example.org/mcp?tenant=a'),
    ('https://User:PaSS@example.org/mcp', 'https://User:pass@example.org/mcp'),
])
async def test_case_sensitive_destination_changes_are_refused_before_send(original_url, changed_url):
    storage, docs, key, long, now, worker = await setup_case()
    meta = await storage.get_json(SPACE + '/_meta.json')
    meta['graph_memory']['url'] = original_url
    await storage.put_json(SPACE + '/_meta.json', meta)
    await storage.delete(key)
    await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    meta['graph_memory']['url'] = changed_url
    await storage.put_json(SPACE + '/_meta.json', meta)
    await worker.run_space(SPACE)
    assert not long.submissions
    assert (await storage.get_json(key))['error'] == 'identity_changed'
