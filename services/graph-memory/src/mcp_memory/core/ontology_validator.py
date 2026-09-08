# -*- coding: utf-8 -*-
"""
Ontology Validator — Validation pure sans I/O ni dépendance externe d'ontologies YAML.
Strictement aligné avec le modèle runtime Ontology / ExtractionRules de Graph Memory.
Sécurisé contre les bombes YAML (alias et ancres strictement interdits, duplication de
clés rejetée, validation stricte de types scalaires et préservation sémantique exacte).
"""

import hashlib
import re
from typing import Any, Tuple
import yaml

MAX_ONTOLOGY_BYTES = 512 * 1024
MAX_TOTAL_YAML_NODES = 5000
MAX_ENTITY_TYPES = 200
MAX_RELATION_TYPES = 300
MAX_EXAMPLES_PER_TYPE = 50
MAX_EXAMPLE_STR_LEN = 512
MAX_NAME_LEN = 64
MAX_VERSION_LEN = 32
MAX_DESC_LEN = 4096
MAX_CONTEXT_LEN = 8192
MAX_TYPE_DESC_LEN = 2048
MAX_SPECIAL_INSTRUCTIONS_LEN = 16384

_NAME_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

ALLOWED_ROOT_KEYS = {
    "name",
    "version",
    "description",
    "context",
    "entity_types",
    "relation_types",
    "extraction_rules",
    "examples",
}

ALLOWED_ENTITY_KEYS = {"name", "description", "examples", "priority"}
ALLOWED_RELATION_KEYS = {"name", "description", "examples", "priority"}
ALLOWED_PRIORITIES = {"normal", "high", "medium", "low"}

ALLOWED_RULES_KEYS = {
    "max_entities",
    "max_relations",
    "include_metrics",
    "include_durations",
    "include_amounts",
    "extract_implicit_relations",
    "priority_entities",
    "special_instructions",
    "extract_value_propositions",
    "extract_differentiators",
    "extract_exact_metrics",
    "infer_personas",
}

RULE_BOOLEAN_FIELDS = {
    "include_metrics",
    "include_durations",
    "include_amounts",
    "extract_implicit_relations",
    "extract_value_propositions",
    "extract_differentiators",
    "extract_exact_metrics",
    "infer_personas",
}


