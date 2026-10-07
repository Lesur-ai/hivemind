"""The first complete MID capture crafts once; documentary ingestion stays separate."""
import hashlib

import pytest

from live_mem.core.mid_archive import prepare_mid_archive, space_identity
from tests.test_mid_archive_projection import PREIMAGE, SPACE, TEXT, LongDouble, setup_case


class ArchiveLongDouble(LongDouble):
    def __init__(self):
        super().__init__()
        self.frozen = False
        self.archive_submissions = []
        self.prepare_calls = []

    async def prepare_archive_ingest(self, space_id, *, preimage_id):
        self.prepare_calls.append(preimage_id)
        return {'status': 'ok', 'ontology_mode': 'frozen' if self.frozen else 'auto'}

    async def get_archive_document(self, space_id, *, source_path):
        return await super().get_document(space_id, source_path=source_path)

    async def archive_ingest_status(self, space_id, job_id):
        return await super().ingest_status(space_id, job_id)

    async def ingest_archive(self, space_id, *, documents, automatic):
        self.archive_submissions.append((documents, automatic))
        items = []
        for i, doc in enumerate(documents):
            jid = f'archive_{len(self.archive_submissions)}_{i}'
            self.jobs[jid] = {'job_id': jid, 'status': 'running',
                             'source_path': doc['source_path'], 'sha256': doc['sha256']}
            if self.complete_immediately:
                self.frozen = True
                self.documents[(space_id, doc['source_path'])] = {
                    'source_path': doc['source_path'], 'sha256': doc['sha256'],
                    'ingestion_status': 'succeeded'}
                self.jobs[jid]['status'] = 'succeeded'
            items.append(dict(self.jobs[jid]))
        if self.lose_ack:
            raise ConnectionError('lost archive acknowledgement')
        return {'status': 'ok', 'items': items}


async def auto_case():
    storage, docs, key, _, now, worker = await setup_case()
    await storage.delete(key)
    text = '# Decisions\nThe contract is renewed annually.'
    await storage.put('_backups/' + PREIMAGE + '/bank/decisions.md', text)
    docs.append({'bank_path': 'decisions.md', 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                 'size_bytes': len(text.encode())})
    key = await prepare_mid_archive(storage, space_id=SPACE, preimage_id=PREIMAGE, documents=docs)
    long = ArchiveLongDouble()
    worker.long_engine = long
    return storage, docs, key, long, now, worker


@pytest.mark.asyncio
async def test_first_capture_is_one_complete_auto_batch_not_first_file():
    storage, docs, key, long, now, worker = await auto_case()
    assert (await storage.get_json(key))['version'] == 2
    await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 1
    sent, automatic = long.archive_submissions[0]
    assert automatic is True
    assert len(sent) == 2
    assert {d['filename'] for d in sent} == {'progress.md', 'decisions.md'}
    assert not long.submissions  # Never the documentary/default destination.
    assert await storage.get(key) is None
    assert await storage.get('_backups/' + PREIMAGE + '/bank/nested/progress.md') == TEXT


@pytest.mark.asyncio
async def test_partial_failed_bootstrap_resubmits_identical_full_capture():
    storage, docs, key, long, now, worker = await auto_case()
    long.complete_immediately = False
    await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 1
    now[0] += 30
    await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 1  # Running bootstrap not duplicated.
    for job in long.jobs.values():
        job['status'] = 'failed'
    now[0] += 30
    await worker.run_space(SPACE)
    assert (await storage.get_json(key))['failures'] == 1
    long.jobs.clear()  # Process restart; durable constructor checkpoint remains server-side.
    long.complete_immediately = True
    now[0] += 4000
    await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 2
    assert long.archive_submissions[0] == long.archive_submissions[1]
    assert await storage.get(key) is None


@pytest.mark.asyncio
async def test_next_capture_reuses_frozen_ontology_without_crafting():
    storage, docs, key, long, now, worker = await auto_case()
    await worker.run_space(SPACE)
    assert long.frozen
    later = SPACE + '/2026-09-17T10-00-00-' + 'b' * 32
    for doc in docs:
        text = await storage.get('_backups/' + PREIMAGE + '/bank/' + doc['bank_path'])
        await storage.put('_backups/' + later + '/bank/' + doc['bank_path'], text)
    later_key = await prepare_mid_archive(storage, space_id=SPACE, preimage_id=later, documents=docs)
    await worker.run_space(SPACE)
    assert [auto for _, auto in long.archive_submissions] == [True, False, False]
    assert not long.submissions and await storage.get(later_key) is None


@pytest.mark.asyncio
async def test_lost_ack_never_recrafts_or_replays_persisted_archive_documents():
    storage, docs, key, long, now, worker = await auto_case()
    long.lose_ack = True
    await worker.run_space(SPACE)
    assert await storage.get(key) is not None
    now[0] += 4000
    await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 1 and await storage.get(key) is None


@pytest.mark.asyncio
async def test_invalid_member_prevents_entire_initial_batch_from_leaving_storage():
    storage, docs, key, long, now, worker = await auto_case()
    await storage.put('_backups/' + PREIMAGE + '/bank/decisions.md', 'altered')
    await worker.run_space(SPACE)
    assert not long.archive_submissions and not long.submissions
    assert (await storage.get_json(key))['error'] == 'archive_unverified'


@pytest.mark.asyncio
async def test_existing_v1_record_keeps_its_original_documentary_target():
    storage, docs, key, long, now, worker = await auto_case()
    record = await storage.get_json(key)
    record['version'] = 1
    record['binding_sha256'] = space_identity(await storage.get_json(SPACE + '/_meta.json'), SPACE)[1]
    await storage.put_json(key, record)
    await worker.run_space(SPACE)
    assert not long.prepare_calls and not long.archive_submissions
    assert len(long.submissions) == 2 and await storage.get(key) is None


@pytest.mark.asyncio
async def test_documentary_ontology_selection_does_not_retarget_new_archives():
    storage, docs, key, long, now, worker = await auto_case()
    meta = await storage.get_json(SPACE + '/_meta.json')
    meta['graph_memory']['ontology'] = 'legal'
    await storage.put_json(SPACE + '/_meta.json', meta)
    await worker.run_space(SPACE)
    assert await storage.get(key) is None and len(long.archive_submissions) == 1


@pytest.mark.asyncio
async def test_first_submission_does_not_wait_for_absent_document_reads(monkeypatch):
    storage, docs, key, long, now, worker = await auto_case()
    read = long.get_archive_document
    async def after_admission(*args, **kwargs):
        assert long.archive_submissions, 'initial absent-document reads can starve the first batch'
        return await read(*args, **kwargs)
    monkeypatch.setattr(long, 'get_archive_document', after_admission)
    await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 1 and await storage.get(key) is None


@pytest.mark.asyncio
async def test_running_bootstrap_does_not_reload_capture_bytes(monkeypatch):
    from live_mem.core import mid_archive_projection
    storage, docs, key, long, now, worker = await auto_case()
    long.complete_immediately = False
    await worker.run_space(SPACE)
    async def unexpected_reload(*args, **kwargs):
        raise AssertionError('running bootstrap must not reload or re-encode retained sources')
    monkeypatch.setattr(mid_archive_projection, 'archive_bytes', unexpected_reload)
    now[0] += 30
    await worker.run_space(SPACE)
    pending = await storage.get_json(key)
    assert pending['failures'] == 0 and pending['error'] is None
    assert len(long.archive_submissions) == 1
