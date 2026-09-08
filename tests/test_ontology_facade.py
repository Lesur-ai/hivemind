# -*- coding: utf-8 -*-
"""
Tests unitaires et d'intégration pour la façade des ontologies (Issue #449 - Final Validation).

Vérifie :
1. Validation pure sans état & alignement runtime complet :
   - Conformité sur toutes les 7 ontologies embarquées (y compris presales, general, etc.).
   - Invariant de reparsabilité exacte du `yaml_content` normalisé avec préservation sémantique (`|+` en EOF et corps).
   - Validation directe de bout en bout avec `Ontology.build_prompt()`.
   - Rejet des types invalides dans `extraction_rules` (ex: `priority_entities: 123`).
   - Rejet strict des alias (`*alias`) et des ancres (`&anchor`).
   - Rejet strict des doublons de clés au niveau racine et dans les listes d'entités/relations.
   - Rejet des types non-scalaires (entiers/listes/dicts au lieu de chaînes).
   - Rejet des clés inconnues au niveau racine, entités, relations et extraction_rules.
   - Protection anti-amplification sur collections explicites (> 5000 nœuds).
2. Enregistrement FastMCP et classification P10 operator.
3. Contrôle d'accès & authentification :
   - Rejet systématique des requêtes non-authentifiées avant tout traitement de charge utile (fail-closed, 0 appel bridge).
   - Rejet des requêtes sur espaces non-autorisés (0 appel bridge).
4. Délégations LongEngine et GraphBridge :
   - Délégation réelle de LongEngine vers GraphBridge.
   - Résolution read-only transparente pour espaces sans binding (fallback embedded runtime, generate=False, zéro mutation S3).
   - Rejet effectif par `_guard_url` d'une URL bloquée (anti-SSRF, 0 construction de client).
"""

import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from mcp.server.fastmcp import FastMCP
from live_mem.tools import register_all_tools
from live_mem.auth.context import current_token_info
from live_mem.core.engines import get_engine_registry
from live_mem.core.graph_bridge import GraphBridgeService, get_graph_bridge
from live_mem.core.models import GraphMemoryConfig
from live_mem.config import Settings
from mcp_memory.core.ontology_validator import (
    MAX_ONTOLOGY_BYTES,
    MAX_ENTITY_TYPES,
    MAX_RELATION_TYPES,
    _validate_ontology_data,
    _validate_and_parse_ontology,
    _normalize_yaml_text,
    StrictSafeLoader,
)


VALID_ONTOLOGY_YAML = """
name: test-domain
version: "1.0.0"
description: "Ontologie de test pour validation"
context: "Contexte d'extraction de test"
entity_types:
  - name: Server
    description: "Serveur physique ou virtuel"
    priority: "high"
    examples: ["srv-01", "srv-02"]
  - name: Database
    description: "Base de données"
    priority: "normal"
    examples: ["db-prod", "db-staging"]
relation_types:
  - name: HOSTS
    description: "Le serveur héberge la base"
    examples: ["Server HOSTS Database"]
extraction_rules:
  max_entities: 50
  max_relations: 50
  include_metrics: true
  include_durations: true
  include_amounts: true
  extract_implicit_relations: false
  priority_entities:
    - Server
  special_instructions: "Extrayez toutes les configurations réseau."
"""

INDENTED_ROOT_YAML = """  name: test-indented
  version: "1.0.0"
  description: "Ontology with indented root mapping"
  entity_types:
    - name: Server
      description: "Serveur"
  relation_types:
    - name: REL
      description: "Relation"
"""

INVALID_SYNTAX_YAML = """
name: test-broken
version: "1.0"
description: "Broken"
entity_types: [unclosed list
"""

ALIAS_YAML = """
name: test-alias
version: "1.0"
description: "Alias test"
a: &a ["data", "data"]
b: *a
entity_types:
  - name: Server
    description: "Serveur"
relation_types:
  - name: REL
    description: "Relation"
"""

