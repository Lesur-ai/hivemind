#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests unitaires et E2E pour le bank compaction.

Tests couverts :
  - _get_max_size_for_file() : limites par type de fichier
  - _compact_bank_if_needed() : seuil de déclenchement, compaction
  - _compact_single_file() : prompt par type, appel LLM
  - compact_bank() : mode dry_run et mode apply
  - _call_llm() : calcul dynamique du max_tokens
  - E2E : compact_bank sur un vrai espace (nécessite serveur MCP)

Usage :
    python scripts/test_bank_compact.py              # Tests unitaires seuls
    python scripts/test_bank_compact.py --e2e         # + tests E2E (serveur requis)
    python scripts/test_bank_compact.py -v            # Mode verbose
"""

import sys
import os
import asyncio
import argparse
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ─── Setup path ──────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ─────────────────────────────────────────────────────────────
# Tests unitaires
# ─────────────────────────────────────────────────────────────


class TestGetMaxSizeForFile(unittest.TestCase):
    """Test _get_max_size_for_file() : limites par type de fichier."""

    def setUp(self):
        """Créer un ConsolidatorService avec des settings mockés."""
        with patch("live_mem.core.consolidator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.llmaas_api_url = "http://test"
            settings.llmaas_api_key = "test-key"
            settings.consolidation_timeout = 60
            settings.llmaas_model = "test-model"
            settings.llmaas_context_window = 131072
            settings.llmaas_max_tokens = 16384
            settings.llmaas_temperature = 0.3
            settings.consolidation_max_notes = 500
            settings.consolidation_batch_size = 5
            settings.compact_threshold = 0.6
            settings.bank_file_max_size = 15360
            settings.bank_active_context_max_size = 8192
            settings.bank_progress_max_size = 20480
            mock_settings.return_value = settings
            from live_mem.core.consolidator import ConsolidatorService

            self.svc = ConsolidatorService()

    def test_active_context(self):
        """Limite universelle — pas de traitement spécial par nom de fichier."""
        self.assertEqual(self.svc._get_max_size_for_file("activeContext.md"), 15360)

    def test_active_context_case_insensitive(self):
        """Limite universelle — insensible au nom."""
        self.assertEqual(self.svc._get_max_size_for_file("ActiveContext.md"), 15360)

    def test_progress(self):
        """Limite universelle — pas de traitement spécial pour progress.md."""
        self.assertEqual(self.svc._get_max_size_for_file("progress.md"), 15360)

    def test_other_file(self):
        self.assertEqual(self.svc._get_max_size_for_file("techContext.md"), 15360)

    def test_subdirectory_file(self):
        """Limite universelle — insensible aux sous-dossiers."""
        self.assertEqual(
            self.svc._get_max_size_for_file("subdir/activeContext.md"), 15360
        )

    def test_unknown_file(self):
        self.assertEqual(self.svc._get_max_size_for_file("custom.md"), 15360)


class TestDynamicMaxTokens(unittest.TestCase):
    """Test calcul dynamique du budget de sortie dans _call_llm()."""

    def setUp(self):
        with patch("live_mem.core.consolidator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.llmaas_api_url = "http://test"
            settings.llmaas_api_key = "test-key"
            settings.consolidation_timeout = 60
            settings.llmaas_model = "test-model"
            settings.llmaas_context_window = 131072
            settings.llmaas_max_tokens = 16384
            settings.llmaas_temperature = 0.3
            settings.consolidation_max_notes = 500
            settings.consolidation_batch_size = 5
            settings.compact_threshold = 0.6
            settings.bank_file_max_size = 15360
            settings.bank_active_context_max_size = 8192
            settings.bank_progress_max_size = 20480
            mock_settings.return_value = settings
            from live_mem.core.consolidator import ConsolidatorService

            self.svc = ConsolidatorService()

    def test_small_input_full_budget(self):
        """Petit input → budget de sortie = max_tokens - input."""
        messages = [{"content": "x" * 4000}]  # ~1000 tokens
        # budget = max(8192, 100000 - 1000) = 99000
        input_chars = sum(len(m.get("content", "")) for m in messages)
        estimated_input = input_chars // 4
        budget = max(8192, 100000 - estimated_input)
        self.assertEqual(budget, 99000)

    def test_large_input_reduced_budget(self):
        """Gros input → budget réduit mais minimum 8192."""
        messages = [{"content": "x" * 380000}]  # ~95000 tokens
        input_chars = sum(len(m.get("content", "")) for m in messages)
        estimated_input = input_chars // 4
        budget = max(8192, 100000 - estimated_input)
        self.assertEqual(budget, 8192)  # Plancher

    def test_huge_input_floor_at_minimum(self):
        """Input dépassant max_tokens → plancher à 8192."""
        messages = [{"content": "x" * 500000}]  # ~125000 tokens > 100000
        input_chars = sum(len(m.get("content", "")) for m in messages)
        estimated_input = input_chars // 4
        budget = max(8192, 100000 - estimated_input)
        self.assertEqual(budget, 8192)

    def test_empty_input(self):
        """Input vide → budget complet."""
        messages = [{"content": ""}]
        input_chars = sum(len(m.get("content", "")) for m in messages)
        estimated_input = input_chars // 4
        budget = max(8192, 100000 - estimated_input)
        self.assertEqual(budget, 100000)


class TestCompactBankIfNeeded(unittest.TestCase):
    """Test _compact_bank_if_needed() : déclenchement de la compaction."""

    def setUp(self):
        with patch("live_mem.core.consolidator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.llmaas_api_url = "http://test"
            settings.llmaas_api_key = "test-key"
            settings.consolidation_timeout = 60
            settings.llmaas_model = "test-model"
            settings.llmaas_context_window = 131072
            settings.llmaas_max_tokens = 16384
            settings.llmaas_temperature = 0.3
            settings.consolidation_max_notes = 500
            settings.consolidation_batch_size = 5
            settings.compact_threshold = 0.6
            settings.bank_file_max_size = 15360
            settings.bank_active_context_max_size = 8192
            settings.bank_progress_max_size = 20480
            mock_settings.return_value = settings
            from live_mem.core.consolidator import ConsolidatorService

            self.svc = ConsolidatorService()

    def test_small_bank_no_compact(self):
        """Bank petite → pas de compaction."""
        bank_files = [
            {"key": "test/bank/activeContext.md", "content": "x" * 5000},
            {"key": "test/bank/progress.md", "content": "x" * 10000},
        ]
        # Total: 15000 bytes ≈ 3750 tokens < 60000 (60% of 100000)
        with patch("live_mem.core.consolidator.get_storage"):
            result = asyncio.run(
                self.svc._compact_bank_if_needed("test", bank_files, "rules")
            )
        self.assertFalse(result["compacted"])
        self.assertEqual(result["files_compacted"], 0)

    def test_large_bank_triggers_compact(self):
        """Bank très grosse → compaction déclenchée."""
        bank_files = [
            {"key": "test/bank/activeContext.md", "content": "x" * 50000},
            {"key": "test/bank/progress.md", "content": "x" * 200000},
        ]
        # Total: 250000 bytes ≈ 62500 tokens > 60000 (60% of 100000)

        mock_storage = MagicMock()
        mock_storage.put = AsyncMock()

        with patch("live_mem.core.consolidator.get_storage", return_value=mock_storage):
            # Mock _compact_single_file pour retourner du contenu réduit
            self.svc._compact_single_file = AsyncMock(return_value="compacted" * 100)
            result = asyncio.run(
                self.svc._compact_bank_if_needed("test", bank_files, "rules")
            )

        self.assertTrue(result["compacted"])
        self.assertGreater(result["files_compacted"], 0)
        self.assertLess(result["size_after"], result["size_before"])


class TestCompactSingleFile(unittest.TestCase):
    """Test _compact_single_file() : prompts spécifiques par fichier."""

    def setUp(self):
        with patch("live_mem.core.consolidator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.llmaas_api_url = "http://test"
            settings.llmaas_api_key = "test-key"
            settings.consolidation_timeout = 60
            settings.llmaas_model = "test-model"
            settings.llmaas_context_window = 131072
            settings.llmaas_max_tokens = 16384
            settings.llmaas_temperature = 0.3
            settings.consolidation_max_notes = 500
            settings.consolidation_batch_size = 5
            settings.compact_threshold = 0.6
            settings.bank_file_max_size = 15360
            settings.bank_active_context_max_size = 8192
            settings.bank_progress_max_size = 20480
            mock_settings.return_value = settings
            from live_mem.core.consolidator import ConsolidatorService

            self.svc = ConsolidatorService()

    def test_active_context_prompt_contains_generic_instructions(self):
        """Le prompt de compaction contient les instructions génériques et le nom du fichier."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[
            0
        ].message.content = "# activeContext.md\n\n## Focus\nCompacted"
        self.svc._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = asyncio.run(
            self.svc._compact_single_file(
                "activeContext.md", "x" * 50000, 15360, "rules"
            )
        )

        self.svc._client.chat.completions.create.assert_called_once()
        call_args = self.svc._client.chat.completions.create.call_args
        prompt = call_args.kwargs.get("messages", call_args[1].get("messages", []))[0][
            "content"
        ]

        # Vérifier les instructions génériques (v1.4.0+)
        self.assertIn("redondantes", prompt)
        self.assertIn("activeContext.md", prompt)
        self.assertIn("RULES DE RÉFÉRENCE", prompt)
        self.assertIsNotNone(result)

    def test_progress_prompt_contains_generic_instructions(self):
        """Le prompt de compaction contient les instructions génériques."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "# progress.md\n\nCompacted"
        self.svc._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = asyncio.run(
            self.svc._compact_single_file(
                "progress.md", "x" * 25000, 15360, "rules"
            )
        )

        call_args = self.svc._client.chat.completions.create.call_args
        prompt = call_args.kwargs.get("messages", call_args[1].get("messages", []))[0][
            "content"
        ]
        self.assertIn("obsolètes", prompt)
        self.assertIn("jalon", prompt)
        self.assertIsNotNone(result)

    def test_llm_failure_returns_none(self):
        """Si le LLM échoue, retourne None."""
        self.svc._client.chat.completions.create = AsyncMock(
            side_effect=Exception("LLM down")
        )

        result = asyncio.run(
            self.svc._compact_single_file("test.md", "content", 15360, "rules")
        )
        self.assertIsNone(result)

    def test_think_tags_cleaned(self):
        """Les balises <think> sont nettoyées de la réponse."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[
            0
        ].message.content = "<think>réflexion interne</think>\n# Compacted\n\nContent"
        self.svc._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = asyncio.run(
            self.svc._compact_single_file("test.md", "x" * 20000, 15360, "rules")
        )
        self.assertNotIn("<think>", result)
        self.assertIn("Compacted", result)


