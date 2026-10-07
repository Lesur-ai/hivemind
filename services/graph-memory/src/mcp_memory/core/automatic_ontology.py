"""One initial batch's calibration, using the existing Graph and inference runtime.

The queue owns execution. Only admitted construction outputs survive restart;
clients resubmit the same batch to resume the existing best-effort queue.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field

import yaml

from hivemind_inference.records import ChatRequest

from ..config import get_settings
from .graph import get_graph_service
from .inference_runtime import get_inference_runtime
from .ingest_pipeline import CancelCheck, IngestCancelled
from .maintenance import get_maintenance_coordinator
from .ontology_validator import _validate_and_parse_ontology
from .ontology_construction import OntologyConstructionError


def batch_fingerprint(documents: list[dict]) -> str:
    manifest = sorted((doc["source_path"], doc["sha256"], doc["filename"])
                      for doc in documents)
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=False).encode()).hexdigest()


@dataclass
class AutomaticOntologyBatch:
    created_at: str
    fingerprint: str
    documents: list[dict] = field(repr=False)
    ontology_yaml: str | None = field(default=None, repr=False)
    diagnostics: dict = field(default_factory=dict)
    _failure_code: str | None = None

    async def check_memory_identity(self, memory_id: str) -> None:
        memory = await get_graph_service().get_memory(memory_id)
        if memory is None or memory.created_at.isoformat() != self.created_at:
            raise OntologyConstructionError("automatic_ontology_memory_changed")

    async def check_admission(self, memory_id: str) -> dict | None:
        """Caller holds exclusive admission; populated graphs are not migrated."""
        await self.check_memory_identity(memory_id)
        graph = get_graph_service()
        state = await graph.load_automatic_ontology(memory_id)
        if state is not None and state["batch_fingerprint"] != self.fingerprint:
            raise OntologyConstructionError("automatic_ontology_batch_mismatch")
        if state is None or state["ontology_yaml"] is None:
            stats = await graph.get_memory_stats(memory_id)
            if any((stats.document_count, stats.entity_count, stats.relation_count)):
                raise OntologyConstructionError("automatic_ontology_requires_empty_memory")
        return state

    async def prepare(self, memory_id: str, cancel_check: CancelCheck | None = None) -> str:
        if self._failure_code:
            raise OntologyConstructionError(self._failure_code)
        if self.ontology_yaml is not None:
            return self.ontology_yaml
        cancelled = False

        def check_cancel() -> None:
            nonlocal cancelled
            if cancel_check and cancel_check():
                cancelled = True
                raise IngestCancelled("automatic_ontology_cancelled")

        try:
            async with get_maintenance_coordinator().maintenance(memory_id):
                check_cancel()
                state = await self.check_admission(memory_id)
                if state is not None and state["ontology_yaml"] is not None:
                    self.ontology_yaml = state["ontology_yaml"]
                    self.diagnostics = self._diagnostics(state["checkpoint"])
                    return self.ontology_yaml

                # Import the constructor lazily: chosen ingestion does not depend
                # on calibration, and no provider is built during submission.
                from .ontology_construction import construct_ontology
                from ..server import _extract_text

                texts = []
                for doc in self.documents:
                    check_cancel()
                    text = await asyncio.to_thread(_extract_text, doc["content"], doc["filename"])
                    if not isinstance(text, str) or not text.strip():
                        raise OntologyConstructionError("automatic_ontology_document_has_no_text")
                    texts.append({"source_path": doc["source_path"], "text": text,
                                  "sha256": hashlib.sha256(text.encode()).hexdigest()})

                runtime = get_inference_runtime()
                profile = runtime.config.chat
                if profile is None:
                    raise OntologyConstructionError("automatic_ontology_chat_not_configured")
                profile_hash = hashlib.sha256(json.dumps(
                    profile.safe_snapshot(), sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                if state is not None:
                    envelope = state["checkpoint"]
                    if (envelope.get("profile_sha256") != profile_hash
                            or not isinstance(envelope.get("construction"), dict)):
                        raise OntologyConstructionError("automatic_ontology_profile_changed")
                    checkpoint = envelope["construction"]
                else:
                    checkpoint = {}
                provider = runtime.chat_provider()

                async def complete(messages):
                    # A completed call must reach the constructor's checkpoint;
                    # observe cancellation only before the next provider call.
                    check_cancel()
                    return await provider.complete(ChatRequest(
                        messages=messages,
                        timeout_seconds=get_settings().extraction_timeout_seconds,
                        reasoning_effort=profile.reasoning_effort,
                        retry_policy="none",
                    ))

                async def save(value):
                    await get_graph_service().save_automatic_ontology(
                        memory_id, expected_created_at=self.created_at,
                        batch_fingerprint=self.fingerprint,
                        checkpoint={"profile_sha256": profile_hash, "construction": value},
                    )

                # UTF-8 bytes conservatively upper-bound text tokens. Reserve
                # generation and framing; measured usage remains provider-native.
                input_budget = profile.context_window - profile.max_output_tokens - 1024
                result = await construct_ontology(
                    texts, checkpoint=checkpoint, complete=complete,
                    save_checkpoint=save, input_budget_bytes=input_budget,
                )
                catalogue = result["catalogue"]
                ontology_yaml = yaml.safe_dump({
                    "name": "auto_" + result["catalogue_sha256"][:16],
                    "version": "1.0",
                    "description": "Corpus-derived documentary ontology (Hivemind D1/D2).",
                    "context": "Classify only the meanings supported by the document.",
                    **catalogue,
                }, sort_keys=False, allow_unicode=True)
                validation, _ = _validate_and_parse_ontology(ontology_yaml)
                if not validation["valid"]:
                    raise OntologyConstructionError("automatic_ontology_catalogue_not_supported")
                check_cancel()
                await get_graph_service().save_automatic_ontology(
                    memory_id, expected_created_at=self.created_at,
                    batch_fingerprint=self.fingerprint,
                    # Frozen catalogues never resume construction: drop its
                    # source quotes and call ledger before document ingestion.
                    checkpoint={"profile_sha256": profile_hash,
                                "diagnostics": {
                                    "method": result["method"],
                                    "catalogue_sha256": result["catalogue_sha256"],
                                    "entity_types": len(catalogue["entity_types"]),
                                    "relation_types": len(catalogue["relation_types"]),
                                    "usage": result["usage"],
                                }},
                    ontology_yaml=ontology_yaml,
                )
                self.ontology_yaml = ontology_yaml
                self.diagnostics = {
                    "method": result["method"],
                    "catalogue_sha256": result["catalogue_sha256"],
                    "entity_types": len(catalogue["entity_types"]),
                    "relation_types": len(catalogue["relation_types"]),
                    "usage": result["usage"],
                }
                return ontology_yaml
        except asyncio.CancelledError:
            self._failure_code = "automatic_ontology_interrupted_resubmit_batch"
            raise
        except IngestCancelled:
            self._failure_code = "automatic_ontology_interrupted_resubmit_batch"
            raise
        except OntologyConstructionError as exc:
            if cancelled:
                # The reusable constructor normalizes caller exceptions after
                # checkpointing. Restore the queue's cancellation contract.
                self._failure_code = "automatic_ontology_interrupted_resubmit_batch"
                raise IngestCancelled("automatic_ontology_cancelled") from None
            self._failure_code = str(exc)
            raise
        except Exception:
            self._failure_code = "automatic_ontology_state_or_storage_failed"
            # Source text, catalogue definitions and provider payloads must not
            # enter the job error or an exception traceback emitted by the queue.
            raise OntologyConstructionError(self._failure_code) from None
        finally:
            # Jobs keep their own bounded payload; history never pins the corpus.
            self.documents.clear()

    @staticmethod
    def _diagnostics(checkpoint: dict) -> dict:
        # A resumed freeze needs no new inference. Its checkpoint retains only
        # the profile fingerprint and these source-free diagnostics.
        return {**checkpoint.get("diagnostics", {}), "resumed": True}
