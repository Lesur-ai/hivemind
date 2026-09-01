# -*- coding: utf-8 -*-
"""
GraphService - Client Neo4j pour le Knowledge Graph.

Gère toutes les opérations sur le graphe de connaissances :
- CRUD pour les mémoires, documents, entités, relations
- Requêtes de recherche et de contexte
- Statistiques
"""

import asyncio
import sys
from typing import Optional, List, Dict, Any
from datetime import datetime
from contextlib import asynccontextmanager
from functools import wraps

from neo4j import AsyncGraphDatabase, AsyncDriver, AsyncSession, Query
from neo4j.exceptions import ServiceUnavailable, AuthError

from ..config import get_settings
from .models import (
    Memory, MemoryStats, Document, DocumentMetadata,
    ExtractedEntity, ExtractedRelation, ExtractionResult,
    SearchResult, GraphContext, SearchMode
)
from .maintenance import (
    MAX_REINDEX_SOURCE_DOCUMENTS,
    ReindexSourceLimitExceeded,
)


_DOCUMENT_SOURCE_NORMALIZATION_BATCH_SIZE = 1_000
_DOCUMENT_SCHEMA_MIGRATION_ID = "document-source-path-empty-to-null-v1"
_DOCUMENT_SCHEMA_MIGRATION_VERSION = 1


class DocumentSchemaUnavailable(RuntimeError):
    """Fixed refusal while the global Document schema is unverified."""

    def __init__(self) -> None:
        super().__init__("document schema initialization is unavailable")


def _iso(v):
    """Convertit une valeur date Neo4j/Python en string ISO 8601, de façon robuste.

    - neo4j.time.DateTime  → .to_native().isoformat()
    - datetime natif       → .isoformat()
    - string (ex: source_modified_at stocké en str) ou autre → str(v)
    - None                 → None
    """
    if v is None:
        return None
    if hasattr(v, "to_native"):  # neo4j.time.DateTime
        return v.to_native().isoformat()
    if hasattr(v, "isoformat"):  # datetime natif
        return v.isoformat()
    return str(v)


def _guard_graph_mutation(method):
    """Guard one memory-scoped Neo4j mutation before opening a session."""

    @wraps(method)
    async def guarded(self, memory_id: str, *args, **kwargs):
        from .maintenance import get_maintenance_coordinator

        async with get_maintenance_coordinator().ordinary(memory_id):
            return await method(self, memory_id, *args, **kwargs)

    return guarded


def _guard_graph_import(method):
    """Derive the exact backup namespace before any graph import effect."""

    @wraps(method)
    async def guarded(self, data: Dict[str, Any], *args, **kwargs):
        memory = data.get("memory") if type(data) is dict else None
        memory_id = memory.get("id") if type(memory) is dict else None
        if type(memory_id) is not str or not memory_id:
            raise ValueError("backup memory id is invalid")
        for collection_name in (
            "documents",
            "entities",
            "relations",
            "mentions",
        ):
            collection = data.get(collection_name, [])
            if type(collection) is not list or any(
                type(item) is not dict for item in collection
            ):
                raise ValueError("backup graph data is invalid")
        for item in data.get("documents", []) + data.get("entities", []):
            if "memory_id" in item and item["memory_id"] != memory_id:
                raise ValueError("backup graph namespace mismatch")
        from .maintenance import get_maintenance_coordinator

        async with get_maintenance_coordinator().ordinary(memory_id):
            return await method(self, data, *args, **kwargs)

    return guarded