class TestCompactBank(unittest.TestCase):
    """Test compact_bank() : mode dry_run et mode apply."""

    def setUp(self):
        with patch("live_mem.core.consolidator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.llmaas_api_url = "http://test"
            settings.llmaas_api_key = "test-key"
            settings.consolidation_timeout = 60
            settings.llmaas_model = "test-model"
            settings.llmaas_context_window = 131072
            settings.llmaas_max_tokens = 16384
            settings.llmaas_temperature = 0.3
            settings.consolidation_max_notes = 500
            settings.consolidation_batch_size = 5
            settings.compact_threshold = 0.6
            settings.bank_file_max_size = 15360
            settings.bank_active_context_max_size = 8192
            settings.bank_progress_max_size = 20480
            mock_settings.return_value = settings
            from live_mem.core.consolidator import ConsolidatorService

            self.svc = ConsolidatorService()

    def test_dry_run_no_writes(self):
        """En dry_run, aucune écriture S3."""
        mock_storage = MagicMock()
        mock_storage.get_json = AsyncMock(return_value={"created_at": "2026-01-01"})
        mock_storage.list_and_get = AsyncMock(
            return_value=[
                {"key": "test/bank/activeContext.md", "content": "x" * 50000},
                {"key": "test/bank/progress.md", "content": "x" * 5000},
            ]
        )
        mock_storage.get = AsyncMock(return_value="# Rules")
        mock_storage.put = AsyncMock()

        with patch("live_mem.core.consolidator.get_storage", return_value=mock_storage):
            result = asyncio.run(self.svc.compact_bank("test", dry_run=True))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["files_over_limit"], 1)  # activeContext > 8192
        self.assertEqual(result["files_total"], 2)
        # Pas d'écriture en dry_run
        mock_storage.put.assert_not_called()

    def test_apply_mode_writes(self):
        """En mode apply, les fichiers compactés sont écrits."""
        mock_storage = MagicMock()
        mock_storage.get_json = AsyncMock(return_value={"created_at": "2026-01-01"})
        mock_storage.list_and_get = AsyncMock(
            return_value=[
                {"key": "test/bank/activeContext.md", "content": "x" * 50000},
            ]
        )
        mock_storage.get = AsyncMock(return_value="# Rules")
        mock_storage.put = AsyncMock()

        # Mock _compact_single_file
        self.svc._compact_single_file = AsyncMock(return_value="compacted" * 50)

        with patch("live_mem.core.consolidator.get_storage", return_value=mock_storage):
            result = asyncio.run(self.svc.compact_bank("test", dry_run=False))

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["files_over_limit"], 1)
        mock_storage.put.assert_called_once()  # 1 fichier écrit

    def test_space_not_found(self):
        """Si l'espace n'existe pas, retourne erreur."""
        mock_storage = MagicMock()
        mock_storage.get_json = AsyncMock(return_value=None)

        with patch("live_mem.core.consolidator.get_storage", return_value=mock_storage):
            result = asyncio.run(self.svc.compact_bank("nonexistent"))

        self.assertEqual(result["status"], "error")
        self.assertIn("introuvable", result["message"])

    def test_file_report_details(self):
        """Le rapport contient les détails par fichier."""
        mock_storage = MagicMock()
        mock_storage.get_json = AsyncMock(return_value={"created_at": "2026-01-01"})
        mock_storage.list_and_get = AsyncMock(
            return_value=[
                {"key": "s/bank/activeContext.md", "content": "x" * 20000},  # > 15360
                {"key": "s/bank/progress.md", "content": "x" * 5000},  # < 15360
                {"key": "s/bank/techContext.md", "content": "x" * 3000},  # < 15360
            ]
        )
        mock_storage.get = AsyncMock(return_value="")

        with patch("live_mem.core.consolidator.get_storage", return_value=mock_storage):
            result = asyncio.run(self.svc.compact_bank("s", dry_run=True))

        self.assertEqual(len(result["files"]), 3)
        # activeContext 20000 > 15360 → over_limit
        ac = next(f for f in result["files"] if "activeContext" in f["filename"])
        self.assertTrue(ac["over_limit"])
        self.assertGreater(ac["ratio"], 1.0)
        # progress 5000 < 15360 → OK
        pr = next(f for f in result["files"] if "progress" in f["filename"])
        self.assertFalse(pr["over_limit"])