ANCHOR_ALONE_YAML = """
name: test-anchor
version: "1.0"
description: &desc "Anchor test without alias"
entity_types:
  - name: Server
    description: "Serveur"
relation_types:
  - name: REL
    description: "Relation"
"""

DUPLICATE_ROOT_KEYS_YAML = """
name: test-dup
version: "1.0"
description: "First description"
name: test-dup-second
description: "Second description"
entity_types:
  - name: Server
    description: "Serveur"
relation_types:
  - name: REL
    description: "Relation"
"""

DUPLICATE_ENTITIES_YAML = """
name: test-duplicate
version: "1.0"
description: "Test duplicate entity types"
context: "Context"
entity_types:
  - name: Server
    description: "Serveur"
  - name: server
    description: "Serveur doublon case insensitive"
relation_types:
  - name: REL
    description: "Relation"
extraction_rules:
  max_entities: 10
  max_relations: 10
"""

DUPLICATE_RELATIONS_YAML = """
name: test-duplicate-rel
version: "1.0"
description: "Test duplicate relations"
context: "Context"
entity_types:
  - name: Server
    description: "Serveur"
relation_types:
  - name: CONNECTS_TO
    description: "Connexion"
  - name: connects_to
    description: "Connexion doublon"
extraction_rules:
  max_entities: 10
  max_relations: 10
"""

NON_SCALAR_TYPES_YAML = """
name: 7
version: {}
description: []
context: false
entity_types:
  - name: E
    description: {}
relation_types:
  - name: R
    description: []
"""

UNKNOWN_ROOT_KEYS_YAML = """
name: test-unknown
version: "1.0"
description: "Test unknown root keys"
unknown_field: "illegal value"
entity_types:
  - name: Server
    description: "Serveur"
relation_types:
  - name: REL
    description: "Relation"
"""

UNKNOWN_TYPE_KEYS_YAML = """
name: test-type-unknown
version: "1.0"
description: "Test unknown type keys"
entity_types:
  - name: Server
    description: "Serveur"
    unexpected_field: true
relation_types:
  - name: REL
    description: "Relation"
"""

UNKNOWN_RULES_KEYS_YAML = """
name: test-rules-unknown
version: "1.0"
description: "Test unknown extraction rules keys"
entity_types:
  - name: Server
    description: "Serveur"
relation_types:
  - name: REL
    description: "Relation"
extraction_rules:
  unknown_rule: true
"""

INVALID_PRIORITY_ENTITIES_YAML = """
name: test-invalid-pe
version: "1.0"
description: "Test invalid priority entities type"
entity_types:
  - name: Server
    description: "Serveur"
relation_types:
  - name: REL
    description: "Relation"
extraction_rules:
  priority_entities: 123
"""


@pytest.fixture
def mcp_server():
    mcp = FastMCP("test-ontology-mcp")
    register_all_tools(mcp)
    return mcp


@pytest.fixture
def auth_read_token():
    token_dict = {
        "client_name": "test-agent",
        "permissions": ["read", "write"],
        "allowed_resources": ["test-space"],
        "token_hash": "sha256:dummy",
    }
    tok = current_token_info.set(token_dict)
    try:
        yield token_dict
    finally:
        current_token_info.reset(tok)


# =============================================================================
# 1. Tests de validation pure & sécurité parseur
# =============================================================================

@pytest.mark.asyncio
async def test_ontology_tools_registered(mcp_server):
    """Vérifie que les 3 outils ontology_* sont bien enregistrés sur FastMCP."""
    tool_names = set(mcp_server._tool_manager._tools.keys())
    assert "ontology_list" in tool_names
    assert "ontology_get" in tool_names
    assert "ontology_validate" in tool_names


