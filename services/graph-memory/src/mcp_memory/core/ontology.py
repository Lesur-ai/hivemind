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
        
        # Section entités prioritaires (extraction OBLIGATOIRE)
        priority_str = ""
        if priority_entities or self.extraction_rules.priority_entities:
            priority_types = priority_entities or [et for et in self.entity_types if et.name in self.extraction_rules.priority_entities]
            priority_str = "\n🔴 ENTITÉS PRIORITAIRES - EXTRACTION OBLIGATOIRE:\n"
            for et in priority_types:
                priority_str += f"- **{et.name}**: {et.description}\n  Exemples: {', '.join(et.examples[:3])}\n"
            priority_str += "\n⚠️ TU DOIS EXTRAIRE TOUTES CES ENTITÉS SI ELLES SONT PRÉSENTES DANS LE DOCUMENT!\n"
        
        # Construction des autres types d'entités
        entity_types_str = "\n".join([
            f"- {et.name}: {et.description}\n  Exemples: {', '.join(et.examples[:3])}"
            for et in other_entities
        ])
        
        # Construction des types de relations
        relation_types_str = "\n".join([
            f"- {rt.name}: {rt.description}\n  Exemples: {', '.join(rt.examples[:2])}"
            for rt in self.relation_types
        ])
        
        # Instructions spéciales
        special_instructions = ""
        if self.extraction_rules.special_instructions:
            special_instructions = f"""
📋 INSTRUCTIONS SPÉCIALES (OBLIGATOIRES):
{self.extraction_rules.special_instructions}
"""
        
        # Section contexte cumulatif (pour extraction chunked)
        cumulative_section = ""
        if cumulative_context:
            cumulative_section = f"""
🔗 CONTEXTE CUMULATIF — ENTITÉS ET RELATIONS DÉJÀ IDENTIFIÉES DANS LES SECTIONS PRÉCÉDENTES:
{cumulative_context}

⚠️ INSTRUCTIONS CONTEXTE CUMULATIF:
- NE PAS re-déclarer les entités déjà listées ci-dessus (sauf pour enrichir leur description)
- Tu PEUX créer des relations VERS ces entités existantes depuis de nouvelles entités
- Concentre-toi sur les NOUVELLES entités et relations de cette section
- Si une entité déjà connue apparaît avec plus de détails, enrichis sa description dans le JSON
"""
        
        prompt = f"""{self.context}

📄 DOCUMENT À ANALYSER:
---
{document_text}
---
{cumulative_section}{priority_str}
AUTRES TYPES D'ENTITÉS:
{entity_types_str}

TYPES DE RELATIONS:
{relation_types_str}
{special_instructions}
RÈGLES STRICTES:
1. Maximum {self.extraction_rules.max_entities} entités
2. Maximum {self.extraction_rules.max_relations} relations
3. EXTRAIT CHAQUE DURÉE MENTIONNÉE (ex: "36 mois", "6 mois de préavis", "12 mois")
4. EXTRAIT CHAQUE MONTANT avec devise (ex: "8 500 EUR HT", "3 150 EUR/mois")
5. ⚠️ TOTAUX PRIORITAIRES: Si tu vois "Total", "estimé", "global" → créer entité OBLIGATOIRE!
6. EXTRAIT CHAQUE CERTIFICATION/NORME LISTÉE (ex: SecNumCloud, HDS, ISO 27001, SOC 2)
7. EXTRAIT CHAQUE SLA/MÉTRIQUE (ex: "99.95%", "GTI 15 min", "GTR 4h")
8. Les noms d'entités doivent être explicites et inclure les valeurs

⚠️ RÈGLES ANTI-HUB (TRÈS IMPORTANT):
9. NE PAS relier toutes les entités à l'organisation principale!
   ❌ MAUVAIS: "Cloud Temple → RELATED_TO → Article 1", "Cloud Temple → RELATED_TO → Article 2", etc.
   ✅ BON: "Article 1 → DEFINES → Services", "Clause confidentialité → HAS_DURATION → 5 ans"
10. Crée des relations ENTRE les entités les plus spécifiques (clause→durée, article→obligation)
11. L'organisation ne doit avoir que des relations STRUCTURELLES: SIGNED_BY, PARTY_TO, LOCATED_AT, HAS_CERTIFICATION, GUARANTEES
12. Les articles/clauses doivent être reliés à leurs CONTENUS (durées, montants, obligations), PAS à l'organisation
13. Utilise les types de relations SPÉCIFIQUES (HAS_DURATION, HAS_AMOUNT, OBLIGATES, DEFINES) plutôt que RELATED_TO
14. RELATED_TO est un DERNIER RECOURS — privilégie toujours un type plus précis

Réponds UNIQUEMENT avec un JSON valide:
```json
{{
  "entities": [
    {{"name": "Nom de l'entité AVEC VALEUR", "type": "TypeEntité", "description": "Description courte"}}
  ],
  "relations": [
    {{"from_entity": "Nom entité source", "to_entity": "Nom entité cible", "type": "TYPE_RELATION", "description": "Description"}}
  ],
  "summary": "Résumé du document en 2-3 phrases",
  "key_topics": ["sujet1", "sujet2", "sujet3"]
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
            print("⚠️ [Ontology] Aucun dossier ONTOLOGIES trouvé", file=sys.stderr)
    
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
                        print(f"✅ [Ontology] Chargée: {ontology.name} (v{ontology.version})", file=sys.stderr)
                except Exception as e:
                    print(f"❌ [Ontology] Erreur chargement {filename}: {e}", file=sys.stderr)
    
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
                f"Ontologie '{name}' introuvable. "
                f"Ontologies disponibles: {available}. "
                f"Chaque mémoire DOIT avoir une ontologie valide."
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
