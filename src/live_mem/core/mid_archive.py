"""Storage-only records for immutable MID captures; never commit authority."""
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from .models import EMBEDDED_TOKEN_SENTINEL

PENDING = '_mid_archive_pending'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_CAPTURE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[a-f0-9]{32}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
ERRORS = {'projection_unavailable', 'invalid_record', 'identity_changed',
          'route_refused', 'archive_unverified', 'document_unverified'}


def require(condition, code='invalid_record'):
    if not condition:
        raise ValueError(code)


def pending_prefix(space_id):
    require(isinstance(space_id, str) and _ID.fullmatch(space_id))
    return f'{space_id}/{PENDING}/'


def capture_id(space_id, preimage_id):
    pending_prefix(space_id)
    require(isinstance(preimage_id, str) and preimage_id.startswith(space_id + '/'))
    capture = preimage_id[len(space_id) + 1:]
    require(_CAPTURE.fullmatch(capture))
    return capture


def source_path(record, document):
    return f".hivemind/mid-archive/{record['preimage_id']}/bank/{document['bank_path']}"


def space_identity(meta, space_id, *, archive=False):
    """Fingerprint an opaque destination, without validating LONG configuration.

    Capture accepts unavailable/malformed LONG configuration. The bridge owns
    its validation; this digest only detects changes before projection. Tokens
    and operational metadata are excluded, and only the digest is persisted.
    """
    require(isinstance(meta, dict) and meta.get('space_id') == space_id, 'identity_changed')
    created = meta.get('created_at')
    require(isinstance(created, str) and created, 'identity_changed')
    try:
        require(datetime.fromisoformat(created.replace('Z', '+00:00')).tzinfo is not None,
                'identity_changed')
    except (ValueError, TypeError):
        raise ValueError('identity_changed') from None
    block = meta.get('graph_memory')
    if not block:
        # No persisted destination yet: capture never reads operator settings.
        return created, None
    if isinstance(block, dict):
        binding = block.get('binding')
        if binding is None:
            binding = 'embedded' if block.get('token') == EMBEDDED_TOKEN_SENTINEL else 'explicit'
        url = block.get('url')
        if isinstance(url, str):
            # Same slash/suffix normalization as the transport. Fold only
            # scheme/DNS authority; paths, queries and userinfo are case-sensitive.
            # This stays lexical, accepting malformed configuration unchanged.
            url = url.strip().rstrip('/')
            for suffix in ('/mcp', '/sse'):
                if url.endswith(suffix):
                    url = url[:-len(suffix)]
            url = url.rstrip('/')
            match = re.fullmatch(r'([A-Za-z][A-Za-z0-9+.-]*://)([^/?#]*)(.*)', url, re.DOTALL)
            if match:
                scheme, authority, rest = match.groups()
                userinfo, separator, host = authority.rpartition('@')
                host = host if separator else authority
                # Preserve IPv6 literals, including case-sensitive zone IDs.
                if not host.startswith('['):
                    host = host.lower()
                url = scheme.lower() + (userinfo + separator if separator else '') + host + rest
        destination = [binding, url, block.get('memory_id')]
        if not archive:
            destination.append(block.get('ontology', 'general'))
    else:
        destination = ['invalid_binding', block]
    raw = json.dumps(destination, ensure_ascii=True, sort_keys=True).encode()
    return created, hashlib.sha256(raw).hexdigest()


