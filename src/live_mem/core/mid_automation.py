"""Server policy for MID maintenance; uses existing locks, engines and captures."""
import asyncio
from datetime import datetime, timezone

from ..config import get_settings
from .engines import RegistryRefused, get_engine_registry
from .hivemind.models import CorruptedStateError
from .write_sink import DirectLocalWriteSink, StagedWriteNotImplemented


def automation_policy():
    settings = get_settings()
    return {
        'compaction_enabled': settings.mid_auto_compact,
        'archive_enabled': settings.mid_auto_archive,
        'compaction_trigger': 'after_successful_consolidation',
        'file_threshold_bytes': settings.bank_file_max_size,
        'scope': 'direct_local',
    }


def _now():
    return datetime.now(timezone.utc).isoformat()


async def compact_after_consolidation(space_id):
    """Caller holds the consolidation lock; never acknowledge SHORT here.

    A maintenance failure is not a consolidation rollback. Fresh engine routing
    and the compactor's final guard retain the manual path's authority boundary.
    """
    if not get_settings().mid_auto_compact:
        return {'status': 'disabled'}
    started = _now()
    entered = False
    try:
        engine = await get_engine_registry().mid_engine(space_id)
        if not isinstance(engine.write_sink, DirectLocalWriteSink):
            raise StagedWriteNotImplemented(op='compact', key=f'{space_id}/bank/')
        entered = True
        result = await engine.compact_bank(space_id, dry_run=False)
        if type(result) is not dict or result.get('status') not in ('ok', 'error', 'partial'):
            raise ValueError('invalid_compaction_result')
        result = dict(result)
        if result['status'] == 'ok' and result.get('files_over_limit') == 0:
            result['status'] = 'not_needed'
    except asyncio.CancelledError as error:
        # Preserve the completed consolidation result even during shutdown.
        # An interrupted apply may need recovery; never assert it did not write.
        error.mid_auto_compaction_result = {
            'status': 'cancelled', 'recovery_required': entered,
            'started_at': started, 'finished_at': _now(),
        }
        # The existing transaction annotates a cancellation after attempting
        # rollback. Retain that safe receipt, especially its recovery location.
        from .consolidator import _sanitize_compaction_failure_payloads
        rollback = getattr(error, 'compaction_rollback_failures', None)
        if isinstance(rollback, tuple):
            error.mid_auto_compaction_result.update(
                recovery_required=bool(rollback),
                rollback_outcome='unverified' if rollback else 'restored',
                failures=_sanitize_compaction_failure_payloads(list(rollback)),
            )
        preimage = getattr(error, 'compaction_preimage_id', None)
        if isinstance(preimage, str):
            error.mid_auto_compaction_result['preimage_id'] = preimage
        raise
    except (RegistryRefused, StagedWriteNotImplemented):
        result = {'status': 'not_applicable', 'reason': 'direct_local_route_required',
                  'recovery_required': entered}
    except CorruptedStateError:
        result = {'status': 'error', 'reason': 'hivemind_state_corrupt', 'recovery_required': True}
    except Exception:
        # Provider exceptions may contain document content or credentials.
        result = {'status': 'error', 'reason': 'compaction_failed', 'recovery_required': entered}
    return {**result, 'started_at': started, 'finished_at': _now()}
