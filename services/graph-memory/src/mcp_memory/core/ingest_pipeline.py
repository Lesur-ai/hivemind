# -*- coding: utf-8 -*-
"""
Pipeline d'ingestion réutilisable (synchrone et asynchrone).

Ce module factorise le cœur de l'ingestion d'un document afin qu'il soit
appelé à l'identique par :
- l'outil MCP synchrone `memory_ingest` (server.py) ;
- le worker de la file d'ingestion asynchrone (`ingest_queue.py`).

Il fournit aussi :
- `resolve_ingestion()` : la logique d'idempotence par `source_path` + `sha256` ;
- `delete_document_everywhere()` : suppression multi-backend ordonnée et
  compensable (Qdrant → Neo4j → S3 ; S3 en dernier pour ne pas créer d'orphelin
  vu par storage_check), utilisée par le remplacement, l'annulation et l'outil
  `document_delete`.

Conception (cf. DESIGN/INGESTION_ASYNCHRONE.md) :
- l'état d'ingestion durable est porté par le nœud Document (`ingestion_status`),
  marqué `succeeded` UNIQUEMENT après succès de Neo4j ET Qdrant ;
- l'annulation est coopérative : testée aux frontières de phase, jamais au
  milieu d'une transaction d'enrichissement du graphe.
"""

from __future__ import annotations

import gc
import sys
import time as _time
import uuid
from functools import wraps
from typing import Any, Awaitable, Callable, Dict, Optional

from hivemind_inference import EmbeddingResult

from ..config import get_settings


class IngestCancelled(Exception):
    """Levée quand un job est annulé à une frontière de phase sûre."""


# Type des callbacks
ProgressCallback = Callable[[str, int, Dict[str, Any]], Awaitable[None]]
CancelCheck = Callable[[], bool]


def _guard_namespace_mutation(method):
    """Guard a keyword-scoped ingestion mutation before its first effect."""

    @wraps(method)
    async def guarded(*args, **kwargs):
        memory_id = kwargs.get("memory_id")
        if memory_id is None and args:
            memory_id = args[0]
        from .maintenance import get_maintenance_coordinator

        async with get_maintenance_coordinator().ordinary(memory_id):
            return await method(*args, **kwargs)

    return guarded


def _merge_embedding_results(
    results: list[EmbeddingResult],
) -> EmbeddingResult:
    """Merge des batches seulement si leur identité embedding est identique.

    Les quatre champs comparés sont exactement ceux que le résultat normalisé
    apporte au consommateur Qdrant. La comparaison et la reconstruction ont lieu
    avant tout appel au vector store : une dérive provider/model/dimensions
    échoue donc sans création de collection ni écriture partielle.
    """
    if not results:
        raise RuntimeError(
            "embedding provider returned no normalized batch result"
        )

    first = results[0]
    if type(first) is not EmbeddingResult:
        raise RuntimeError(
            "embedding provider returned an invalid normalized batch result"
        )

    identity = (
        first.configured_model,
        first.resolved_model,
        first.model_evidence,
        first.effective_dimensions,
    )
    vectors: list[tuple[float, ...]] = []

    for result in results:
        if type(result) is not EmbeddingResult:
            raise RuntimeError(
                "embedding provider returned an invalid normalized batch result"
            )
        if (
            result.configured_model,
            result.resolved_model,
            result.model_evidence,
            result.effective_dimensions,
        ) != identity:
            # Value-free by design: model identities may be provider-derived and
            # must never be copied into an outward exception.
            raise RuntimeError("embedding identity changed between batches")
        vectors.extend(result.vectors)

    def _complete_usage_sum(field: str) -> int | None:
        values = [getattr(result, field) for result in results]
        if any(value is None for value in values):
            return None
        return sum(values)

    return EmbeddingResult(
        vectors=tuple(vectors),
        configured_model=first.configured_model,
        resolved_model=first.resolved_model,
        model_evidence=first.model_evidence,
        effective_dimensions=first.effective_dimensions,
        input_tokens=_complete_usage_sum("input_tokens"),
        total_tokens=_complete_usage_sum("total_tokens"),
    )