@pytest.mark.asyncio
async def test_ontology_validate_pure_success():
    """Vérifie la validation pure d'une ontologie YAML valide et son schéma de retour complet."""
    res = _validate_ontology_data(VALID_ONTOLOGY_YAML)
    assert res["status"] == "ok"
    assert res["valid"] is True
    assert res["name"] == "test-domain"
    assert res["version"] == "1.0.0"
    assert "sha256" in res
    assert res["entity_types_count"] == 2
    assert res["relation_types_count"] == 1
    assert res["yaml_content"] is not None
    assert res["errors"] == []

    # Invariant de reparsabilité exacte du yaml_content retourné
    reloaded = yaml.load(res["yaml_content"], Loader=StrictSafeLoader)
    initial = yaml.load(VALID_ONTOLOGY_YAML, Loader=StrictSafeLoader)
    assert reloaded == initial


@pytest.mark.asyncio
async def test_ontology_validate_preserves_root_indentation():
    """Vérifie que _normalize_yaml_text n'altère pas l'indentation d'un document indenté."""
    res = _validate_ontology_data(INDENTED_ROOT_YAML)
    assert res["status"] == "ok"
    assert res["valid"] is True
    reloaded = yaml.load(res["yaml_content"], Loader=StrictSafeLoader)
    initial = yaml.load(INDENTED_ROOT_YAML, Loader=StrictSafeLoader)
    assert reloaded == initial


@pytest.mark.asyncio
async def test_ontology_validate_chomping_keep_preserves_trailing_newlines_at_eof():
    """Vérifie que la normalisation préserve les sauts de ligne finaux d'un scalaire block |+ situé à EOF."""
    yaml_text = """name: test-chomping
version: "1.0"
description: "Test chomping"
entity_types:
  - name: Server
    description: "Serveur"
relation_types:
  - name: REL
    description: "Relation"
context: |+
  final retained context


"""
    res = _validate_ontology_data(yaml_text)
    assert res["status"] == "ok"
    assert res["valid"] is True
    reloaded = yaml.load(res["yaml_content"], Loader=StrictSafeLoader)
    initial = yaml.load(yaml_text, Loader=StrictSafeLoader)
    assert reloaded == initial
    assert reloaded["context"] == initial["context"]


@pytest.mark.asyncio
async def test_ontology_validate_runtime_alignment_build_prompt():
    """Vérifie qu'une ontologie validée peut être instanciée par Ontology et exécuter build_prompt()."""
    from mcp_memory.core.ontology import (
        Ontology,
        EntityTypeDefinition,
        RelationTypeDefinition,
        ExtractionRules,
    )
    res = _validate_ontology_data(VALID_ONTOLOGY_YAML)
    assert res["valid"] is True

    parsed = yaml.load(res["yaml_content"], Loader=StrictSafeLoader)
    entity_types = [EntityTypeDefinition(**et) for et in parsed["entity_types"]]
    relation_types = [RelationTypeDefinition(**rt) for rt in parsed["relation_types"]]
    extraction_rules = ExtractionRules(**parsed["extraction_rules"])
    ont = Ontology(
        name=parsed["name"],
        version=parsed["version"],
        description=parsed["description"],
        context=parsed["context"],
        entity_types=entity_types,
        relation_types=relation_types,
        extraction_rules=extraction_rules,
    )
    prompt = ont.build_prompt("Texte de document test sur srv-01 et db-prod")
    assert "Server" in prompt
    assert "Database" in prompt
    assert "HOSTS" in prompt


@pytest.mark.asyncio
async def test_ontology_validate_rejects_invalid_priority_entities_type():
    """Vérifie que priority_entities non-liste est rejeté avec valid=false."""
    res = _validate_ontology_data(INVALID_PRIORITY_ENTITIES_YAML)
    assert res["status"] == "ok"
    assert res["valid"] is False
    assert any("priority_entities must be a list" in err for err in res.get("errors", []))


@pytest.mark.asyncio
async def test_ontology_validate_rejects_unknown_rules_keys():
    """Vérifie le rejet de clés inconnues dans extraction_rules."""
    res = _validate_ontology_data(UNKNOWN_RULES_KEYS_YAML)
    assert res["status"] == "ok"
    assert res["valid"] is False
    assert any("extraction_rules contains unknown key 'unknown_rule'" in err for err in res.get("errors", []))