class GraphService:
    """
    Service de gestion du Knowledge Graph (Neo4j).
    
    Utilise des labels préfixés par memory_id pour l'isolation multi-tenant.
    Ex: quoteflow_legal_Document, quoteflow_legal_Entity
    
    Recherche: utilise un index fulltext Lucene avec analyzer 'standard-folding'
    pour la recherche accent-insensitive (é→e, ç→c, etc.).
    """
    
    # Nom de l'index fulltext dans Neo4j
    FULLTEXT_INDEX_NAME = "entity_fulltext"
    _schema_query_timeout_seconds = 30
    _doc_migration_marker_ready = False
    
    def __init__(self):
        """Initialise la connexion Neo4j."""
        settings = get_settings()
        
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_lifetime=3600,
            max_connection_pool_size=50,
            connection_acquisition_timeout=60
        )
        self._database = settings.neo4j_database
        self._schema_query_timeout_seconds = settings.neo4j_query_timeout_seconds
        self._fulltext_index_ready = False  # Lazy init de l'index fulltext
        self._doc_constraints_ready = False
        self._doc_source_normalization_done = False
        self._doc_migration_marker_ready = False
        # The composite constraint and its legacy normalization are GLOBAL
        # Neo4j schema lifecycle.  Per-memory mutation gates cannot serialize
        # two namespaces that concurrently discover the schema as absent.
        self._doc_constraints_lock = asyncio.Lock()

    async def close(self):
        """Ferme la connexion Neo4j."""
        await self._driver.close()
    
    @asynccontextmanager
    async def session(self) -> AsyncSession:
        """Context manager pour obtenir une session Neo4j."""
        session = self._driver.session(database=self._database)
        cancelled = False
        try:
            yield session
        except asyncio.CancelledError:
            # Neo4j's async driver documents cancel() as the cancellation
            # escape hatch. Awaiting close() here can try to consume an
            # unanswered auto-result and defeat the outer startup deadline.
            cancelled = True
            try:
                session.cancel()
            except Exception:
                pass
            raise
        finally:
            if not cancelled:
                try:
                    await session.close()
                except asyncio.CancelledError:
                    try:
                        session.cancel()
                    except Exception:
                        pass
                    raise

    @staticmethod
    async def _run_consumed(session: AsyncSession, query, **params) -> None:
        """Run one write/DDL and observe its deferred PULL/commit outcome."""

        result = await session.run(query, **params)
        await result.consume()

    def _document_schema_is_ready(self) -> bool:
        return (
            self._doc_source_normalization_done
            and self._doc_constraints_ready
            and self._doc_migration_marker_ready
        )

    def document_schema_status(self) -> dict[str, object]:
        """Return the process startup invariant without touching Neo4j."""

        ready = self._document_schema_is_ready()
        return {
            "status": "ok" if ready else "error",
            "ready": ready,
        }
    
    def _ns(self, memory_id: str) -> str:
        """Retourne le préfixe namespace pour les labels."""
        # Remplace les caractères non-alphanumériques par _
        safe_id = "".join(c if c.isalnum() else "_" for c in memory_id)
        return safe_id
    
    # =========================================================================
    # Test de connexion
    # =========================================================================
    
    async def test_connection(self) -> dict:
        """Teste la connexion Neo4j."""
        try:
            async with self.session() as session:
                result = await session.run("RETURN 1 AS test")
                record = await result.single()
                
                # Récupérer quelques stats
                stats_result = await session.run(
                    "CALL apoc.meta.stats() YIELD nodeCount, relCount "
                    "RETURN nodeCount, relCount"
                )
                stats = await stats_result.single()
                
                return {
                    "status": "ok",
                    "database": self._database,
                    "node_count": stats["nodeCount"] if stats else 0,
                    "rel_count": stats["relCount"] if stats else 0,
                    "message": "Neo4j connection succeeded"
                }
                
        except AuthError:
            return {
                "status": "error",
                "database": self._database,
                "message": "Neo4j authentication failed"
            }
        except ServiceUnavailable:
            return {
                "status": "error",
                "database": self._database,
                "message": "Neo4j is unavailable"
            }
        except Exception as e:
            return {
                "status": "error",
                "database": self._database,
                "message": f"Neo4j error: {str(e)}"
            }
    
    # =========================================================================
    # Gestion des Mémoires
    # =========================================================================
    
    @_guard_graph_mutation
    async def create_memory(
        self,
        memory_id: str,
        name: str,
        description: Optional[str] = None,
        ontology: str = "default",
        ontology_uri: Optional[str] = None,
        owner_token: Optional[str] = None
    ) -> Memory:
        """
        Crée une nouvelle mémoire (namespace).
        
        Crée un nœud :Memory pour tracker les métadonnées.
        L'ontologie est stockée sur S3, son URI est sauvegardée.
        """
        ns = self._ns(memory_id)
        
        async with self.session() as session:
            # Vérifier si la mémoire existe déjà
            check = await session.run(
                "MATCH (m:Memory {id: $id}) RETURN m",
                id=memory_id
            )
            existing = await check.single()
            
            if existing:
                raise ValueError(f"Memory '{memory_id}' already exists")
            
            # Créer la mémoire avec l'URI de l'ontologie
            result = await session.run(
                """
                CREATE (m:Memory {
                    id: $id,
                    name: $name,
                    description: $description,
                    ontology: $ontology,
                    ontology_uri: $ontology_uri,
                    namespace: $namespace,
                    owner_token_hash: $owner_token,
                    created_at: datetime()
                })
                RETURN m
                """,
                id=memory_id,
                name=name,
                description=description,
                ontology=ontology,
                ontology_uri=ontology_uri,
                namespace=ns,
                owner_token=owner_token
            )
            
            record = await result.single()
            node = record["m"]
            
            print(f"🧠 [Graph] Memory created: {memory_id} (ns: {ns}, ontology: {ontology}, uri: {ontology_uri})", file=sys.stderr)
            
            return Memory(
                id=memory_id,
                name=name,
                description=description,
                ontology=ontology,
                ontology_uri=ontology_uri,
                created_at=node["created_at"].to_native() if node.get("created_at") else datetime.utcnow(),
                owner_token=owner_token
            )
    
    async def get_memory(self, memory_id: str) -> Optional[Memory]:
        """Récupère une mémoire par son ID."""
        async with self.session() as session:
            result = await session.run(
                "MATCH (m:Memory {id: $id}) RETURN m",
                id=memory_id
            )
            record = await result.single()
            
            if not record:
                return None
            
            node = record["m"]
            return Memory(
                id=node["id"],
                name=node["name"],
                description=node.get("description"),
                ontology=node.get("ontology", "default"),
                created_at=node["created_at"].to_native() if node.get("created_at") else datetime.utcnow()
            )
    
    @_guard_graph_mutation
    async def update_memory(
        self,
        memory_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Memory]:
        """
        Met à jour les métadonnées d'une mémoire (name, description).
        
        Seuls les champs fournis (non None) sont modifiés.
        L'ontologie n'est pas modifiable après création.
        
        Returns:
            La mémoire mise à jour, ou None si non trouvée.
        """
        async with self.session() as session:
            # Construire dynamiquement la clause SET
            set_parts = []
            params = {"id": memory_id}
            
            if name is not None:
                set_parts.append("m.name = $name")
                params["name"] = name
            if description is not None:
                set_parts.append("m.description = $description")
                params["description"] = description
            
            if not set_parts:
                return await self.get_memory(memory_id)
            
            set_clause = ", ".join(set_parts)
            query = f"""
                MATCH (m:Memory {{id: $id}})
                SET {set_clause}
                RETURN m
            """
            
            result = await session.run(query, **params)
            record = await result.single()
            
            if not record:
                return None
            
            node = record["m"]
            print(f"✏️ [Graph] Memory updated: {memory_id} ({', '.join(set_parts)})", file=sys.stderr)
            return Memory(
                id=node["id"],
                name=node["name"],
                description=node.get("description"),
                ontology=node.get("ontology", "default"),
                created_at=node["created_at"].to_native() if node.get("created_at") else datetime.utcnow()
            )
    
    @_guard_graph_mutation
    async def delete_memory(self, memory_id: str) -> bool:
        """
        Supprime une mémoire et tous ses nœuds associés.
        
        ATTENTION: Opération destructive !
        """
        ns = self._ns(memory_id)
        
        async with self.session() as session:
            # Supprimer tous les nœuds du namespace
            # Les labels dynamiques ne sont pas supportés directement,
            # donc on utilise apoc ou on supprime par propriété memory_id
            await self._run_consumed(
                session,
                """
                MATCH (n)
                WHERE n.memory_id = $memory_id
                DETACH DELETE n
                """,
                memory_id=memory_id
            )
            
            # Supprimer le nœud Memory
            result = await session.run(
                """
                MATCH (m:Memory {id: $id})
                DELETE m
                RETURN count(m) as deleted
                """,
                id=memory_id
            )
            
            record = await result.single()
            deleted = record["deleted"] > 0 if record else False
            
            if deleted:
                print(f"🗑️ [Graph] Memory deleted: {memory_id}", file=sys.stderr)
            
            return deleted
    
    async def list_memories(self) -> List[Memory]:
        """Liste toutes les mémoires."""
        async with self.session() as session:
            result = await session.run(
                "MATCH (m:Memory) RETURN m ORDER BY m.created_at DESC"
            )
            
            memories = []
            async for record in result:
                node = record["m"]
                memories.append(Memory(
                    id=node["id"],
                    name=node["name"],
                    description=node.get("description"),
                    ontology=node.get("ontology", "default"),
                    ontology_uri=node.get("ontology_uri"),
                    created_at=node["created_at"].to_native() if node.get("created_at") else datetime.utcnow()
                ))
            
            return memories
    
    # =========================================================================
    # Gestion des Documents
    # =========================================================================
    
    @staticmethod
    def normalize_source_path(source_path: Optional[str]) -> Optional[str]:
        """
        Normalise un source_path pour servir de clé métier stable.

        - None / chaîne vide → None (pas de clé source_path)
        - sinon : strip + suppression des slashes de tête redondants
        """
        if not source_path:
            return None
        normalized = source_path.strip().lstrip("/")
        return normalized or None

    @staticmethod
    def derive_repo_path(source_path: Optional[str]) -> Optional[str]:
        """
        Dérive un chemin relatif au dépôt Git quand le source_path commence par 'repo/'.

        Ex: 'repo/MCO/1.Incidents/x/report.md' → 'MCO/1.Incidents/x/report.md'
        Sinon (pas de préfixe 'repo/', ou None) → None.

        S'appuie sur le source_path normalisé (cohérence avec la clé métier).
        """
        norm = GraphService.normalize_source_path(source_path)
        if norm and norm.startswith("repo/"):
            return norm[len("repo/"):] or None
        return None

    async def ensure_document_lookup_index(self) -> None:
        """Attempt the optional lookup index within its own small budget."""

        timeout_seconds = min(
            2.0,
            max(0.1, self._schema_query_timeout_seconds / 10),
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self.session() as session:
                    result = await session.run(
                        Query(
                            """
                        CREATE INDEX document_memory_id_id IF NOT EXISTS
                        FOR (d:Document) ON (d.memory_id, d.id)
                            """,
                            timeout=timeout_seconds,
                        )
                    )
                    await result.consume()
        except Exception:
            print(
                "⚠️ [Graph] Index (memory_id, id) was not created",
                file=sys.stderr,
            )

    async def _ensure_document_migration_marker_constraint_locked(self) -> None:
        """Require uniqueness for the durable global migration marker."""

        try:
            async with self.session() as session:
                await self._run_consumed(
                    session,
                    Query(
                        """
                    CREATE CONSTRAINT hivemind_schema_migration_id_unique
                    IF NOT EXISTS
                    FOR (m:HivemindSchemaMigration) REQUIRE m.id IS UNIQUE
                        """,
                        timeout=self._schema_query_timeout_seconds,
                    ),
                )
            await self._verify_node_uniqueness_constraint_locked(
                label="HivemindSchemaMigration",
                properties=("id",),
            )
        except Exception:
            print(
                "⚠️ [Graph] Document migration constraint was NOT verified",
                file=sys.stderr,
            )
            raise DocumentSchemaUnavailable() from None

    async def _verify_node_uniqueness_constraint_locked(
        self,
        *,
        label: str,
        properties: tuple[str, ...],
    ) -> None:
        """Verify the consumed DDL left the exact semantic node constraint.

        ``IF NOT EXISTS`` is also a no-op when an unrelated constraint or index
        already owns the requested name, or when an equivalent constraint has a
        legacy name. Consuming the DDL alone therefore cannot publish a schema
        invariant; read the catalog and compare every semantic field. The name
        is deliberately not authoritative when an exact equivalent constraint
        already enforces the invariant.
        """

        try:
            async with self.session() as session:
                result = await session.run(
                    Query(
                        """
                    SHOW CONSTRAINTS
                    YIELD name, type, entityType, labelsOrTypes, properties
                    WHERE entityType = 'NODE'
                      AND type IN ['UNIQUENESS', 'NODE_PROPERTY_UNIQUENESS']
                      AND labelsOrTypes = $labels_or_types
                      AND properties = $constraint_properties
                    RETURN name, type, entityType, labelsOrTypes, properties
                    LIMIT 2
                        """,
                        timeout=self._schema_query_timeout_seconds,
                    ),
                    labels_or_types=[label],
                    constraint_properties=list(properties),
                )
                records = [record async for record in result]
                await result.consume()
        except Exception:
            raise DocumentSchemaUnavailable() from None

        record = records[0] if len(records) == 1 else None
        try:
            actual = {
                "name": record["name"],
                "type": record["type"],
                "entity_type": record["entityType"],
                "labels_or_types": record["labelsOrTypes"],
                "properties": record["properties"],
            }
        except Exception:
            raise DocumentSchemaUnavailable() from None
        if (
            type(actual["name"]) is not str
            or not actual["name"]
            or actual["type"]
            not in {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS"}
            or actual["entity_type"] != "NODE"
            or type(actual["labels_or_types"]) is not list
            or actual["labels_or_types"] != [label]
            or type(actual["properties"]) is not list
            or actual["properties"] != list(properties)
        ):
            raise DocumentSchemaUnavailable()

    async def _read_document_migration_marker_locked(self) -> bool:
        """Consume and validate the versioned completion marker, if present."""

        try:
            async with self.session() as session:
                result = await session.run(
                    Query(
                        """
                    MATCH (m:HivemindSchemaMigration {id: $migration_id})
                    RETURN properties(m) AS marker
                    LIMIT 2
                        """,
                        timeout=self._schema_query_timeout_seconds,
                    ),
                    migration_id=_DOCUMENT_SCHEMA_MIGRATION_ID,
                )
                records = [record async for record in result]
                await result.consume()
        except Exception:
            print(
                "⚠️ [Graph] Document migration marker was NOT verified",
                file=sys.stderr,
            )
            raise DocumentSchemaUnavailable() from None

        if not records:
            return False
        marker = None
        if len(records) == 1:
            try:
                marker = records[0]["marker"]
            except Exception:
                pass
        if (
            len(records) != 1
            or type(marker) is not dict
            or marker.get("id") != _DOCUMENT_SCHEMA_MIGRATION_ID
            or type(marker.get("version")) is not int
            or marker["version"] != _DOCUMENT_SCHEMA_MIGRATION_VERSION
        ):
            print(
                "⚠️ [Graph] Invalid document migration marker",
                file=sys.stderr,
            )
            raise DocumentSchemaUnavailable()
        return True

    async def _write_document_migration_marker_locked(self) -> None:
        """Publish durable completion only after migration and DDL succeeded."""

        try:
            async with self.session() as session:
                result = await session.run(
                    Query(
                        """
                    MERGE (m:HivemindSchemaMigration {id: $migration_id})
                    ON CREATE SET m.version = $migration_version
                    RETURN m.id AS migration_id, m.version AS version
                        """,
                        timeout=self._schema_query_timeout_seconds,
                    ),
                    migration_id=_DOCUMENT_SCHEMA_MIGRATION_ID,
                    migration_version=_DOCUMENT_SCHEMA_MIGRATION_VERSION,
                )
                record = await result.single()
                await result.consume()
        except Exception:
            print(
                "⚠️ [Graph] Document migration marker was NOT published",
                file=sys.stderr,
            )
            raise DocumentSchemaUnavailable() from None

        marker_id = None
        marker_version = None
        if record is not None:
            try:
                marker_id = record["migration_id"]
                marker_version = record["version"]
            except Exception:
                pass
        if (
            marker_id != _DOCUMENT_SCHEMA_MIGRATION_ID
            or type(marker_version) is not int
            or marker_version != _DOCUMENT_SCHEMA_MIGRATION_VERSION
        ):
            print(
                "⚠️ [Graph] Invalid document migration marker",
                file=sys.stderr,
            )
            raise DocumentSchemaUnavailable()
        self._doc_migration_marker_ready = True

    async def _ensure_document_constraint_locked(self) -> None:
        """Require the global DDL after startup data migration."""

        if self._doc_constraints_ready or not self._doc_source_normalization_done:
            return
        try:
            async with self.session() as session:
                result = await session.run(
                    Query(
                        """
                    CREATE CONSTRAINT document_source_path_unique IF NOT EXISTS
                    FOR (d:Document) REQUIRE (d.memory_id, d.source_path) IS UNIQUE
                        """,
                        timeout=self._schema_query_timeout_seconds,
                    )
                )
                await result.consume()
            await self._verify_node_uniqueness_constraint_locked(
                label="Document",
                properties=("memory_id", "source_path"),
            )
        except Exception:
            print("⚠️ [Graph] source_path constraint was NOT created (legacy duplicates may require repair)", file=sys.stderr)
            print("   ⚠️ The database does NOT guarantee uniqueness until the constraint exists.", file=sys.stderr)
            print("   → Resolve duplicate (memory_id, source_path) pairs, then restart to enable the constraint.", file=sys.stderr)
            raise DocumentSchemaUnavailable() from None
        else:
            # Neo4j may defer data/constraint errors until PULL/commit. Publish
            # readiness only after the result was explicitly consumed; the
            # driver's implicit session close can suppress those failures.
            self._doc_constraints_ready = True
            print("🔒 [Graph] Verified unique (memory_id, source_path) constraint", file=sys.stderr)

    async def initialize_document_schema(self) -> None:
        """Run the one global legacy-data migration during ASGI startup.

        The data query intentionally has no per-memory namespace. It therefore
        belongs to the process startup boundary, before request admission, and
        never to an ordinary ``add_document(memory_id)`` call. Initialization
        fails closed before request admission when normalization or the
        uniqueness constraint cannot be verified. Legacy non-empty duplicates
        remain untouched and require explicit repair.
        """

        async with self._doc_constraints_lock:
            if self._document_schema_is_ready():
                return
            await self._ensure_document_migration_marker_constraint_locked()
            marker_ready = await self._read_document_migration_marker_locked()
            if marker_ready:
                self._doc_source_normalization_done = True
                self._doc_migration_marker_ready = True
            if not self._doc_source_normalization_done:
                try:
                    async with self.session() as session:
                        while True:
                            result = await session.run(
                                Query(
                                    """
                                MATCH (d:Document)
                                WHERE d.source_path = ''
                                WITH d LIMIT $batch_size
                                SET d.source_path = null
                                RETURN count(d) AS normalized
                                    """,
                                    timeout=self._schema_query_timeout_seconds,
                                ),
                                batch_size=_DOCUMENT_SOURCE_NORMALIZATION_BATCH_SIZE,
                            )
                            record = await result.single()
                            await result.consume()
                            normalized = (
                                record["normalized"] if record is not None else None
                            )
                            if (
                                type(normalized) is not int
                                or normalized < 0
                                or normalized
                                > _DOCUMENT_SOURCE_NORMALIZATION_BATCH_SIZE
                            ):
                                raise DocumentSchemaUnavailable()
                            if normalized == 0:
                                break
                except Exception:
                    print(
                        "⚠️ [Graph] source_path normalization was NOT verified",
                        file=sys.stderr,
                    )
                    raise DocumentSchemaUnavailable() from None
                else:
                    # Every bounded write and its terminating empty batch were
                    # explicitly consumed before this process flag is visible.
                    self._doc_source_normalization_done = True
            await self._ensure_document_constraint_locked()
            if not self._doc_migration_marker_ready:
                await self._write_document_migration_marker_locked()

    async def ensure_document_constraints(self) -> None:
        """Retry admission-safe schema DDL, never the global data migration."""

        if self._document_schema_is_ready():
            return
        # Acquire even when normalization is not yet marked done: a request
        # racing startup must wait for the same global owner, then re-check.
        async with self._doc_constraints_lock:
            if self._document_schema_is_ready():
                return
            if (
                not self._doc_source_normalization_done
                or not self._doc_migration_marker_ready
            ):
                raise DocumentSchemaUnavailable()
            await self._ensure_document_constraint_locked()

    @_guard_graph_mutation
    async def add_document(
        self,
        memory_id: str,
        doc_id: str,
        uri: str,
        filename: str,
        doc_hash: str,
        metadata: Optional[Dict[str, Any]] = None,
        source_path: Optional[str] = None,
        source_modified_at: Optional[str] = None,
        size_bytes: int = 0,
        text_length: int = 0,
        content_type: str = "",
        ingestion_status: str = "running",
        last_ingest_job_id: Optional[str] = None,
        chunk_count: int = 0,
    ) -> Document:
        """
        Ajoute un document au graphe avec métadonnées enrichies.

        Args:
            memory_id: ID de la mémoire
            doc_id: UUID du document
            uri: URI S3 du document
            filename: Nom du fichier
            doc_hash: SHA-256 du contenu
            metadata: Métadonnées custom (dict libre, sérialisé en JSON)
            source_path: Chemin complet d'origine du fichier (ex: "legal/contracts/CGA.pdf")
            source_modified_at: Date de dernière modification du fichier source (ISO 8601)
            size_bytes: Taille du fichier en bytes
            text_length: Longueur du texte extrait en caractères
            content_type: Extension/type du fichier (ex: "pdf", "docx")
            ingestion_status: État d'ingestion durable ("running" → "succeeded" après Qdrant)
            last_ingest_job_id: ID du job d'ingestion asynchrone (None pour l'ingestion synchrone)
            chunk_count: Nombre de chunks vectorisés (finalisé après Qdrant)
        """
        import json

        # Retry only admission-safe DDL. The global legacy-data migration is a
        # startup hook and can never be triggered by this per-memory operation.
        if not self._document_schema_is_ready():
            await self.ensure_document_constraints()

        async with self.session() as session:
            # Neo4j n'accepte que les types primitifs, convertir metadata en JSON string
            metadata_json = json.dumps(metadata) if metadata else "{}"
            # source_path normalisé : null si absent (compatibilité contrainte d'unicité)
            norm_source_path = self.normalize_source_path(source_path)

            result = await session.run(
                """
                CREATE (d:Document {
                    id: $doc_id,
                    memory_id: $memory_id,
                    uri: $uri,
                    filename: $filename,
                    hash: $hash,
                    ingested_at: datetime(),
                    metadata_json: $metadata_json,
                    source_modified_at: $source_modified_at,
                    size_bytes: $size_bytes,
                    text_length: $text_length,
                    content_type: $content_type,
                    ingestion_status: $ingestion_status,
                    last_ingest_job_id: $last_ingest_job_id,
                    chunk_count: $chunk_count
                })
                SET d.source_path = $source_path
                RETURN d
                """,
                doc_id=doc_id,
                memory_id=memory_id,
                uri=uri,
                filename=filename,
                hash=doc_hash,
                metadata_json=metadata_json,
                source_path=norm_source_path,
                source_modified_at=source_modified_at or "",
                size_bytes=size_bytes,
                text_length=text_length,
                content_type=content_type,
                ingestion_status=ingestion_status,
                last_ingest_job_id=last_ingest_job_id,
                chunk_count=chunk_count,
            )

            record = await result.single()
            node = record["d"]

            print(f"📄 [Graph] Document added: {filename} ({doc_id}) [status={ingestion_status}]", file=sys.stderr)

            return Document(
                id=doc_id,
                memory_id=memory_id,
                uri=uri,
                filename=filename,
                hash=doc_hash,
                ingested_at=node["ingested_at"].to_native(),
                metadata=DocumentMetadata(
                    filename=filename,
                    custom=metadata or {}
                )
            )

    @_guard_graph_mutation
    async def update_document_ingestion(
        self,
        memory_id: str,
        doc_id: str,
        ingestion_status: str,
        chunk_count: Optional[int] = None,
        last_ingest_job_id: Optional[str] = None,
    ) -> bool:
        """
        Met à jour l'état d'ingestion durable d'un document.

        Appelé en fin de pipeline pour marquer `ingestion_status = "succeeded"`
        UNIQUEMENT après succès de toutes les étapes (Neo4j + Qdrant). C'est ce
        marqueur durable qui empêche un faux `skipped` sur ingestion partielle.
        """
        async with self.session() as session:
            result = await session.run(
                """
                MATCH (d:Document {id: $doc_id, memory_id: $memory_id})
                SET d.ingestion_status = $ingestion_status
                SET d.chunk_count = CASE WHEN $chunk_count IS NULL THEN d.chunk_count ELSE $chunk_count END
                SET d.last_ingest_job_id = CASE WHEN $last_ingest_job_id IS NULL THEN d.last_ingest_job_id ELSE $last_ingest_job_id END
                RETURN count(d) as updated
                """,
                doc_id=doc_id,
                memory_id=memory_id,
                ingestion_status=ingestion_status,
                chunk_count=chunk_count,
                last_ingest_job_id=last_ingest_job_id,
            )
            record = await result.single()
            return bool(record and record["updated"] > 0)

    async def get_document_by_source_path(self, memory_id: str, source_path: str) -> Optional[Dict[str, Any]]:
        """
        Trouve un document par son source_path (clé métier stable).

        Retourne un dict avec hash, ingestion_status et last_ingest_job_id pour
        permettre la logique d'idempotence (skipped / changed / replace).
        """
        norm = self.normalize_source_path(source_path)
        if not norm:
            return None
        async with self.session() as session:
            result = await session.run(
                """
                MATCH (d:Document {memory_id: $memory_id, source_path: $source_path})
                RETURN d.id as id, d.filename as filename, d.uri as uri,
                       d.hash as hash, d.ingested_at as ingested_at,
                       d.source_path as source_path,
                       d.ingestion_status as ingestion_status,
                       d.last_ingest_job_id as last_ingest_job_id,
                       d.chunk_count as chunk_count
                ORDER BY d.ingested_at DESC
                LIMIT 1
                """,
                memory_id=memory_id,
                source_path=norm,
            )
            record = await result.single()
            if not record:
                return None
            return {
                "id": record["id"],
                "filename": record["filename"],
                "uri": record["uri"],
                "hash": record["hash"],
                "ingested_at": record["ingested_at"].isoformat() if record["ingested_at"] else None,
                "source_path": record["source_path"],
                "ingestion_status": record["ingestion_status"] or "unknown",
                "last_ingest_job_id": record["last_ingest_job_id"],
                "chunk_count": record["chunk_count"] or 0,
            }
    
    async def get_document_by_hash(self, memory_id: str, doc_hash: str) -> Optional[Document]:
        """Trouve un document par son hash."""
        async with self.session() as session:
            result = await session.run(
                """
                MATCH (d:Document {memory_id: $memory_id, hash: $hash})
                RETURN d
                """,
                memory_id=memory_id,
                hash=doc_hash
            )
            
            record = await result.single()
            if not record:
                return None
            
            node = record["d"]
            return Document(
                id=node["id"],
                memory_id=node["memory_id"],
                uri=node["uri"],
                filename=node["filename"],
                hash=node["hash"],
                ingested_at=node["ingested_at"].to_native(),
                metadata=DocumentMetadata(
                    filename=node["filename"],
                    custom=node.get("metadata", {})
                )
            )
    
    async def get_document(self, memory_id: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """Récupère les informations complètes d'un document (métadonnées enrichies)."""
        async with self.session() as session:
            result = await session.run(
                """
                MATCH (d:Document {id: $doc_id, memory_id: $memory_id})
                RETURN d.id as id, d.filename as filename, d.uri as uri,
                       d.hash as hash, d.ingested_at as ingested_at,
                       d.source_path as source_path,
                       d.source_modified_at as source_modified_at,
                       d.size_bytes as size_bytes,
                       d.text_length as text_length,
                       d.content_type as content_type,
                       d.ingestion_status as ingestion_status,
                       d.last_ingest_job_id as last_ingest_job_id,
                       d.chunk_count as chunk_count
                """,
                doc_id=doc_id,
                memory_id=memory_id
            )
            record = await result.single()
            if record:
                # source_path renvoyé NORMALISÉ (contrat canonique inter-outils) + repo_path dérivé
                sp = self.normalize_source_path(record["source_path"])
                return {
                    "id": record["id"],
                    "filename": record["filename"],
                    "uri": record["uri"],
                    "hash": record["hash"],
                    "sha256": record["hash"],  # alias métier
                    "ingested_at": record["ingested_at"],
                    "source_path": sp,
                    "repo_path": self.derive_repo_path(sp),
                    "source_modified_at": record["source_modified_at"] or None,
                    "size_bytes": record["size_bytes"] or 0,
                    "text_length": record["text_length"] or 0,
                    "content_type": record["content_type"] or None,
                    "ingestion_status": record.get("ingestion_status") or "unknown",
                    "last_ingest_job_id": record.get("last_ingest_job_id"),
                    "chunk_count": record.get("chunk_count") or 0,
                }
            return None

    async def get_documents_meta(self, memory_id: str, doc_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Métadonnées de plusieurs documents en une seule requête (clé du dict = doc_id).

        Utilisé par memory_query pour injecter source_path/repo_path/sha256/… dans
        les rag_chunks et source_documents par jointure sur doc_id, sans toucher au
        payload Qdrant (rétroactif sur l'index déjà ingéré).

        Les doc_ids absents sont simplement omis du dict (pas d'erreur).
        S'appuie sur l'index (memory_id, id) créé par ensure_document_constraints.
        """
        if not doc_ids:
            return {}
        async with self.session() as session:
            result = await session.run(
                """
                MATCH (d:Document {memory_id: $memory_id})
                WHERE d.id IN $doc_ids
                RETURN d.id as id, d.filename as filename, d.uri as uri,
                       d.hash as hash, d.source_path as source_path,
                       d.source_modified_at as source_modified_at,
                       d.ingested_at as ingested_at,
                       d.ingestion_status as ingestion_status,
                       d.last_ingest_job_id as last_ingest_job_id,
                       d.chunk_count as chunk_count,
                       d.size_bytes as size_bytes,
                       d.text_length as text_length,
                       d.content_type as content_type
                """,
                memory_id=memory_id,
                doc_ids=list(doc_ids),
            )
            out: Dict[str, Dict[str, Any]] = {}
            async for r in result:
                sp = self.normalize_source_path(r["source_path"])
                out[r["id"]] = {
                    "id": r["id"],
                    "filename": r["filename"],
                    "uri": r["uri"],
                    "hash": r["hash"],
                    "sha256": r["hash"],  # alias métier
                    "source_path": sp,
                    "repo_path": self.derive_repo_path(sp),
                    "source_modified_at": r["source_modified_at"] or None,  # string → pas de .isoformat()
                    "ingested_at": _iso(r["ingested_at"]),  # DateTime Neo4j
                    "ingestion_status": r["ingestion_status"] or "unknown",
                    "last_ingest_job_id": r["last_ingest_job_id"],
                    "chunk_count": r["chunk_count"] or 0,
                    "size_bytes": r["size_bytes"] or 0,
                    "text_length": r["text_length"] or 0,
                    "content_type": r["content_type"],
                }
            return out

    async def list_reindex_documents(self, memory_id: str) -> List[Dict[str, Any]]:
        """Return the exact retained-source fields used by maintenance reindex.

        Values are intentionally not coerced or defaulted here. The reindex
        boundary validates native types and fails closed on legacy/partial rows.
        """
        async with self.session() as session:
            result = await session.run(
                """
                MATCH (d:Document {memory_id: $memory_id})
                RETURN d.memory_id as memory_id,
                       d.id as document_id,
                       d.filename as filename,
                       d.uri as uri,
                       d.hash as sha256,
                       d.size_bytes as size_bytes,
                       d.ingestion_status as status,
                       d.chunk_count as chunk_count
                LIMIT $limit
                """,
                memory_id=memory_id,
                limit=MAX_REINDEX_SOURCE_DOCUMENTS + 1,
            )
            documents: List[Dict[str, Any]] = []
            async for record in result:
                # The query asks for MAX+1 rows precisely so the first excess
                # row is observable. Refuse it before appending: checking for
                # MAX+1 here would require the driver to violate its own LIMIT
                # and return MAX+2 rows before this local guard fired.
                if len(documents) >= MAX_REINDEX_SOURCE_DOCUMENTS:
                    raise ReindexSourceLimitExceeded(
                        "source inventory limit exceeded"
                    )
                documents.append(
                    {
                        "memory_id": record["memory_id"],
                        "document_id": record["document_id"],
                        "filename": record["filename"],
                        "uri": record["uri"],
                        "sha256": record["sha256"],
                        "size_bytes": record["size_bytes"],
                        "status": record["status"],
                        "chunk_count": record["chunk_count"],
                    }
                )
            return documents

    async def get_reindex_ontology_uri(self, memory_id: str) -> Optional[str]:
        """Return the exact configured ontology object URI without coercion."""
        async with self.session() as session:
            result = await session.run(
                """
                MATCH (m:Memory {id: $memory_id})
                RETURN m.ontology_uri as ontology_uri
                """,
                memory_id=memory_id,
            )
            record = await result.single()
            if record is None:
                return None
            return record["ontology_uri"]

    @_guard_graph_mutation
    async def delete_document(self, memory_id: str, doc_id: str) -> Dict[str, Any]:
        """
        Supprime un document et nettoie le graphe.
        
        Supprime :
        1. Le document lui-même
        2. Les relations MENTIONS du document
        3. Les entités orphelines (non mentionnées par d'autres documents)
        4. Les relations RELATED_TO impliquant des entités orphelines
        """
        async with self.session() as session:
            # D'abord, récupérer les entités mentionnées UNIQUEMENT par ce document
            # (celles qui deviendront orphelines après suppression)
            orphan_result = await session.run(
                """
                MATCH (d:Document {id: $doc_id, memory_id: $memory_id})-[:MENTIONS]->(e:Entity)
                WHERE NOT exists {
                    MATCH (other:Document)-[:MENTIONS]->(e)
                    WHERE other.id <> $doc_id
                }
                RETURN collect(e.name) as orphan_names
                """,
                doc_id=doc_id,
                memory_id=memory_id
            )
            orphan_record = await orphan_result.single()
            orphan_names = orphan_record["orphan_names"] if orphan_record else []
            
            # Compter les relations MENTIONS qui vont être supprimées
            count_result = await session.run(
                """
                MATCH (d:Document {id: $doc_id, memory_id: $memory_id})-[r:MENTIONS]->()
                RETURN count(r) as relations
                """,
                doc_id=doc_id,
                memory_id=memory_id
            )
            count_record = await count_result.single()
            mentions_count = count_record["relations"] if count_record else 0
            
            # Supprimer les entités orphelines et leurs relations RELATED_TO
            entities_deleted = 0
            if orphan_names:
                delete_orphans = await session.run(
                    """
                    MATCH (e:Entity {memory_id: $memory_id})
                    WHERE e.name IN $orphan_names
                    DETACH DELETE e
                    RETURN count(e) as deleted
                    """,
                    memory_id=memory_id,
                    orphan_names=orphan_names
                )
                orphan_deleted = await delete_orphans.single()
                entities_deleted = orphan_deleted["deleted"] if orphan_deleted else 0
            
            # Puis supprimer le document lui-même
            result = await session.run(
                """
                MATCH (d:Document {id: $doc_id, memory_id: $memory_id})
                DETACH DELETE d
                RETURN count(d) as deleted
                """,
                doc_id=doc_id,
                memory_id=memory_id
            )

            record = await result.single()
            deleted = record["deleted"] > 0 if record else False
            
            if deleted:
                print(f"🗑️ [Graph] Document deleted: {doc_id}", file=sys.stderr)
                print(f"   Orphaned entities deleted: {entities_deleted}", file=sys.stderr)
                print(f"   MENTIONS relations deleted: {mentions_count}", file=sys.stderr)
            
            return {
                "deleted": deleted,
                "relations_deleted": mentions_count if deleted else 0,
                "entities_deleted": entities_deleted if deleted else 0
            }
    
    # =========================================================================
    # Gestion des Entités et Relations
    # =========================================================================
    
    @_guard_graph_mutation
    async def add_entities_and_relations(
        self,
        memory_id: str,
        doc_id: str,
        extraction: ExtractionResult
    ) -> Dict[str, int]:
        """
        Ajoute les entités et relations extraites au graphe.
        
        Fusion multi-documents intelligente :
        - MERGE pour éviter les doublons d'entités (clé: name + memory_id)
        - Descriptions ENRICHIES (concaténation au lieu d'écrasement)
        - Source documents trackés sur chaque entité (propriété source_docs)
        - Relations ENRICHIES au MATCH (description + poids cumulatif)
        - Lien MENTIONS entre document et entité avec compteur
        """
        entities_created = 0
        entities_merged = 0
        relations_created = 0
        relations_merged = 0
        
        async with self.session() as session:
            # =================================================================
            # Phase 1 : Ajouter/Merger les entités
            # =================================================================
            for entity in extraction.entities:
                result = await session.run(
                    """
                    MERGE (e:Entity {name: $name, memory_id: $memory_id})
                    ON CREATE SET 
                        e.type = $type,
                        e.description = $description,
                        e.source_docs = [$doc_id],
                        e.created_at = datetime(),
                        e.updated_at = datetime(),
                        e.mention_count = 1
                    ON MATCH SET 
                        e.mention_count = e.mention_count + 1,
                        e.updated_at = datetime(),
                        e.source_docs = CASE 
                            WHEN NOT $doc_id IN coalesce(e.source_docs, []) 
                            THEN coalesce(e.source_docs, []) + $doc_id
                            ELSE e.source_docs 
                        END,
                        e.description = CASE 
                            WHEN $description IS NULL THEN e.description
                            WHEN e.description IS NULL THEN $description
                            WHEN e.description CONTAINS $description THEN e.description
                            ELSE e.description + ' | ' + $description
                        END,
                        e.type = CASE 
                            WHEN e.type = 'Unknown' OR e.type = 'Other' THEN $type
                            ELSE e.type
                        END
                    WITH e,
                         CASE WHEN e.created_at = e.updated_at THEN true ELSE false END as was_created
                    MATCH (d:Document {id: $doc_id})
                    MERGE (d)-[r:MENTIONS]->(e)
                    ON CREATE SET r.count = 1
                    ON MATCH SET r.count = r.count + 1
                    RETURN was_created
                    """,
                    name=entity.name,
                    memory_id=memory_id,
                    type=entity.type,
                    description=entity.description,
                    doc_id=doc_id
                )
                record = await result.single()
                if record and record["was_created"]:
                    entities_created += 1
                else:
                    entities_merged += 1
            
            # =================================================================
            # Phase 2 : Ajouter/Enrichir les relations entre entités
            # =================================================================
            for relation in extraction.relations:
                result = await session.run(
                    """
                    MATCH (from:Entity {name: $from_name, memory_id: $memory_id})
                    MATCH (to:Entity {name: $to_name, memory_id: $memory_id})
                    MERGE (from)-[r:RELATED_TO {type: $rel_type}]->(to)
                    ON CREATE SET 
                        r.description = $description,
                        r.weight = $weight,
                        r.source_doc = $doc_id,
                        r.created_at = datetime()
                    ON MATCH SET
                        r.weight = r.weight + coalesce($weight, 1.0),
                        r.description = CASE 
                            WHEN $description IS NULL THEN r.description
                            WHEN r.description IS NULL THEN $description
                            WHEN r.description CONTAINS $description THEN r.description
                            ELSE r.description + ' | ' + $description
                        END
                    RETURN r.created_at = datetime() as was_created
                    """,
                    from_name=relation.from_entity,
                    to_name=relation.to_entity,
                    memory_id=memory_id,
                    rel_type=relation.type,
                    description=relation.description,
                    weight=relation.weight,
                    doc_id=doc_id
                )
                record = await result.single()
                if record:
                    relations_created += 1
                else:
                    relations_merged += 1
        
        total_entities = entities_created + entities_merged
        total_relations = relations_created + relations_merged
        print(f"🔗 [Graph] Entities: {entities_created} new + {entities_merged} merged = {total_entities}", file=sys.stderr)
        print(f"🔗 [Graph] Relations: {relations_created} new + {relations_merged} merged = {total_relations}", file=sys.stderr)
        
        return {
            "entities_created": entities_created,
            "entities_merged": entities_merged,
            "relations_created": relations_created,
            "relations_merged": relations_merged
        }
    
    # =========================================================================
    # Recherche et Contexte
    # =========================================================================
    
    async def ensure_fulltext_index(self):
        """
        Crée l'index fulltext pour la recherche d'entités (accent-insensitive).
        
        Utilise l'analyzer 'standard-folding' qui fait:
        - Tokenisation standard (découpe en mots)
        - Lowercase (minuscules)
        - ASCII folding (suppression des accents: é→e, ç→c, ü→u, etc.)
        
        Idempotent: ne fait rien si l'index existe déjà.
        L'index couvre name, description et type de toutes les :Entity.
        """
        try:
            async with self.session() as session:
                await self._run_consumed(
                    session,
                    Query(
                        """
                        CREATE FULLTEXT INDEX entity_fulltext IF NOT EXISTS
                        FOR (n:Entity) ON EACH [n.name, n.description, n.type]
                        OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}}
                        """,
                        timeout=self._schema_query_timeout_seconds,
                    ),
                )
                self._fulltext_index_ready = True
                print("🔍 [Graph] Verified full-text index 'entity_fulltext' (standard-folding)", file=sys.stderr)
        except Exception:
            print(
                "⚠️ [Graph] Could not create the full-text index",
                file=sys.stderr,
            )
            print("   Search will use degraded CONTAINS mode", file=sys.stderr)
    
    @staticmethod
    def _escape_lucene(text: str) -> str:
        """
        Échappe les caractères spéciaux de la syntaxe Lucene.
        
        Lucene utilise ces caractères comme opérateurs:
        + - && || ! ( ) { } [ ] ^ " ~ * ? : \\ /
        On les préfixe avec \\ pour les traiter comme du texte littéral.
        """
        special_chars = set('+-&|!(){}[]^"~*?:\\/') 
        result = []
        for char in text:
            if char in special_chars:
                result.append('\\')
            result.append(char)
        return ''.join(result)
    
    async def _search_fulltext(
        self,
        memory_id: str,
        tokens: List[str],
        limit: int
    ) -> List[Dict[str, Any]]:
        """
        Recherche via l'index fulltext Neo4j (accent-insensitive, scoring Lucene).
        
        L'analyzer 'standard-folding' normalise automatiquement les accents
        DANS L'INDEX et DANS LA REQUÊTE. Donc:
        - "réversibilité" matche "Réversibilité", "REVERSIBILITE", "reversibilite"
        - "resiliation" matche "Résiliation", "RÉSILIATION", etc.
        
        Retourne les entités triées par score de pertinence Lucene.
        """
        try:
            # Construire la requête Lucene: échapper les tokens et joindre avec OR
            escaped_tokens = [self._escape_lucene(t) for t in tokens]
            lucene_query = " OR ".join(escaped_tokens)
            
            async with self.session() as session:
                result = await session.run(
                    """
                    CALL db.index.fulltext.queryNodes('entity_fulltext', $search_text)
                    YIELD node, score
                    WHERE node.memory_id = $memory_id
                    RETURN node.name as name, node.type as type,
                           node.description as description,
                           node.mention_count as mentions, score
                    ORDER BY score DESC
                    LIMIT $limit
                    """,
                    search_text=lucene_query,
                    memory_id=memory_id,
                    limit=limit
                )
                
                entities = []
                async for record in result:
                    entities.append({
                        "name": record["name"],
                        "type": record["type"],
                        "description": record["description"],
                        "mentions": record["mentions"],
                        "score": round(record["score"], 4)
                    })
                return entities
        except Exception as e:
            print(f"⚠️ [Search] Full-text search error: {e}", file=sys.stderr)
            return []
    
    async def _search_contains(
        self,
        memory_id: str,
        raw_tokens: List[str],
        normalized_tokens: List[str],
        limit: int
    ) -> List[Dict[str, Any]]:
        """
        Recherche via CONTAINS (fallback si fulltext indisponible).
        
        Envoie les deux formes de tokens (avec et sans accents) pour maximiser
        les chances de match avec toLower() de Neo4j (qui conserve les accents).
        
        Stratégie: AND d'abord (tous les concepts), puis OR (au moins un concept).
        """
        # Combiner raw (avec accents) + normalized (sans accents) pour couvrir les 2 cas
        all_tokens = list(set(raw_tokens + normalized_tokens))
        
        async with self.session() as session:
            # Recherche avec ANY (au moins un token matche)
            # On utilise ANY plutôt que ALL car les tokens contiennent les 2 formes
            # de chaque mot (avec/sans accents), ALL serait trop restrictif
            result = await session.run(
                """
                MATCH (e:Entity {memory_id: $memory_id})
                WHERE ANY(token IN $tokens WHERE 
                    toLower(e.name) CONTAINS token 
                    OR toLower(e.description) CONTAINS token
                    OR toLower(e.type) CONTAINS token
                )
                RETURN e.name as name, e.type as type, e.description as description,
                       e.mention_count as mentions
                ORDER BY e.mention_count DESC
                LIMIT $limit
                """,
                memory_id=memory_id,
                tokens=all_tokens,
                limit=limit
            )
            
            entities = []
            async for record in result:
                entities.append({
                    "name": record["name"],
                    "type": record["type"],
                    "description": record["description"],
                    "mentions": record["mentions"]
                })
            
            return entities
    
    async def search_entities(
        self,
        memory_id: str,
        search_query: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Recherche des entités par nom, description et TYPE.
        
        Stratégie en 2 niveaux:
        1. Index fulltext Lucene (accent-insensitive, scoring par pertinence)
        2. Fallback CONTAINS (tokens raw + normalisés, si fulltext indisponible)
        
        Tokenise la requête, retire les stop words français, et recherche.
        Ex: "réversibilité" → trouve "Réversibilité", "REVERSIBILITE", etc.
        Ex: "Cloud Temple" → trouve "Cloud Temple SAS", "Contrat Cloud Temple", etc.
        Ex: "certification" → trouve toutes les entités de type Certification
        """
        import re
        import unicodedata
        
        # Mots vides français à ignorer
        STOP_WORDS = {
            'les', 'des', 'une', 'uns', 'aux', 'par', 'pour', 'dans',
            'sur', 'avec', 'sans', 'sous', 'entre', 'vers', 'chez',
            'que', 'qui', 'quoi', 'dont', 'est', 'sont', 'être',
            'avoir', 'fait', 'faire', 'peut', 'tout', 'tous', 'cette',
            'ces', 'son', 'ses', 'leur', 'nos', 'vos', 'plus', 'moins',
            'aussi', 'très', 'bien', 'mais', 'comme', 'donc', 'car',
            'quel', 'quelle', 'quels', 'quelles', 'contient', 'corpus',
        }
        
        def _normalize(text: str) -> str:
            """Retire accents et ponctuation pour normaliser."""
            text = re.sub(r'[^\w\s]', '', text)
            nfkd = unicodedata.normalize('NFKD', text)
            return ''.join(c for c in nfkd if not unicodedata.combining(c))
        
        # Tokeniser la requête (mots individuels, sans stop words, sans ponctuation)
        raw_tokens_all = re.findall(r'[a-zA-ZÀ-ÿ]+', search_query.lower())
        
        # Tokens significatifs (> 2 chars, pas de stop words)
        meaningful_raw = [t for t in raw_tokens_all if len(t) > 2 and t not in STOP_WORDS]
        meaningful_normalized = [_normalize(t) for t in meaningful_raw]
        
        print(f"🔤 [Search] Tokenization: '{search_query}' → raw={meaningful_raw}, normalized={meaningful_normalized}", file=sys.stderr)
        
        if not meaningful_raw:
            print("⚠️ [Search] No significant token found → empty result", file=sys.stderr)
            return []
        
        # === Stratégie 1: Fulltext index (accent-insensitive, scoring Lucene) ===
        # Lazy init de l'index au premier appel
        if not self._fulltext_index_ready:
            await self.ensure_fulltext_index()
        
        entities = await self._search_fulltext(memory_id, meaningful_raw, limit)
        
        if entities:
            top3 = ", ".join(
                e["name"] + "=" + str(e.get("score", 0))
                for e in entities[:3]
            )
            print(f"✅ [Search] Full-text: {len(entities)} results (scores: {top3}...)",
                  file=sys.stderr)
            return entities
        
        # === Stratégie 2: CONTAINS fallback (raw + normalized tokens) ===
        print("🔄 [Search] Full-text: 0 results → CONTAINS fallback", file=sys.stderr)
        entities = await self._search_contains(memory_id, meaningful_raw, meaningful_normalized, limit)
        
        print(f"{'✅' if entities else '❌'} [Search] CONTAINS fallback: {len(entities)} results "
              f"(tokens: {list(set(meaningful_raw + meaningful_normalized))})", file=sys.stderr)
        return entities
    
    async def get_entity_context(
        self,
        memory_id: str,
        entity_name: str,
        depth: int = 1
    ) -> GraphContext:
        """
        Récupère le contexte complet d'une entité.
        
        Retourne:
        - L'entité elle-même
        - Les documents qui la mentionnent
        - Les entités reliées (jusqu'à depth niveaux)
        - Les relations
        
        Note: Utilise une recherche tolérante si le nom exact n'est pas trouvé.
        """
        async with self.session() as session:
            # Essayer d'abord avec le nom exact
            result = await session.run(
                """
                MATCH (e:Entity {name: $name, memory_id: $memory_id})
                OPTIONAL MATCH (d:Document)-[:MENTIONS]->(e)
                OPTIONAL MATCH (e)-[r:RELATED_TO]-(other:Entity)
                RETURN e, collect(DISTINCT d) as docs, 
                       collect(DISTINCT {entity: other, relation: r}) as related
                """,
                name=entity_name,
                memory_id=memory_id
            )
            
            record = await result.single()
            
            # Si pas trouvé, essayer une recherche tolérante (CONTAINS)
            if not record or not record["e"]:
                result = await session.run(
                    """
                    MATCH (e:Entity {memory_id: $memory_id})
                    WHERE toLower(e.name) CONTAINS toLower($name)
                    OPTIONAL MATCH (d:Document)-[:MENTIONS]->(e)
                    OPTIONAL MATCH (e)-[r:RELATED_TO]-(other:Entity)
                    RETURN e, collect(DISTINCT d) as docs, 
                           collect(DISTINCT {entity: other, relation: r}) as related
                    LIMIT 1
                    """,
                    name=entity_name,
                    memory_id=memory_id
                )
                record = await result.single()
            
            if not record or not record["e"]:
                return GraphContext(
                    entity_name=entity_name,
                    depth=depth,
                    documents=[],
                    related_entities=[],
                    relations=[]
                )
            
            entity = record["e"]
            documents = []
            for d in record["docs"]:
                if not d:
                    continue
                sp = self.normalize_source_path(d.get("source_path"))
                documents.append({
                    "id": d["id"],
                    "filename": d["filename"],
                    "uri": d["uri"],
                    "source_path": sp,
                    "repo_path": self.derive_repo_path(sp),
                    "hash": d.get("hash"),
                    "sha256": d.get("hash"),  # alias métier
                    "ingestion_status": d.get("ingestion_status") or "unknown",
                    "chunk_count": d.get("chunk_count") or 0,
                    "last_ingest_job_id": d.get("last_ingest_job_id"),
                })
            
            related_entities = []
            relations = []
            for item in record["related"]:
                if item["entity"]:
                    related_entities.append({
                        "name": item["entity"]["name"],
                        "type": item["entity"]["type"]
                    })
                if item["relation"]:
                    relations.append({
                        "type": item["relation"]["type"],
                        "description": item["relation"].get("description")
                    })
            
            return GraphContext(
                entity_name=entity_name,
                entity_type=entity.get("type"),
                depth=depth,
                documents=documents,
                related_entities=related_entities,
                relations=relations
            )
    
    # =========================================================================
    # Export du Graphe Complet
    # =========================================================================
    
    async def get_full_graph(self, memory_id: str) -> Dict[str, Any]:
        """
        Récupère le graphe complet d'une mémoire (entités + relations + documents).
        
        Retourne un format adapté à la visualisation :
        - nodes: Liste des entités avec id, name, type, description
        - edges: Liste des relations avec source, target, type, label
        - documents: Liste des documents avec id, filename, uri S3
        
        Compatible avec les libraries de visualisation (vis.js, D3.js, etc.)
        """
        async with self.session() as session:
            # Récupérer toutes les entités
            nodes_result = await session.run(
                """
                MATCH (e:Entity {memory_id: $memory_id})
                RETURN e.name as id, e.name as label, e.type as type, 
                       e.description as description, e.mention_count as mentions,
                       coalesce(e.source_docs, []) as source_docs
                ORDER BY e.mention_count DESC
                """,
                memory_id=memory_id
            )
            
            nodes = []
            node_ids = set()
            async for record in nodes_result:
                node_id = record["id"]
                nodes.append({
                    "id": node_id,
                    "label": record["label"],
                    "type": record["type"] or "Unknown",
                    "description": record["description"] or "",
                    "mentions": record["mentions"] or 1,
                    "source_docs": list(record["source_docs"]),
                    "node_type": "entity"
                })
                node_ids.add(node_id)
            
            # Récupérer tous les documents avec leur URI S3 et métadonnées enrichies
            docs_result = await session.run(
                """
                MATCH (d:Document {memory_id: $memory_id})
                RETURN d.id as id, d.filename as filename, d.uri as uri,
                       d.hash as hash, d.ingested_at as ingested_at,
                       d.source_path as source_path,
                       d.source_modified_at as source_modified_at,
                       d.size_bytes as size_bytes,
                       d.text_length as text_length,
                       d.content_type as content_type,
                       d.ingestion_status as ingestion_status,
                       d.last_ingest_job_id as last_ingest_job_id,
                       d.chunk_count as chunk_count
                ORDER BY d.ingested_at DESC
                """,
                memory_id=memory_id
            )

            documents = []
            doc_ids = set()
            async for record in docs_result:
                doc_id = f"doc:{record['id']}"
                doc_entry = {
                    "id": record["id"],
                    "filename": record["filename"],
                    "uri": record["uri"],  # URI S3 pour récupérer le fichier
                    "hash": record["hash"],
                    "sha256": record["hash"],  # alias explicite (checksum métier)
                    "ingested_at": record["ingested_at"].isoformat() if record["ingested_at"] else None,
                    "ingestion_status": record.get("ingestion_status") or "unknown",
                    "last_ingest_job_id": record.get("last_ingest_job_id"),
                    "chunk_count": record.get("chunk_count") or 0,
                }
                # source_path NORMALISÉ (contrat canonique) + repo_path dérivé.
                # Clés TOUJOURS exposées (None si absent) pour un contrat homogène
                # avec document_get et memory_query (finding Codex #4).
                source_path = self.normalize_source_path(record.get("source_path"))
                doc_entry["source_path"] = source_path
                doc_entry["repo_path"] = self.derive_repo_path(source_path)
                source_modified = record.get("source_modified_at")
                if source_modified:
                    doc_entry["source_modified_at"] = source_modified
                size_bytes = record.get("size_bytes")
                if size_bytes:
                    doc_entry["size_bytes"] = size_bytes
                text_length = record.get("text_length")
                if text_length:
                    doc_entry["text_length"] = text_length
                content_type = record.get("content_type")
                if content_type:
                    doc_entry["content_type"] = content_type
                
                documents.append(doc_entry)
                # Ajouter les documents comme nœuds aussi (pour visualisation)
                nodes.append({
                    "id": doc_id,
                    "label": f"📄 {record['filename']}",
                    "type": "Document",
                    "description": f"URI: {record['uri']}",
                    "mentions": 0,
                    "node_type": "document",
                    "uri": record["uri"],
                    "filename": record["filename"]
                })
                node_ids.add(doc_id)
                doc_ids.add(record["id"])
            
            # Récupérer les relations entité-entité
            edges_result = await session.run(
                """
                MATCH (from:Entity {memory_id: $memory_id})-[r:RELATED_TO]->(to:Entity {memory_id: $memory_id})
                RETURN from.name as source, to.name as target, 
                       r.type as type, r.description as description, r.weight as weight
                """,
                memory_id=memory_id
            )
            
            edges = []
            async for record in edges_result:
                source = record["source"]
                target = record["target"]
                if source in node_ids and target in node_ids:
                    edges.append({
                        "from": source,
                        "to": target,
                        "type": record["type"] or "RELATED_TO",
                        "label": record["type"] or "",
                        "description": record["description"] or "",
                        "weight": record["weight"] or 1.0
                    })
            
            # Récupérer les relations document-entité (MENTIONS)
            mentions_result = await session.run(
                """
                MATCH (d:Document {memory_id: $memory_id})-[r:MENTIONS]->(e:Entity {memory_id: $memory_id})
                RETURN d.id as doc_id, e.name as entity_name, r.count as count
                """,
                memory_id=memory_id
            )
            
            async for record in mentions_result:
                doc_id = f"doc:{record['doc_id']}"
                entity_name = record["entity_name"]
                if doc_id in node_ids and entity_name in node_ids:
                    edges.append({
                        "from": doc_id,
                        "to": entity_name,
                        "type": "MENTIONS",
                        "label": "mentions",
                        "description": f"Mentioned {record['count']} times",
                        "weight": record["count"] or 1
                    })
            
            return {
                "nodes": nodes,
                "edges": edges,
                "documents": documents  # Liste séparée avec URIs S3
            }
    
    # =========================================================================
    # Export / Import (Backup)
    # =========================================================================
    
    async def export_memory_data(self, memory_id: str) -> Dict[str, Any]:
        """
        Exporte toutes les données d'une mémoire pour backup.
        
        Retourne un dict contenant :
        - memory: propriétés du nœud Memory
        - documents: liste des nœuds Document (propriétés)
        - entities: liste des nœuds Entity (propriétés)
        - relations: liste des relations RELATED_TO (from, to, propriétés)
        - mentions: liste des relations MENTIONS (doc_id, entity_name, count)
        
        Args:
            memory_id: ID de la mémoire à exporter
            
        Returns:
            Dictionnaire complet des données de la mémoire
        """
        async with self.session() as session:
            # 1. Exporter le nœud Memory
            mem_result = await session.run(
                "MATCH (m:Memory {id: $id}) RETURN m",
                id=memory_id
            )
            mem_record = await mem_result.single()
            if not mem_record:
                raise ValueError(f"Memory '{memory_id}' not found")
            
            memory_props = dict(mem_record["m"])
            # Convertir les types Neo4j en types sérialisables
            for k, v in memory_props.items():
                if hasattr(v, 'to_native'):
                    memory_props[k] = v.to_native().isoformat()
            
            # 2. Exporter les Documents
            docs_result = await session.run(
                """
                MATCH (d:Document {memory_id: $memory_id})
                RETURN d
                ORDER BY d.ingested_at
                """,
                memory_id=memory_id
            )
            documents = []
            async for record in docs_result:
                props = dict(record["d"])
                for k, v in props.items():
                    if hasattr(v, 'to_native'):
                        props[k] = v.to_native().isoformat()
                documents.append(props)
            
            # 3. Exporter les Entities
            ents_result = await session.run(
                """
                MATCH (e:Entity {memory_id: $memory_id})
                RETURN e
                ORDER BY e.name
                """,
                memory_id=memory_id
            )
            entities = []
            async for record in ents_result:
                props = dict(record["e"])
                for k, v in props.items():
                    if hasattr(v, 'to_native'):
                        props[k] = v.to_native().isoformat()
                    elif isinstance(v, list):
                        props[k] = list(v)  # Convertir les listes Neo4j
                entities.append(props)
            
            # 4. Exporter les relations RELATED_TO
            rels_result = await session.run(
                """
                MATCH (from:Entity {memory_id: $memory_id})-[r:RELATED_TO]->(to:Entity {memory_id: $memory_id})
                RETURN from.name as from_name, to.name as to_name,
                       r.type as rel_type, r.description as description,
                       r.weight as weight, r.source_doc as source_doc,
                       r.created_at as created_at
                """,
                memory_id=memory_id
            )
            relations = []
            async for record in rels_result:
                rel = {
                    "from_name": record["from_name"],
                    "to_name": record["to_name"],
                    "type": record["rel_type"],
                    "description": record["description"],
                    "weight": record["weight"],
                    "source_doc": record["source_doc"],
                }
                if record["created_at"] and hasattr(record["created_at"], 'to_native'):
                    rel["created_at"] = record["created_at"].to_native().isoformat()
                relations.append(rel)
            
            # 5. Exporter les relations MENTIONS
            ments_result = await session.run(
                """
                MATCH (d:Document {memory_id: $memory_id})-[r:MENTIONS]->(e:Entity {memory_id: $memory_id})
                RETURN d.id as doc_id, e.name as entity_name, r.count as count
                """,
                memory_id=memory_id
            )
            mentions = []
            async for record in ments_result:
                mentions.append({
                    "doc_id": record["doc_id"],
                    "entity_name": record["entity_name"],
                    "count": record["count"]
                })
            
            print(f"📦 [Graph Export] {memory_id}: {len(documents)} docs, "
                  f"{len(entities)} entities, {len(relations)} relations, "
                  f"{len(mentions)} mentions", file=sys.stderr)
            
            return {
                "memory": memory_props,
                "documents": documents,
                "entities": entities,
                "relations": relations,
                "mentions": mentions
            }
    
    @_guard_graph_import
    async def import_memory_data(self, data: Dict[str, Any]) -> Dict[str, int]:
        """
        Importe les données d'une mémoire depuis un backup.
        
        Recrée tous les nœuds et relations tels qu'ils étaient.
        La mémoire NE DOIT PAS exister (erreur sinon).
        
        Args:
            data: Dictionnaire issu de export_memory_data()
            
        Returns:
            Compteurs : memory, documents, entities, relations, mentions créés
        """
        memory_props = data["memory"]
        memory_id = memory_props["id"]
        
        # Vérifier que la mémoire n'existe pas
        existing = await self.get_memory(memory_id)
        if existing:
            raise ValueError(
                f"Memory '{memory_id}' already exists. "
                "Delete it before restoring."
            )
        
        counters = {
            "memory": 0,
            "documents": 0,
            "entities": 0,
            "relations": 0,
            "mentions": 0
        }
        
        async with self.session() as session:
            # 1. Recréer le nœud Memory
            await self._run_consumed(
                session,
                """
                CREATE (m:Memory {
                    id: $id,
                    name: $name,
                    description: $description,
                    ontology: $ontology,
                    ontology_uri: $ontology_uri,
                    namespace: $namespace,
                    owner_token_hash: $owner_token_hash,
                    created_at: datetime($created_at)
                })
                """,
                id=memory_props["id"],
                name=memory_props.get("name", ""),
                description=memory_props.get("description"),
                ontology=memory_props.get("ontology", "default"),
                ontology_uri=memory_props.get("ontology_uri"),
                namespace=memory_props.get("namespace", self._ns(memory_id)),
                owner_token_hash=memory_props.get("owner_token_hash"),
                created_at=memory_props.get("created_at", datetime.utcnow().isoformat())
            )
            counters["memory"] = 1
            
            # 2. Recréer les Documents
            for doc in data.get("documents", []):
                await self._run_consumed(
                    session,
                    """
                    CREATE (d:Document {
                        id: $id,
                        memory_id: $memory_id,
                        uri: $uri,
                        filename: $filename,
                        hash: $hash,
                        ingested_at: datetime($ingested_at),
                        metadata_json: $metadata_json,
                        source_path: $source_path,
                        source_modified_at: $source_modified_at,
                        size_bytes: $size_bytes,
                        text_length: $text_length,
                        content_type: $content_type
                    })
                    """,
                    id=doc["id"],
                    memory_id=memory_id,
                    uri=doc.get("uri", ""),
                    filename=doc.get("filename", ""),
                    hash=doc.get("hash", ""),
                    ingested_at=doc.get("ingested_at", datetime.utcnow().isoformat()),
                    metadata_json=doc.get("metadata_json", "{}"),
                    source_path=self.normalize_source_path(doc.get("source_path")),
                    source_modified_at=doc.get("source_modified_at", ""),
                    size_bytes=doc.get("size_bytes", 0),
                    text_length=doc.get("text_length", 0),
                    content_type=doc.get("content_type", "")
                )
                counters["documents"] += 1
            
            # 3. Recréer les Entities
            for entity in data.get("entities", []):
                await self._run_consumed(
                    session,
                    """
                    CREATE (e:Entity {
                        name: $name,
                        memory_id: $memory_id,
                        type: $type,
                        description: $description,
                        source_docs: $source_docs,
                        mention_count: $mention_count,
                        created_at: datetime($created_at),
                        updated_at: datetime($updated_at)
                    })
                    """,
                    name=entity["name"],
                    memory_id=memory_id,
                    type=entity.get("type", "Other"),
                    description=entity.get("description"),
                    source_docs=entity.get("source_docs", []),
                    mention_count=entity.get("mention_count", 1),
                    created_at=entity.get("created_at", datetime.utcnow().isoformat()),
                    updated_at=entity.get("updated_at", datetime.utcnow().isoformat())
                )
                counters["entities"] += 1
            
            # 4. Recréer les relations RELATED_TO
            for rel in data.get("relations", []):
                await self._run_consumed(
                    session,
                    """
                    MATCH (from:Entity {name: $from_name, memory_id: $memory_id})
                    MATCH (to:Entity {name: $to_name, memory_id: $memory_id})
                    CREATE (from)-[r:RELATED_TO {
                        type: $rel_type,
                        description: $description,
                        weight: $weight,
                        source_doc: $source_doc
                    }]->(to)
                    """,
                    from_name=rel["from_name"],
                    to_name=rel["to_name"],
                    memory_id=memory_id,
                    rel_type=rel.get("type", "RELATED_TO"),
                    description=rel.get("description"),
                    weight=rel.get("weight", 1.0),
                    source_doc=rel.get("source_doc")
                )
                counters["relations"] += 1
            
            # 5. Recréer les relations MENTIONS
            for mention in data.get("mentions", []):
                await self._run_consumed(
                    session,
                    """
                    MATCH (d:Document {id: $doc_id, memory_id: $memory_id})
                    MATCH (e:Entity {name: $entity_name, memory_id: $memory_id})
                    CREATE (d)-[r:MENTIONS {count: $count}]->(e)
                    """,
                    doc_id=mention["doc_id"],
                    entity_name=mention["entity_name"],
                    memory_id=memory_id,
                    count=mention.get("count", 1)
                )
                counters["mentions"] += 1
        
        print(f"📥 [Graph Import] {memory_id}: {counters}", file=sys.stderr)
        return counters
    
    # =========================================================================
    # Statistiques
    # =========================================================================
    
    async def get_memory_stats(self, memory_id: str) -> MemoryStats:
        """Récupère les statistiques d'une mémoire."""
        async with self.session() as session:
            result = await session.run(
                """
                MATCH (m:Memory {id: $memory_id})
                OPTIONAL MATCH (d:Document {memory_id: $memory_id})
                OPTIONAL MATCH (e:Entity {memory_id: $memory_id})
                WITH m, count(DISTINCT d) as doc_count, count(DISTINCT e) as entity_count
                OPTIONAL MATCH (:Entity {memory_id: $memory_id})-[r:RELATED_TO]-()
                RETURN doc_count, entity_count, count(DISTINCT r) as rel_count
                """,
                memory_id=memory_id
            )
            
            record = await result.single()
            
            if not record:
                return MemoryStats(memory_id=memory_id)
            
            # Top entités
            top_result = await session.run(
                """
                MATCH (e:Entity {memory_id: $memory_id})
                RETURN e.name as name, e.type as type, e.mention_count as mentions
                ORDER BY e.mention_count DESC
                LIMIT 10
                """,
                memory_id=memory_id
            )
            
            top_entities = []
            async for r in top_result:
                top_entities.append({
                    "name": r["name"],
                    "type": r["type"],
                    "mentions": r["mentions"]
                })
            
            return MemoryStats(
                memory_id=memory_id,
                document_count=record["doc_count"],
                entity_count=record["entity_count"],
                relation_count=record["rel_count"],
                top_entities=top_entities
            )


# Singleton pour usage global
_graph_service: Optional[GraphService] = None


def get_graph_service() -> GraphService:
    """Retourne l'instance singleton du GraphService."""
    global _graph_service
    if _graph_service is None:
        _graph_service = GraphService()
    return _graph_service