# =============================================================================
# Services (réutilise les singletons lazy-loaded des modules core)
# =============================================================================

def _graph():
    from .graph import get_graph_service
    return get_graph_service()


def _storage():
    from .storage import get_storage_service
    return get_storage_service()


def _extractor():
    from .extractor import get_extractor_service
    return get_extractor_service()


def _embedder():
    from .embedder import get_embedding_service
    return get_embedding_service()


def _chunker():
    from .chunker import get_chunker
    return get_chunker()


def _vector_store():
    from .vector_store import get_vector_store
    return get_vector_store()


# =============================================================================
# Suppression multi-backend ordonnée (Qdrant → Neo4j → S3)
# =============================================================================

@_guard_namespace_mutation
async def delete_document_everywhere(
    memory_id: str,
    doc_id: str,
    uri: Optional[str] = None,
    *,
    delete_vectors: bool = True,
) -> Dict[str, Any]:
    """
    Supprime un document de TOUS les backends, dans un ordre sûr.

    Ordre : Qdrant (chunks) → Neo4j (graphe + entités orphelines) → S3 (objet).
    L'objet S3 est retiré EN DERNIER : tant que le nœud Neo4j existe encore et
    pointe dessus, storage_check ne le considère pas comme orphelin.
    Best-effort compensable : chaque étape est isolée ; si une étape échoue, on
    poursuit les suivantes et on renvoie le détail dans `errors`.

    Args:
        memory_id: ID de la mémoire
        doc_id: UUID du document à supprimer
        uri: URI S3 (optionnel — récupéré depuis le graphe si absent)
        delete_vectors: False seulement si aucune écriture Qdrant n'a encore
                        été tentée pour ce document
    """
    result: Dict[str, Any] = {
        "neo4j_deleted": False,
        "entities_deleted": 0,
        "relations_deleted": 0,
        "qdrant_chunks_deleted": 0,
        "s3_deleted": False,
        "errors": [],
    }

    # Récupérer l'URI avant suppression du graphe si non fourni
    if uri is None:
        try:
            info = await _graph().get_document(memory_id, doc_id)
            if info:
                uri = info.get("uri")
        except Exception as e:  # pragma: no cover - défensif
            result["errors"].append(f"get_document: {e}")

    # 1. Qdrant (chunks vectoriels)
    if delete_vectors:
        try:
            result["qdrant_chunks_deleted"] = await _vector_store().delete_document_chunks(memory_id, doc_id)
        except Exception as e:
            from .vector_store import EmbeddingCollectionError

            if isinstance(e, EmbeddingCollectionError):
                # Identity/unavailability means Qdrant ownership was not
                # decided. Deleting Neo4j/S3 anyway would orphan vectors whose
                # safe target is precisely what the resolver refused to guess.
                raise
            result["errors"].append(f"qdrant: {e}")
            print(f"⚠️ [delete_everywhere] Qdrant: {e}", file=sys.stderr)

    # 2. Neo4j (document + entités orphelines)
    try:
        graph_res = await _graph().delete_document(memory_id, doc_id)
        result["neo4j_deleted"] = bool(graph_res.get("deleted"))
        result["entities_deleted"] = graph_res.get("entities_deleted", 0)
        result["relations_deleted"] = graph_res.get("relations_deleted", 0)
    except Exception as e:
        result["errors"].append(f"neo4j: {e}")
        print(f"⚠️ [delete_everywhere] Neo4j: {e}", file=sys.stderr)

    # 3. S3 (objet original) — en dernier : tant que Neo4j pointe encore dessus,
    #    storage_check ne le considère pas comme orphelin.
    if uri:
        try:
            deleted = await _storage().delete_document(memory_id, uri)
            result["s3_deleted"] = deleted
            # delete_document() renvoie False sur ClientError → considérer comme erreur
            if not deleted:
                result["errors"].append(f"s3: deletion not confirmed for {uri}")
        except Exception as e:
            result["errors"].append(f"s3: {e}")
            print(f"⚠️ [delete_everywhere] S3: {e}", file=sys.stderr)

    return result


# =============================================================================
# Idempotence : résolution par source_path + sha256
# =============================================================================

