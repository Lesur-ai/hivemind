"""Regression proofs for incident #680; synthetic content only."""
from copy import deepcopy
import hashlib
import json

import pytest

from live_mem.core.mid_archive import projection_status, source_path
from tests.test_mid_archive_projection import SPACE, PREIMAGE, TEXT, setup_case
from tests.test_mid_archive_auto_projection import auto_case
from tests.test_automatic_ontology_construction import Provider, Saved, catalogue, construct, document, result
from mcp_memory.core import ontology_construction as core


@pytest.mark.parametrize("automatic", [True, False])
async def test_admission_does_not_erase_failed_cycle_or_backoff(automatic):
    storage, docs, key, long, now, worker = await (auto_case() if automatic else setup_case())
    long.complete_immediately = False
    submissions = long.archive_submissions if automatic else long.submissions
    await worker.run_space(SPACE)
    for failure in range(1, 4):
        for job in long.jobs.values():
            job['status'] = 'failed'
        now[0] += 30
        await worker.run_space(SPACE)
        failed = await storage.get_json(key)
        assert failed['failures'] == failure
        assert failed['next_attempt_at'] == now[0] + 30 * 2 ** (failure - 1)
        count = len(submissions)
        await worker.run_space(SPACE)
        assert len(submissions) == count
        now[0] = failed['next_attempt_at']
        await worker.run_space(SPACE)
        admitted = await storage.get_json(key)
        assert admitted['failures'] == failure
        assert admitted['error'] == 'projection_unavailable'
        assert len(submissions) == count + 1
    assert await storage.get('_backups/' + PREIMAGE + '/bank/nested/progress.md') == TEXT


async def paused_case():
    storage, docs, key, long, now, worker = await auto_case()
    long.complete_immediately = False
    for failure in range(1, 4):
        await worker.run_space(SPACE)
        for job in long.jobs.values():
            job.update(status='failed', error='invalid_output', automatic_ontology={
                'rejection_reason': 'malformed_json', 'untrusted': 'PRIVATE SOURCE'})
        now[0] += 30
        await worker.run_space(SPACE)
        record = await storage.get_json(key)
        assert record['failures'] == failure
        now[0] = record['next_attempt_at']
    return storage, docs, key, long, now, worker


async def test_invalid_output_pauses_durably_and_resume_preserves_sources():
    storage, docs, key, long, now, worker = await paused_case()
    record = await storage.get_json(key)
    assert record['error'] == 'invalid_output'
    assert record['rejection_reason'] == 'malformed_json'
    for _ in range(4):
        now[0] += 7200
        await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 3
    assert await storage.get_json(key) == record
    status = await projection_status(storage, SPACE)
    assert status['blocked'] == 1 and status['next_attempt_at'] is None
    assert status['rejection_reason'] == 'malformed_json'
    assert 'PRIVATE SOURCE' not in json.dumps(status)
    before = deepcopy(record)
    reply = await worker.retry_capture(SPACE, PREIMAGE)
    assert reply['status'] == 'ok' and reply['resumed'] is True
    after = await storage.get_json(key)
    assert after['documents'] == before['documents'] and after['jobs'] == before['jobs']
    assert after['completed'] == before['completed'] and after['binding_sha256'] == before['binding_sha256']
    assert len(long.archive_submissions) == 3  # Resume schedules only; no LLM/Graph call.
    assert (await worker.retry_capture(SPACE, PREIMAGE))['resumed'] is False
    long.complete_immediately = True
    await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 4 and await storage.get(key) is None
    assert await storage.get('_backups/' + PREIMAGE + '/bank/nested/progress.md') == TEXT


@pytest.mark.parametrize('tamper', ['source', 'identity', 'binding', 'space_id', 'route'])
async def test_resume_refuses_changed_authority_or_source(tamper, monkeypatch):
    storage, docs, key, long, now, worker = await paused_case()
    original = await storage.get_json(key)
    target = SPACE
    if tamper == 'source':
        await storage.put('_backups/' + PREIMAGE + '/bank/nested/progress.md', 'altered')
    elif tamper == 'space_id':
        target = 'another-space'
    elif tamper == 'route':
        from unittest.mock import AsyncMock
        worker.registry.resolve_sink = AsyncMock(return_value=object())
    else:
        meta = await storage.get_json(SPACE + '/_meta.json')
        if tamper == 'identity':
            meta['created_at'] = '2026-09-17T09:00:00+00:00'
        else:
            meta['graph_memory']['memory_id'] = 'different-memory'
        await storage.put_json(SPACE + '/_meta.json', meta)
    with pytest.raises(ValueError):
        await worker.retry_capture(target, PREIMAGE)
    assert await storage.get_json(key) == original
    assert len(long.archive_submissions) == 3