class StrictSafeLoader(yaml.SafeLoader):
    """Chargeur YAML sûr rejetant strictement les alias, ancres, doublons de clés et collections démesurées."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._node_count = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        self._node_count += 1
        if self._node_count > MAX_TOTAL_YAML_NODES:
            raise yaml.YAMLError(
                f"YAML document exceeds maximum complexity ({self._node_count} > {MAX_TOTAL_YAML_NODES} nodes)"
            )
        if self.check_event(yaml.AliasEvent):
            event = self.get_event()
            raise yaml.YAMLError(f"YAML aliases are forbidden (found alias '{event.anchor}')")
        return super().compose_node(parent, index)

    def compose_scalar_node(self, anchor: Any) -> Any:
        if anchor is not None:
            raise yaml.YAMLError(f"YAML anchors are forbidden (found anchor '&{anchor}')")
        return super().compose_scalar_node(anchor)

    def compose_sequence_node(self, anchor: Any) -> Any:
        if anchor is not None:
            raise yaml.YAMLError(f"YAML anchors are forbidden (found anchor '&{anchor}')")
        return super().compose_sequence_node(anchor)

    def compose_mapping_node(self, anchor: Any) -> Any:
        if anchor is not None:
            raise yaml.YAMLError(f"YAML anchors are forbidden (found anchor '&{anchor}')")
        return super().compose_mapping_node(anchor)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, f"expected a mapping node, but found {node.id}", node.start_mark
            )
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                hash(key)
            except TypeError:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found unhashable key: {type(key).__name__}",
                    key_node.start_mark,
                )
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key: '{key}'",
                    key_node.start_mark,
                )
            value = self.construct_object(value_node, deep=deep)
            mapping[key] = value
        return mapping


def _normalize_yaml_text(text: str) -> str:
    """Normalise les sauts de ligne (CRLF/CR -> LF) en préservant le contenu et la sémantique."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _validate_and_parse_ontology(content_yaml: str) -> Tuple[dict, dict | None]:
    """Parse et valide une ontologie YAML. Retourne (result_dict, parsed_data_or_None)."""
    if not isinstance(content_yaml, str):
        return {
            "status": "ok",
            "valid": False,
            "name": None,
            "version": None,
            "sha256": None,
            "entity_types_count": 0,
            "relation_types_count": 0,
            "yaml_content": None,
            "errors": ["content_yaml must be a string"],
            "warnings": [],
        }, None

    try:
        encoded = content_yaml.encode("utf-8")
    except (UnicodeError, UnicodeEncodeError) as e:
        return {
            "status": "ok",
            "valid": False,
            "name": None,
            "version": None,
            "sha256": None,
            "entity_types_count": 0,
            "relation_types_count": 0,
            "yaml_content": None,
            "errors": [f"Invalid Unicode encoding: {e}"],
            "warnings": [],
        }, None

    if len(encoded) > MAX_ONTOLOGY_BYTES:
        return {
            "status": "ok",
            "valid": False,
            "name": None,
            "version": None,
            "sha256": None,
            "entity_types_count": 0,
            "relation_types_count": 0,
            "yaml_content": None,
            "errors": [
                f"Ontology content exceeds maximum allowed size ({len(encoded)} > {MAX_ONTOLOGY_BYTES} bytes)"
            ],
            "warnings": [],
        }, None

    try:
        data = yaml.load(content_yaml, Loader=StrictSafeLoader)
    except Exception as e:
        err_msg = str(e)
        if len(err_msg) > 200:
            err_msg = err_msg[:200] + "..."
        return {
            "status": "ok",
            "valid": False,
            "name": None,
            "version": None,
            "sha256": None,
            "entity_types_count": 0,
            "relation_types_count": 0,
            "yaml_content": None,
            "errors": [f"Invalid YAML: {err_msg}"],
            "warnings": [],
        }, None

    if not isinstance(data, dict):
        return {
            "status": "ok",
            "valid": False,
            "name": None,
            "version": None,
            "sha256": None,
            "entity_types_count": 0,
            "relation_types_count": 0,
            "yaml_content": None,
            "errors": ["Ontology root must be a YAML mapping (dictionary)"],
            "warnings": [],
        }, None

    errors: list[str] = []
    warnings: list[str] = []

    # 1. Root keys validation
    for k in data.keys():
        if not isinstance(k, str):
            errors.append(f"Root key must be string, found {type(k).__name__}")
        elif k not in ALLOWED_ROOT_KEYS:
            errors.append(f"Unknown root key '{k}'")

    # 2. Name validation
    raw_name = data.get("name")
    if raw_name is None:
        errors.append("Field 'name' is required")
        name = None
    elif not isinstance(raw_name, str):
        errors.append(f"Field 'name' must be a string, found {type(raw_name).__name__}")
        name = None
    else:
        name = raw_name.strip()
        if not name:
            errors.append("Field 'name' cannot be empty")
        elif len(name) > MAX_NAME_LEN or not _NAME_REGEX.fullmatch(name):
            errors.append(f"Invalid ontology name '{name}' (must match ^[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}$)")

    # 3. Version validation
    raw_version = data.get("version")
    if raw_version is None:
        errors.append("Field 'version' is required")
        version = None
    elif not isinstance(raw_version, str):
        errors.append(f"Field 'version' must be a string, found {type(raw_version).__name__}")
        version = None
    else:
        version = raw_version.strip()
        if not version:
            errors.append("Field 'version' cannot be empty")
        elif len(version) > MAX_VERSION_LEN:
            errors.append(f"Field 'version' exceeds maximum length ({len(version)} > {MAX_VERSION_LEN})")

    # 4. Description validation
    raw_desc = data.get("description")
    if raw_desc is None:
        errors.append("Field 'description' is required")
    elif not isinstance(raw_desc, str):
        errors.append(f"Field 'description' must be a string, found {type(raw_desc).__name__}")
    else:
        desc = raw_desc.strip()
        if not desc:
            errors.append("Field 'description' cannot be empty")
        elif len(desc) > MAX_DESC_LEN:
            errors.append(f"Field 'description' exceeds maximum length ({len(desc)} > {MAX_DESC_LEN})")

    # 5. Context validation (optional)
    raw_context = data.get("context")
    if raw_context is not None:
        if not isinstance(raw_context, str):
            errors.append(f"Field 'context' must be a string, found {type(raw_context).__name__}")
        elif len(raw_context) > MAX_CONTEXT_LEN:
            errors.append(f"Field 'context' exceeds maximum length ({len(raw_context)} > {MAX_CONTEXT_LEN})")

    # 6. Entity types validation
    entity_types = data.get("entity_types")
    if entity_types is None:
        errors.append("Field 'entity_types' is required")
        entity_count = 0
    elif not isinstance(entity_types, list) or len(entity_types) == 0:
        errors.append("Field 'entity_types' must be a non-empty list")
        entity_count = 0
    elif len(entity_types) > MAX_ENTITY_TYPES:
        errors.append(f"Field 'entity_types' exceeds maximum count ({len(entity_types)} > {MAX_ENTITY_TYPES})")
        entity_count = len(entity_types)
    else:
        entity_count = len(entity_types)
        seen_entities = set()
        for idx, et in enumerate(entity_types):
            if not isinstance(et, dict):
                errors.append(f"entity_types[{idx}] must be a dictionary, found {type(et).__name__}")
                continue
            for k in et.keys():
                if k not in ALLOWED_ENTITY_KEYS:
                    errors.append(f"entity_types[{idx}] contains unknown key '{k}'")
            et_name = et.get("name")
            if et_name is None:
                errors.append(f"entity_types[{idx}].name is required")
            elif not isinstance(et_name, str):
                errors.append(f"entity_types[{idx}].name must be a string, found {type(et_name).__name__}")
            else:
                s_name = et_name.strip()
                if not s_name or not _NAME_REGEX.fullmatch(s_name):
                    errors.append(f"entity_types[{idx}].name '{s_name}' is invalid")
                else:
                    low_name = s_name.lower()
                    if low_name in seen_entities:
                        errors.append(f"Duplicate entity type name '{s_name}' in entity_types")
                    seen_entities.add(low_name)
            et_desc = et.get("description")
            if et_desc is None:
                errors.append(f"entity_types[{idx}].description is required")
            elif not isinstance(et_desc, str):
                errors.append(f"entity_types[{idx}].description must be a string, found {type(et_desc).__name__}")
            elif len(et_desc) > MAX_TYPE_DESC_LEN:
                errors.append(f"entity_types[{idx}].description exceeds maximum length ({len(et_desc)} > {MAX_TYPE_DESC_LEN})")
            if "priority" in et:
                p_val = et["priority"]
                if not isinstance(p_val, str) or p_val.lower() not in ALLOWED_PRIORITIES:
                    errors.append(
                        f"entity_types[{idx}].priority '{p_val}' is invalid (must be one of {sorted(ALLOWED_PRIORITIES)})"
                    )
            if "examples" in et:
                exs = et["examples"]
                if not isinstance(exs, list) or not all(isinstance(x, str) for x in exs):
                    errors.append(f"entity_types[{idx}].examples must be a list of strings")
                elif len(exs) > MAX_EXAMPLES_PER_TYPE:
                    errors.append(f"entity_types[{idx}].examples exceeds maximum count ({len(exs)} > {MAX_EXAMPLES_PER_TYPE})")
                elif any(len(x) > MAX_EXAMPLE_STR_LEN for x in exs):
                    errors.append(f"entity_types[{idx}].examples contains items exceeding {MAX_EXAMPLE_STR_LEN} chars")

    # 7. Relation types validation
    relation_types = data.get("relation_types")
    if relation_types is None:
        errors.append("Field 'relation_types' is required")
        relation_count = 0
    elif not isinstance(relation_types, list) or len(relation_types) == 0:
        errors.append("Field 'relation_types' must be a non-empty list")
        relation_count = 0
    elif len(relation_types) > MAX_RELATION_TYPES:
        errors.append(f"Field 'relation_types' exceeds maximum count ({len(relation_types)} > {MAX_RELATION_TYPES})")
        relation_count = len(relation_types)
    else:
        relation_count = len(relation_types)
        seen_relations = set()
        for idx, rt in enumerate(relation_types):
            if not isinstance(rt, dict):
                errors.append(f"relation_types[{idx}] must be a dictionary, found {type(rt).__name__}")
                continue
            for k in rt.keys():
                if k not in ALLOWED_RELATION_KEYS:
                    errors.append(f"relation_types[{idx}] contains unknown key '{k}'")
            rt_name = rt.get("name")
            if rt_name is None:
                errors.append(f"relation_types[{idx}].name is required")
            elif not isinstance(rt_name, str):
                errors.append(f"relation_types[{idx}].name must be a string, found {type(rt_name).__name__}")
            else:
                s_name = rt_name.strip()
                if not s_name or not _NAME_REGEX.fullmatch(s_name):
                    errors.append(f"relation_types[{idx}].name '{s_name}' is invalid")
                else:
                    low_name = s_name.lower()
                    if low_name in seen_relations:
                        errors.append(f"Duplicate relation type name '{s_name}' in relation_types")
                    seen_relations.add(low_name)
            rt_desc = rt.get("description")
            if rt_desc is None:
                errors.append(f"relation_types[{idx}].description is required")
            elif not isinstance(rt_desc, str):
                errors.append(f"relation_types[{idx}].description must be a string, found {type(rt_desc).__name__}")
            elif len(rt_desc) > MAX_TYPE_DESC_LEN:
                errors.append(f"relation_types[{idx}].description exceeds maximum length ({len(rt_desc)} > {MAX_TYPE_DESC_LEN})")
            if "priority" in rt:
                p_val = rt["priority"]
                if not isinstance(p_val, str) or p_val.lower() not in ALLOWED_PRIORITIES:
                    errors.append(
                        f"relation_types[{idx}].priority '{p_val}' is invalid (must be one of {sorted(ALLOWED_PRIORITIES)})"
                    )
            if "examples" in rt:
                exs = rt["examples"]
                if not isinstance(exs, list) or not all(isinstance(x, str) for x in exs):
                    errors.append(f"relation_types[{idx}].examples must be a list of strings")
                elif len(exs) > MAX_EXAMPLES_PER_TYPE:
                    errors.append(f"relation_types[{idx}].examples exceeds maximum count ({len(exs)} > {MAX_EXAMPLES_PER_TYPE})")
                elif any(len(x) > MAX_EXAMPLE_STR_LEN for x in exs):
                    errors.append(f"relation_types[{idx}].examples contains items exceeding {MAX_EXAMPLE_STR_LEN} chars")

    # 8. Extraction rules validation (optional, strictly aligned with runtime ExtractionRules)
    rules = data.get("extraction_rules")
    if rules is not None:
        if not isinstance(rules, dict):
            errors.append(f"Field 'extraction_rules' must be a dictionary, found {type(rules).__name__}")
        else:
            for k in rules.keys():
                if k not in ALLOWED_RULES_KEYS:
                    errors.append(f"extraction_rules contains unknown key '{k}'")
            if "max_entities" in rules:
                val = rules["max_entities"]
                if isinstance(val, bool) or not isinstance(val, int) or val < 1 or val > MAX_ENTITY_TYPES:
                    errors.append(f"extraction_rules.max_entities must be an integer between 1 and {MAX_ENTITY_TYPES}")
            if "max_relations" in rules:
                val = rules["max_relations"]
                if isinstance(val, bool) or not isinstance(val, int) or val < 1 or val > MAX_RELATION_TYPES:
                    errors.append(f"extraction_rules.max_relations must be an integer between 1 and {MAX_RELATION_TYPES}")
            for bool_field in RULE_BOOLEAN_FIELDS:
                if bool_field in rules and not isinstance(rules[bool_field], bool):
                    errors.append(f"extraction_rules.{bool_field} must be a boolean")
            if "priority_entities" in rules:
                pe = rules["priority_entities"]
                if not isinstance(pe, list) or not all(isinstance(x, str) for x in pe):
                    errors.append("extraction_rules.priority_entities must be a list of entity type name strings")
                elif len(pe) > MAX_ENTITY_TYPES:
                    errors.append(f"extraction_rules.priority_entities exceeds maximum count ({len(pe)} > {MAX_ENTITY_TYPES})")
            if "special_instructions" in rules:
                si = rules["special_instructions"]
                if not isinstance(si, str):
                    errors.append("extraction_rules.special_instructions must be a string")
                elif len(si) > MAX_SPECIAL_INSTRUCTIONS_LEN:
                    errors.append(f"extraction_rules.special_instructions exceeds maximum length ({len(si)} > {MAX_SPECIAL_INSTRUCTIONS_LEN})")

    # 9. Examples validation (optional)
    examples = data.get("examples")
    if examples is not None:
        if not isinstance(examples, list) or not all(isinstance(x, dict) for x in examples):
            errors.append("Field 'examples' must be a list of dictionaries")
        elif len(examples) > MAX_EXAMPLES_PER_TYPE:
            errors.append(f"Field 'examples' exceeds maximum count ({len(examples)} > {MAX_EXAMPLES_PER_TYPE})")

    if errors:
        return {
            "status": "ok",
            "valid": False,
            "name": name,
            "version": version,
            "sha256": None,
            "entity_types_count": entity_count,
            "relation_types_count": relation_count,
            "yaml_content": None,
            "errors": errors,
            "warnings": warnings,
        }, None

    normalized = _normalize_yaml_text(content_yaml)
    
    # Invariant de sécurité : re-parser le contenu normalisé doit produire exactement les mêmes données
    try:
        reloaded = yaml.load(normalized, Loader=StrictSafeLoader)
        if reloaded != data:
            return {
                "status": "ok",
                "valid": False,
                "name": name,
                "version": version,
                "sha256": None,
                "entity_types_count": entity_count,
                "relation_types_count": relation_count,
                "yaml_content": None,
                "errors": ["Normalized YAML altered data semantics"],
                "warnings": warnings,
            }, None
    except Exception as e:
        return {
            "status": "ok",
            "valid": False,
            "name": name,
            "version": version,
            "sha256": None,
            "entity_types_count": entity_count,
            "relation_types_count": relation_count,
            "yaml_content": None,
            "errors": [f"Normalized YAML failed reparsing: {e}"],
            "warnings": warnings,
        }, None

    sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    return {
        "status": "ok",
        "valid": True,
        "name": name,
        "version": version,
        "sha256": sha256,
        "entity_types_count": entity_count,
        "relation_types_count": relation_count,
        "yaml_content": normalized,
        "errors": [],
        "warnings": warnings,
    }, data


def _validate_ontology_data(content_yaml: str) -> dict:
    """Valide la structure et la syntaxe d'une ontologie YAML sans aucun effet de bord."""
    res, _ = _validate_and_parse_ontology(content_yaml)
    return res