async def resolve_ingestion(
    memory_id: str,
    source_path: Optional[str],
    doc_hash: str,
    replace_existing: bool,
) -> Dict[str, Any]:
    """
    Décide de l'action à mener pour un (source_path, sha256) donné.

    Retourne un dict :
      {"action": "ingest"|"skip"|"replace"|"conflict",
       "existing": {...} | None,
       "reason": str}

    Règles (cf. DESIGN §5.3) :
      - source_path inconnu                         → ingest
      - connu, même sha, ingestion succeeded        → skip
      - connu, même sha, ingestion NON succeeded    → replace (ingestion partielle)
      - connu, sha différent, replace_existing=True → replace
      - connu, sha différent, replace_existing=False→ conflict (changed_skipped)
      - source_path absent → fallback dédup SHA-256 (hash)
    """
    graph = _graph()
    norm = graph.normalize_source_path(source_path)

    # norm_source_path est remonté pour servir de clé canonique à la file
    # d'ingestion (comparaisons coalescing / in-flight cohérentes avec Neo4j).
    if not norm:
        # Legacy : pas de source_path → dédup historique par hash
        existing = await graph.get_document_by_hash(memory_id, doc_hash)
        if existing and not replace_existing:
            return {"action": "skip", "existing": {"id": existing.id, "filename": existing.filename}, "reason": "hash_match_legacy", "norm_source_path": None}
        if existing and replace_existing:
            return {"action": "replace", "existing": {"id": existing.id, "filename": existing.filename}, "reason": "hash_match_force", "norm_source_path": None}
        return {"action": "ingest", "existing": None, "reason": "new_no_source_path", "norm_source_path": None}

    existing = await graph.get_document_by_source_path(memory_id, norm)
    if not existing:
        return {"action": "ingest", "existing": None, "reason": "new_source_path", "norm_source_path": norm}

    same_hash = (existing.get("hash") == doc_hash)
    succeeded = (existing.get("ingestion_status") == "succeeded")

    if same_hash and succeeded:
        return {"action": "skip", "existing": existing, "reason": "same_checksum_succeeded", "norm_source_path": norm}
    if same_hash and not succeeded:
        # Ingestion précédente incomplète (ex. crash entre Neo4j et Qdrant)
        return {"action": "replace", "existing": existing, "reason": "same_checksum_incomplete", "norm_source_path": norm}
    # Checksum différent
    if replace_existing:
        return {"action": "replace", "existing": existing, "reason": "checksum_changed_replace", "norm_source_path": norm}
    return {"action": "conflict", "existing": existing, "reason": "checksum_changed_no_replace", "norm_source_path": norm}


# =============================================================================
# Pipeline principal
# =============================================================================