async def test_reference_transport_rehydrates_owned_quote_before_checkpoint():
    docs, state, saved, packets = [document('one.md', 'Alice knows Bob.')], {}, Saved(), []
    provider = Provider()
    async def references(messages):
        packet = json.loads(messages[1].content)
        packets.append(packet)
        if 'passages' not in packet['data']:
            return await provider(messages)
        evidence_schema = packet['schema']['properties']['assertions']['items']['properties']['evidence']['items']
        assert evidence_schema['required'] == ['passage_id']
        assert 'quote' not in evidence_schema['properties']
        return result({'assertions': [{'subject': 'Alice', 'predicate': 'knows', 'object': 'Bob',
            'polarity': 'affirmed', 'condition': None, 'evidence': [{'passage_id': packet['data']['passages'][0]['passage_id']}]}]})
    await construct(docs, references, state, saved)
    extraction = next(v for v in state['calls'].values() if 'assertions' in v['payload'])
    assert extraction['payload']['assertions'][0]['evidence'][0]['quote'] == docs[0]['text']
    assert extraction['payload_sha256'] == core._digest(extraction['payload'])
    assert state['extraction_transport_sha256']


@pytest.mark.parametrize('kind', ['malformed_json', 'schema_failure', 'evidence_failure'])
async def test_rejection_category_is_fixed_and_persisted_without_response(kind, monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr(core.asyncio, 'sleep', no_wait)
    state = {}
    async def rejected(messages):
        packet = json.loads(messages[1].content)
        if kind == 'malformed_json':
            return result(None).__class__(text='{"PRIVATE SOURCE":', configured_model='synthetic', model_evidence='configured_only', finish_reason='stop')
        if kind == 'schema_failure':
            return result({'assertions': 'PRIVATE SOURCE'})
        return result({'assertions': [{'subject': 'Alice', 'predicate': 'knows', 'object': 'Bob', 'polarity': 'affirmed', 'condition': None, 'evidence': [{'passage_id': 'PRIVATE SOURCE'}]}]})
    with pytest.raises(core.OntologyConstructionError, match='^invalid_output$') as failure:
        await construct([document('one.md', 'Alice knows Bob.')], rejected, state)
    assert failure.value.rejection_reason == kind
    assert [r['rejection_reason'] for r in state['attempts']] == [kind, kind]
    assert not state['calls'] and 'PRIVATE SOURCE' not in json.dumps(state)


async def test_sibling_job_failures_count_one_construction_cycle():
    storage, docs, key, long, now, worker = await auto_case()
    long.complete_immediately = False
    await worker.run_space(SPACE)
    jobs = list(long.jobs.values())
    jobs[0].update(status='failed', error='invalid_output')
    now[0] += 30
    await worker.run_space(SPACE)
    assert (await storage.get_json(key))['failures'] == 0
    assert len(long.archive_submissions) == 1
    jobs[1].update(status='failed', error='invalid_output')
    now[0] += 30
    await worker.run_space(SPACE)
    record = await storage.get_json(key)
    assert record['failures'] == record['invalid_output_failures'] == 1
    assert len(long.archive_submissions) == 1


async def test_legacy_checkpoint_reuses_all_admissions_without_rekeying():
    docs = [document(f'{i}.md', f'Actor {i} knows Bob.') for i in range(14)]
    manifest, d1, d2 = core._surface(docs)
    selection = {phase: [{k: deepcopy(v) for k,v in p.items() if k != 'text'} for p in rows]
                 for phase, rows in [('d1', d1), ('d2', d2)]}
    binding = core._digest([core.METHOD, manifest, 100_000, core.SYSTEM, core.INDUCE,
        core.REPLACE, core.EXTRACT, core.CORRECTION, core.RUNTIME_RELATION_POLICY,
        core._CATALOGUE_SCHEMA, core._EXTRACTION_SCHEMA, selection])
    state = {'method': core.METHOD, 'input_sha256': binding, 'calls': {}, 'splits': [], 'attempts': []}
    for source in (d1+d2)[:9]:
        units = core._passage_units(source)
        data = {'source_id': source['id'], 'passages': units}
        key = core._digest([{'role':m.role, 'content':m.content} for m in core._messages(core.EXTRACT, data, core._EXTRACTION_SCHEMA)])
        payload = {'assertions':[{'subject':'Alice', 'predicate':'knows', 'object':'Bob',
            'polarity':'affirmed', 'condition':None, 'evidence':[{'passage_id':p['passage_id'], 'quote':p['text']}]} for p in units]}
        state['calls'][key] = {'payload':payload, 'payload_sha256':core._digest(payload)}
    prior = deepcopy(state['calls'])
    provider = Provider()
    await construct(docs, provider, state)
    assert len(provider.requests) == 5  # 3 missing extractions + D1/D2 only.
    assert all(state['calls'][k] == v for k,v in prior.items())
    assert state['input_sha256'] == binding
    assert state['extraction_transport_sha256'] == core._digest([core.EXTRACT_REFERENCES, core._REFERENCE_SCHEMA])
    assert state['attempts'][0]['logical_call_sha256'] != state['attempts'][0]['request_sha256']


@pytest.mark.parametrize('change', ['source', 'budget', 'canonical', 'transport', 'framing', 'admission'])
async def test_changed_binding_refused_before_provider_or_checkpoint_write(change, monkeypatch):
    docs = [document('one.md', 'Alice knows Bob.')]
    state = {}
    await construct(docs, state=state)
    before = deepcopy(state)
    budget = 100_000
    if change == 'source':
        docs = [document('one.md', 'A changed claim.')]
    elif change == 'budget':
        budget += 1
    elif change == 'canonical':
        monkeypatch.setattr(core, 'EXTRACT', core.EXTRACT+'Changed.')
    elif change == 'transport':
        monkeypatch.setattr(core, 'EXTRACT_REFERENCES', core.EXTRACT_REFERENCES+'Changed.')
    elif change == 'framing':
        monkeypatch.setattr(core, 'OUTPUT_FRAMING', core.OUTPUT_FRAMING+'Changed.')
    else:
        state.pop('extraction_transport_sha256')  # Legacy migration must not bless corruption.
        next(iter(state['calls'].values()))['payload']['assertions'][0]['subject'] = 'Changed'
        before = deepcopy(state)
    async def forbidden(*_):
        raise AssertionError('refused checkpoint reached provider/storage')
    with pytest.raises(core.OntologyConstructionError, match='checkpoint'):
        await core.construct_ontology(docs, checkpoint=state, complete=forbidden, save_checkpoint=forbidden,
                                      input_budget_bytes=budget)
    assert state == before


@pytest.mark.parametrize('change', ['unknown', 'duplicate', 'order', 'missing_id', 'empty_id', 'quote', 'empty_evidence', 'polarity', 'extra', 'assertions_type'])
def test_reference_guards_reject_before_attaching_quotes(change):
    units=[{'passage_id':'p1', 'text':'First claim.', 'text_start':0},
           {'passage_id':'p2', 'text':'Second claim.', 'text_start':13}]
    payload={'assertions':[{'subject':'Alice', 'predicate':'knows', 'object':'Bob', 'polarity':'affirmed',
        'condition':None, 'evidence':[{'passage_id':'p1'}]}]}
    row=payload['assertions'][0]
    if change == 'unknown': row['evidence'][0]['passage_id']='unknown'
    elif change == 'duplicate': row['evidence']*=2
    elif change == 'order': row['evidence']=[{'passage_id':'p2'},{'passage_id':'p1'}]
    elif change == 'missing_id': row['evidence'][0].clear()
    elif change == 'empty_id': row['evidence'][0]['passage_id']=''
    elif change == 'quote': row['evidence'][0]['quote']='PRIVATE SOURCE'
    elif change == 'empty_evidence': row['evidence']=[]
    elif change == 'polarity': row['polarity']='unknown'
    elif change == 'extra': row['fabricated']='PRIVATE SOURCE'
    else: payload['assertions']='PRIVATE SOURCE'
    before=deepcopy(payload)
    with pytest.raises(core.OntologyConstructionError): core._restore_references(payload, units)
    assert payload == before


@pytest.mark.parametrize('permissions,spaces', [(['read'],[SPACE]), (['write'],[SPACE]), (['manage'],['different-space'])])
async def test_retry_tool_checks_scope_and_manage_before_any_storage(permissions, spaces, monkeypatch):
    from unittest.mock import Mock
    from tests.test_mcp_tool_surface import _build, _token
    from live_mem.auth.context import current_token_info
    from live_mem.core import mid_archive_projection
    created = Mock(side_effect=AssertionError('unauthorized request reached projector'))
    monkeypatch.setattr(mid_archive_projection, 'MidArchiveProjector', created)
    mcp, _ = _build()
    token = current_token_info.set(_token('synthetic-user', permissions, spaces))
    try:
        reply = await mcp._tool_manager._tools['mid_archive_retry'].fn(space_id=SPACE, preimage_id=PREIMAGE)
    finally:
        current_token_info.reset(token)
    assert reply['status'] == 'error'
    created.assert_not_called()


async def test_retry_tool_returns_only_safe_code_when_storage_fails(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tests.test_mcp_tool_surface import _build, _token
    from live_mem.auth.context import current_token_info
    from live_mem.core import mid_archive_projection
    worker=SimpleNamespace(retry_capture=AsyncMock(side_effect=RuntimeError('PRIVATE SOURCE token=secret')))
    monkeypatch.setattr(mid_archive_projection,'MidArchiveProjector',lambda: worker)
    mcp,_ = _build()
    token=current_token_info.set(_token('synthetic-user',['manage'],[SPACE]))
    try:
        reply=await mcp._tool_manager._tools['mid_archive_retry'].fn(space_id=SPACE,preimage_id=PREIMAGE)
    finally:
        current_token_info.reset(token)
    assert reply['status']=='error' and reply['failure_reason']=='projection_unavailable'
    assert 'PRIVATE SOURCE' not in json.dumps(reply) and 'secret' not in json.dumps(reply)


async def test_framing_mismatch_cannot_write_missing_transport_upgrade():
    docs=[document("one.md", "Alice knows Bob.")]
    state={}
    await construct(docs,state=state)
    state.pop("extraction_transport_sha256")
    state["output_framing_sha256"]="0"*64
    before=deepcopy(state)
    async def forbidden(*_):
        raise AssertionError("mismatched framing reached provider/storage")
    with pytest.raises(core.OntologyConstructionError,match="checkpoint_binding_mismatch"):
        await core.construct_ontology(docs,checkpoint=state,complete=forbidden,save_checkpoint=forbidden,input_budget_bytes=100_000)
    assert state==before


@pytest.mark.parametrize("code", ["inference_timeout", "inference_rate_limited", "inference_unavailable"])
async def test_transport_job_category_survives_status_with_backoff(code):
    storage,docs,key,long,now,worker=await auto_case()
    long.complete_immediately=False
    await worker.run_space(SPACE)
    for job in long.jobs.values():job.update(status="failed",error=code)
    now[0]+=30
    await worker.run_space(SPACE)
    record=await storage.get_json(key)
    assert record["error"]==code and record["failures"]==1
    assert record["next_attempt_at"]==now[0]+30
    assert record.get("invalid_output_failures",0)==0
    status=await projection_status(storage,SPACE)
    assert status["error"]==code and status["blocked"]==0


async def test_retry_raising_route_resolver_returns_route_refused_without_changes():
    from unittest.mock import AsyncMock
    storage,docs,key,long,now,worker=await paused_case()
    before=await storage.get_json(key)
    worker.registry.resolve_sink=AsyncMock(side_effect=RuntimeError("PRIVATE SOURCE token=secret"))
    with pytest.raises(ValueError,match="^route_refused$"):
        await worker.retry_capture(SPACE,PREIMAGE)
    assert await storage.get_json(key)==before
    assert len(long.archive_submissions)==3


@pytest.mark.parametrize('codes,paused', [
    (('inference_timeout',) * 3, True),
    (('inference_rate_limited',) * 3, False),
    (('inference_unavailable',) * 3, False),
    (('inference_unavailable', 'inference_rate_limited', 'inference_timeout'), True),
    (('inference_timeout', 'inference_timeout', 'inference_unavailable'), False),
])
async def test_three_failed_transport_cycles_pause_only_latest_timeout(codes, paused):
    storage, docs, key, long, now, worker = await auto_case()
    long.complete_immediately = False
    for cycle, code in enumerate(codes, 1):
        await worker.run_space(SPACE)
        for job in long.jobs.values():
            job.update(status='failed', error=code)
        now[0] += 30
        await worker.run_space(SPACE)
        record = await storage.get_json(key)
        assert record['failures'] == cycle
        now[0] = record['next_attempt_at']
    status = await projection_status(storage, SPACE)
    assert status['error'] == code
    assert status['blocked'] == int(paused)
    if not paused:
        await worker.run_space(SPACE)
        assert len(long.archive_submissions) == 4
        return
    before = deepcopy(record)
    # Restart with the same durable intent; polling cannot reopen a paused capture.
    worker = type(worker)(storage=storage, long_engine=long, registry=worker.registry,
                          locks=worker.locks, clock=lambda: now[0])
    for _ in range(3):
        now[0] += 7200
        await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 3
    assert await storage.get_json(key) == before
    assert status['next_attempt_at'] is None
    assert await storage.get('_backups/' + PREIMAGE + '/bank/nested/progress.md') == TEXT
    reply = await worker.retry_capture(SPACE, PREIMAGE)
    assert reply['resumed'] is True and len(long.archive_submissions) == 3
    after = await storage.get_json(key)
    for field in ['documents', 'jobs', 'completed', 'binding_sha256', 'preimage_id']:
        assert after[field] == before[field]
    assert after['failures'] == 0 and after['error'] is None
    long.complete_immediately = True
    await worker.run_space(SPACE)
    assert len(long.archive_submissions) == 4 and await storage.get(key) is None
