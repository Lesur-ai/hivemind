# -*- coding: utf-8 -*-
"""
File d'ingestion asynchrone in-memory (best-effort).

Transposition du pattern `live-mem/core/consolidation_queue.py` à l'ingestion
de documents Graph Memory.

- FIFO avec UN worker asyncio par `memory_id` (sérialise les écritures
  Neo4j/Qdrant d'une même mémoire ; parallélise entre mémoires distinctes).
- État des jobs en mémoire → garantie explicite `in_memory_best_effort`
  (l'historique ne survit pas à un redémarrage du conteneur). L'idempotence
  documentaire, elle, est durable (source_path + sha256 + ingestion_status
  vivent dans Neo4j).
- Annulation coopérative : `cancel_requested` testé par le pipeline aux
  frontières de phase, sans corrompre le graphe.
- Quotas anti-saturation (jobs/mémoire + octets en file).

Voir DESIGN/INGESTION_ASYNCHRONE.md.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Optional

from ..config import get_settings
from .ingest_pipeline import resolve_ingestion, run_ingest_pipeline

logger = logging.getLogger("mcp_memory.ingest_queue")

QUEUE_GUARANTEE = "in_memory_best_effort"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "skipped", "changed_skipped"}
NO_AUTO_POLLING_CONTRACT = {
    "recommended": False,
    "mode": "manual_only",
    "status_tool": "ingest_job_status",
    "instruction": (
        "Ne pas attendre la fin ni poller automatiquement. Conserver le job_id "
        "et n'appeler ingest_job_status que pour une vérification explicite."
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guard_queue_admission(method):
    """Linearize source reads and enqueue with maintenance admission."""

    @wraps(method)
    async def guarded(self, *args, **kwargs):
        memory_id = kwargs.get("memory_id")
        if memory_id is None and args:
            memory_id = args[0]
        from .maintenance import get_maintenance_coordinator

        async with get_maintenance_coordinator().ordinary(memory_id):
            return await method(self, *args, **kwargs)

    return guarded


@dataclass
class IngestJob:
    job_id: str
    memory_id: str
    source_path: Optional[str]
    sha256: str
    filename: str
    requested_by: str = ""
    replace_existing: bool = False
    batch_id: Optional[str] = None
    status: str = "queued"
    current_step: str = "queued"
    progress_percent: int = 0
    created_entities: int = 0
    created_relations: int = 0
    document_id: Optional[str] = None
    error: Optional[str] = None
    guarantee: str = QUEUE_GUARANTEE
    created_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    updated_at: str = field(default_factory=_now)
    finished_at: Optional[str] = None
    cancel_requested: bool = False
    result: Optional[dict] = None
    # Payload d'entrée (libéré dès consommation par le worker)
    _content: Optional[bytes] = None
    _metadata: Optional[dict] = None
    _source_modified_at: Optional[str] = None
    _content_size: int = 0


class IngestQueueService:
    """File FIFO avec un worker en arrière-plan par mémoire."""

    def __init__(self):
        settings = get_settings()
        self._state_lock = asyncio.Lock()
        self._queues: dict[str, deque[str]] = defaultdict(deque)
        self._active_jobs: dict[str, str] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._jobs: dict[str, IngestJob] = {}
        self._queued_bytes = 0
        self._max_history = settings.ingest_max_history
        self._max_queued_per_memory = settings.ingest_max_queued_per_memory
        self._max_queued_bytes = settings.ingest_max_queued_bytes

    # ------------------------------------------------------------------ submit
    @_guard_queue_admission
    async def submit(
        self,
        *,
        memory_id: str,
        content: bytes,
        filename: str,
        sha256: str,
        source_path: Optional[str],
        replace_existing: bool,
        metadata: Optional[dict] = None,
        source_modified_at: Optional[str] = None,
        requested_by: str = "",
        job_id: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> dict:
        """
        Résout l'idempotence puis met le job en file si nécessaire.

        Réponses immédiates possibles sans créer de job :
          - skipped        (source_path + sha256 déjà ingérés avec succès)
          - changed_skipped (checksum différent, replace_existing=False)
        Sinon : crée et met en file un job (status queued|running).
        """
        # 1. Résolution d'idempotence (lecture Neo4j durable). Décision PRÉLIMINAIRE :
        #    elle n'est validée comme terminale (skip/conflict) que sous le lock et
        #    seulement s'il n'y a PAS de job en vol sur le même source_path (anti-TOCTOU).
        decision = await resolve_ingestion(memory_id, source_path, sha256, replace_existing)
        action = decision["action"]
        existing = decision.get("existing") or {}
        replace_doc_id = existing.get("id") if action == "replace" else None
        content_size = len(content)
        # Clé canonique = source_path NORMALISÉ (cohérent avec Neo4j) pour que
        # le coalescing et la détection de job en vol ne soient pas contournables
        # par des variantes lexicales ('/foo.md' vs 'foo.md').
        sp_key = decision.get("norm_source_path") or ""

        async with self._state_lock:
            # 2. Idempotence sur job_id client : si déjà connu, le renvoyer
            #    AVANT tout contrôle de quota (un resubmit ne doit jamais voir queue_full).
            jid = job_id or f"ing_{uuid.uuid4().hex}"
            if jid in self._jobs:
                return self._job_payload(self._jobs[jid])

            # 3. Coalescing : un job identique (même source_path + sha) déjà en cours/attente ?
            dup = self._find_pending_job(memory_id, sp_key, sha256)
            if dup:
                return {**self._job_payload(self._jobs[dup]), "coalesced": True}

            # 4. Y a-t-il un job NON terminal pour le même source_path (autre sha) ?
            #    Si oui, la décision skip/conflict serait fondée sur un état Neo4j en
            #    train de changer → on met en file et le worker rejouera resolve_ingestion.
            inflight = self._find_active_source_path_locked(memory_id, sp_key)

            # 5. Décision terminale (skip/conflict) uniquement si rien en vol sur ce source_path
            if action in ("skip", "conflict") and not inflight:
                status = "skipped" if action == "skip" else "changed_skipped"
                payload = {
                    "status": status,
                    "document_id": existing.get("id"),
                    "source_path": source_path,
                    "sha256": sha256,
                    "reason": decision["reason"],
                    "message": (
                        "Document déjà ingéré (même source_path + checksum)."
                        if action == "skip" else
                        "Le checksum a changé pour ce source_path. Relancez avec "
                        "replace_existing=true pour remplacer explicitement."
                    ),
                }
                if batch_id:
                    payload["job_id"] = await self._record_terminal_locked(
                        memory_id=memory_id, source_path=sp_key, sha256=sha256,
                        filename=filename, status=status, batch_id=batch_id,
                        document_id=existing.get("id"),
                    )
                return payload

            # 6. Quotas anti-saturation
            if len(self._queues.get(memory_id, ())) >= self._max_queued_per_memory:
                return {
                    "status": "queue_full",
                    "message": f"File pleine pour '{memory_id}' (max {self._max_queued_per_memory}).",
                }
            if self._queued_bytes + content_size > self._max_queued_bytes:
                return {
                    "status": "queue_full",
                    "message": "Capacité mémoire de la file dépassée (max_queued_bytes).",
                }

            # 7. Création du job (le worker rejouera resolve_ingestion avant le pipeline)
            job = IngestJob(
                job_id=jid,
                memory_id=memory_id,
                source_path=sp_key,  # clé canonique normalisée
                sha256=sha256,
                filename=filename,
                requested_by=requested_by,
                replace_existing=replace_existing,
                batch_id=batch_id,
                _content=content,
                _metadata=metadata,
                _source_modified_at=source_modified_at,
                _content_size=content_size,
            )
            # On mémorise la décision/replace_doc_id pour le worker
            job.result = {"_replace_doc_id": replace_doc_id}
            self._jobs[jid] = job
            self._queued_bytes += content_size

            if memory_id not in self._active_jobs:
                job.status = "running"
                job.started_at = _now()
                self._active_jobs[memory_id] = jid
            else:
                self._queues[memory_id].append(jid)

            self._trim_history_locked()
            self._ensure_worker_locked(memory_id)
            return self._job_payload(job)

    async def _record_terminal_locked(
        self, *, memory_id, source_path, sha256, filename, status, batch_id, document_id
    ) -> str:
        """Crée un job déjà terminal (skipped/changed_skipped) pour la traçabilité d'un lot."""
        jid = f"ing_{uuid.uuid4().hex}"
        now = _now()
        job = IngestJob(
            job_id=jid, memory_id=memory_id, source_path=source_path, sha256=sha256,
            filename=filename, batch_id=batch_id, status=status, current_step=status,
            progress_percent=100 if status == "skipped" else 0, document_id=document_id,
            started_at=now, finished_at=now,
        )
        self._jobs[jid] = job
        self._trim_history_locked()
        return jid

    # ------------------------------------------------------------------ status
    async def get_job(self, job_id: str) -> dict:
        async with self._state_lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"status": "not_found", "message": f"Job '{job_id}' introuvable"}
            return self._job_payload(job)

    async def list_jobs(
        self,
        memory_id: str,
        status: Optional[str] = None,
        source_path: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> dict:
        async with self._state_lock:
            jobs = [j for j in self._jobs.values() if j.memory_id == memory_id]
            if status:
                jobs = [j for j in jobs if j.status == status]
            if source_path:
                # Normaliser le filtre comme la clé stockée (cohérent avec Neo4j)
                sp_filter = source_path.strip().lstrip("/")
                jobs = [j for j in jobs if (j.source_path or "") == sp_filter]
            if batch_id:
                jobs = [j for j in jobs if j.batch_id == batch_id]
            jobs.sort(key=lambda j: j.created_at, reverse=True)
            return {
                "status": "ok",
                "memory_id": memory_id,
                "count": len(jobs),
                "guarantee": QUEUE_GUARANTEE,
                "jobs": [self._job_payload(j) for j in jobs],
            }

    async def is_idle_for_memory(self, memory_id: str) -> bool:
        """Return exact queue/worker idleness for maintenance admission."""
        async with self._state_lock:
            if memory_id in self._active_jobs:
                return False
            # Every deque entry is worker-visible: _pop_next_job_locked will
            # promote it to running regardless of a stale/missing job record.
            # Treat even terminal or orphan entries as non-idle so corrupted
            # queue bookkeeping cannot admit maintenance over future work.
            return not bool(self._queues.get(memory_id))

    async def batch_summary(self, batch_id: str) -> dict:
        async with self._state_lock:
            jobs = [j for j in self._jobs.values() if j.batch_id == batch_id]
            counts = {k: 0 for k in ("queued", "running", "succeeded", "failed", "cancelled", "skipped", "changed_skipped")}
            errors = []
            for j in jobs:
                counts[j.status] = counts.get(j.status, 0) + 1
                if j.status == "failed":
                    errors.append({"source_path": j.source_path, "filename": j.filename, "error": j.error})
            return {
                "status": "ok",
                "batch_id": batch_id,
                "total": len(jobs),
                "counts": counts,
                "errors": errors,
                "jobs": [self._job_payload(j) for j in jobs],
            }

    async def cancel(self, job_id: str) -> dict:
        async with self._state_lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"status": "not_found", "message": f"Job '{job_id}' introuvable"}
            if job.status in TERMINAL_STATUSES:
                return {"status": "noop", "job_status": job.status, "message": "Job déjà terminé"}
            if job.status == "queued":
                # Retirer de la file immédiatement
                try:
                    self._queues[job.memory_id].remove(job_id)
                except (KeyError, ValueError):
                    pass
                self._release_content_locked(job)
                job.status = "cancelled"
                job.current_step = "cancelled"
                job.finished_at = _now()
                job.updated_at = _now()
                return {"status": "cancelled", "job_id": job_id, "message": "Job en attente annulé"}
            # running : annulation coopérative (le pipeline s'arrête à la prochaine frontière)
            job.cancel_requested = True
            job.updated_at = _now()
            return {
                "status": "cancelling",
                "job_id": job_id,
                "message": "Annulation demandée (best-effort, à la prochaine frontière de phase).",
            }

    # ------------------------------------------------------------------ worker
    def _ensure_worker_locked(self, memory_id: str) -> None:
        worker = self._workers.get(memory_id)
        if worker and not worker.done():
            return
        self._workers[memory_id] = asyncio.create_task(self._worker(memory_id))

    async def _worker(self, memory_id: str) -> None:
        while True:
            async with self._state_lock:
                active_id = self._active_jobs.get(memory_id)
                if not active_id:
                    next_id = self._pop_next_job_locked(memory_id)
                    if not next_id:
                        self._workers.pop(memory_id, None)
                        return
                    active_id = next_id
                job = self._jobs[active_id]
                # Consommer le contenu (libère la comptabilité d'octets)
                content = job._content
                metadata = job._metadata
                source_modified_at = job._source_modified_at
                replace_doc_id = (job.result or {}).get("_replace_doc_id")
                self._release_content_locked(job)
                job.result = None

            # Annulation demandée avant démarrage effectif
            if job.cancel_requested:
                async with self._state_lock:
                    job.status = "cancelled"
                    job.current_step = "cancelled"
                    job.finished_at = _now()
                    job.updated_at = _now()
                    self._finish_active_locked(memory_id, job.job_id)
                continue

            # Re-résolution de l'idempotence à l'EXÉCUTION (anti-TOCTOU) :
            # entre la soumission et maintenant, l'état Neo4j a pu changer
            # (un job précédent du même source_path vient de réussir, etc.).
            # Comme un seul worker tourne par mémoire, cette décision est sérialisée.
            try:
                decision = await resolve_ingestion(
                    memory_id, job.source_path, job.sha256, job.replace_existing
                )
            except Exception as e:
                # Ne JAMAIS ingérer à l'aveugle si l'idempotence ne peut pas être
                # re-résolue (Neo4j indisponible, etc.) → échec explicite du job.
                async with self._state_lock:
                    job.status = "failed"
                    job.current_step = "failed"
                    job.error = f"Re-résolution idempotence impossible: {e}"
                    job.finished_at = _now()
                    job.updated_at = _now()
                    self._finish_active_locked(memory_id, job.job_id)
                continue

            if decision["action"] in ("skip", "conflict"):
                existing = decision.get("existing") or {}
                async with self._state_lock:
                    job.status = "skipped" if decision["action"] == "skip" else "changed_skipped"
                    job.current_step = job.status
                    job.document_id = existing.get("id")
                    job.progress_percent = 100 if decision["action"] == "skip" else 0
                    job.finished_at = _now()
                    job.updated_at = _now()
                    self._finish_active_locked(memory_id, job.job_id)
                continue

            # Décision fraîche : remplacer l'éventuel replace_doc_id périmé
            if decision["action"] == "replace":
                replace_doc_id = (decision.get("existing") or {}).get("id")
            else:  # ingest
                replace_doc_id = None

            try:
                async def progress_cb(step: str, percent: int, extra: dict):
                    async with self._state_lock:
                        job.current_step = step
                        job.progress_percent = percent
                        job.updated_at = _now()

                def cancel_check() -> bool:
                    return job.cancel_requested

                result = await run_ingest_pipeline(
                    memory_id=memory_id,
                    content=content if content is not None else b"",
                    filename=job.filename,
                    doc_hash=job.sha256,
                    metadata=metadata,
                    source_path=job.source_path,
                    source_modified_at=source_modified_at,
                    last_ingest_job_id=job.job_id,
                    replace_doc_id=replace_doc_id,
                    progress_cb=progress_cb,
                    cancel_check=cancel_check,
                )
                async with self._state_lock:
                    self._apply_result_locked(job, result)
                    self._finish_active_locked(memory_id, job.job_id)
            except Exception as e:  # pragma: no cover - défensif
                logger.exception("Ingest job failed — job=%s", job.job_id)
                async with self._state_lock:
                    job.status = "failed"
                    job.error = str(e)
                    job.current_step = "failed"
                    job.finished_at = _now()
                    job.updated_at = _now()
                    self._finish_active_locked(memory_id, job.job_id)

    def _apply_result_locked(self, job: IngestJob, result: dict) -> None:
        status = result.get("status")
        job.updated_at = _now()
        job.finished_at = _now()
        if status == "ok":
            job.status = "succeeded"
            job.current_step = "done"
            job.progress_percent = 100
            job.document_id = result.get("document_id")
            job.created_entities = result.get("entities_created", 0)
            job.created_relations = result.get("relations_created", 0)
        elif status == "cancelled":
            job.status = "cancelled"
            job.current_step = "cancelled"
            if result.get("cleanup", {}).get("errors"):
                job.error = f"rollback incomplet: {result['cleanup']['errors']}"
        elif status == "warning":
            job.status = "failed"
            job.error = result.get("message", "Extraction texte impossible")
        else:
            job.status = "failed"
            job.error = result.get("message", "Échec d'ingestion")

    # ------------------------------------------------------------------ helpers
    def _find_active_source_path_locked(self, memory_id: str, source_path: str) -> Optional[str]:
        """Job NON terminal (running/queued) pour ce source_path (toute version sha)."""
        if not source_path:
            return None
        active_id = self._active_jobs.get(memory_id)
        candidates = list(self._queues.get(memory_id, ()))
        if active_id:
            candidates.append(active_id)
        for jid in candidates:
            j = self._jobs.get(jid)
            if not j or j.status in TERMINAL_STATUSES:
                continue
            if (j.source_path or "") == source_path:
                return jid
        return None

    def _find_pending_job(self, memory_id: str, norm_source_path: str, sha256: str) -> Optional[str]:
        active_id = self._active_jobs.get(memory_id)
        candidates = list(self._queues.get(memory_id, ()))
        if active_id:
            candidates.append(active_id)
        for jid in candidates:
            j = self._jobs.get(jid)
            if not j or j.status in TERMINAL_STATUSES:
                continue
            if (j.source_path or "") == norm_source_path and j.sha256 == sha256:
                return jid
        return None

    def _pop_next_job_locked(self, memory_id: str) -> Optional[str]:
        queue = self._queues.get(memory_id)
        if not queue:
            return None
        job_id = queue.popleft()
        if not queue:
            self._queues.pop(memory_id, None)
        job = self._jobs[job_id]
        job.status = "running"
        job.started_at = _now()
        job.updated_at = _now()
        self._active_jobs[memory_id] = job_id
        return job_id

    def _finish_active_locked(self, memory_id: str, job_id: str) -> None:
        if self._active_jobs.get(memory_id) == job_id:
            self._active_jobs.pop(memory_id, None)
        self._trim_history_locked()

    def _release_content_locked(self, job: IngestJob) -> None:
        if job._content is not None:
            self._queued_bytes = max(0, self._queued_bytes - job._content_size)
            job._content = None
            job._metadata = None

    def _queue_position_locked(self, job: IngestJob) -> int:
        if self._active_jobs.get(job.memory_id) == job.job_id:
            return 1
        queue = self._queues.get(job.memory_id, ())
        try:
            return list(queue).index(job.job_id) + 2
        except ValueError:
            return 0

    def _job_payload(self, job: IngestJob) -> dict:
        payload = {
            "status": job.status,
            "job_id": job.job_id,
            "memory_id": job.memory_id,
            "source_path": job.source_path,
            "sha256": job.sha256,
            "filename": job.filename,
            "batch_id": job.batch_id,
            "current_step": job.current_step,
            "progress_percent": job.progress_percent,
            "created_entities": job.created_entities,
            "created_relations": job.created_relations,
            "document_id": job.document_id,
            "queue_position": self._queue_position_locked(job),
            "guarantee": job.guarantee,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "updated_at": job.updated_at,
            "finished_at": job.finished_at,
            "polling": dict(NO_AUTO_POLLING_CONTRACT),
        }
        if job.error:
            payload["error"] = job.error
        if job.status in ("queued", "running"):
            payload["message"] = (
                f"Job d'ingestion {job.status} pour '{job.memory_id}'. Ne pas attendre "
                "la fin par défaut ; utiliser ingest_job_status pour un point explicite."
            )
        return payload

    def _trim_history_locked(self) -> None:
        if len(self._jobs) <= self._max_history:
            return
        protected = set(self._active_jobs.values())
        for queue in self._queues.values():
            protected.update(queue)
        # Supprimer les plus anciens jobs terminaux
        for job_id, job in sorted(self._jobs.items(), key=lambda kv: kv[1].created_at):
            if len(self._jobs) <= self._max_history:
                break
            if job_id not in protected and job.status in TERMINAL_STATUSES:
                self._jobs.pop(job_id, None)


_queue_service: Optional[IngestQueueService] = None


def get_ingest_queue() -> IngestQueueService:
    global _queue_service
    if _queue_service is None:
        _queue_service = IngestQueueService()
    return _queue_service


def reset_ingest_queue_for_tests() -> None:
    global _queue_service
    _queue_service = None