@_guard_namespace_mutation
async def run_ingest_pipeline(
    *,
    memory_id: str,
    content: bytes,
    filename: str,
    doc_hash: str,
    metadata: Optional[Dict[str, Any]] = None,
    source_path: Optional[str] = None,
    source_modified_at: Optional[str] = None,
    last_ingest_job_id: Optional[str] = None,
    replace_doc_id: Optional[str] = None,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> Dict[str, Any]:
    """
    Exécute le pipeline d'ingestion complet pour un document déjà décodé.

    Pré-requis assurés par l'appelant : validation memory_id/filename, contrôle
    d'accès/écriture, décodage base64, contrôle de taille, résolution
    d'idempotence (skip/conflict gérés en amont).

    Étapes : [remplacement éventuel] → S3 → texte → LLM → Neo4j doc(running)
    → entités/relations → chunking → embeddings → Qdrant → finalize(succeeded).

    Args:
        replace_doc_id: si fourni, l'ancien document est supprimé proprement
                        (Neo4j+Qdrant+S3) AVANT la nouvelle ingestion.
        progress_cb: async (current_step, progress_percent, extra) — observabilité.
        cancel_check: callable() -> bool — testé aux frontières de phase.

    Returns:
        dict de stats (même forme que l'ancien memory_ingest).
    """
    settings = get_settings()
    _t0 = _time.monotonic()
    _steps_log = []

    async def _report(step: str, percent: int, msg: str, **extra):
        _steps_log.append({"t": round(_time.monotonic() - _t0, 1), "msg": msg})
        print(f"📋 [Ingest] {msg}", file=sys.stderr)
        sys.stderr.flush()
        if progress_cb:
            try:
                await progress_cb(step, percent, {"message": msg, **extra})
            except Exception:
                pass

    def _check_cancel(where: str):
        if cancel_check and cancel_check():
            raise IngestCancelled(where)

    content_size = len(content)
    file_ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    s3_uploaded_uri: Optional[str] = None
    doc_id: Optional[str] = None
    vector_write_attempted = False

    try:
        # Vérifier la mémoire + ontologie (nécessaire pour l'extraction)
        memory = await _graph().get_memory(memory_id)
        if not memory:
            return {"status": "error", "message": f"Memory '{memory_id}' not found"}
        if not memory.ontology:
            return {
                "status": "error",
                "message": f"Memory '{memory_id}' has no ontology.",
            }

        # Remplacement : supprimer proprement l'ancien document d'abord.
        # Si la suppression est INCOMPLÈTE (ex. Qdrant en erreur), on ABANDONNE
        # avant de créer une nouvelle version — sinon on laisserait des vecteurs
        # orphelins de l'ancien doc en marquant le nouveau 'succeeded'.
        if replace_doc_id:
            _check_cancel("before_replace")
            await _report("replace", 8, f"🔄 Replacing: deleting {replace_doc_id}")
            del_res = await delete_document_everywhere(memory_id, replace_doc_id)
            if del_res.get("errors"):
                # Si l'ancien nœud Neo4j a survécu, le marquer pour re-traitement
                if not del_res.get("neo4j_deleted"):
                    try:
                        await _graph().update_document_ingestion(
                            memory_id=memory_id, doc_id=replace_doc_id,
                            ingestion_status="cleanup_pending",
                        )
                    except Exception:
                        pass
                return {
                    "status": "error",
                    "message": f"Replacement abandoned: deletion of the old document was incomplete: {del_res['errors']}",
                    "steps": _steps_log,
                }

        # --- S3 ---
        _check_cancel("before_s3")
        await _report("s3_upload", 10, f"📤 Upload S3 ({content_size} bytes)...")
        s3_result = await _storage().upload_document(
            memory_id=memory_id, filename=filename, content=content, metadata=metadata
        )
        s3_uploaded_uri = s3_result["uri"]
        await _report("s3_upload", 12, "✅ S3 upload complete")

        # --- Extraction texte ---
        _check_cancel("before_text")
        await _report("text_extract", 15, f"📄 Extracting text ({file_ext})...")
        from ..server import _extract_text  # réutilise l'extracteur de formats existant
        text = _extract_text(content, filename)
        if not text:
            # Rien d'exploitable : on nettoie l'objet S3 pour ne pas créer d'orphelin
            if s3_uploaded_uri:
                try:
                    await _storage().delete_document(memory_id, s3_uploaded_uri)
                except Exception:
                    pass
            return {"status": "warning", "message": "Document uploaded, but text extraction failed"}

        await _report("text_extract", 15, f"📄 Extracted {len(text)} characters")
        del content
        gc.collect()

        # --- Extraction LLM (entités/relations) ---
        _check_cancel("before_llm")

        async def _extraction_progress(event: str, data: dict):
            if event == "extraction_start":
                total = data.get("chunks_total", 1)
                await _report("llm_extract", 15, f"🔍 LLM extraction: {total} chunk(s)", chunks_total=total)
            elif event == "extraction_chunk_done":
                chunk = data.get("chunk", 0)
                total = max(1, data.get("chunks_total", 1))
                pct = 15 + int(45 * chunk / total)  # 15 → 60
                await _report(
                    "llm_extract", pct,
                    f"🔍 Chunk {chunk}/{total} : +{data.get('entities_new', 0)}E +{data.get('relations_new', 0)}R",
                    chunk=chunk, chunks_total=total,
                )

        await _report("llm_extract", 15, f"🔍 Starting LLM extraction (ontology: {memory.ontology})...")
        extraction = await _extractor().extract_with_ontology_chunked(
            text, memory.ontology, progress_callback=_extraction_progress
        )
        await _report(
            "llm_extract", 60,
            f"✅ Extraction: {len(extraction.entities)} entities, {len(extraction.relations)} relations",
        )

        # --- Neo4j : document (running) + entités/relations ---
        # À partir d'ici on n'interrompt plus en plein milieu : on laisse finir
        # puis on rollback si annulation (delete_document_everywhere).
        _check_cancel("before_graph")
        await _report("graph_write", 65, "📊 Storing data in the Neo4j graph...")
        doc_id = str(uuid.uuid4())
        await _graph().add_document(
            memory_id=memory_id,
            doc_id=doc_id,
            uri=s3_result["uri"],
            filename=filename,
            doc_hash=doc_hash,
            metadata=metadata,
            source_path=source_path,
            source_modified_at=source_modified_at,
            size_bytes=content_size,
            text_length=len(text),
            content_type=file_ext,
            ingestion_status="running",
            last_ingest_job_id=last_ingest_job_id,
        )
        graph_result = await _graph().add_entities_and_relations(
            memory_id=memory_id, doc_id=doc_id, extraction=extraction
        )
        await _report("graph_write", 70, "✅ Neo4j graph updated")
        # Annulation après écriture graphe : on supprime proprement le document
        # qu'on vient de créer (rollback dans le except IngestCancelled).
        _check_cancel("after_graph")

        # --- RAG : chunking + embeddings + Qdrant (couplage strict) ---
        chunks_stored = 0
        EMBED_BATCH_SIZE = 5
        try:
            await _report("chunking", 72, "🧩 Semantic chunking...")
            import asyncio
            loop = asyncio.get_event_loop()
            chunks = await loop.run_in_executor(None, _chunker().chunk_document, text, filename)
            await _report("chunking", 75, f"🧩 Created {len(chunks)} chunks")

            if chunks:
                for chunk in chunks:
                    chunk.doc_id = doc_id
                    chunk.memory_id = memory_id

                chunk_texts = [c.text for c in chunks]
                total_chunks = len(chunk_texts)
                batch_results: list[EmbeddingResult] = []
                total_batches = (total_chunks + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE

                for batch_start in range(0, total_chunks, EMBED_BATCH_SIZE):
                    # Frontière de phase sûre : annulation possible entre deux batches
                    _check_cancel("during_embedding")
                    batch_end = min(batch_start + EMBED_BATCH_SIZE, total_chunks)
                    batch_num = batch_start // EMBED_BATCH_SIZE + 1
                    batch_texts = chunk_texts[batch_start:batch_end]
                    pct = 75 + int(20 * batch_num / max(1, total_batches))  # 75 → 95
                    await _report("embedding", pct, f"🔢 Embedding batch {batch_num}/{total_batches}")
                    batch_result = await _embedder().embed_texts_result(batch_texts)
                    if len(batch_result.vectors) != len(batch_texts):
                        raise RuntimeError(
                            "embedding provider returned an invalid batch cardinality"
                        )
                    batch_results.append(batch_result)

                embedding_result = _merge_embedding_results(batch_results)
                await _report("vector_store", 96, f"📦 Storing {len(embedding_result.vectors)} vectors in Qdrant...")
                vector_store = _vector_store()
                # Dès que le store est invoqué, sa livraison peut être partielle.
                # Avant ce point, le rollback ne doit pas créer à lui seul un
                # chemin de mutation Qdrant.
                vector_write_attempted = True
                chunks_stored = await vector_store.store_chunks(
                    memory_id=memory_id, doc_id=doc_id, filename=filename,
                    chunks=chunks, embedding_result=embedding_result,
                )
                await _report("vector_store", 98, f"✅ RAG: vectorized {chunks_stored} chunks")
        except IngestCancelled:
            raise  # laisser remonter pour le rollback (ne pas masquer en RuntimeError)
        except Exception as e:
            print(f"❌ [Ingest] Vector RAG error: {e}", file=sys.stderr)
            raise RuntimeError(f"Qdrant vectorization failed (strict coupling): {e}")

        # Dernière frontière avant de marquer le document comme succeeded
        _check_cancel("before_finalize")

        # --- Finalisation : marqueur durable succeeded (APRÈS Qdrant) ---
        await _graph().update_document_ingestion(
            memory_id=memory_id, doc_id=doc_id,
            ingestion_status="succeeded", chunk_count=chunks_stored,
            last_ingest_job_id=last_ingest_job_id,
        )

        from collections import Counter
        relation_types = Counter(r.type for r in extraction.relations)
        entity_types = Counter(e.type for e in extraction.entities)
        _elapsed = round(_time.monotonic() - _t0, 1)
        await _report("done", 100, f"🏁 Ingestion completed in {_elapsed}s")

        return {
            "status": "ok",
            "document_id": doc_id,
            "filename": filename,
            "s3_uri": s3_result["uri"],
            "size_bytes": s3_result["size_bytes"],
            "sha256": doc_hash,
            "entities_extracted": len(extraction.entities),
            "relations_extracted": len(extraction.relations),
            "entities_created": graph_result.get("entities_created", 0),
            "entities_merged": graph_result.get("entities_merged", 0),
            "relations_created": graph_result.get("relations_created", 0),
            "relations_merged": graph_result.get("relations_merged", 0),
            "entity_types": dict(entity_types),
            "relation_types": dict(relation_types),
            "chunks_stored": chunks_stored,
            "summary": extraction.summary,
            "key_topics": extraction.key_topics,
            "steps": _steps_log,
            "elapsed_seconds": _elapsed,
        }

    except IngestCancelled as c:
        # Annulation propre : nettoyer ce qui a pu être écrit
        cleanup = await _rollback(
            memory_id,
            doc_id,
            s3_uploaded_uri,
            delete_vectors=vector_write_attempted,
        )
        out = {"status": "cancelled", "message": f"Cancelled ({c})", "steps": _steps_log}
        if cleanup.get("errors"):
            out["cleanup"] = cleanup
        return out
    except Exception as e:
        cleanup = await _rollback(
            memory_id,
            doc_id,
            s3_uploaded_uri,
            delete_vectors=vector_write_attempted,
        )
        print(f"❌ [Ingest] Error: {e}", file=sys.stderr)
        out = {"status": "error", "message": str(e), "steps": _steps_log}
        if cleanup.get("errors"):
            out["cleanup"] = cleanup
            out["message"] += f" | incomplete rollback: {cleanup['errors']}"
        return out


async def _rollback(
    memory_id: str,
    doc_id: Optional[str],
    s3_uri: Optional[str],
    *,
    delete_vectors: bool = True,
) -> Dict[str, Any]:
    """
    Nettoie les écritures partielles pour ne laisser aucun orphelin.

    En cas de rollback INCOMPLET (erreurs) et si le nœud Document Neo4j a
    survécu, on le marque durablement `ingestion_status = "cleanup_pending"`
    pour qu'il soit visible (storage_check.partial_ingestions) et re-traité.
    """
    result: Dict[str, Any] = {"errors": []}
    try:
        if doc_id:
            # Le document a été créé → suppression complète (Qdrant + Neo4j + S3)
            result = await delete_document_everywhere(
                memory_id,
                doc_id,
                uri=s3_uri,
                delete_vectors=delete_vectors,
            )
            if result.get("errors") and not result.get("neo4j_deleted"):
                # Le nœud subsiste : marqueur durable pour ne pas le voir 'succeeded'
                try:
                    await _graph().update_document_ingestion(
                        memory_id=memory_id, doc_id=doc_id, ingestion_status="cleanup_pending"
                    )
                except Exception as e:
                    result["errors"].append(f"mark_cleanup_pending: {e}")
        elif s3_uri:
            # Seul l'objet S3 a été uploadé → le retirer
            await _storage().delete_document(memory_id, s3_uri)
    except Exception as e:  # pragma: no cover - défensif
        print(f"⚠️ [Ingest] Partial rollback: {e}", file=sys.stderr)
        result.setdefault("errors", []).append(str(e))
    return result