@pytest.mark.asyncio
async def test_ontology_validate_large_explicit_collection_rejected():
    """Vérifie qu'un document avec une collection explicite de >5000 nœuds est rejeté tôt."""
    large_yaml = "name: large\nversion: '1.0'\ndescription: 'desc'\nentity_types:\n"
    for i in range(150):
        large_yaml += f"  - name: E{i}\n    description: D{i}\n    examples: [" + ", ".join(f"'ex_{j}'" for j in range(40)) + "]\n"
    large_yaml += "relation_types:\n  - name: REL\n    description: D\n"

    res = _validate_ontology_data(large_yaml)
    assert res["status"] == "ok"
    assert res["valid"] is False
    assert any("exceeds maximum complexity" in err for err in res.get("errors", []))


@pytest.mark.asyncio
async def test_legacy_load_ontology_yaml_minimal_schema(monkeypatch):
    """Vérifie que _load_ontology_yaml accepte les schémas minimaux legacy sans version ni description."""
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "mock-s3-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "mock-s3-secret")
    monkeypatch.setenv("NEO4J_PASSWORD", "mock-neo4j-pass")
    from mcp_memory.server import _load_ontology_yaml
    legacy_yaml = """
name: legacy-ontology
entity_types:
  - name: Machine
relation_types:
  - name: CONNECTS
"""
    parsed = _load_ontology_yaml(legacy_yaml)
    assert parsed["name"] == "legacy-ontology"
    assert len(parsed["entity_types"]) == 1
    assert len(parsed["relation_types"]) == 1


@pytest.mark.asyncio
async def test_ontology_validate_invalid_syntax():
    """Vérifie le rejet d'un YAML syntaxiquement invalide."""
    res = _validate_ontology_data(INVALID_SYNTAX_YAML)
    assert res["status"] == "ok"
    assert res["valid"] is False
    assert len(res.get("errors", [])) > 0


@pytest.mark.asyncio
async def test_ontology_validate_alias_and_anchor_rejection():
    """Vérifie le rejet strict des alias et des ancres YAML (anti-bombes YAML / OOM)."""
    # 1. Alias
    res_alias = _validate_ontology_data(ALIAS_YAML)
    assert res_alias["status"] == "ok"
    assert res_alias["valid"] is False
    assert any("forbidden" in err.lower() for err in res_alias.get("errors", []))

    # 2. Ancre seule
    res_anchor = _validate_ontology_data(ANCHOR_ALONE_YAML)
    assert res_anchor["status"] == "ok"
    assert res_anchor["valid"] is False
    assert any("anchors are forbidden" in err.lower() for err in res_anchor.get("errors", []))


@pytest.mark.asyncio
async def test_ontology_validate_duplicate_root_keys():
    """Vérifie le rejet strict des doublons de clés YAML à la racine."""
    res = _validate_ontology_data(DUPLICATE_ROOT_KEYS_YAML)
    assert res["status"] == "ok"
    assert res["valid"] is False
    assert any("duplicate key" in err.lower() for err in res.get("errors", []))


@pytest.mark.asyncio
async def test_ontology_validate_duplicate_entities():
    """Vérifie le rejet des types d'entités dupliqués."""
    res = _validate_ontology_data(DUPLICATE_ENTITIES_YAML)
    assert res["status"] == "ok"
    assert res["valid"] is False
    assert any("duplicate" in err.lower() for err in res.get("errors", []))


@pytest.mark.asyncio
async def test_ontology_validate_duplicate_relations():
    """Vérifie le rejet des types de relations dupliqués."""
    res = _validate_ontology_data(DUPLICATE_RELATIONS_YAML)
    assert res["status"] == "ok"
    assert res["valid"] is False
    assert any("duplicate" in err.lower() for err in res.get("errors", []))


@pytest.mark.asyncio
async def test_ontology_validate_non_scalar_types():
    """Vérifie le rejet strict des types non-scalaires (name entier, version dict, etc.)."""
    res = _validate_ontology_data(NON_SCALAR_TYPES_YAML)
    assert res["status"] == "ok"
    assert res["valid"] is False
    errs = " ".join(res.get("errors", []))
    assert "must be a string" in errs


