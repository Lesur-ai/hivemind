"""One process-owned projector of retained MID captures, not MID recovery."""
import asyncio
import base64
import contextlib
import time

from .mid_archive import (ERRORS, archive_bytes, pending_prefix,
                          require, source_path, space_identity, validate_record)


_LONG_TIMEOUT_SECONDS = 60.0


class MidArchiveProjector:
    def __init__(self, storage=None, long_engine=None, registry=None, locks=None, clock=time.time):
        from .engines import get_engine_registry
        from .locks import get_lock_manager
        from .storage import get_storage
        self.storage = storage if storage is not None else get_storage()
        self.registry = registry if registry is not None else get_engine_registry()
        self.long_engine = long_engine if long_engine is not None else self.registry.long_engine()
        self.locks = locks if locks is not None else get_lock_manager()
        self.clock = clock
        self._run_lock = asyncio.Lock()

    async def run_once(self):
        """Discover only current live-space prefixes; never sweep old backups."""
        from ..config import get_settings
        if not get_settings().mid_auto_archive:
            return
        async with self._run_lock:
            for prefix in await self.storage.list_prefixes(''):
                space_id = prefix.rstrip('/')
                if not space_id or space_id.startswith('_'):
                    continue
                await self.run_space(space_id)

    async def run_space(self, space_id):
        from ..config import get_settings
        if not get_settings().mid_auto_archive:
            return
        try:
            items = await self.storage.list_objects(pending_prefix(space_id))
            for item in items:
                async with self.locks.space_lifecycle(space_id):
                    await self._project_record(space_id, item['Key'])
        except asyncio.CancelledError:
            raise
        except Exception:
            # Storage outage leaves all persisted work retryable next pass.
            return {'error': 'projection_unavailable'}

    async def _save(self, key, record):
        await self.storage.put_json(key, record)
        require(await self.storage.get_json(key) == record)

    async def _project_record(self, space_id, key):
        from .write_sink import DirectLocalWriteSink
        record = None
        try:
            # Both objects are re-read under the lifecycle lock. A stale list
            # cannot resurrect a record removed by delete/recreate.
            raw = await self.storage.get_json(key)
            if raw is None:
                return
            record = validate_record(raw, space_id, key)
            record.setdefault('completed', [])
            if self.clock() < record['next_attempt_at']:
                return
            meta = await self.storage.get_json(f'{space_id}/_meta.json')
            incarnation, binding = space_identity(meta, space_id, archive=record['version'] == 2)
            require(incarnation == record['space_created_at'], 'identity_changed')
            if record['binding_sha256'] is not None:
                require(binding == record['binding_sha256'], 'identity_changed')
            try:
                sink = await self.registry.resolve_sink(space_id)
                require(isinstance(sink, DirectLocalWriteSink), 'route_refused')
            except Exception:
                raise ValueError('route_refused') from None
            deadline = time.monotonic() + _LONG_TIMEOUT_SECONDS
            if record['binding_sha256'] is None:
                if binding is None:
                    prepared = await self._long_call(deadline, self.long_engine.prepare_ingest, space_id)
                    require(isinstance(prepared, dict) and prepared.get('status') == 'ok',
                            'projection_unavailable')
                meta = await self.storage.get_json(f'{space_id}/_meta.json')
                current_incarnation, current_binding = space_identity(meta, space_id, archive=record['version'] == 2)
                require(current_incarnation == record['space_created_at'] and current_binding is not None,
                        'identity_changed')
                require(binding is None or binding == current_binding, 'identity_changed')
                record['binding_sha256'] = current_binding
                # Pin and verify the actual destination BEFORE any document
                # can leave storage, including when a later acknowledgement is lost.
                await self._save(key, record)
            archive = record['version'] == 2
            if archive:
                prepared = await self._long_call(
                    deadline, self.long_engine.prepare_archive_ingest, space_id,
                    preimage_id=record['preimage_id'],
                )
                require(isinstance(prepared, dict) and prepared.get('status') == 'ok'
                        and prepared.get('ontology_mode') in ('auto', 'frozen'),
                        'projection_unavailable')
                if prepared['ontology_mode'] == 'auto':
                    complete = await self._bootstrap_capture(space_id, key, record, deadline)
                    if complete:
                        await self.storage.delete(key)
                    else:
                        record.update(error=None, failures=0, next_attempt_at=self.clock() + 30)
                        await self._save(key, record)
                    return
            complete = True
            for document in record['documents']:
                path = source_path(record, document)
                if path in record['completed']:
                    continue
                raw_bytes = await archive_bytes(self.storage, record, document)
                if await self._document_done(space_id, path, document['sha256'], deadline, archive=archive):
                    await self._acknowledge(key, record, path)
                    continue
                job_id = record['jobs'].get(path)
                if job_id:
                    job = await self._long_call(
                        deadline, self.long_engine.archive_ingest_status if archive else
                        self.long_engine.ingest_status, space_id, job_id)
                    require(isinstance(job, dict), 'projection_unavailable')
                    status = job.get('status')
                    if status in ('queued', 'running'):
                        require(job.get('source_path') == path and job.get('sha256') == document['sha256'],
                                'document_unverified')
                        complete = False
                        continue
                    if status not in ('not_found', 'failed', 'cancelled', 'succeeded', 'skipped'):
                        raise ValueError('projection_unavailable')
                    # A succeeded job without a succeeded persisted document is
                    # not proof, and must not trigger an immediate extraction loop.
                    if status in ('succeeded', 'skipped'):
                        raise ValueError('document_unverified')
                    record['jobs'].pop(path, None)
                    if status in ('failed', 'cancelled'):
                        raise ValueError('projection_unavailable')
                require(bool(raw_bytes), 'document_unverified')
                payload = self._payload(record, document, raw_bytes)
                if archive:
                    result = await self._long_call(deadline, self.long_engine.ingest_archive,
                                                   space_id, documents=[payload], automatic=False)
                else:
                    # Existing v1 captures keep the destination they were pinned to.
                    result = await self._long_call(deadline, self.long_engine.ingest_async,
                                                   space_id, documents=[payload])
                require(isinstance(result, dict) and result.get('status') == 'ok', 'projection_unavailable')
                responses = result.get('items')
                require(isinstance(responses, list) and len(responses) == 1, 'projection_unavailable')
                item = responses[0]
                require(isinstance(item, dict) and item.get('source_path') == path, 'document_unverified')
                require(item.get('status') in ('queued', 'running', 'succeeded', 'skipped'),
                        'projection_unavailable')
                if item.get('status') in ('queued', 'running'):
                    require(bool(item.get('job_id')), 'projection_unavailable')
                if item.get('job_id'):
                    jobs = {**record['jobs'], path: item['job_id']}
                    validate_record({**record, 'jobs': jobs}, space_id, key)
                    record['jobs'] = jobs
                await self._save(key, record)
                if await self._document_done(space_id, path, document['sha256'], deadline, archive=archive):
                    await self._acknowledge(key, record, path)
                else:
                    complete = False
            if complete:
                await self.storage.delete(key)
            else:
                record.update(error=None, failures=0, next_attempt_at=self.clock() + 30)
                await self._save(key, record)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if record is None:
                return  # Corrupt work is retained and reported by passive status.
            code = str(error) if isinstance(error, ValueError) and str(error) in ERRORS else 'projection_unavailable'
            record['failures'] += 1
            record.update(error=code, next_attempt_at=self.clock() + min(3600, 30 * 2 ** min(record['failures'] - 1, 7)))
            await self._save(key, record)

    @staticmethod
    def _payload(record, document, raw_bytes):
        return {
            'filename': document['bank_path'].rsplit('/', 1)[-1],
            'source_path': source_path(record, document), 'sha256': document['sha256'],
            'content_base64': base64.b64encode(raw_bytes).decode('ascii'),
            'metadata': {'provenance': 'mid_archive', 'preimage_id': record['preimage_id'],
                         'bank_path': document['bank_path'], 'captured_at': record['created_at']},
        }

    async def _bootstrap_capture(self, space_id, key, record, deadline):
        """Craft from the entire immutable capture, replaying the same fingerprint.

        Construction and provider retries belong to Graph's existing worker.
        Partial queue history never turns this into a different initial corpus.
        """
        # First admission goes directly to the idempotent Graph batch API.
        # Reading every absent document first can exhaust the pass indefinitely.
        # A lost ACK replays the identical batch; Graph owns its checkpoints.
        if record['jobs'] or record['completed']:
            complete, running, failed = True, False, False
            for document in record['documents']:
                path = source_path(record, document)
                if path in record['completed']:
                    continue
                if await self._document_done(space_id, path, document['sha256'], deadline, archive=True):
                    await self._acknowledge(key, record, path)
                    continue
                complete = False
                job_id = record['jobs'].get(path)
                if job_id:
                    job = await self._long_call(deadline, self.long_engine.archive_ingest_status,
                                               space_id, job_id)
                    require(isinstance(job, dict), 'projection_unavailable')
                    state = job.get('status')
                    if state in ('queued', 'running'):
                        require(job.get('source_path') == path and job.get('sha256') == document['sha256'],
                                'document_unverified')
                        running = True
                    elif state in ('failed', 'cancelled', 'not_found'):
                        record['jobs'].pop(path, None)
                        failed = failed or state != 'not_found'
                    else:
                        raise ValueError('document_unverified')
            if failed:
                raise ValueError('projection_unavailable')
            if complete or running:
                return complete
        payload = []
        for document in record['documents']:
            raw_bytes = await archive_bytes(self.storage, record, document)
            require(bool(raw_bytes), 'document_unverified')
            payload.append(self._payload(record, document, raw_bytes))
        result = await self._long_call(deadline, self.long_engine.ingest_archive,
                                       space_id, documents=payload, automatic=True)
        require(isinstance(result, dict) and result.get('status') == 'ok', 'projection_unavailable')
        items = result.get('items')
        expected = {doc['source_path'] for doc in payload}
        require(isinstance(items, list) and len(items) == len(payload)
                and all(isinstance(item, dict) for item in items)
                and {item.get('source_path') for item in items} == expected, 'document_unverified')
        jobs = dict(record['jobs'])
        for item in items:
            require(item.get('status') in ('queued', 'running', 'succeeded', 'skipped'),
                    'projection_unavailable')
            if item['status'] in ('queued', 'running'):
                require(bool(item.get('job_id')), 'projection_unavailable')
            if item.get('job_id'):
                jobs[item['source_path']] = item['job_id']
        validate_record({**record, 'jobs': jobs}, space_id, key)
        record['jobs'] = jobs
        await self._save(key, record)
        complete = True
        for document in record['documents']:
            path = source_path(record, document)
            if path in record['completed']:
                continue
            if await self._document_done(space_id, path, document['sha256'], deadline, archive=True):
                await self._acknowledge(key, record, path)
            else:
                complete = False
        return complete

    async def _acknowledge(self, key, record, path):
        # A persisted document proof needs no re-read on later bounded passes.
        record['completed'].append(path)
        record['jobs'].pop(path, None)
        record.update(error=None, failures=0)
        await self._save(key, record)

    async def _long_call(self, deadline, operation, *args, **kwargs):
        # One monotonic budget for all LONG calls of this capture. Storage
        # writes stay outside it. Bridge provisioning drains any local write
        # already begun before propagating cancellation, so that cleanup may
        # outlast the network budget while retaining the lifecycle lock.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('projection_unavailable')
        return await asyncio.wait_for(operation(*args, **kwargs), timeout=remaining)

    async def _document_done(self, space_id, path, sha, deadline, *, archive=False):
        operation = self.long_engine.get_archive_document if archive else self.long_engine.get_document
        response = await self._long_call(deadline, operation,
                                         space_id, source_path=path)
        require(isinstance(response, dict), 'projection_unavailable')
        # Existing Graph document_get has an exact untyped absence envelope,
        # pinned against the real server by test_long_document_catalog.
        # test_document_read_not_found_never_downloads (id and source_path).
        if (response.get('status') == 'not_found' or response ==
                {'status': 'error', 'message': f"Document '{path}' not found"}):
            return False
        require(response.get('status') == 'ok', 'projection_unavailable')
        doc = response.get('document')
        require(isinstance(doc, dict) and doc.get('source_path') == path and doc.get('sha256') == sha,
                'document_unverified')
        return doc.get('ingestion_status') == 'succeeded'


_projector_task = None


async def start_mid_archive_projector():
    """Idempotent process startup; never an MCP-session lifecycle hook."""
    global _projector_task
    if _projector_task is not None and not _projector_task.done():
        return
    worker = MidArchiveProjector()
    async def run():
        while True:
            try:
                await worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Passive storage status remains the diagnostic surface.
            await asyncio.sleep(30)
    _projector_task = asyncio.create_task(run(), name='mid-archive-projector')


async def stop_mid_archive_projector():
    global _projector_task
    task, _projector_task = _projector_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
