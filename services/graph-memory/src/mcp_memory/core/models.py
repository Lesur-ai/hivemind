# -*- coding: utf-8 -*-
"""
Modèles Pydantic pour MCP Memory.

Définit les structures de données utilisées dans tout le service.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field


# =============================================================================
# Enums
# =============================================================================

class EntityType(str, Enum):
    """Types d'entités reconnus."""
    PERSON = "Person"
    ORGANIZATION = "Organization"
    CONCEPT = "Concept"
    LOCATION = "Location"
    DATE = "Date"
    PRODUCT = "Product"
    SERVICE = "Service"
    CLAUSE = "Clause"
    CERTIFICATION = "Certification"  # ISO 27001, HDS, SecNumCloud
    METRIC = "Metric"                # SLA 99.95%, GTI 15 min
    DURATION = "Duration"            # 36 mois, préavis 6 mois
    AMOUNT = "Amount"                # 50 000 EUR/mois
    OTHER = "Other"


class RelationType(str, Enum):
    """Types de relations reconnus."""
    MENTIONS = "MENTIONS"
    DEFINES = "DEFINES"
    RELATED_TO = "RELATED_TO"
    CONTAINS = "CONTAINS"
    BELONGS_TO = "BELONGS_TO"
    SIGNED_BY = "SIGNED_BY"
    CREATED_BY = "CREATED_BY"
    REFERENCES = "REFERENCES"


class SearchMode(str, Enum):
    """Modes de recherche disponibles."""
    GRAPH = "graph"      # Recherche graphe uniquement
    VECTOR = "vector"    # Recherche vectorielle uniquement
    AUTO = "auto"        # Graph-first, fallback vector si nécessaire


# =============================================================================
# Entités & Relations (pour extraction LLM)
# =============================================================================

class ExtractedEntity(BaseModel):
    """Entité extraite par le LLM.
    
    Note: 'type' est une string libre depuis v1.3.1 pour supporter les types
    dynamiques des ontologies (presales, cloud, etc.) sans être limité à l'Enum
    EntityType. L'Enum est conservée pour la compatibilité avec le code existant.
    """
    name: str = Field(..., description="Entity name")
    type: str = Field(default="Other", description="Free-form entity type supporting ontology-defined types")
    description: Optional[str] = Field(None, description="Contextual description")
    aliases: List[str] = Field(default_factory=list, description="Alternative names")


class ExtractedRelation(BaseModel):
    """Relation extraite par le LLM."""
    from_entity: str = Field(..., description="Source entity name")
    to_entity: str = Field(..., description="Target entity name")
    type: str = Field(default="RELATED_TO", description="Free-form relation type supporting ontology-defined types")
    description: Optional[str] = Field(None, description="Relation description")
    weight: float = Field(default=1.0, ge=0.0, le=1.0, description="Relation strength")
    
    class Config:
        use_enum_values = True


class ExtractionResult(BaseModel):
    """Résultat complet d'une extraction LLM."""
    entities: List[ExtractedEntity] = Field(default_factory=list)
    relations: List[ExtractedRelation] = Field(default_factory=list)
    summary: Optional[str] = Field(None, description="Document summary")
    key_topics: List[str] = Field(default_factory=list, description="Main topics")


# =============================================================================
# Documents
# =============================================================================

class DocumentMetadata(BaseModel):
    """Métadonnées d'un document."""
    filename: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    source: Optional[str] = None  # Ex: "upload", "s3_sync"
    custom: Dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    """Représentation d'un document dans le système."""
    id: str = Field(..., description="Unique identifier (UUID)")
    memory_id: str = Field(..., description="Owning memory identifier")
    uri: str = Field(..., description="Document S3 URI")
    filename: str
    hash: str = Field(..., description="Content SHA256")
    ingested_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: DocumentMetadata
    entity_count: int = Field(default=0)
    relation_count: int = Field(default=0)


# =============================================================================
# Mémoires
# =============================================================================

class Memory(BaseModel):
    """Représentation d'une mémoire (namespace)."""
    id: str = Field(..., description="Unique memory identifier")
    name: str = Field(..., description="Human-readable name")
    description: Optional[str] = None
    ontology: str = Field(default="default", description="Ontology used for extraction")
    ontology_uri: Optional[str] = Field(None, description="S3 URI of the copied ontology")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    owner_token: Optional[str] = Field(None, description="Owner token")