@pytest.mark.asyncio
async def test_ontology_validate_unknown_root_and_type_keys():
    """Vérifie le rejet des clés racine et clés de type non reconnues."""
    # 1. Root keys
    res_root = _validate_ontology_data(UNKNOWN_ROOT_KEYS_YAML)
    assert res_root["status"] == "ok"
    assert res_root["valid"] is False
    assert any("unknown root key 'unknown_field'" in err.lower() for err in res_root.get("errors", []))

    # 2. Type keys
    res_type = _validate_ontology_data(UNKNOWN_TYPE_KEYS_YAML)
    assert res_type["status"] == "ok"
    assert res_type["valid"] is False
    assert any("contains unknown key 'unexpected_field'" in err.lower() for err in res_type.get("errors", []))


@pytest.mark.asyncio
async def test_ontology_validate_size_limit():
    """Vérifie le rejet des contenus dépassant MAX_ONTOLOGY_BYTES."""
    huge_yaml = "name: huge\nversion: '1.0'\ndescription: 'desc'\nentity_types:\n" + ("  - name: E\n    description: D\n" * 20000)
    assert len(huge_yaml.encode("utf-8")) > MAX_ONTOLOGY_BYTES
    res = _validate_ontology_data(huge_yaml)
    assert res["status"] == "ok"
    assert res["valid"] is False
    assert any("maximum allowed size" in err.lower() for err in res.get("errors", []))


@pytest.mark.asyncio
async def test_ontology_validate_packaged_ontologies():
    """Vérifie que toutes les 7 ontologies livrées par défaut passent la validation stricte."""
    ont_dir = Path("services/graph-memory/ONTOLOGIES")
    assert ont_dir.exists()
    yaml_files = list(ont_dir.glob("*.yaml"))
    assert len(yaml_files) >= 5
    for yf in yaml_files:
        content = yf.read_text(encoding="utf-8")
        res = _validate_ontology_data(content)
        assert res["valid"] is True, f"Failed for {yf.name}: {res.get('errors')}"
        assert res["name"] is not None
        assert res["sha256"] is not None
        assert res["entity_types_count"] > 0
        assert res["relation_types_count"] > 0


# =============================================================================
# 2. Tests de gouvernance Auth & Contrôle d'accès (Matrice 3 outils)
# =============================================================================

@pytest.mark.parametrize("tool_name,args", [
    ("ontology_list", {"space_id": "test-space"}),
    ("ontology_get", {"space_id": "test-space", "name": "general"}),
    ("ontology_validate", {"space_id": "test-space", "content_yaml": VALID_ONTOLOGY_YAML}),
    ("ontology_validate", {"space_id": "test-space", "content_yaml": "x" * (MAX_ONTOLOGY_BYTES + 10)}),
])
@pytest.mark.asyncio
async def test_ontology_tools_unauthenticated_rejected_matrix(mcp_server, tool_name, args):
    """Vérifie le rejet immédiat d'une requête sans token sur tous les outils avant tout traitement."""
    tok = current_token_info.set(None)
    try:
        tool_fn = mcp_server._tool_manager._tools[tool_name].fn
        with patch.object(get_graph_bridge(), "list_ontologies", new_callable=AsyncMock) as mock_list, \
             patch.object(get_graph_bridge(), "get_ontology", new_callable=AsyncMock) as mock_get, \
             patch.object(get_graph_bridge(), "validate_ontology", new_callable=AsyncMock) as mock_val:

            res = await tool_fn(**args)
            assert res["status"] == "error"
            assert "authentication required" in res["message"].lower()
            mock_list.assert_not_called()
            mock_get.assert_not_called()
            mock_val.assert_not_called()
    finally:
        current_token_info.reset(tok)


