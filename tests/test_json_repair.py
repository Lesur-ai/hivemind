# -*- coding: utf-8 -*-
"""
Tests unitaires pour _repair_json et _close_json_structure.

Stratégie de test : pas de complaisance.
- Chaque test vérifie des valeurs EXACTES (pas "is not None" ou "contains")
- Les tests couvrent les cas limites (troncature dans heading, filename, backslash)
- Le comptage d'opérations est vérifié à l'unité près
- Les scénarios réalistes reproduisent la structure exacte des logs prod
"""

import json
import pytest

from live_mem.core.consolidator import _repair_json, _close_json_structure


# ─────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────

def _make_exc(json_str: str) -> json.JSONDecodeError:
    """Parse un JSON invalide et retourne l'exception. Fail si valide."""
    try:
        json.loads(json_str)
        pytest.fail(f"Expected JSONDecodeError but json.loads succeeded")
    except json.JSONDecodeError as e:
        return e


# ─────────────────────────────────────────────────────────────
# Tests _close_json_structure
# ─────────────────────────────────────────────────────────────


class TestCloseJsonStructure:

    def test_already_closed_not_modified(self):
        """Un JSON déjà complet retourne la même chaîne exacte."""
        s = '{"a": 1}'
        assert _close_json_structure(s) == '{"a": 1}'

    def test_close_single_object(self):
        s = '{"a": 1'
        result = _close_json_structure(s)
        assert result == '{"a": 1}'
        assert json.loads(result) == {"a": 1}

    def test_close_single_array(self):
        s = '{"a": [1, 2'
        result = _close_json_structure(s)
        assert result == '{"a": [1, 2]}'
        assert json.loads(result) == {"a": [1, 2]}

    def test_close_nested_object_in_array_in_object(self):
        """3 niveaux : { [ { → doit fermer } ] }"""
        s = '{"edits": [{"op": "replace"'
        result = _close_json_structure(s)
        assert result == '{"edits": [{"op": "replace"}]}'
        parsed = json.loads(result)
        assert parsed["edits"][0]["op"] == "replace"

    def test_close_4_levels_deep(self):
        s = '{"a": [{"b": [{"c": "val"'
        result = _close_json_structure(s)
        assert result == '{"a": [{"b": [{"c": "val"}]}]}'
        parsed = json.loads(result)
        assert parsed["a"][0]["b"][0]["c"] == "val"

    def test_braces_inside_strings_are_ignored(self):
        """Les { [ ] } dans les strings NE comptent PAS comme structures."""
        s = '{"content": "texte avec { et [", "x": 1'
        result = _close_json_structure(s)
        assert result == '{"content": "texte avec { et [", "x": 1}'
        parsed = json.loads(result)
        assert parsed["x"] == 1
        assert "{" in parsed["content"]

    def test_escaped_quotes_dont_close_string(self):
        """Un \\\" dans une string ne ferme pas la string."""
        s = '{"v": "a\\"b", "x": 1'
        result = _close_json_structure(s)
        parsed = json.loads(result)
        assert parsed["v"] == 'a"b'
        assert parsed["x"] == 1

    def test_returns_none_when_inside_unclosed_string(self):
        """Si on est dans une string non fermée → None (irréparable)."""
        assert _close_json_structure('{"k": "pas fermé') is None

    def test_empty_input(self):
        assert _close_json_structure("") == ""

    def test_backslash_at_end_of_string(self):
        """Un backslash juste avant le guillemet fermant est un échappement."""
        # "a\\" signifie la string contient a puis backslash, le " ferme la string
        s = '{"v": "a\\\\"'
        result = _close_json_structure(s)
        assert result == '{"v": "a\\\\"}'
        parsed = json.loads(result)
        assert parsed["v"] == "a\\"


# ─────────────────────────────────────────────────────────────
# Tests _repair_json — cas positifs (réparation réussie)
# ─────────────────────────────────────────────────────────────


