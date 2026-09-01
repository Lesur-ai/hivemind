# -*- coding: utf-8 -*-
"""
OntologyManager - Gestion des ontologies pour l'extraction.

Charge et gère les ontologies YAML qui définissent les règles d'extraction
spécifiques à chaque domaine (juridique, cloud, infogérance, etc.).
"""

import os
import sys
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field

import yaml


@dataclass
class EntityTypeDefinition:
    """Définition d'un type d'entité."""
    name: str
    description: str
    examples: List[str] = field(default_factory=list)
    priority: str = "normal"  # normal, high


@dataclass
class RelationTypeDefinition:
    """Définition d'un type de relation."""
    name: str
    description: str
    examples: List[str] = field(default_factory=list)


@dataclass
class ExtractionRules:
    """Règles d'extraction."""
    max_entities: int = 60
    max_relations: int = 80
    include_metrics: bool = True
    include_durations: bool = True
    include_amounts: bool = True
    extract_implicit_relations: bool = False
    priority_entities: List[str] = field(default_factory=list)
    special_instructions: str = ""


@dataclass
class Ontology:
    """Représente une ontologie chargée."""
    name: str
    version: str
    description: str
    context: str
    entity_types: List[EntityTypeDefinition]
    relation_types: List[RelationTypeDefinition]
    extraction_rules: ExtractionRules
    examples: List[Dict[str, Any]] = field(default_factory=list)
    
    def build_prompt(self, document_text: str, cumulative_context: str = "") -> str:
        """
        Construit le prompt d'extraction à partir de l'ontologie.
        
        Args:
            document_text: Le texte du document à analyser
            cumulative_context: Contexte cumulatif des extractions précédentes
                                (entités et relations déjà identifiées dans les chunks précédents)
            
        Returns:
            Le prompt complet pour le LLM
        """
        # Séparer les entités prioritaires des autres
        priority_entities = [et for et in self.entity_types if et.priority == "high"]
        other_entities = [et for et in self.entity_types if et.priority != "high"]
        
        # Priority-entity section (mandatory extraction)
        priority_str = ""
        if priority_entities or self.extraction_rules.priority_entities:
            priority_types = priority_entities or [et for et in self.entity_types if et.name in self.extraction_rules.priority_entities]
            priority_str = "\n🔴 PRIORITY ENTITIES — MANDATORY EXTRACTION:\n"
            for et in priority_types:
                priority_str += f"- **{et.name}**: {et.description}\n  Examples: {', '.join(et.examples[:3])}\n"
            priority_str += "\nExtract every priority entity that appears in the document.\n"
        
        # Construction des autres types d'entités
        entity_types_str = "\n".join([
            f"- {et.name}: {et.description}\n  Examples: {', '.join(et.examples[:3])}"
            for et in other_entities
        ])
        
        # Construction des types de relations
        relation_types_str = "\n".join([
            f"- {rt.name}: {rt.description}\n  Examples: {', '.join(rt.examples[:2])}"
            for rt in self.relation_types
        ])
        
        # Instructions spéciales
        special_instructions = ""
        if self.extraction_rules.special_instructions:
            special_instructions = f"""
📋 SPECIAL INSTRUCTIONS (MANDATORY):
{self.extraction_rules.special_instructions}
"""
        
        # Section contexte cumulatif (pour extraction chunked)
        cumulative_section = ""
        if cumulative_context:
            cumulative_section = f"""
🔗 CUMULATIVE CONTEXT — ENTITIES AND RELATIONS IDENTIFIED IN EARLIER SECTIONS:
{cumulative_context}

CUMULATIVE-CONTEXT INSTRUCTIONS:
- Do not redeclare entities listed above unless enriching their descriptions.
- You may create relations from new entities to existing entities.
- Focus on new entities and relations in this section.
- When a known entity appears with more detail, enrich its description in the JSON.
"""
        
        prompt = f"""{self.context}

📄 DOCUMENT TO ANALYZE:
---
{document_text}
---
{cumulative_section}{priority_str}
OTHER ENTITY TYPES:
{entity_types_str}

RELATION TYPES:
{relation_types_str}
{special_instructions}
STRICT RULES:
1. At most {self.extraction_rules.max_entities} entities.
2. At most {self.extraction_rules.max_relations} relations.
3. Extract every mentioned duration.
4. Extract every monetary amount with its currency.
5. Treat totals as priority entities: create an entity for every total, estimate, or global amount.
6. Extract every listed certification and standard.
7. Extract every SLA and metric.
8. Entity names must be explicit and include their values.

ANTI-HUB RULES:
9. Do not connect every entity to the primary organization.
10. Create relations between the most specific entities.
11. The organization should have only structural relations such as SIGNED_BY,
    PARTY_TO, LOCATED_AT, HAS_CERTIFICATION, and GUARANTEES.
12. Connect articles and clauses to their content, not to the organization.
13. Prefer specific relation types such as HAS_DURATION, HAS_AMOUNT, OBLIGATES,
    and DEFINES over RELATED_TO.
14. Use RELATED_TO only as a last resort.

Return ONLY valid JSON:
```json
{{
  "entities": [
    {{"name": "Entity name including value", "type": "EntityType", "description": "Short description"}}
  ],
  "relations": [
    {{"from_entity": "Source entity", "to_entity": "Target entity", "type": "RELATION_TYPE", "description": "Description"}}
  ],
  "summary": "A 2-3 sentence document summary",
  "key_topics": ["topic1", "topic2", "topic3"]
}}
```
"""
        return prompt