def validate_record(record, space_id, key):
    require(isinstance(record, dict) and type(record.get('version')) is int
            and record['version'] in (1, 2)
            and record.get('space_id') == space_id)
    capture = capture_id(space_id, record.get('preimage_id'))
    require(key == pending_prefix(space_id) + capture + '.json')
    require('binding_sha256' in record and (record['binding_sha256'] is None or
            (isinstance(record['binding_sha256'], str) and _SHA.fullmatch(record['binding_sha256']))))
    require(isinstance(record.get('space_created_at'), str) and record['space_created_at'])
    created = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
    require(created.tzinfo is not None)
    for number in ('next_attempt_at', 'failures'):
        value = record.get(number)
        require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
    require(type(record['failures']) is int)
    require(record.get('error') is None or record['error'] in ERRORS)
    documents = record.get('documents')
    require(isinstance(documents, list) and documents)
    paths = set()
    for document in documents:
        require(isinstance(document, dict) and set(document) == {'bank_path', 'sha256', 'size_bytes'})
        path = document['bank_path']
        require(isinstance(path, str) and path and path not in paths and '\\' not in path
                and not any(ord(c) < 32 or ord(c) == 127 for c in path)
                and all(part not in ('', '.', '..') for part in path.split('/')))
        paths.add(path)
        require(isinstance(document['sha256'], str) and _SHA.fullmatch(document['sha256']))
        require(type(document['size_bytes']) is int and document['size_bytes'] >= 0)
    completed = record.get('completed', [])
    require(isinstance(completed, list) and all(isinstance(path, str) for path in completed))
    require(len(completed) == len(set(completed))
            and set(completed) <= {source_path(record, d) for d in documents})
    jobs = record.get('jobs')
    require(isinstance(jobs, dict) and set(jobs) <= {source_path(record, d) for d in documents})
    require(all(isinstance(j, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', j) for j in jobs.values()))
    return record


async def archive_bytes(storage, record, document):
    value = await storage.get(f"_backups/{record['preimage_id']}/bank/{document['bank_path']}")
    require(isinstance(value, str), 'archive_unverified')
    raw = value.encode('utf-8')
    require(len(raw) == document['size_bytes'] and hashlib.sha256(raw).hexdigest() == document['sha256'],
            'archive_unverified')
    return raw


async def prepare_mid_archive(storage, *, space_id, preimage_id, documents):
    """Persist and verify an intent before MID mutation; never consult LONG."""
    key = pending_prefix(space_id) + capture_id(space_id, preimage_id) + '.json'
    existing = await storage.get_json(key)
    if existing is not None:
        validate_record(existing, space_id, key)
    version = existing['version'] if existing is not None else 2
    created, binding = space_identity(await storage.get_json(f'{space_id}/_meta.json'),
                                      space_id, archive=version == 2)
    record = {'version': version, 'space_id': space_id, 'preimage_id': preimage_id,
              'created_at': datetime.now(timezone.utc).isoformat(), 'space_created_at': created,
              'binding_sha256': binding, 'documents': documents, 'jobs': {}, 'completed': [],
              'next_attempt_at': 0, 'failures': 0, 'error': None}
    validate_record(record, space_id, key)
    for document in documents:
        await archive_bytes(storage, record, document)
    if existing is not None:
        require(all(existing[k] == record[k] for k in
                    ('space_created_at', 'binding_sha256', 'documents', 'preimage_id')))
        return key
    await storage.put_json(key, record)
    require(await storage.get_json(key) == record)
    return key


async def projection_status(storage, space_id):
    """Safe, passive status; no content, URLs, identifiers or provider messages."""
    result = {'pending': 0, 'oldest_at': None, 'oldest_age_seconds': None, 'error': None}
    try:
        items = await storage.list_objects(pending_prefix(space_id))
        result['pending'] = len(items)
        for item in items:
            try:
                record = validate_record(await storage.get_json(item['Key']), space_id, item['Key'])
                timestamp = record['created_at']
                if result['oldest_at'] is None or timestamp < result['oldest_at']:
                    result['oldest_at'] = timestamp
                if record['error']:
                    result['error'] = result['error'] or record['error']
            except Exception:
                result['error'] = 'invalid_record'
        if result['oldest_at']:
            timestamp = datetime.fromisoformat(result['oldest_at'].replace('Z', '+00:00'))
            result['oldest_age_seconds'] = max(0, (datetime.now(timezone.utc) - timestamp).total_seconds())
    except Exception:
        result['error'] = 'projection_unavailable'
    return result