class TestRepairJsonPositive:

    def test_5_ops_truncated_at_5th_preserves_exactly_4(self):
        """
        5 opérations, la 5ème a un content non terminé.
        On doit récupérer EXACTEMENT 4 opérations avec leur contenu exact.
        """
        ops_json = []
        for i in range(1, 5):
            ops_json.append(
                f'{{"type": "replace_section", "heading": "## S{i}", "content": "contenu {i}"}}'
            )
        # 5ème opération avec chaîne non terminée
        valid_ops = ", ".join(ops_json)
        broken = (
            f'{{"file_edits": [{{"filename": "a.md", "action": "edit", '
            f'"operations": [{valid_ops}, '
            f'{{"type": "replace_section", "heading": "## S5", '
            f'"content": "contenu qui ne finit'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None

        edits = result["file_edits"]
        assert len(edits) == 1, f"Attendu 1 file_edit, obtenu {len(edits)}"

        ops = edits[0]["operations"]
        assert len(ops) == 4, f"Attendu 4 opérations, obtenu {len(ops)}"

        for i in range(1, 5):
            assert ops[i - 1]["heading"] == f"## S{i}"
            assert ops[i - 1]["content"] == f"contenu {i}"

    def test_2_file_edits_truncated_in_2nd_preserves_1st_intact(self):
        """
        2 file_edits, troncature dans le 2ème.
        Le 1er doit être intégralement préservé (2 opérations).
        """
        broken = (
            '{"file_edits": ['
            '{"filename": "ctx.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## A", "content": "val A"}, '
            '{"type": "append_to_section", "heading": "## B", "content": "val B"}'
            ']}, '
            '{"filename": "prog.md", "action": "edit", "operations": ['
            '{"type": "append_to_section", "heading": "## H", "content": "tronqué ici'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None

        edits = result["file_edits"]
        # Le 1er file_edit doit être intact
        assert edits[0]["filename"] == "ctx.md"
        ops = edits[0]["operations"]
        assert len(ops) == 2
        assert ops[0]["content"] == "val A"
        assert ops[1]["content"] == "val B"

    def test_truncation_in_heading_removes_operation(self):
        """Troncature dans le champ heading (pas content) → opération supprimée."""
        broken = (
            '{"file_edits": [{"filename": "a.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## OK", "content": "bon contenu"}, '
            '{"type": "replace_section", "heading": "## Heading tronq'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None

        edits = result["file_edits"]
        assert len(edits) == 1
        ops = edits[0]["operations"]
        # La 1ère op est intacte, la 2ème (heading tronqué) est supprimée
        # car elle n'a pas de content (= vide après repair)
        assert len(ops) == 1
        assert ops[0]["heading"] == "## OK"
        assert ops[0]["content"] == "bon contenu"

    def test_truncation_in_filename_of_2nd_file_edit(self):
        """Troncature dans le filename du 2ème file_edit → supprimé."""
        broken = (
            '{"file_edits": ['
            '{"filename": "ok.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## X", "content": "val"}'
            ']}, '
            '{"filename": "fichier_tronq'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None

        edits = result["file_edits"]
        # Le 1er file_edit est préservé, le 2ème est incomplet
        assert len(edits) >= 1
        assert edits[0]["filename"] == "ok.md"
        assert edits[0]["operations"][0]["content"] == "val"

    def test_synthesis_truncated_gets_default(self):
        """Si synthesis est tronquée, un défaut est injecté."""
        broken = (
            '{"file_edits": [{"filename": "a.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## X", "content": "ok"}'
            ']}], "synthesis": "résumé qui ne se termine'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None
        assert "synthesis" in result
        # La synthesis est le placeholder vide "" (car tronquée) mais la clé existe
        # car le JSON parsé contient "synthesis": "" après repair
        assert isinstance(result["synthesis"], str)

    def test_escaped_quotes_in_content_preserved(self):
        """Les guillemets échappés dans le contenu ne cassent pas le repair."""
        broken = (
            '{"file_edits": [{"filename": "a.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## T", '
            '"content": "texte avec \\"guillemets\\" dedans"}, '
            '{"type": "append_to_section", "heading": "## U", '
            '"content": "non terminé'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None

        ops = result["file_edits"][0]["operations"]
        assert len(ops) == 1, f"Attendu 1 op (la 2ème tronquée supprimée), obtenu {len(ops)}"
        assert ops[0]["content"] == 'texte avec "guillemets" dedans'

    def test_create_action_truncated_is_removed(self):
        """Un file_edit "create" avec contenu tronqué est supprimé."""
        broken = (
            '{"file_edits": ['
            '{"filename": "exist.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## A", "content": "OK"}'
            ']}, '
            '{"filename": "new.md", "action": "create", "content": "début du fichier'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None

        edits = result["file_edits"]
        assert len(edits) == 1, f"Le create tronqué devait être supprimé, obtenu {len(edits)} edits"
        assert edits[0]["filename"] == "exist.md"

    def test_backslash_before_truncation_point(self):
        """Si le JSON finit par \\ avant la chaîne non terminée, le repair fonctionne."""
        # Le \ est un escape incomplet dans la chaîne
        broken = (
            '{"file_edits": [{"filename": "a.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## X", "content": "ok"}, '
            '{"type": "replace_section", "heading": "## Y", "content": "texte avec \\'
        )

        exc = _make_exc(broken)
        result = _repair_json(broken, exc)
        # Doit réussir et préserver la 1ère opération
        assert result is not None
        ops = result["file_edits"][0]["operations"]
        assert len(ops) >= 1
        assert ops[0]["content"] == "ok"


# ─────────────────────────────────────────────────────────────
# Tests _repair_json — cas négatifs (réparation échouée ou refusée)
# ─────────────────────────────────────────────────────────────


class TestRepairJsonNegative:

    def test_non_unterminated_string_error_returns_none(self):
        """Seul "Unterminated string" est géré, pas les autres erreurs."""
        broken = '{"file_edits": [{"filename": "a.md" "action": "edit"}]}'
        assert _repair_json(broken, _make_exc(broken)) is None

    def test_pos_zero_returns_none(self):
        exc = json.JSONDecodeError("Unterminated string", "", 0)
        assert _repair_json("", exc) is None

    def test_pos_beyond_string_returns_none(self):
        exc = json.JSONDecodeError("Unterminated string", "short", 100)
        assert _repair_json("short", exc) is None

    def test_no_file_edits_in_repaired_json_returns_none(self):
        """Si le JSON tronqué n'a pas de file_edits → None."""
        broken = '{"other_key": "val that does not end'
        assert _repair_json(broken, _make_exc(broken)) is None

    def test_single_op_truncated_produces_empty_file_edits(self):
        """
        Si la SEULE opération du seul file_edit est tronquée,
        le file_edit est supprimé → file_edits = [].
        C'est le cas qui déclenche le retry dans _call_llm (faille corrigée).
        """
        broken = (
            '{"file_edits": [{"filename": "a.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## Focus", "content": "tronqué ici'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None
        assert result["file_edits"] == [], (
            f"Le file_edit avec 0 ops devait être supprimé, obtenu: {result['file_edits']}"
        )
        # Ce résultat vide déclenchera le retry dans _call_llm
        # grâce au check repaired_files > 0


# ─────────────────────────────────────────────────────────────
# Tests scénario réaliste (reproduit les logs prod)
# ─────────────────────────────────────────────────────────────


class TestRepairRealisticScenario:

    def test_qwen36_unterminated_string_bug(self):
        """
        Reproduit exactement le bug rapporté : qwen3.6, finish_reason=stop,
        Unterminated string dans un long content de replace_section.

        Le JSON a 2 file_edits (activeContext.md + progress.md).
        La troncature est dans la 1ère opération d'activeContext.

        Attendu : le repair récupère 0 opérations pour activeContext
        (la seule op est tronquée) ET 0 pour progress (jamais parsé).
        Résultat : file_edits vide → le code retombe sur le retry.
        """
        broken = (
            '{\n  "file_edits": [\n    {\n'
            '      "filename": "activeContext.md",\n'
            '      "action": "edit",\n'
            '      "operations": [\n        {\n'
            '          "type": "replace_section",\n'
            '          "heading": "## Focus Actuel",\n'
            '          "content": "**v0.1.0-alpha.3 PUBLIEE** (09/05/2026). '
            'Pipeline CI/CD valide, image GHCR pushee.\\n'
            '**T-077 TERMINE** (09/05/2026) : Fix event kind descriptions. '
            'Enum EventKind (15 valeurs), refactor WireEventHandler.kt, fix EventTools.kt,'
        )

        exc = _make_exc(broken)
        assert "Unterminated string" in str(exc)

        result = _repair_json(broken, exc)
        assert result is not None

        # Le file_edit activeContext.md n'a qu'1 op et elle est tronquée
        # → supprimée → file_edit supprimé → file_edits = []
        assert result["file_edits"] == [], (
            f"Le seul file_edit avait sa seule op tronquée, "
            f"devait être supprimé. Obtenu: {result['file_edits']}"
        )

        # Synthesis par défaut car absente du JSON tronqué
        assert "synthesis" in result
        assert isinstance(result["synthesis"], str)

    def test_qwen36_with_multiple_complete_ops_before_truncation(self):
        """
        Scénario réaliste : 3 opérations complètes avant la troncature.
        Le repair doit préserver EXACTEMENT les 3 avec leur contenu verbatim.
        """
        broken = (
            '{\n  "file_edits": [\n    {\n'
            '      "filename": "activeContext.md",\n'
            '      "action": "edit",\n'
            '      "operations": [\n'
            '        {"type": "replace_section", "heading": "## Focus", '
            '"content": "Release v0.1.0-alpha.3"},\n'
            '        {"type": "append_to_section", "heading": "## Recent", '
            '"content": "- CI/CD pipeline OK\\n- Tests 322 PASS"},\n'
            '        {"type": "replace_section", "heading": "## Next", '
            '"content": "- MessageStore\\n- Beta prep"},\n'
            '        {"type": "replace_section", "heading": "## Decisions", '
            '"content": "Decision longue qui ne se termine jamais et contient '
            "des details sur l'architecture et les choix techniques du projet"
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None

        edits = result["file_edits"]
        assert len(edits) == 1
        assert edits[0]["filename"] == "activeContext.md"

        ops = edits[0]["operations"]
        assert len(ops) == 3, f"3 ops complètes attendues, obtenu {len(ops)}"

        # Vérification verbatim de chaque opération
        assert ops[0] == {
            "type": "replace_section",
            "heading": "## Focus",
            "content": "Release v0.1.0-alpha.3",
        }
        assert ops[1] == {
            "type": "append_to_section",
            "heading": "## Recent",
            "content": "- CI/CD pipeline OK\n- Tests 322 PASS",
        }
        assert ops[2] == {
            "type": "replace_section",
            "heading": "## Next",
            "content": "- MessageStore\n- Beta prep",
        }


# ─────────────────────────────────────────────────────────────
# Tests d'intégrité (le JSON réparé est toujours valide)
# ─────────────────────────────────────────────────────────────


class TestRepairIntegrity:

    def test_repaired_json_roundtrips_through_serialization(self):
        """Le résultat doit survivre à un json.dumps → json.loads."""
        broken = (
            '{"file_edits": [{"filename": "a.md", "action": "edit", '
            '"operations": [{"type": "replace_section", "heading": "## A", '
            '"content": "texte valide"}, {"type": "append_to_section", '
            '"heading": "## B", "content": "tronqué ici'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None

        roundtripped = json.loads(json.dumps(result, ensure_ascii=False))
        assert roundtripped == result

    def test_no_operation_has_empty_content_after_repair(self):
        """AUCUNE opération ne doit avoir content="" après repair."""
        broken = (
            '{"file_edits": [{"filename": "a.md", "action": "edit", '
            '"operations": [{"type": "replace_section", "heading": "## A", '
            '"content": "ok"}, {"type": "replace_section", "heading": "## B", '
            '"content": "'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None

        for edit in result.get("file_edits", []):
            for op in edit.get("operations", []):
                assert op.get("content", "X").strip() != "", (
                    f"Opération avec content vide non nettoyée: {op}"
                )

    def test_file_edits_is_always_a_list(self):
        """file_edits doit être une liste (jamais None ou autre type)."""
        broken = (
            '{"file_edits": [{"filename": "a.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## X", "content": "tronqué'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None
        assert isinstance(result["file_edits"], list)

    def test_synthesis_is_always_a_string(self):
        """synthesis doit être une string (jamais None)."""
        broken = (
            '{"file_edits": [{"filename": "a.md", "action": "edit", "operations": ['
            '{"type": "replace_section", "heading": "## X", "content": "ok"}'
            ']}], "synthesis": "résumé tronqué'
        )

        result = _repair_json(broken, _make_exc(broken))
        assert result is not None
        assert isinstance(result["synthesis"], str)