class OntologyManager:
    """
    Gestionnaire des ontologies.
    
    Charge les ontologies depuis le dossier ONTOLOGIES/ et permet
    de les récupérer par nom.
    """
    
    # Chemin par défaut des ontologies (dans le conteneur ou en local)
    DEFAULT_ONTOLOGY_PATHS = [
        "/app/ONTOLOGIES",  # Dans le conteneur Docker
        str(Path(__file__).parent.parent.parent.parent / "ONTOLOGIES"),  # Relatif au code
    ]
    
    def __init__(self, ontology_path: Optional[str] = None):
        """
        Initialise le gestionnaire d'ontologies.
        
        Args:
            ontology_path: Chemin vers le dossier des ontologies (optionnel)
        """
        self._ontologies: Dict[str, Ontology] = {}
        self._ontology_path = self._find_ontology_path(ontology_path)
        
        if self._ontology_path:
            self._load_all_ontologies()
        else:
            print("⚠️ [Ontology] No ONTOLOGIES directory found", file=sys.stderr)
    
    def _find_ontology_path(self, custom_path: Optional[str]) -> Optional[str]:
        """Trouve le chemin du dossier d'ontologies."""
        if custom_path and os.path.isdir(custom_path):
            return custom_path
        
        for path in self.DEFAULT_ONTOLOGY_PATHS:
            if os.path.isdir(path):
                return path
        
        return None
    
    def _load_all_ontologies(self):
        """Charge toutes les ontologies du dossier."""
        if not self._ontology_path:
            return
        
        for filename in os.listdir(self._ontology_path):
            if filename.endswith('.yaml') or filename.endswith('.yml'):
                filepath = os.path.join(self._ontology_path, filename)
                try:
                    ontology = self._load_ontology_file(filepath)
                    if ontology:
                        self._ontologies[ontology.name] = ontology
                        print(f"✅ [Ontology] Loaded: {ontology.name} (v{ontology.version})", file=sys.stderr)
                except Exception as e:
                    print(f"❌ [Ontology] Error loading {filename}: {e}", file=sys.stderr)
    
    def _load_ontology_file(self, filepath: str) -> Optional[Ontology]:
        """Charge une ontologie depuis un fichier YAML."""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        if not data:
            return None
        
        # Parser les types d'entités
        entity_types = []
        for et in data.get('entity_types', []):
            entity_types.append(EntityTypeDefinition(
                name=et.get('name', ''),
                description=et.get('description', ''),
                examples=et.get('examples', []),
                priority=et.get('priority', 'normal')
            ))
        
        # Parser les types de relations
        relation_types = []
        for rt in data.get('relation_types', []):
            relation_types.append(RelationTypeDefinition(
                name=rt.get('name', ''),
                description=rt.get('description', ''),
                examples=rt.get('examples', [])
            ))
        
        # Parser les règles d'extraction
        rules_data = data.get('extraction_rules', {})
        extraction_rules = ExtractionRules(
            max_entities=rules_data.get('max_entities', 60),
            max_relations=rules_data.get('max_relations', 80),
            include_metrics=rules_data.get('include_metrics', True),
            include_durations=rules_data.get('include_durations', True),
            include_amounts=rules_data.get('include_amounts', True),
            extract_implicit_relations=rules_data.get('extract_implicit_relations', False),
            priority_entities=rules_data.get('priority_entities', []),
            special_instructions=rules_data.get('special_instructions', '')
        )
        
        return Ontology(
            name=data.get('name', 'unknown'),
            version=data.get('version', '1.0'),
            description=data.get('description', ''),
            context=data.get('context', ''),
            entity_types=entity_types,
            relation_types=relation_types,
            extraction_rules=extraction_rules,
            examples=data.get('examples', [])
        )
    
    def get_ontology(self, name: str) -> Optional[Ontology]:
        """
        Récupère une ontologie par son nom.
        
        Args:
            name: Nom de l'ontologie (ex: "legal", "cloud", "default")
            
        Returns:
            L'ontologie ou None si non trouvée
        """
        return self._ontologies.get(name)
    
    def get_ontology_or_error(self, name: str) -> Ontology:
        """
        Récupère une ontologie par nom. Lève une erreur si introuvable.
        
        Args:
            name: Nom de l'ontologie (ex: "legal", "cloud")
            
        Returns:
            L'ontologie demandée
            
        Raises:
            ValueError: Si l'ontologie n'existe pas
        """
        ontology = self._ontologies.get(name)
        if not ontology:
            available = list(self._ontologies.keys())
            raise ValueError(
                f"Ontology '{name}' not found. "
                f"Available ontologies: {available}. "
                "Every memory MUST have a valid ontology."
            )
        return ontology
    
    def list_ontologies(self) -> List[Dict[str, Any]]:
        """
        Liste toutes les ontologies disponibles.
        
        Returns:
            Liste des ontologies avec nom, version et description
        """
        return [
            {
                "name": ont.name,
                "version": ont.version,
                "description": ont.description.strip()[:100] + "..." if len(ont.description) > 100 else ont.description.strip(),
                "entity_types_count": len(ont.entity_types),
                "relation_types_count": len(ont.relation_types)
            }
            for ont in self._ontologies.values()
        ]
    
    def reload(self):
        """Recharge toutes les ontologies depuis le disque."""
        self._ontologies.clear()
        self._load_all_ontologies()


# Singleton pour usage global
_ontology_manager: Optional[OntologyManager] = None


def get_ontology_manager() -> OntologyManager:
    """Retourne l'instance singleton du OntologyManager."""
    global _ontology_manager
    if _ontology_manager is None:
        _ontology_manager = OntologyManager()
    return _ontology_manager