@pytest.mark.parametrize("tool_name,args", [
    ("ontology_list", {"space_id": "forbidden-space"}),
    ("ontology_get", {"space_id": "forbidden-space", "name": "general"}),
    ("ontology_validate", {"space_id": "forbidden-space", "content_yaml": VALID_ONTOLOGY_YAML}),
])
@pytest.mark.asyncio
async def test_ontology_tools_access_denied_matrix(mcp_server, auth_read_token, tool_name, args):
    """Vérifie que l'accès à un espace non autorisé est refusé sur tous les outils sans appel bridge."""
    tool_fn = mcp_server._tool_manager._tools[tool_name].fn
    with patch.object(get_graph_bridge(), "list_ontologies", new_callable=AsyncMock) as mock_list, \
         patch.object(get_graph_bridge(), "get_ontology", new_callable=AsyncMock) as mock_get, \
         patch.object(get_graph_bridge(), "validate_ontology", new_callable=AsyncMock) as mock_val:

        res = await tool_fn(**args)
        assert res["status"] == "error"
        assert "not authorized" in res["message"].lower() or "access denied" in res["message"].lower()
        mock_list.assert_not_called()
        mock_get.assert_not_called()
        mock_val.assert_not_called()


# =============================================================================
# 3. Tests d'intégration LongEngine & GraphBridge
# =============================================================================

@pytest.mark.asyncio
async def test_long_engine_delegations():
    """Vérifie que LongEngine délègue fidèlement les appels d'ontologie à GraphBridge."""
    long_eng = get_engine_registry().long_engine()
    bridge = get_graph_bridge()

    with patch.object(bridge, "list_ontologies", new_callable=AsyncMock) as mock_list, \
         patch.object(bridge, "get_ontology", new_callable=AsyncMock) as mock_get, \
         patch.object(bridge, "validate_ontology", new_callable=AsyncMock) as mock_val:

        mock_list.return_value = {"status": "ok", "ontologies": ["general"]}
        mock_get.return_value = {"status": "ok", "name": "general"}
        mock_val.return_value = {"status": "ok", "valid": True}

        # 1. list
        res1 = await long_eng.list_ontologies("test-space")
        assert res1["status"] == "ok"
        mock_list.assert_called_once_with("test-space")

        # 2. get
        res2 = await long_eng.get_ontology("test-space", "general")
        assert res2["status"] == "ok"
        mock_get.assert_called_once_with("test-space", "general")

        # 3. validate
        res3 = await long_eng.validate_ontology("test-space", VALID_ONTOLOGY_YAML)
        assert res3["status"] == "ok"
        mock_val.assert_called_once_with("test-space", VALID_ONTOLOGY_YAML)


@pytest.mark.asyncio
async def test_graph_bridge_resolve_read_client_unlinked_fallback():
    """Vérifie que _resolve_read_client bascule sur le runtime embarqué pour les espaces non liés sans muter S3."""
    mock_settings = Settings(
        long_embedded_url="http://127.0.0.1:8765/mcp",
        long_embedded_enabled=True,
    )
    mock_storage = MagicMock()
    mock_storage.get_json = AsyncMock(return_value={"space_id": "test-space"})

    with patch("live_mem.core.graph_bridge.get_settings", return_value=mock_settings), \
         patch("live_mem.core.graph_bridge.get_storage", return_value=mock_storage), \
         patch("live_mem.core.graph_bridge.resolve_embedded_token", return_value="test-bearer-token") as mock_tok,          patch("live_mem.core.graph_bridge.GraphMemoryClient") as mock_client_cls:

        bridge = GraphBridgeService(client_factory=mock_client_cls)
        with patch.object(bridge, "_load_gm_config", new_callable=AsyncMock) as mock_load, \
             patch.object(bridge, "_guard_url", return_value=None):

            mock_load.return_value = (None, {"status": "error", "message": "Space 'test-space' is not connected to Graph Memory"})
            client, err = await bridge._resolve_read_client("test-space")
            assert err is None
            assert client is not None
            mock_load.assert_called_once_with("test-space")
            mock_storage.get_json.assert_called_once_with("test-space/_meta.json")
            mock_tok.assert_called_once_with(mock_settings, generate=False)
            mock_client_cls.assert_called_once_with("http://127.0.0.1:8765/mcp", "test-bearer-token")


