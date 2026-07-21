# -*- coding: utf-8 -*-
"""
VectorStoreService - Client Qdrant pour le stockage vectoriel (RAG).

Gère les collections Qdrant (une par mémoire) pour stocker
les chunks de documents avec leurs embeddings.

Couplage strict avec Neo4j :
- Toute ingestion dans le graphe = stockage dans Qdrant
- Toute suppression dans le graphe = suppression dans Qdrant
- Si Qdrant est down, l'opération échoue (pas de mode dégradé)
"""

import sys
from typing import Optional, List
from uuid import uuid4

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from ..config import get_settings
from .models import Chunk, ChunkResult


class VectorStoreService:
    """
    Service de stockage vectoriel via Qdrant.
    
    Chaque mémoire (memory_id) correspond à une collection Qdrant nommée
    {prefix}{safe_memory_id}. Les chunks sont stockés avec leurs embeddings
    et des métadonnées (doc_id, filename, section, article) pour permettre
    le filtrage graph-guided.
    """
    
    def __init__(self):
        """Initialise le client Qdrant."""
        settings = get_settings()
        
        self._client = QdrantClient(
            url=settings.qdrant_url,
            timeout=30
        )
        self._prefix = settings.qdrant_collection_prefix
        self._dimensions = settings.llmaas_embedding_dimensions
    
    def _collection_name(self, memory_id: str) -> str:
        """Retourne le nom de collection Qdrant pour une mémoire."""
        safe_id = "".join(c if c.isalnum() else "_" for c in memory_id)
        return f"{self._prefix}{safe_id}"
    
    # =========================================================================
    # Gestion des collections
    # =========================================================================
    
    async def ensure_collection(self, memory_id: str) -> bool:
        """
        Crée la collection Qdrant si elle n'existe pas.
        
        Args:
            memory_id: ID de la mémoire
            
        Returns:
            True si la collection existait déjà, False si créée
        """
        name = self._collection_name(memory_id)
        
        try:
            # Vérifier si la collection existe
            collections = self._client.get_collections().collections
            existing_names = [c.name for c in collections]
            
            if name in existing_names:
                return True
            
            # Créer la collection
            self._client.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(
                    size=self._dimensions,
                    distance=qmodels.Distance.COSINE
                )
            )
            
            # Créer les index de payload pour le filtrage
            self._client.create_payload_index(
                collection_name=name,
                field_name="doc_id",
                field_schema=qmodels.PayloadSchemaType.KEYWORD
            )
            self._client.create_payload_index(
                collection_name=name,
                field_name="memory_id",
                field_schema=qmodels.PayloadSchemaType.KEYWORD
            )
            
            print(f"📦 [Qdrant] Collection créée: {name} ({self._dimensions}d, cosine)", file=sys.stderr)
            return False
            
        except Exception as e:
            print(f"❌ [Qdrant] Erreur création collection {name}: {e}", file=sys.stderr)
            raise
    
    async def delete_collection(self, memory_id: str) -> bool:
        """
        Supprime toute la collection d'une mémoire.
        
        Utilisé lors de la suppression d'une mémoire entière.
        
        Args:
            memory_id: ID de la mémoire
            
        Returns:
            True si supprimée, False si n'existait pas
        """
        name = self._collection_name(memory_id)
        
        try:
            self._client.delete_collection(collection_name=name)
            print(f"🗑️ [Qdrant] Collection supprimée: {name}", file=sys.stderr)
            return True
        except UnexpectedResponse as e:
            if "404" in str(e) or "not found" in str(e).lower():
                print(f"⚠️ [Qdrant] Collection {name} n'existait pas", file=sys.stderr)
                return False
            raise
        except Exception as e:
            print(f"❌ [Qdrant] Erreur suppression collection {name}: {e}", file=sys.stderr)
            raise
    
    # =========================================================================
    # Stockage des chunks
    # =========================================================================
    
    async def store_chunks(
        self,
        memory_id: str,
        doc_id: str,
        filename: str,
        chunks: List[Chunk],
        embeddings: List[List[float]]
    ) -> int:
        """
        Stocke les chunks d'un document avec leurs embeddings dans Qdrant.
        
        Chaque chunk est un point Qdrant avec :
        - vector : l'embedding
        - payload : métadonnées (doc_id, filename, section, article, texte)
        
        Args:
            memory_id: ID de la mémoire
            doc_id: ID du document dans le graphe
            filename: Nom du fichier source
            chunks: Liste de Chunk à stocker
            embeddings: Liste d'embeddings correspondants
            
        Returns:
            Nombre de chunks stockés
        """
        if not chunks or not embeddings:
            return 0
        
        if len(chunks) != len(embeddings):
            raise ValueError(f"Mismatch: {len(chunks)} chunks vs {len(embeddings)} embeddings")
        
        name = self._collection_name(memory_id)
        
        # Construire les points Qdrant
        points = []
        for chunk, embedding in zip(chunks, embeddings):
            point_id = str(uuid4())
            
            payload = {
                "memory_id": memory_id,
                "doc_id": doc_id,
                "filename": filename,
                "text": chunk.text,
                "chunk_index": chunk.index,
                "total_chunks": chunk.total_chunks,
                "section_title": chunk.section_title,
                "article_number": chunk.article_number,
                "heading_hierarchy": chunk.heading_hierarchy,
                "char_count": chunk.char_count,
                "token_estimate": chunk.token_estimate,
            }
            
            points.append(qmodels.PointStruct(
                id=point_id,
                vector=embedding,
                payload=payload
            ))
        
        try:
            # Upsert par batch (Qdrant gère les gros batch en interne)
            self._client.upsert(
                collection_name=name,
                points=points
            )
            
            print(f"📦 [Qdrant] {len(points)} chunks stockés pour {filename} (collection: {name})", file=sys.stderr)
            return len(points)
            
        except Exception as e:
            print(f"❌ [Qdrant] Erreur stockage chunks: {e}", file=sys.stderr)
            raise
    
    # =========================================================================
    # Recherche vectorielle
    # =========================================================================
    
    async def search(
        self,
        memory_id: str,
        query_embedding: List[float],
        doc_ids: Optional[List[str]] = None,
        limit: int = 5
    ) -> List[ChunkResult]:
        """
        Recherche vectorielle dans une mémoire, filtrable par documents.
        
        C'est le cœur du Graph-Guided RAG :
        - Le graphe identifie les doc_ids pertinents
        - Qdrant cherche les chunks les plus similaires DANS ces documents
        
        Args:
            memory_id: ID de la mémoire
            query_embedding: Vecteur de la requête
            doc_ids: Si fourni, filtre par ces document IDs (graph-guided)
            limit: Nombre max de résultats
            
        Returns:
            Liste de ChunkResult triés par score décroissant
        """
        name = self._collection_name(memory_id)
        
        # Construire le filtre Qdrant
        query_filter = None
        if doc_ids:
            query_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="doc_id",
                        match=qmodels.MatchAny(any=doc_ids)
                    )
                ]
            )
        
        try:
            results = self._client.query_points(
                collection_name=name,
                query=query_embedding,
                query_filter=query_filter,
                limit=limit,
                with_payload=True
            )
            
            chunk_results = []
            for point in results.points:
                payload = point.payload or {}
                
                chunk = Chunk(
                    text=payload.get("text", ""),
                    index=payload.get("chunk_index", 0),
                    total_chunks=payload.get("total_chunks", 0),
                    doc_id=payload.get("doc_id"),
                    memory_id=payload.get("memory_id"),
                    filename=payload.get("filename"),
                    section_title=payload.get("section_title"),
                    article_number=payload.get("article_number"),
                    heading_hierarchy=payload.get("heading_hierarchy", []),
                    char_count=payload.get("char_count", 0),
                    token_estimate=payload.get("token_estimate", 0)
                )
                
                chunk_results.append(ChunkResult(
                    chunk=chunk,
                    score=point.score
                ))
            
            return chunk_results
            
        except UnexpectedResponse as e:
            if "404" in str(e) or "not found" in str(e).lower():
                print(f"⚠️ [Qdrant] Collection {name} n'existe pas encore", file=sys.stderr)
                return []
            raise
        except Exception as e:
            print(f"❌ [Qdrant] Erreur recherche: {e}", file=sys.stderr)
            raise
    
    # =========================================================================
    # Suppression
    # =========================================================================
    
    async def delete_document_chunks(self, memory_id: str, doc_id: str) -> int:
        """
        Supprime tous les chunks d'un document spécifique.
        
        Utilisé lors de la suppression d'un document ou d'une réingestion forcée.
        
        Args:
            memory_id: ID de la mémoire
            doc_id: ID du document dont on supprime les chunks
            
        Returns:
            Nombre de chunks supprimés (estimé)
        """
        name = self._collection_name(memory_id)
        
        try:
            # Compter les chunks avant suppression
            count_result = self._client.count(
                collection_name=name,
                count_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="doc_id",
                            match=qmodels.MatchValue(value=doc_id)
                        )
                    ]
                )
            )
            count = count_result.count
            
            if count == 0:
                return 0
            
            # Supprimer par filtre
            self._client.delete(
                collection_name=name,
                points_selector=qmodels.FilterSelector(
                    filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="doc_id",
                                match=qmodels.MatchValue(value=doc_id)
                            )
                        ]
                    )
                )
            )
            
            print(f"🗑️ [Qdrant] {count} chunks supprimés pour doc {doc_id}", file=sys.stderr)
            return count
            
        except UnexpectedResponse as e:
            if "404" in str(e) or "not found" in str(e).lower():
                return 0
            raise
        except Exception as e:
            print(f"❌ [Qdrant] Erreur suppression chunks: {e}", file=sys.stderr)
            raise
    
    async def count_document_chunks(self, memory_id: str, doc_id: str) -> int:
        """Compte les chunks Qdrant d'un document (0 si collection absente)."""
        name = self._collection_name(memory_id)
        try:
            res = self._client.count(
                collection_name=name,
                count_filter=qmodels.Filter(
                    must=[qmodels.FieldCondition(key="doc_id", match=qmodels.MatchValue(value=doc_id))]
                ),
            )
            return res.count
        except UnexpectedResponse as e:
            if "404" in str(e) or "not found" in str(e).lower():
                return 0  # collection absente = 0 chunk (pas une erreur)
            raise
        # Toute autre erreur est PROPAGÉE : renvoyer 0 produirait un faux
        # "chunks manquants" dans storage_check sur une panne Qdrant transitoire.

    async def list_doc_ids(self, memory_id: str) -> set:
        """
        Retourne l'ensemble des doc_id distincts présents dans la collection Qdrant.

        Utilisé par storage_check pour détecter les vecteurs orphelins (chunks
        Qdrant dont le document n'existe plus dans Neo4j).
        """
        name = self._collection_name(memory_id)
        doc_ids = set()
        try:
            offset = None
            while True:
                points, next_offset = self._client.scroll(
                    collection_name=name,
                    limit=200,
                    offset=offset,
                    with_payload=["doc_id"],
                    with_vectors=False,
                )
                for point in points:
                    did = (point.payload or {}).get("doc_id")
                    if did:
                        doc_ids.add(did)
                if next_offset is None:
                    break
                offset = next_offset
        except UnexpectedResponse as e:
            if "404" in str(e) or "not found" in str(e).lower():
                return set()  # collection absente = aucun vecteur (pas une erreur)
            raise
        # Toute autre erreur (réseau, scroll interrompu) est PROPAGÉE : un set
        # incomplet produirait de faux orphelins/chunks manquants dans storage_check.
        return doc_ids

    # =========================================================================
    # Export / Import (Backup)
    # =========================================================================

    async def export_collection(self, memory_id: str) -> List[dict]:
        """
        Exporte tous les points d'une collection Qdrant pour backup.
        
        Utilise le scroll API pour paginer et éviter les problèmes de mémoire.
        Chaque point est exporté avec son id, vector et payload.
        
        Args:
            memory_id: ID de la mémoire
            
        Returns:
            Liste de dicts {id, vector, payload} pour chaque point
        """
        name = self._collection_name(memory_id)
        all_points = []
        
        try:
            # Vérifier que la collection existe
            collections = self._client.get_collections().collections
            existing_names = [c.name for c in collections]
            if name not in existing_names:
                print(f"⚠️ [Qdrant Export] Collection {name} n'existe pas", file=sys.stderr)
                return []
            
            # Scroll pour récupérer tous les points par pages
            offset = None
            page_size = 100
            
            while True:
                scroll_result = self._client.scroll(
                    collection_name=name,
                    limit=page_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=True,
                )
                
                points, next_offset = scroll_result
                
                for point in points:
                    all_points.append({
                        "id": str(point.id),
                        "vector": list(point.vector) if point.vector else [],
                        "payload": dict(point.payload) if point.payload else {}
                    })
                
                if next_offset is None:
                    break
                offset = next_offset
            
            print(f"📦 [Qdrant Export] {name}: {len(all_points)} points exportés", file=sys.stderr)
            return all_points
            
        except Exception as e:
            print(f"❌ [Qdrant Export] Erreur export {name}: {e}", file=sys.stderr)
            raise
    
    async def import_collection(
        self,
        memory_id: str,
        points_data: List[dict],
        batch_size: int = 100
    ) -> int:
        """
        Importe des points dans une collection Qdrant depuis un backup.
        
        Recrée la collection (si elle n'existe pas) et upsert tous les points.
        Les vecteurs et payloads sont restaurés tels quels.
        
        Args:
            memory_id: ID de la mémoire
            points_data: Liste de dicts {id, vector, payload}
            batch_size: Taille des batches d'upsert
            
        Returns:
            Nombre de points importés
        """
        if not points_data:
            return 0
        
        name = self._collection_name(memory_id)
        
        # S'assurer que la collection existe
        await self.ensure_collection(memory_id)
        
        # Construire et upsert par batches
        total_imported = 0
        
        for i in range(0, len(points_data), batch_size):
            batch = points_data[i:i + batch_size]
            
            points = []
            for p in batch:
                points.append(qmodels.PointStruct(
                    id=p["id"],
                    vector=p["vector"],
                    payload=p.get("payload", {})
                ))
            
            self._client.upsert(
                collection_name=name,
                points=points
            )
            total_imported += len(points)
        
        print(f"📥 [Qdrant Import] {name}: {total_imported} points importés", file=sys.stderr)
        return total_imported
    
    # =========================================================================
    # Diagnostic
    # =========================================================================
    
    async def test_connection(self) -> dict:
        """Teste la connexion à Qdrant."""
        try:
            collections = self._client.get_collections()
            
            return {
                "status": "ok",
                "collections": len(collections.collections),
                "message": f"Qdrant OK ({len(collections.collections)} collections)"
            }
            
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erreur Qdrant: {str(e)}"
            }
    
    async def get_collection_info(self, memory_id: str) -> Optional[dict]:
        """Retourne les infos d'une collection (nombre de chunks, etc.)."""
        name = self._collection_name(memory_id)
        
        try:
            info = self._client.get_collection(collection_name=name)
            return {
                "collection_name": name,
                "points_count": info.points_count,
                "vectors_count": info.vectors_count,
                "status": info.status.value if info.status else "unknown"
            }
        except UnexpectedResponse:
            return None
        except Exception:
            return None


# Singleton pour usage global
_vector_store: Optional[VectorStoreService] = None


def get_vector_store() -> VectorStoreService:
    """Retourne l'instance singleton du VectorStoreService."""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStoreService()
    return _vector_store
