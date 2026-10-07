"""Common LONG reads retain independently typed archive/documentary evidence."""
from copy import deepcopy

import pytest

from tests.test_mid_archive_bridge import archive_case, local_archive_case  # noqa: F401


@pytest.fixture
async def retrieval_case(archive_case):
    c = archive_case
    await c.engine.prepare_archive_ingest('space-a', preimage_id='capture-1')
    meta = await c.storage.get_json('space-a/_meta.json')
    c.archive_id = (await c.storage.get_json(c.bridge._archive_binding_key('space-a', meta)))['memory_id']
    c.remote[c.archive_id]['ontology'] = 'auto_0123456789abcdef'
    previous = c.client.call_tool.side_effect
    c.read_calls = []
    c.rows = {
        'documentary': [{'id': f'doc-{i}', 'source_path': f'docs/{i}', 'content': f'document {i}'} for i in range(3)],
        c.archive_id: [{'id': f'archive-{i}', 'source_path': f'archive/{i}', 'content': f'archived {i}'} for i in range(3)],
    }
    c.error = None
    async def read(name, args):
        if name == 'memory_list':
            return await previous(name, args)
        c.read_calls.append((name, deepcopy(args)))
        mid = args['memory_id']
        if c.error and mid == c.archive_id:
            if isinstance(c.error, Exception):
                raise c.error
            return c.error
        if name == 'memory_search':
            return {'status': 'ok', 'memory_id': mid, 'result_count': 3,
                    'results': [{'entity': {'name': 'Same name', 'type': mid}, 'documents': [d]} for d in c.rows[mid]]}
        if name == 'memory_query':
            return {'status': 'ok', 'memory_id': mid, 'retrieval_mode': 'hybrid',
                    'entities': [{'name': 'Same name', 'type': mid} for _ in range(3)],
                    'rag_chunks': [{'doc_id': d['id'], 'text': d['content']} for d in c.rows[mid]],
                    'source_documents': c.rows[mid],
                    'stats': {'entities_found': 3, 'rag_chunks_retained': 3, 'rag_chunk_limit': 3,
                              'rag_chunks_filtered': 0}}
        if name == 'document_list':
            docs = c.rows[mid]
            off, lim = args.get('offset', 0), args.get('limit')
            return {'status': 'ok', 'total_count': len(docs), 'documents': docs[off:off+lim] if lim else docs}
        if name == 'document_get':
            docs = [d for d in c.rows[mid] if d['id'] == args.get('document_id') or d['source_path'] == args.get('source_path')]
            if not docs:
                target = args.get('document_id') or args.get('source_path')
                return {'status': 'error', 'message': f"Document '{target}' not found"}
            return {'status': 'ok', 'document': docs[0], 'content': docs[0]['content']}
        raise AssertionError(name)
    c.client.call_tool.side_effect = read
    return c


async def test_search_and_query_keep_both_catalogues_bounded_with_provenance(retrieval_case):
    c = retrieval_case
    result = await c.engine.search('space-a', 'Same name', limit=2)
    assert result['status'] == 'ok' and result['result_count'] == 2
    assert {r['memory_id'] for r in result['results']} == {'documentary', c.archive_id}
    assert {r['entity']['type'] for r in result['results']} == {'documentary', c.archive_id}
    result = await c.engine.query('space-a', 'Same name', limit=2)
    assert len(result['entities']) == result['stats']['entities_found'] == 2
    assert len(result['rag_chunks']) == result['stats']['rag_chunks_retained'] == 3
    assert {r['memory_id'] for r in result['entities']} == {'documentary', c.archive_id}
    assert all('memory_id' in r for r in result['source_documents'])
    assert set(result['memory_ids']) == {'documentary', c.archive_id}


@pytest.mark.parametrize('offset,expected', [(0, ['doc-0', 'doc-1']), (2, ['doc-2', 'archive-0']), (4, ['archive-1', 'archive-2']), (6, [])])
async def test_document_listing_paginates_across_namespaces(retrieval_case, offset, expected):
    c = retrieval_case
    result = await c.engine.list_documents('space-a', limit=2, offset=offset)
    assert result['status'] == 'ok' and result['total_count'] == 6
    assert [d['document_id'] for d in result['documents']] == expected
    assert result['count'] == len(expected)
    assert all('memory_id' in d for d in result['documents'])


@pytest.mark.parametrize('lookup', [{'document_id': 'archive-1'}, {'source_path': 'archive/1'}])
async def test_document_get_opens_archive_evidence(retrieval_case, lookup):
    c = retrieval_case
    result = await c.engine.get_document('space-a', include_content=True, **lookup)
    assert result['status'] == 'ok' and result['content'] == 'archived 1'
    assert result['document']['memory_id'] == c.archive_id


@pytest.mark.parametrize('method', ['search', 'query', 'list_documents'])
@pytest.mark.parametrize('transport_error', [False, True])
async def test_unavailable_archive_preserves_documentary_results_with_explicit_warning(retrieval_case, method, transport_error):
    c = retrieval_case
    c.error = ConnectionError('unreachable') if transport_error else {'status': 'error', 'message': 'archive unavailable'}
    args = ('space-a', 'query') if method != 'list_documents' else ('space-a',)
    result = await getattr(c.engine, method)(*args)
    assert result['status'] == 'ok' and result['partial'] is True
    assert result['warnings'] == [{'scope': 'mid_archive', 'reason': 'archive_unavailable',
                                    'message': 'Archive evidence is unavailable; documentary results only.'}]
    field = {'search': 'results', 'query': 'entities', 'list_documents': 'documents'}[method]
    assert len(result[field]) == 3


async def test_changed_archive_incarnation_is_rejected_before_query(retrieval_case):
    c = retrieval_case
    c.remote[c.archive_id]['created_at'] = 'different-incarnation'
    result = await c.engine.search('space-a', 'anything')
    assert result['status'] == 'ok' and result['partial'] is True
    assert result['warnings'][0]['reason'] == 'archive_destination_changed'
    assert not any(args['memory_id'] == c.archive_id for _, args in c.read_calls)


async def test_archive_lookup_unavailable_never_becomes_document_absence(retrieval_case):
    c = retrieval_case
    c.error = {'status': 'error', 'message': 'archive unavailable'}
    result = await c.engine.get_document('space-a', source_path='archive/1')
    assert result == c.error
    result = await c.engine.get_document('space-a', document_id='doc-1')
    assert result['status'] == 'ok'


@pytest.mark.parametrize('method', ['search', 'query', 'list_documents', 'get_document'])
async def test_local_archive_collision_never_delivers_archive_evidence(retrieval_case, monkeypatch, method):
    c = retrieval_case
    assert await local_archive_case(c, monkeypatch) == c.archive_id
    await c.storage.put(f'{c.archive_id}/_hivemind/node.json', 'historical authority')
    before = c.storage.snapshot()
    c.read_calls.clear()
    if method == 'get_document':
        result = await c.engine.get_document('space-a', source_path='archive/1', include_content=True)
        assert result['status'] == 'error' and result.get('recovery_required') is True
    else:
        args = ('space-a', 'query') if method != 'list_documents' else ('space-a',)
        result = await getattr(c.engine, method)(*args)
        assert result['status'] == 'ok' and result['partial'] is True
        field = {'search': 'results', 'query': 'entities', 'list_documents': 'documents'}[method]
        assert len(result[field]) == 3
    assert all(args['memory_id'] == 'documentary' for _, args in c.read_calls)
    assert c.storage.snapshot() == before