@pytest.mark.asyncio
async def test_graph_bridge_ssrf_protection_executes_real_guard_and_fails_closed():
    """Vérifie que _resolve_read_client exécute la vraie protection _guard_url et bloque l'instanciation de client."""
    mock_settings = Settings(long_embedded_enabled=False)
    blocked_config = GraphMemoryConfig(url="http://169.254.169.254/mcp", token="secret")

    with patch("live_mem.core.graph_bridge.GraphMemoryClient") as mock_client_cls:
        bridge = GraphBridgeService(client_factory=mock_client_cls)
        with patch.object(bridge, "_load_gm_config", new_callable=AsyncMock) as mock_load:
            mock_load.return_value = (blocked_config, None)
            client, err = await bridge._resolve_read_client("test-space")
            assert client is None
            assert err is not None
            assert "not allowed" in err["message"].lower() or "blocked" in err["message"].lower()
            mock_client_cls.assert_not_called()


def test_ontology_manager_resolves_named_and_raw_yaml_direct():
    """Vérifie que OntologyManager résout fidèlement les ontologies nommées et les payloads YAML bruts."""
    from mcp_memory.core.ontology import OntologyManager

    manager = OntologyManager()
    # 1. Named ontology
    general_ont = manager.get_ontology("general")
    assert general_ont is not None
    assert general_ont.name == "general"

    # 2. Raw canonical YAML
    raw_yaml = """
name: my-cloud
version: "1.0.0"
description: "Cloud test ontology"
context: "Cloud context"
entity_types:
  - name: Service
    description: Microservice component
relation_types:
  - name: CALLS
    description: Service call
"""
    custom_ont = manager.get_ontology(raw_yaml)
    assert custom_ont is not None
    assert custom_ont.name == "my-cloud"
    assert any(et.name == "Service" for et in custom_ont.entity_types)
    assert any(rt.name == "CALLS" for rt in custom_ont.relation_types)

    # 3. Invalid YAML returns None fail-closed
    assert manager.get_ontology("invalid:\n  - [unclosed") is None


def test_ontology_label_redacts_raw_yaml():
    """Vérifie que get_ontology_label ne fait fuiter aucun contenu brut et compte les octets UTF-8."""
    from mcp_memory.core.ontology import OntologyManager

    manager = OntologyManager()
    # 1. Registered name returns name as-is
    assert manager.get_ontology_label("general") == "general"

    # 2. Raw YAML returns redacted digest and exact byte count (e.g. unicode multi-byte characters)
    raw_schema = "entities: [ProjetÉléphant]"
    label = manager.get_ontology_label(raw_schema)
    assert "Projet" not in label
    assert "Éléphant" not in label
    assert "custom_yaml(" in label
    assert f"{len(raw_schema.encode('utf-8'))} bytes" in label


@pytest.mark.asyncio
async def test_memory_create_rejects_raw_yaml_and_requires_registered_name():
    """Vérifie que memory_create n'accepte JAMAIS de schéma YAML brut et exige un nom enregistré."""
    from mcp_memory.server import memory_create, current_auth

    tok = current_auth.set(
        {"client_name": "test", "permissions": ["admin"], "memory_ids": []}
    )
    try:
        raw_yaml = """
name: inline-yaml
version: "1.0.0"
description: "Inline"
entity_types:
  - name: Service
"""
        # 1. Reject raw YAML in memory_create
        res = await memory_create(
            memory_id="test-mem-raw",
            name="Test Mem Raw",
            ontology=raw_yaml,
        )
        assert res["status"] == "error"
        assert "not found" in res["message"]
        assert "inline-yaml" not in res["message"]
        assert "Service" not in res["message"]
        assert "custom_yaml(" in res["message"]
    finally:
        current_auth.reset(tok)