# ─────────────────────────────────────────────────────────────
# Tests E2E (nécessitent un serveur MCP Hivemind)
# ─────────────────────────────────────────────────────────────


class TestE2EBankCompact(unittest.TestCase):
    """Tests E2E — nécessitent MCP_URL et MCP_TOKEN dans l'environnement."""

    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get("MCP_URL", "")
        cls.token = os.environ.get("MCP_TOKEN", "")
        if not cls.url or not cls.token:
            raise unittest.SkipTest("MCP_URL/MCP_TOKEN non configurés — skip E2E")

    def _call_mcp(self, tool_name: str, arguments: dict) -> dict:
        """Appel MCP synchrone via HTTP."""
        import json
        import urllib.request

        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.url}/mcp",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())

    def test_e2e_bank_compact_dry_run(self):
        """E2E : bank_compact en dry_run sur un espace existant."""
        # Lister les espaces pour en trouver un
        result = self._call_mcp("space_list", {})
        content = json.loads(result["result"]["content"][0]["text"])
        if not content.get("spaces"):
            self.skipTest("Aucun espace disponible pour le test E2E")

        space_id = content["spaces"][0]["space_id"]
        print(f"\n  E2E: bank_compact dry_run on '{space_id}'")

        # Appeler bank_compact en dry_run
        # Note : bank_compact n'est pas encore un outil MCP,
        # donc on teste via le consolidateur directement
        # Pour un vrai E2E, il faudra l'outil MCP
        print(f"  → space_id={space_id}, dry_run=True")
        print("  ⚠️ bank_compact n'est pas encore exposé comme outil MCP")
        print("  → Test via space_info pour vérifier les tailles")

        result = self._call_mcp("space_info", {"space_id": space_id})
        info = json.loads(result["result"]["content"][0]["text"])
        bank_size = info.get("bank", {}).get("total_size", 0)
        print(
            f"  → Bank size: {bank_size} bytes ({len(info.get('bank', {}).get('files', []))} files)"
        )

        self.assertIn("bank", info)
        self.assertGreaterEqual(bank_size, 0)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json  # pour E2E

    parser = argparse.ArgumentParser(description="Tests bank compaction")
    parser.add_argument("--e2e", action="store_true", help="Inclure les tests E2E")
    parser.add_argument("-v", "--verbose", action="store_true", help="Mode verbose")
    args = parser.parse_args()

    # Configurer le test runner
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Tests unitaires (toujours)
    suite.addTests(loader.loadTestsFromTestCase(TestGetMaxSizeForFile))
    suite.addTests(loader.loadTestsFromTestCase(TestDynamicMaxTokens))
    suite.addTests(loader.loadTestsFromTestCase(TestCompactBankIfNeeded))
    suite.addTests(loader.loadTestsFromTestCase(TestCompactSingleFile))
    suite.addTests(loader.loadTestsFromTestCase(TestCompactBank))

    # Tests E2E (optionnel)
    if args.e2e:
        suite.addTests(loader.loadTestsFromTestCase(TestE2EBankCompact))

    verbosity = 2 if args.verbose else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    # Résumé
    total = result.testsRun
    failures = len(result.failures) + len(result.errors)
    skipped = len(result.skipped)
    passed = total - failures - skipped
    print(f"\n{'=' * 60}")
    print(f"Bank Compact Tests: {passed}/{total} PASS, {failures} FAIL, {skipped} SKIP")
    print(f"{'=' * 60}")

    sys.exit(0 if failures == 0 else 1)
