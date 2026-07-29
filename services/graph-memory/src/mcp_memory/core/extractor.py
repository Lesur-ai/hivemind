# -*- coding: utf-8 -*-
"""
ExtractorService - Extraction d'entités et relations via LLMaaS.

Utilise l'API LLMaaS Cloud Temple (compatible OpenAI) pour extraire
les entités, relations et concepts à partir de texte.
"""

import sys
import json
from typing import Optional, List
from tenacity import retry, stop_after_attempt, wait_exponential

from openai import AsyncOpenAI
from openai import APIError, APITimeoutError

from ..config import get_settings
from .egress import (
    build_owned_async_http_client,
    close_owned_client_from_sync,
    display_proxy_url,
    redact_proxy_errors_async,
    redact_proxy_secrets,
)
from .models import (
    ExtractionResult, ExtractedEntity, ExtractedRelation,
    EntityType, RelationType
)
from .ontology import Ontology, get_ontology_manager


# Minimal extraction prompt used as a fallback without an ontology.
# Domain logic (entity types, relations, and rules) comes from the ontology.
# This prompt is used only by extract_from_text() when no ontology is loaded.
EXTRACTION_PROMPT = """You are an expert in structured information extraction. Analyze the following document and extract important entities and relations.

DOCUMENT:
---
{document_text}
---

INSTRUCTIONS:
1. Identify named entities such as people, organizations, places, concepts, and values.
2. Identify relations between those entities.
3. Provide a short summary.

Entity names must be explicit and include values when relevant.
Create relations between the most specific entities instead of connecting
everything to a central entity. Prefer descriptive relation types such as
SIGNED_BY, HAS_DURATION, and DEFINES over RELATED_TO.

Return ONLY valid JSON:
```json
{{
  "entities": [
    {{"name": "Entity name", "type": "Person|Organization|Concept|Other", "description": "Short description"}}
  ],
  "relations": [
    {{"from_entity": "Source entity", "to_entity": "Target entity", "type": "RELATION_TYPE", "description": "Description"}}
  ],
  "summary": "A 2-3 sentence document summary",
  "key_topics": ["topic1", "topic2"]
}}
```
"""