class MemoryStats(BaseModel):
    """Statistiques d'une mémoire."""
    memory_id: str
    document_count: int = 0
    entity_count: int = 0
    relation_count: int = 0
    total_size_bytes: int = 0
    last_ingestion: Optional[datetime] = None
    top_entities: List[Dict[str, Any]] = Field(default_factory=list)


# =============================================================================
# Recherche
# =============================================================================

class SearchResult(BaseModel):
    """Résultat d'une recherche."""
    query: str
    mode: SearchMode
    confidence: float = Field(ge=0.0, le=1.0)
    entities: List[Dict[str, Any]] = Field(default_factory=list)
    documents: List[Dict[str, Any]] = Field(default_factory=list)
    relations: List[Dict[str, Any]] = Field(default_factory=list)
    context: Optional[str] = Field(None, description="Synthesized context")
    used_fallback: bool = Field(default=False, description="Whether vector RAG was used")


class GraphContext(BaseModel):
    """Contexte d'une entité dans le graphe."""
    entity_name: str
    entity_type: Optional[str] = None
    depth: int = 1
    documents: List[Dict[str, Any]] = Field(default_factory=list)
    related_entities: List[Dict[str, Any]] = Field(default_factory=list)
    relations: List[Dict[str, Any]] = Field(default_factory=list)


# =============================================================================
# Chunks (pour RAG vectoriel)
# =============================================================================

class Chunk(BaseModel):
    """
    Fragment sémantique d'un document.
    
    Créé par le SemanticChunker, stocké dans Qdrant avec son embedding.
    Chaque chunk respecte les frontières naturelles du texte :
    sections, articles, paragraphes, phrases.
    """
    text: str = Field(..., description="Chunk text content")
    index: int = Field(..., description="Chunk position in the document (zero-based)")
    total_chunks: int = Field(default=0, description="Total number of document chunks")
    
    # Métadonnées de provenance
    doc_id: Optional[str] = Field(None, description="Source document ID")
    memory_id: Optional[str] = Field(None, description="Memory identifier")
    filename: Optional[str] = Field(None, description="Source filename")
    
    # Métadonnées sémantiques (détectées par le chunker)
    section_title: Optional[str] = Field(None, description="Containing section title")
    article_number: Optional[str] = Field(None, description="Article number (for example, '23.2')")
    heading_hierarchy: List[str] = Field(default_factory=list, description="Heading hierarchy")
    
    # Statistiques
    char_count: int = Field(default=0, description="Character count")
    token_estimate: int = Field(default=0, description="Estimated token count")


class ChunkResult(BaseModel):
    """
    Résultat d'une recherche vectorielle dans Qdrant.
    
    Contient le chunk retrouvé + son score de similarité.
    """
    chunk: Chunk
    score: float = Field(..., ge=0.0, le=1.0, description="Cosine-similarity score")
    
    # Contexte pour le prompt LLM
    @property
    def context_text(self) -> str:
        """Texte formaté pour inclusion dans un prompt LLM."""
        parts = []
        if self.chunk.filename:
            parts.append(f"[Source: {self.chunk.filename}")
            if self.chunk.section_title:
                parts.append(f" > {self.chunk.section_title}")
            if self.chunk.article_number:
                parts.append(f" > Art. {self.chunk.article_number}")
            parts.append("]")
        header = "".join(parts)
        return f"{header}\n{self.chunk.text}" if header else self.chunk.text


# =============================================================================
# Tokens / Auth
# =============================================================================

class TokenInfo(BaseModel):
    """Information sur un token client."""
    token_hash: str = Field(..., description="Token hash, not the token itself")
    client_name: str
    email: Optional[str] = Field(None, description="Token owner's email address")
    created_at: datetime
    expires_at: Optional[datetime] = None
    permissions: List[str] = Field(default_factory=list)
    is_active: bool = True
    memory_ids: List[str] = Field(default_factory=list, description="Allowed memories; empty means all")


class TokenCreateRequest(BaseModel):
    """Requête de création de token."""
    client_name: str
    email: Optional[str] = Field(None, description="Owner's email address")
    permissions: List[str] = Field(default_factory=lambda: ["read", "write"])
    memory_ids: List[str] = Field(default_factory=list)
    expires_in_days: Optional[int] = None