class ExtractorService:
    """
    Service d'extraction via LLMaaS.
    
    Utilise le modèle gpt-oss:120b de Cloud Temple pour extraire
    les entités et relations structurées depuis un texte.
    """
    
    def __init__(self):
        """Initialise le client OpenAI compatible.

        P12-3 (Hivemind #268) : quand ``PROXY_URL`` est configurée, un
        ``httpx.AsyncClient`` POSSÉDÉ route toutes les requêtes (extraction,
        Q&A, sonde provider-health) via le proxy. AsyncOpenAI ne prend pas
        ownership du client injecté : le service le ferme sur échec du
        constructeur et via ``close()`` au shutdown. Sans proxy, le transport
        interne historique d'AsyncOpenAI est préservé à l'identique.
        """
        settings = get_settings()

        self._owned_http_client = None
        if settings.proxy_url:
            self._owned_http_client = build_owned_async_http_client(
                settings.proxy_url,
                timeout=settings.extraction_timeout_seconds,
            )
            # Jamais l'URL brute (potentiellement porteuse de credentials).
            print(
                f"🔀 [Extractor] LLM egress via proxy "
                f"{display_proxy_url(settings.proxy_url)}",
                file=sys.stderr,
            )
        try:
            self._client = AsyncOpenAI(
                base_url=settings.llmaas_base_url,
                api_key=settings.llmaas_api_key,
                timeout=settings.extraction_timeout_seconds,
                http_client=self._owned_http_client,
            )
        except BaseException:
            # Échec du constructeur : ne pas fuiter le transport possédé.
            if self._owned_http_client is not None:
                close_owned_client_from_sync(self._owned_http_client)
                self._owned_http_client = None
            raise
        self._model = settings.llmaas_model
        self._max_tokens = settings.llmaas_max_tokens
        self._temperature = settings.llmaas_temperature
        self._max_text_length = settings.extraction_max_text_length

    async def close(self) -> None:
        """Ferme le transport proxy possédé (idempotent, référence conservée).

        Appelé au shutdown du service (lifespan ASGI). Une annulation d'appel
        en vol ne ferme JAMAIS ce transport partagé — seul close() le fait.
        Sans proxy configuré c'est un no-op — AsyncOpenAI gère son transport
        interne comme avant.
        """
        if self._owned_http_client is not None:
            await self._owned_http_client.aclose()
    
    @redact_proxy_errors_async
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True
    )
    async def extract_from_text(
        self,
        text: str,
    ) -> ExtractionResult:
        """
        Extrait les entités et relations d'un texte.
        
        Args:
            text: Texte à analyser
            
        Returns:
            ExtractionResult avec entités, relations, résumé
        """
        prompt = EXTRACTION_PROMPT.format(document_text=text)
        
        try:
            print(f"🔍 [Extractor] Extraction en cours ({len(text)} chars)...", file=sys.stderr)
            
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": "You specialize in structured information extraction. Return only valid JSON."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                max_tokens=self._max_tokens,
                temperature=self._temperature
                # Note: response_format non supporté par LLMaaS Cloud Temple
            )
            
            # Parser la réponse - DEBUG COMPLET
            print(f"🔍 [Extractor] DEBUG response type: {type(response)}", file=sys.stderr)
            print(f"🔍 [Extractor] DEBUG choices count: {len(response.choices)}", file=sys.stderr)
            if response.choices:
                print(f"🔍 [Extractor] DEBUG message: {response.choices[0].message}", file=sys.stderr)
                print(f"🔍 [Extractor] DEBUG finish_reason: {response.choices[0].finish_reason}", file=sys.stderr)
            
            content = response.choices[0].message.content
            if content is None:
                print(f"⚠️ [Extractor] Empty LLM response - full message: {response.choices[0].message}", file=sys.stderr)
                return ExtractionResult(summary=None)
            
            print(f"🔍 [Extractor] DEBUG content length: {len(content)}", file=sys.stderr)
            result = self._parse_extraction(content)
            
            print(f"✅ [Extractor] Extrait: {len(result.entities)} entités, {len(result.relations)} relations", file=sys.stderr)
            
            return result
            
        except APITimeoutError:
            print("⏰ [Extractor] Timeout - the document may be too long", file=sys.stderr)
            raise
        except APIError as e:
            print(f"❌ [Extractor] API error: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            raise
    
    def _parse_extraction(
        self,
        content: str,
        known_relation_types: Optional[set] = None,
        known_entity_types: Optional[set] = None,
    ) -> ExtractionResult:
        """
        Parse la réponse JSON du LLM.
        
        Args:
            content: Contenu JSON brut du LLM
            known_relation_types: Types de relations connus (depuis l'ontologie).
                                   Si None, utilise BASE_RELATION_TYPES.
            known_entity_types: Types d'entités connus (depuis l'ontologie).
                                 Permet d'accepter les types dynamiques (ex: Differentiator, KPI…).
                                 Si None, seuls les 12 types de base sont reconnus.
        """
        try:
            # Nettoyer le contenu (parfois le LLM ajoute des ```json)
            content = content.strip()
            if content.startswith("```"):
                # Trouver le premier { et le dernier }
                start = content.find("{")
                end = content.rfind("}") + 1
                content = content[start:end]
            
            data = json.loads(content)
            
            # Parser les entités
            entities = []
            for e in data.get("entities", []):
                entity_type = self._normalize_entity_type(
                    e.get("type", "Other"),
                    known_types=known_entity_types,
                )
                entities.append(ExtractedEntity(
                    name=e.get("name", "").strip(),
                    type=entity_type,
                    description=e.get("description")
                ))
            
            # Parser les relations — avec les types connus de l'ontologie
            relations = []
            for r in data.get("relations", []):
                rel_type = self._parse_relation_type(
                    r.get("type", "RELATED_TO"),
                    known_types=known_relation_types
                )
                relations.append(ExtractedRelation(
                    from_entity=r.get("from_entity", "").strip(),
                    to_entity=r.get("to_entity", "").strip(),
                    type=rel_type,
                    description=r.get("description")
                ))
            
            return ExtractionResult(
                entities=entities,
                relations=relations,
                summary=data.get("summary"),
                key_topics=data.get("key_topics", [])
            )
            
        except json.JSONDecodeError as e:
            print(f"⚠️ [Extractor] JSON parsing error: {e}", file=sys.stderr)
            print(f"   Contenu reçu: {content[:200]}...", file=sys.stderr)
            # Retourner un résultat vide plutôt que crasher
            return ExtractionResult(summary=None)
    
    @staticmethod
    def _normalize_entity_type(type_str: str, known_types: Optional[set] = None) -> str:
        """
        Normalise un type d'entité selon l'ontologie active.
        
        Règle unique : l'ontologie est la seule source de vérité.
        - Si le type retourné par le LLM est dans l'ontologie → retourner avec la casse exacte de l'ontologie
        - Sinon → "Other"
        
        Si aucune ontologie n'est chargée (known_types=None), tout est "Other".
        
        Args:
            type_str: Type brut retourné par le LLM (ex: "Differentiator", "KPI", "Person")
            known_types: Set des types définis par l'ontologie (ex: {"Differentiator", "KPI", "Organization"})
        """
        if not type_str or not known_types:
            return "Other"
        
        type_lower = type_str.strip().lower()
        for kt in known_types:
            if kt.lower() == type_lower:
                return kt  # Casse exacte de l'ontologie
        
        # LOG: capturer les types LLM rejetés pour analyse
        print(f"⚠️ [Normalize] Type LLM rejeté: '{type_str}' → Other (known: {len(known_types)} types)", file=sys.stderr)
        return "Other"
    
    # Types de base (utilisés quand aucune ontologie n'est chargée)
    BASE_RELATION_TYPES = {
        "MENTIONS", "DEFINES", "RELATED_TO", "BELONGS_TO",
        "SIGNED_BY", "CREATED_BY", "REFERENCES", "CONTAINS",
        "HAS_VALUE", "CERTIFIES", "PART_OF",
    }
    
    @staticmethod
    def _parse_relation_type(type_str: str, known_types: Optional[set] = None) -> str:
        """
        Convertit une string en type de relation.
        
        Accepte les types définis par l'ontologie (dynamique).
        Les types inconnus qui ont un format valide (MAJ + underscores) sont acceptés tels quels.
        
        Args:
            type_str: Type brut retourné par le LLM
            known_types: Set de types connus (provenant de l'ontologie). Si None, utilise BASE_RELATION_TYPES.
        """
        # Normaliser : majuscules, underscores
        normalized = type_str.strip().upper().replace(" ", "_").replace("-", "_")
        
        # Types connus depuis l'ontologie (ou base par défaut)
        valid_types = known_types or ExtractorService.BASE_RELATION_TYPES
        
        if normalized in valid_types:
            return normalized
        
        # Accepter tout type au format valide (MAJ + underscores) — le LLM peut inventer
        if normalized.replace("_", "").isalpha() and normalized == normalized.upper():
            return normalized
        
        return "RELATED_TO"
    
    @redact_proxy_errors_async
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True
    )
    async def extract_with_ontology(
        self,
        text: str,
        ontology_name: str = "default",
    ) -> ExtractionResult:
        """
        Extrait les entités et relations d'un texte en utilisant une ontologie.
        
        Args:
            text: Texte à analyser
            ontology_name: Nom de l'ontologie à utiliser (ex: "legal", "cloud")
            
        Returns:
            ExtractionResult avec entités, relations, résumé
        """
        # Charger l'ontologie — OBLIGATOIRE
        ontology_manager = get_ontology_manager()
        ontology = ontology_manager.get_ontology(ontology_name)
        
        if not ontology:
            available = [o["name"] for o in ontology_manager.list_ontologies()]
            raise ValueError(
                f"Ontology '{ontology_name}' not found. "
                f"Available ontologies: {available}. "
                f"Every memory must have a valid ontology."
            )
        
        # Construire le prompt avec l'ontologie
        prompt = ontology.build_prompt(text)
        
        try:
            print(f"🔍 [Extractor] Extraction with ontology '{ontology.name}' ({len(text)} chars)...", file=sys.stderr)
            
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": "You specialize in structured information extraction. Return only valid JSON."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                max_tokens=self._max_tokens,
                temperature=self._temperature
            )
            
            content = response.choices[0].message.content
            if content is None:
                print("⚠️ [Extractor] Empty LLM response", file=sys.stderr)
                return ExtractionResult(summary=None)
            
            # Extraire les types depuis l'ontologie chargée
            ontology_relation_types = {
                rt.name.upper() for rt in ontology.relation_types
            } | self.BASE_RELATION_TYPES  # Union avec les types de base
            ontology_entity_types = {et.name for et in ontology.entity_types}
            
            print(f"🔗 [Extractor] Types ontologie '{ontology.name}': {len(ontology_entity_types)} entités, {len(ontology_relation_types)} relations", file=sys.stderr)
            
            result = self._parse_extraction(
                content,
                known_relation_types=ontology_relation_types,
                known_entity_types=ontology_entity_types,
            )
            
            print(f"✅ [Extractor] Extrait ({ontology.name}): {len(result.entities)} entités, {len(result.relations)} relations", file=sys.stderr)
            
            return result
            
        except APITimeoutError:
            print("⏰ [Extractor] Timeout - the document may be too long", file=sys.stderr)
            raise
        except APIError as e:
            print(f"❌ [Extractor] API error: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            raise

    # =========================================================================
    # Extraction chunked (gros documents)
    # =========================================================================

    @redact_proxy_errors_async
    async def extract_with_ontology_chunked(
        self,
        text: str,
        ontology_name: str = "default",
        progress_callback=None,
    ) -> ExtractionResult:
        """
        Extrait les entités et relations d'un texte long en le découpant en chunks.
        
        Stratégie séquentielle avec contexte cumulatif :
        - Si le texte est court (< extraction_chunk_size), délègue à extract_with_ontology()
        - Sinon, découpe en chunks aux frontières de sections
        - Chaque chunk reçoit le contexte des entités/relations déjà extraites
        - Les résultats sont fusionnés à la fin
        
        Args:
            text: Texte complet du document
            ontology_name: Nom de l'ontologie à utiliser
            
        Returns:
            ExtractionResult fusionné avec toutes les entités et relations
        """
        settings = get_settings()
        chunk_size = settings.extraction_chunk_size
        
        # Garde-fou : rejeter les documents trop volumineux (anti-DoS LLM)
        # Avec des chunks de 25K chars, un document de 950K = ~38 chunks → raisonnable.
        # Au-delà, le coût LLM et le temps d'extraction deviennent prohibitifs.
        max_text_length = settings.extraction_max_text_length
        if len(text) > max_text_length:
            raise ValueError(
                f"Document trop volumineux pour l'extraction : {len(text):,} caractères "
                f"(limite : {max_text_length:,} caractères, configurable via EXTRACTION_MAX_TEXT_LENGTH). "
                f"Avec des chunks de {chunk_size:,} chars, cela représenterait "
                f"~{len(text) // chunk_size} appels LLM."
            )
        
        # Si le texte tient dans un seul chunk, pas besoin de découper
        if len(text) <= chunk_size:
            print(f"📄 [Extractor] Document court ({len(text)} chars ≤ {chunk_size}) → extraction simple",
                  file=sys.stderr)
            if progress_callback:
                await progress_callback("extraction_start", {
                    "chunks_total": 1, "chunk_current": 1,
                    "text_length": len(text), "mode": "single"
                })
            result = await self.extract_with_ontology(text, ontology_name)
            if progress_callback:
                await progress_callback("extraction_chunk_done", {
                    "chunk": 1, "chunks_total": 1,
                    "entities_new": len(result.entities),
                    "relations_new": len(result.relations),
                    "entities_cumul": len(result.entities),
                    "relations_cumul": len(result.relations),
                })
            return result
        
        # Découper le texte en chunks aux frontières de sections
        chunks = self._split_text_for_extraction(text, chunk_size)
        print(f"📐 [Extractor] Document long ({len(text)} chars) → {len(chunks)} chunks d'extraction",
              file=sys.stderr)
        
        # Notifier le début de l'extraction multi-chunk
        if progress_callback:
            await progress_callback("extraction_start", {
                "chunks_total": len(chunks), "chunk_current": 0,
                "text_length": len(text), "mode": "chunked",
                "chunk_sizes": [len(c) for c in chunks],
            })
        
        # Charger l'ontologie (une seule fois)
        ontology_manager = get_ontology_manager()
        ontology = ontology_manager.get_ontology(ontology_name)
        if not ontology:
            available = [o["name"] for o in ontology_manager.list_ontologies()]
            raise ValueError(
                f"Ontologie '{ontology_name}' introuvable. "
                f"Ontologies disponibles: {available}."
            )
        
        # Types depuis l'ontologie (entités et relations)
        ontology_relation_types = {
            rt.name.upper() for rt in ontology.relation_types
        } | self.BASE_RELATION_TYPES
        ontology_entity_types = {et.name for et in ontology.entity_types}
        
        # Extraction séquentielle avec contexte cumulatif
        all_entities: List[ExtractedEntity] = []
        all_relations: List[ExtractedRelation] = []
        all_summaries: List[str] = []
        all_key_topics: List[str] = []
        
        for i, chunk_text in enumerate(chunks):
            chunk_num = i + 1
            
            # Construire le contexte cumulatif (vide pour le premier chunk)
            cumulative_context = ""
            if all_entities or all_relations:
                cumulative_context = self._build_cumulative_context(all_entities, all_relations)
            
            print(f"🔄 [Extractor] Chunk {chunk_num}/{len(chunks)} "
                  f"({len(chunk_text)} chars, contexte cumulatif: {len(all_entities)} entités, "
                  f"{len(all_relations)} relations)", file=sys.stderr)
            
            # Construire le prompt avec contexte cumulatif
            prompt = ontology.build_prompt(chunk_text, cumulative_context=cumulative_context)
            
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {
                            "role": "system",
                            "content": "You specialize in structured information extraction. Return only valid JSON."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    max_tokens=self._max_tokens,
                    temperature=self._temperature
                )
                
                content = response.choices[0].message.content
                if content is None:
                    print(f"⚠️ [Extractor] Chunk {chunk_num}: empty LLM response", file=sys.stderr)
                    continue
                
                result = self._parse_extraction(
                    content,
                    known_relation_types=ontology_relation_types,
                    known_entity_types=ontology_entity_types,
                )
                
                print(f"✅ [Extractor] Chunk {chunk_num}: +{len(result.entities)} entités, "
                      f"+{len(result.relations)} relations", file=sys.stderr)
                
                # Accumuler les résultats
                all_entities.extend(result.entities)
                all_relations.extend(result.relations)
                if result.summary:
                    all_summaries.append(result.summary)
                all_key_topics.extend(result.key_topics)
                
                # Notifier la progression
                if progress_callback:
                    await progress_callback("extraction_chunk_done", {
                        "chunk": chunk_num, "chunks_total": len(chunks),
                        "chunk_chars": len(chunk_text),
                        "entities_new": len(result.entities),
                        "relations_new": len(result.relations),
                        "entities_cumul": len(all_entities),
                        "relations_cumul": len(all_relations),
                    })
                
            except APITimeoutError:
                print(f"⏰ [Extractor] Timeout chunk {chunk_num}/{len(chunks)} — on continue", file=sys.stderr)
                # On continue avec les chunks suivants au lieu de tout perdre
                continue
            except APIError as e:
                print(f"❌ [Extractor] API error for chunk {chunk_num}/{len(chunks)}: {redact_proxy_secrets(str(e))}", file=sys.stderr)
                raise
        
        # Fusionner les résultats
        merged = self._merge_extraction_results(all_entities, all_relations, all_summaries, all_key_topics)
        
        print(f"🏁 [Extractor] Extraction chunked terminée: "
              f"{len(merged.entities)} entités, {len(merged.relations)} relations "
              f"(depuis {len(chunks)} chunks)", file=sys.stderr)
        
        return merged

    def _split_text_for_extraction(self, text: str, chunk_size: int) -> List[str]:
        """
        Découpe un texte long en chunks pour l'extraction graph.
        
        Stratégie : découpe aux frontières de sections (double saut de ligne,
        articles, titres) pour ne jamais couper au milieu d'un paragraphe.
        
        Args:
            text: Texte complet du document
            chunk_size: Taille max en caractères par chunk
            
        Returns:
            Liste de chunks de texte
        """
        import re
        
        # Identifier les points de coupe naturels (double saut de ligne)
        # On préfère couper aux frontières de sections/articles
        sections = re.split(r'(\n\s*\n)', text)
        
        chunks = []
        current_chunk = ""
        
        for section in sections:
            # Si ajouter cette section dépasse la taille ET qu'on a déjà du contenu
            if len(current_chunk) + len(section) > chunk_size and current_chunk.strip():
                chunks.append(current_chunk.strip())
                current_chunk = section
            else:
                current_chunk += section
        
        # Dernier chunk
        if current_chunk.strip():
            chunks.append(current_chunk.strip())
        
        # Si un chunk est encore trop gros (section unique très longue),
        # on le re-découpe sur les simples sauts de ligne
        final_chunks = []
        for chunk in chunks:
            if len(chunk) > chunk_size * 1.5:  # Tolérance de 50%
                sub_chunks = self._force_split_chunk(chunk, chunk_size)
                final_chunks.extend(sub_chunks)
            else:
                final_chunks.append(chunk)
        
        return final_chunks

    def _force_split_chunk(self, text: str, chunk_size: int) -> List[str]:
        """
        Découpe forcée d'un chunk trop gros (section unique très longue).
        
        Coupe aux frontières de lignes pour ne jamais couper mid-phrase.
        """
        lines = text.split('\n')
        chunks = []
        current_chunk = ""
        
        for line in lines:
            if len(current_chunk) + len(line) + 1 > chunk_size and current_chunk.strip():
                chunks.append(current_chunk.strip())
                current_chunk = line + '\n'
            else:
                current_chunk += line + '\n'
        
        if current_chunk.strip():
            chunks.append(current_chunk.strip())
        
        return chunks

    @staticmethod
    def _build_cumulative_context(
        entities: List[ExtractedEntity],
        relations: List[ExtractedRelation]
    ) -> str:
        """
        Construit un résumé compact des entités et relations déjà extraites.
        
        Format optimisé pour le budget tokens :
        - ~10-15 tokens par entité
        - ~15-20 tokens par relation
        - Total typique : 2-3K tokens pour 100 entités + 100 relations
        
        Args:
            entities: Entités déjà extraites
            relations: Relations déjà extraites
            
        Returns:
            Texte compact du contexte cumulatif
        """
        parts = []
        
        # Liste compacte des entités (nom + type)
        if entities:
            entity_lines = []
            for e in entities:
                entity_lines.append(f"- {e.name} ({e.type})")
            parts.append("ENTITÉS DÉJÀ EXTRAITES:\n" + "\n".join(entity_lines))
        
        # Liste compacte des relations (from --TYPE--> to)
        if relations:
            relation_lines = []
            for r in relations:
                relation_lines.append(f"- {r.from_entity} --{r.type}--> {r.to_entity}")
            parts.append("RELATIONS DÉJÀ EXTRAITES:\n" + "\n".join(relation_lines))
        
        return "\n\n".join(parts)

    @staticmethod
    def _merge_extraction_results(
        all_entities: List[ExtractedEntity],
        all_relations: List[ExtractedRelation],
        all_summaries: List[str],
        all_key_topics: List[str]
    ) -> ExtractionResult:
        """
        Fusionne les résultats de N extractions chunked.
        
        Déduplication :
        - Entités : par (nom normalisé, type), on garde la description la plus longue
        - Relations : par (from, to, type), on garde la description la plus longue
        - Key topics : unicité
        - Summaries : concaténation
        
        Args:
            all_entities: Toutes les entités extraites (avec doublons potentiels)
            all_relations: Toutes les relations extraites
            all_summaries: Résumés partiels de chaque chunk
            all_key_topics: Topics de chaque chunk
            
        Returns:
            ExtractionResult fusionné et dédupliqué
        """
        # Dédupliquer les entités par (nom normalisé, type)
        entity_map = {}  # (name_lower, type) -> ExtractedEntity
        for e in all_entities:
            key = (e.name.strip().lower(), e.type)
            if key not in entity_map:
                entity_map[key] = e
            else:
                # Garder la description la plus longue (la plus riche)
                existing = entity_map[key]
                if e.description and (not existing.description or len(e.description) > len(existing.description)):
                    entity_map[key] = ExtractedEntity(
                        name=existing.name,  # Garder le nom original (première occurrence)
                        type=existing.type,
                        description=e.description
                    )
        
        # Dédupliquer les relations par (from_lower, to_lower, type)
        relation_map = {}  # (from, to, type) -> ExtractedRelation
        for r in all_relations:
            key = (r.from_entity.strip().lower(), r.to_entity.strip().lower(), r.type)
            if key not in relation_map:
                relation_map[key] = r
            else:
                existing = relation_map[key]
                if r.description and (not existing.description or len(r.description) > len(existing.description)):
                    relation_map[key] = ExtractedRelation(
                        from_entity=existing.from_entity,
                        to_entity=existing.to_entity,
                        type=existing.type,
                        description=r.description
                    )
        
        # Fusionner les résumés
        merged_summary = " ".join(all_summaries) if all_summaries else None
        
        # Dédupliquer les topics
        seen_topics = set()
        unique_topics = []
        for topic in all_key_topics:
            topic_lower = topic.strip().lower()
            if topic_lower not in seen_topics:
                seen_topics.add(topic_lower)
                unique_topics.append(topic.strip())
        
        return ExtractionResult(
            entities=list(entity_map.values()),
            relations=list(relation_map.values()),
            summary=merged_summary,
            key_topics=unique_topics
        )

    async def test_connection(self) -> dict:
        """Teste la connexion au LLMaaS."""
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": "Reply only with 'OK'"}],
                max_tokens=10
            )
            
            return {
                "status": "ok",
                "model": self._model,
                "message": "LLMaaS connection succeeded"
            }
            
        except APIError as e:
            return {
                "status": "error",
                "model": self._model,
                # P12-3 : jamais d'URL proxy brute dans la sortie santé.
                "message": f"LLMaaS error: {redact_proxy_secrets(str(e))}"
            }


    async def generate_answer(self, prompt: str) -> str:
        """
        Génère une réponse à partir d'un prompt.
        
        Utilisé pour le Q&A sur le graphe de connaissances.
        
        Args:
            prompt: Prompt complet avec contexte et question
            
        Returns:
            Réponse générée par le LLM
        """
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": "You answer questions using a knowledge graph. Be concise and precise."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.3,  # Plus déterministe pour les réponses factuelles
                max_tokens=self._max_tokens
            )
            
            return response.choices[0].message.content or "No answer was generated."
            
        except Exception as e:
            # P12-3 R1 : chemin récupéré retourné au client — redaction
            # systématique (no-op sans secret proxy dans le message).
            safe_message = redact_proxy_secrets(str(e))
            print(
                f"❌ [Extractor] Erreur génération réponse: {safe_message}",
                file=sys.stderr,
            )
            return f"Error generating the answer: {safe_message}"


# Singleton pour usage global
_extractor_service: Optional[ExtractorService] = None


def get_extractor_service() -> ExtractorService:
    """Retourne l'instance singleton du ExtractorService."""
    global _extractor_service
    if _extractor_service is None:
        _extractor_service = ExtractorService()
    return _extractor_service


async def close_extractor_service_if_initialized() -> None:
    """Ferme le singleton s'il a été instancié (shutdown du service).

    P12-3 : libère le transport proxy possédé injecté dans AsyncOpenAI quand
    ``PROXY_URL`` est défini. Sans instanciation préalable, no-op.
    """
    global _extractor_service
    if _extractor_service is not None:
        service = _extractor_service
        _extractor_service = None
        await service.close()
