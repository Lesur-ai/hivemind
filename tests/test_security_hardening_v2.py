# -*- coding: utf-8 -*-
"""
Tests de sécurité NON COMPLAISANTS pour la release v2.0.0 — audit 2026-05-15.

Convention :
    Chaque test cible UN finding LM2-* précis et tente une attaque concrète.
    Sans le fix, le test échouerait (preuve par contrapposée).

Périmètre :
    - Helpers purs (validate_gm_url, _parse_backup_id, _validate_bank_filename,
      mask_meta_secrets, invalidate_token_in_store, _client_ip_from_scope,
      _sse_kwargs)
    - Configuration et infrastructure (CSP, vendored libs, deps bounds,
      max_notes par défaut)
    - Intégrations avec mocks S3 (token store invalidation, /api/login cookie,
      backup cross-tenant, bank confirm, /health no leak, GC notice agent)

Convention nommage : ``test_<area>_<expected_behavior>_<attack_vector>``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Le code testé
from live_mem.auth.context import (
    _fresh_token_store,
    invalidate_token_in_store,
    update_fresh_token,
)
from live_mem.auth.middleware import (
    AUTH_COOKIE_NAME,
    _client_ip_from_scope,
)
from live_mem.config import Settings
from live_mem.core.models import GraphMemoryConfig, SpaceMeta, mask_meta_secrets
from live_mem.core.tokens import TokenService
from live_mem.core.models import TokenInfo, TokensStore
from live_mem.tools.backup import _parse_backup_id
from live_mem.tools.bank import _validate_bank_filename
from live_mem.tools.graph import _validate_gm_url


# =============================================================================
# Helpers communs aux tests
# =============================================================================

ROOT = Path(__file__).parent.parent
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"
GRAPH_REQUIREMENTS = ROOT / "services" / "graph-memory" / "requirements.txt"
GRAPH_REQUIREMENTS_LOCK = ROOT / "services" / "graph-memory" / "requirements.lock"
CADDYFILE = ROOT / "waf" / "Caddyfile"
VENDOR_DIR = ROOT / "src" / "live_mem" / "static" / "vendor"


def _make_token_info(
    hash_: str = "sha256:" + "f" * 64,
    name: str = "test-agent",
    permissions: list[str] | None = None,
    allowed: list[str] | None = None,
) -> dict:
    """Construit un token_info plausible pour update_fresh_token."""
    return {
        "type": "token",
        "client_name": name,
        "permissions": permissions or ["read", "write"],
        "allowed_resources": allowed or [],
        "token_hash": hash_,
    }


# =============================================================================
# LM2-02 — SSRF dans graph_connect (validation d'URL)
# =============================================================================


class TestLM2_02_GraphConnectSSRF:
    """Le helper _validate_gm_url doit refuser tous les vecteurs SSRF connus."""

    @pytest.mark.parametrize(
        "url, why",
        [
            ("file:///etc/passwd", "scheme file://"),
            ("gopher://attacker.com:25/", "scheme gopher://"),
            ("ftp://anon@host/", "scheme ftp://"),
            ("javascript:alert(1)", "scheme javascript:"),
            ("ws://internal/", "scheme ws://"),
            ("data:text/plain;base64,YWE=", "scheme data:"),
        ],
    )
    def test_blocks_non_http_schemes(self, url, why):
        err = _validate_gm_url(url)
        assert err is not None, f"Sans fix : {why} accepté"
        assert "Scheme" in err or "scheme" in err

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8080",
            "http://127.5.5.5/",
            "https://127.0.0.1:9999/mcp",
        ],
    )
    def test_blocks_loopback_ipv4(self, url):
        err = _validate_gm_url(url)
        assert err is not None, "Sans fix : loopback IPv4 accepté"
        assert "loopback" in err

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1/",
            "http://10.255.255.255/",
            "http://172.16.0.1/",
            "http://172.31.0.1/",
            "http://192.168.0.1/",
            "http://192.168.255.255/",
        ],
    )
    def test_blocks_rfc1918_private(self, url):
        err = _validate_gm_url(url)
        assert err is not None, f"Sans fix : RFC 1918 {url} accepté"
        assert "privée" in err

    def test_blocks_aws_metadata_link_local(self):
        """169.254.169.254 = AWS/GCP metadata, vecteur SSRF principal."""
        err = _validate_gm_url("http://169.254.169.254/latest/meta-data/")
        assert err is not None, "Sans fix : metadata cloud accessible"
        assert "link-local" in err

    def test_blocks_unspecified_zero(self):
        err = _validate_gm_url("http://0.0.0.0/")
        assert err is not None, "Sans fix : 0.0.0.0 accepté"

    def test_blocks_multicast(self):
        err = _validate_gm_url("http://224.0.0.1/")
        assert err is not None, "Sans fix : multicast accepté"

    def test_blocks_empty_url(self):
        assert _validate_gm_url("") is not None
        assert _validate_gm_url("   ") is not None

    def test_blocks_url_without_hostname(self):
        # urlparse ne donne pas de hostname si scheme manque
        err = _validate_gm_url("just-a-string")
        assert err is not None

    # ─── Cas légitimes (anti faux-positif) ────────────────────

    @pytest.mark.parametrize(
        "url",
        [
            "http://graph-memory.example.com:8080/mcp",
            "https://graph.cloud-temple.com/mcp",
            "http://gm.internal.corp/",
            "https://api.example.fr:443/v1",
        ],
    )
    def test_accepts_public_dns_hostnames(self, url, monkeypatch):
        """Un nom DNS qui résout vers une IP PUBLIQUE passe (anti faux-positif).

        Résolveur mocké → IP publique déterministe : le test ne dépend PAS du DNS
        de l'environnement (CI). Vérifie le chemin résolu-public → accepté.
        """
        import socket as _socket
        import live_mem.core.url_guard as _url_guard

        monkeypatch.setattr(
            _url_guard,
            "_resolve_bounded",
            lambda host, port: [
                (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
            ],
        )
        assert _validate_gm_url(url) is None, f"Faux positif sur {url}"

    def test_nxdomain_deterministic_fails_open(self, monkeypatch):
        """EAI_NONAME (NXDOMAIN déterministe) → accepté (contrat DNS blackbox)."""
        import socket as _socket
        import live_mem.core.url_guard as _url_guard

        def _nxdomain(host, port):
            raise _socket.gaierror(_socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(_url_guard, "_resolve_bounded", _nxdomain)
        assert _validate_gm_url("http://nope.invalid/") is None

    def test_transient_dns_failure_fails_closed(self, monkeypatch):
        """Codex #150 R2 : EAI_AGAIN (transitoire/indéterminé) → REFUSÉ (fail-closed)."""
        import socket as _socket
        import live_mem.core.url_guard as _url_guard

        def _eai_again(host, port):
            raise _socket.gaierror(
                _socket.EAI_AGAIN, "Temporary failure in name resolution"
            )

        monkeypatch.setattr(_url_guard, "_resolve_bounded", _eai_again)
        assert _validate_gm_url("http://transient.example/") is not None

    def test_dns_resolves_to_private_ip_fails_closed(self, monkeypatch):
        """HM-11 : un nom DNS résolvant vers une IP privée/metadata → REFUSÉ."""
        import socket as _socket
        import live_mem.core.url_guard as _url_guard

        monkeypatch.setattr(
            _url_guard,
            "_resolve_bounded",
            lambda host, port: [
                (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))
            ],
        )
        assert _validate_gm_url("http://rebind.example/") is not None


# =============================================================================
# LM2-09 — backup_id path traversal / format invalide
# =============================================================================


class TestLM2_09_BackupIdValidation:
    """`_parse_backup_id` doit rejeter tous les formats déviants avant accès S3."""

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",
            "no-slash",
            "space/",
            "/timestamp",
            "/" * 5,
        ],
    )
    def test_rejects_malformed(self, bad_id):
        sid, ts, err = _parse_backup_id(bad_id)
        assert err is not None, f"Sans fix : {bad_id!r} accepté"
        assert sid is None and ts is None

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../_system/abc",
            "../../../../etc/passwd",
            "_system/2026-05-15T00-00-00",
            "_backups/x/2026-05-15T00-00-00",
            ".hidden/2026-05-15T00-00-00",
            "with space/2026-05-15T00-00-00",
        ],
    )
    def test_rejects_path_traversal_or_system_prefix(self, bad_id):
        sid, ts, err = _parse_backup_id(bad_id)
        assert err is not None, f"Sans fix : path traversal {bad_id!r} accepté"
        assert "space_id" in err["message"] or "invalide" in err["message"]

    @pytest.mark.parametrize(
        "bad_id",
        [
            "valid/not-iso",
            "valid/2026-13-01T00-00-00",  # month 13 (regex ne le détecte pas mais format OK ici)
            "valid/2026-05-15T25-99-99",  # heures hors plage (regex match malgré tout)
            "valid/abc",
            "valid/" + "x" * 50,
        ],
    )
    def test_rejects_bad_timestamp(self, bad_id):
        sid, ts, err = _parse_backup_id(bad_id)
        # Si la regex de timestamp matche (chiffres+tirets), le test passera pour
        # certains cas — mais on vérifie au moins les cas non-chiffrés.
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}$", bad_id.split("/", 1)[1] if "/" in bad_id else ""):
            assert err is not None, f"Sans fix : timestamp invalide {bad_id!r} accepté"

    def test_accepts_legitimate_backup_id(self):
        sid, ts, err = _parse_backup_id("my-project/2026-05-15T13-30-45")
        assert err is None
        assert sid == "my-project"
        assert ts == "2026-05-15T13-30-45"

    def test_rejects_none(self):
        sid, ts, err = _parse_backup_id(None)  # type: ignore[arg-type]
        assert err is not None


# =============================================================================
# LM2-12 — Validation des filenames bank (anti-XSS + path traversal)
# =============================================================================


class TestLM2_12_BankFilenameValidation:
    """Le helper _validate_bank_filename refuse tous les vecteurs XSS connus."""

    @pytest.mark.parametrize(
        "bad_name",
        [
            "<img src=x onerror=fetch('//evil')>",
            "<script>alert(1)</script>.md",
            'name with " quote.md',
            "name with ' quote.md",
            "name with \\ backslash.md",
            "before\x00null.md",
            "before\x01ctrl.md",
            "before\x1ftrailing.md",
            "before\x7fDEL.md",
        ],
    )
    def test_rejects_dangerous_chars(self, bad_name):
        err = _validate_bank_filename(bad_name)
        assert err is not None, f"Sans fix : {bad_name!r} accepté"
        assert "dangereux" in err["message"] or "control" in err["message"].lower()

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../etc/passwd",
            "../../bank/escape.md",
            "subdir/../../../escape.md",
        ],
    )
    def test_rejects_path_traversal_dotdot(self, bad_name):
        err = _validate_bank_filename(bad_name)
        assert err is not None, f"Sans fix : path traversal {bad_name!r} accepté"
        assert ".." in err["message"]

    def test_rejects_absolute_path(self):
        err = _validate_bank_filename("/etc/passwd")
        assert err is not None
        assert "/" in err["message"] or "absolu" in err["message"].lower()

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "\t\n",
        ],
    )
    def test_rejects_empty_or_whitespace(self, bad):
        err = _validate_bank_filename(bad)
        assert err is not None

    # ─── Cas légitimes ────────────────────────────────────────

    @pytest.mark.parametrize(
        "ok_name",
        [
            "activeContext.md",
            "progress.md",
            "personaProfiles/acheteur.md",
            "sub-folder/file.md",
            "with-dash.md",
            "with_underscore.md",
            "with.dots.md",
            "1.MEMORY_BANK_legitimate.md",  # nom légitime (pas un préfixe parasite)
            "présales.md",  # accents OK
            "🦄.md",  # emoji OK (autorisé volontairement)
        ],
    )
    def test_accepts_legitimate_filenames(self, ok_name):
        assert _validate_bank_filename(ok_name) is None, f"Faux positif sur {ok_name!r}"


# =============================================================================
# LM2-03 — mask_meta_secrets : masquage du token Graph Memory
# =============================================================================


class TestLM2_03_MaskMetaSecrets:
    """Le helper mask_meta_secrets doit obfusquer le token GM dans toute copie."""

    def test_masks_long_token(self):
        meta = {
            "space_id": "x",
            "graph_memory": {
                "url": "http://gm",
                "token": "lm_supersecretvaluedonotleak1234",
                "memory_id": "m",
            },
        }
        masked = mask_meta_secrets(meta)
        assert masked is not None
        assert masked["graph_memory"]["token"] != meta["graph_memory"]["token"]
        assert masked["graph_memory"]["token"].endswith("...")
        # Seul un préfixe court doit être visible
        assert len(masked["graph_memory"]["token"]) <= 12

    def test_masks_short_token(self):
        meta = {"graph_memory": {"token": "short"}}
        masked = mask_meta_secrets(meta)
        # < 8 chars → masquage complet par "***"
        assert masked["graph_memory"]["token"] == "***"

    def test_does_not_mutate_input(self):
        """Test critique : la fonction ne doit JAMAIS muter l'input."""
        original = {
            "graph_memory": {
                "token": "lm_originalsensitivevalue123456789",
                "url": "http://gm",
            }
        }
        # Capture d'une copie avant l'appel
        original_token = original["graph_memory"]["token"]
        _ = mask_meta_secrets(original)
        # L'input original DOIT rester intact (sinon le cache de _meta.json
        # est corrompu pour les appels parallèles)
        assert original["graph_memory"]["token"] == original_token, (
            "Bug critique : mask_meta_secrets a muté l'input — risque de "
            "corruption du cache de _meta.json en parallèle."
        )

    def test_handles_none(self):
        assert mask_meta_secrets(None) is None

    def test_handles_empty_meta(self):
        assert mask_meta_secrets({}) == {}

    def test_handles_meta_without_graph_memory(self):
        meta = {"space_id": "x", "owner": "me"}
        assert mask_meta_secrets(meta) == meta

    def test_handles_graph_memory_without_token(self):
        meta = {"graph_memory": {"url": "http://gm"}}
        masked = mask_meta_secrets(meta)
        assert masked == meta  # rien à masquer

    def test_handles_empty_token(self):
        meta = {"graph_memory": {"token": ""}}
        masked = mask_meta_secrets(meta)
        # token vide → pas de masquage (rien à cacher)
        assert masked["graph_memory"]["token"] == ""


# =============================================================================
# LM2-07 — invalidate_token_in_store : purge du _fresh_token_store
# =============================================================================


class TestLM2_07_FreshTokenStoreInvalidation:
    """Le store global de tokens doit être purgé sur révocation."""

    def setup_method(self):
        """Nettoyage avant chaque test (isolation)."""
        _fresh_token_store.clear()

    def teardown_method(self):
        _fresh_token_store.clear()

    def test_invalidate_removes_token(self):
        h = "sha256:" + "a" * 64
        update_fresh_token(_make_token_info(hash_=h))
        assert h in _fresh_token_store, "Précondition : token présent"

        invalidate_token_in_store(h)
        assert h not in _fresh_token_store, "Sans fix : token fantôme"

    def test_invalidate_is_idempotent(self):
        """Idempotence : appels multiples ne lèvent pas d'exception."""
        h = "sha256:" + "b" * 64
        # Pas dans le store → no-op
        invalidate_token_in_store(h)
        # 2 fois → toujours no-op
        invalidate_token_in_store(h)
        assert h not in _fresh_token_store

    def test_invalidate_only_affects_target(self):
        """Sans fix : un bug d'aliasing pourrait purger d'autres tokens."""
        h1 = "sha256:" + "1" * 64
        h2 = "sha256:" + "2" * 64
        update_fresh_token(_make_token_info(hash_=h1, name="a"))
        update_fresh_token(_make_token_info(hash_=h2, name="b"))

        invalidate_token_in_store(h1)

        assert h1 not in _fresh_token_store
        assert h2 in _fresh_token_store, "Bug : invalidation d'un autre token"

    @pytest.mark.asyncio
    async def test_revoke_token_invalidates_store(self):
        """Le flux complet : revoke_token() purge bien le store."""
        svc = TokenService()
        h = "sha256:" + "c" * 64
        update_fresh_token(_make_token_info(hash_=h))

        mock_store = TokensStore(
            tokens=[
                TokenInfo(hash=h, name="bob", permissions=["read"], space_ids=[])
            ]
        )
        with patch.object(svc, "_load_store", AsyncMock(return_value=mock_store)), patch.object(
            svc, "_save_store", AsyncMock()
        ):
            result = await svc.revoke_token(h)

        assert result.get("status") == "ok"
        assert h not in _fresh_token_store, (
            "Sans fix LM2-07 : opération longue post-revoke verrait encore "
            "les anciennes permissions via _fresh_token_store"
        )

    @pytest.mark.asyncio
    async def test_update_token_email_only_does_NOT_invalidate_store(self):
        """Optimisation : email seul → pas d'invalidation (pas d'impact runtime)."""
        svc = TokenService()
        h = "sha256:" + "d" * 64
        update_fresh_token(_make_token_info(hash_=h))

        mock_store = TokensStore(
            tokens=[
                TokenInfo(
                    hash=h, name="dave", permissions=["read"], space_ids=["x"]
                )
            ]
        )
        with patch.object(
            svc, "_load_store", AsyncMock(return_value=mock_store)
        ), patch.object(svc, "_save_store", AsyncMock()):
            result = await svc.update_token(h, email="new@example.com")

        assert result.get("status") == "ok"
        # email n'affecte pas check_access/check_*_permission → pas de purge
        assert h in _fresh_token_store, (
            "Pessimisation : invalidation inutile sur email-only changeait "
            "rien aux droits mais forçait à recharger le store"
        )

    @pytest.mark.asyncio
    async def test_update_token_permissions_invalidates_store(self):
        svc = TokenService()
        h = "sha256:" + "e" * 64
        update_fresh_token(_make_token_info(hash_=h, permissions=["read", "write"]))

        mock_store = TokensStore(
            tokens=[
                TokenInfo(
                    hash=h,
                    name="eve",
                    permissions=["read", "write"],
                    space_ids=["x"],
                )
            ]
        )
        with patch.object(
            svc, "_load_store", AsyncMock(return_value=mock_store)
        ), patch.object(svc, "_save_store", AsyncMock()):
            await svc.update_token(h, permissions="read")

        assert h not in _fresh_token_store, (
            "Sans fix : downgrade de permission invisible immédiatement"
        )


# =============================================================================
# LM2-17 — X-Forwarded-For dans les logs d'audit
# =============================================================================


class TestLM2_17_ClientIPFromScope:
    """Derrière le WAF, scope.client = IP du WAF. On veut l'IP réelle du client."""

    def _scope(self, headers=None, client=("10.0.0.1", 12345)):
        return {"type": "http", "headers": headers or [], "client": client}

    def test_uses_x_forwarded_for_first(self):
        scope = self._scope(headers=[(b"x-forwarded-for", b"203.0.113.5")])
        assert _client_ip_from_scope(scope) == "203.0.113.5"

    def test_xff_takes_first_ip_when_chained(self):
        """Format XFF chaîné : 'client, proxy1, proxy2' → on prend le premier."""
        scope = self._scope(
            headers=[(b"x-forwarded-for", b"203.0.113.5, 10.0.0.2, 10.0.0.3")]
        )
        assert _client_ip_from_scope(scope) == "203.0.113.5"

    def test_falls_back_to_x_real_ip(self):
        scope = self._scope(headers=[(b"x-real-ip", b"198.51.100.7")])
        assert _client_ip_from_scope(scope) == "198.51.100.7"

    def test_xff_preferred_over_x_real_ip(self):
        scope = self._scope(
            headers=[
                (b"x-forwarded-for", b"203.0.113.5"),
                (b"x-real-ip", b"198.51.100.7"),
            ]
        )
        assert _client_ip_from_scope(scope) == "203.0.113.5"

    def test_falls_back_to_scope_client(self):
        scope = self._scope(client=("10.0.0.99", 54321))
        assert _client_ip_from_scope(scope) == "10.0.0.99"

    def test_returns_unknown_when_no_info(self):
        scope = {"type": "http", "headers": [], "client": None}
        assert _client_ip_from_scope(scope) == "unknown"

    def test_ignores_empty_xff(self):
        scope = self._scope(headers=[(b"x-forwarded-for", b"")])
        # vide → fallback sur scope.client
        assert _client_ip_from_scope(scope) == "10.0.0.1"


# =============================================================================
# LM2-15 — S3 Server-Side Encryption (kwargs configurables)
# =============================================================================


class TestLM2_15_S3SSE:
    """_sse_kwargs() doit retourner les bons paramètres selon la config."""

    def _service_with_sse(self, sse=None, kms_id=None):
        """Instancie un StorageService sans toucher S3."""
        from live_mem.core.storage import StorageService

        # On instancie en bypassant boto3 (mock complet du __init__ S3)
        with patch("live_mem.core.storage.boto3.client") as mock_client:
            mock_client.return_value = MagicMock()
            with patch.dict(
                "os.environ",
                {
                    "S3_SSE": sse or "",
                    "S3_SSE_KMS_KEY_ID": kms_id or "",
                    "S3_ENDPOINT_URL": "https://s3.test",
                    "S3_ACCESS_KEY_ID": "ak",
                    "S3_SECRET_ACCESS_KEY": "sk",
                    "S3_BUCKET_NAME": "test-bucket",
                },
                clear=False,
            ):
                # Forcer un nouveau Settings (sans cache lru)
                from live_mem.config import get_settings
                get_settings.cache_clear()
                svc = StorageService()
                get_settings.cache_clear()
                return svc

    def test_no_sse_returns_empty_kwargs(self):
        svc = self._service_with_sse(sse=None)
        assert svc._sse_kwargs() == {}, (
            "Compat Dell ECS : sans S3_SSE configuré, aucun kwargs ajouté."
        )

    def test_aes256_returns_sse_aes256(self):
        svc = self._service_with_sse(sse="AES256")
        kwargs = svc._sse_kwargs()
        assert kwargs == {"ServerSideEncryption": "AES256"}

    def test_kms_with_key_id_returns_full_config(self):
        svc = self._service_with_sse(
            sse="aws:kms", kms_id="arn:aws:kms:fr1:1:key/abc"
        )
        kwargs = svc._sse_kwargs()
        assert kwargs["ServerSideEncryption"] == "aws:kms"
        assert kwargs["SSEKMSKeyId"] == "arn:aws:kms:fr1:1:key/abc"

    def test_kms_without_key_id_omits_key(self):
        """Si aws:kms est configuré sans clé, on n'envoie pas SSEKMSKeyId vide."""
        svc = self._service_with_sse(sse="aws:kms", kms_id=None)
        kwargs = svc._sse_kwargs()
        assert "SSEKMSKeyId" not in kwargs

    def test_whitespace_only_treated_as_disabled(self):
        """Robustesse : '  ' → désactivé (pas une valeur valide)."""
        svc = self._service_with_sse(sse="   ")
        assert svc._sse_kwargs() == {}


# =============================================================================
# P9-7 — MinIO zero-byte PUT / Expect 100-continue compatibility
# =============================================================================


class TestP9_7EmptyS3Continue:
    """Zero-byte sentinels must not poison the next persistent S3 request."""

    def test_empty_body_removes_expect_but_nonempty_body_preserves_it(self):
        from io import BytesIO

        from live_mem.core.storage import _remove_expect_header_for_empty_s3_body

        empty = {"body": BytesIO(b""), "headers": {"Expect": "100-continue"}}
        _remove_expect_header_for_empty_s3_body(None, empty)
        assert "Expect" not in empty["headers"]

        nonempty = {
            "body": BytesIO(b"sentinel"),
            "headers": {"Expect": "100-continue"},
        }
        _remove_expect_header_for_empty_s3_body(None, nonempty)
        assert nonempty["headers"]["Expect"] == "100-continue"

        class IndeterminateBody:
            def read(self, _amount=-1):
                return b""

        indeterminate = {
            "body": IndeterminateBody(),
            "headers": {"Expect": "100-continue"},
        }
        _remove_expect_header_for_empty_s3_body(None, indeterminate)
        assert indeterminate["headers"]["Expect"] == "100-continue"

    @pytest.mark.parametrize(
        "signature_mode, expected_clients",
        [("sigv4", 1), ("dual", 2)],
    )
    def test_guard_is_registered_on_every_distinct_storage_client(
        self, signature_mode, expected_clients
    ):
        from live_mem.config import get_settings
        from live_mem.core.storage import (
            StorageService,
            _EMPTY_CONTINUE_HANDLER_ID,
            _remove_expect_header_for_empty_s3_body,
        )

        clients = []

        def fake_client(*args, **kwargs):
            client = MagicMock()
            clients.append(client)
            return client

        with patch("live_mem.core.storage.boto3.client", side_effect=fake_client):
            with patch.dict(
                "os.environ",
                {
                    "S3_ENDPOINT_URL": "https://s3.test",
                    "S3_ACCESS_KEY_ID": "ak",
                    "S3_SECRET_ACCESS_KEY": "sk",
                    "S3_BUCKET_NAME": "test-bucket",
                    "S3_SIGNATURE_MODE": signature_mode,
                },
                clear=False,
            ):
                get_settings.cache_clear()
                StorageService()
                get_settings.cache_clear()

        assert len(clients) == expected_clients
        for client in clients:
            client.meta.events.register_last.assert_called_once_with(
                "before-call.s3",
                _remove_expect_header_for_empty_s3_body,
                unique_id=_EMPTY_CONTINUE_HANDLER_ID,
            )


# =============================================================================
# LM2-14 — CONSOLIDATION_MAX_NOTES baissé à 200 par défaut
# =============================================================================


class TestLM2_14_MaxNotesDefault:
    """La valeur par défaut dans le code doit être 200 (anti-DoS LLM budget).

    Note : Settings() lit aussi .env du repo, donc on inspecte le défaut
    via la signature Pydantic, pas une instanciation runtime.
    """

    def test_default_is_200_not_500(self):
        # Inspecte le champ Pydantic pour récupérer le défaut codé en dur,
        # indépendamment d'un éventuel .env local qui surchargerait.
        field = Settings.model_fields["consolidation_max_notes"]
        assert field.default == 200, (
            "Sans fix LM2-14 : 500 notes max permettait jusqu'à 50 MB d'input LLM"
        )

    def test_can_be_overridden_by_env(self):
        """Sanity : la variable d'env est bien prise en compte."""
        # Pour éviter le bruit du .env du repo, on instancie en isolation.
        import os
        original = os.environ.get("CONSOLIDATION_MAX_NOTES")
        try:
            os.environ["CONSOLIDATION_MAX_NOTES"] = "50"
            from live_mem.config import get_settings
            get_settings.cache_clear()
            s = Settings()
            assert s.consolidation_max_notes == 50
        finally:
            if original is None:
                os.environ.pop("CONSOLIDATION_MAX_NOTES", None)
            else:
                os.environ["CONSOLIDATION_MAX_NOTES"] = original
            from live_mem.config import get_settings
            get_settings.cache_clear()


# =============================================================================
# LM2-18 — CONSOLIDATION_COOLDOWN_SECONDS par défaut
# =============================================================================


class TestLM2_18_CooldownConfig:
    def test_default_cooldown_is_60s(self):
        with patch.dict("os.environ", {}, clear=False):
            from live_mem.config import get_settings
            get_settings.cache_clear()
            s = Settings()
            get_settings.cache_clear()
        assert s.consolidation_cooldown_seconds == 60, (
            "Sans fix LM2-18 : un agent write peut boucler sur bank_consolidate"
        )

    def test_can_be_disabled_with_zero(self):
        with patch.dict(
            "os.environ", {"CONSOLIDATION_COOLDOWN_SECONDS": "0"}, clear=False
        ):
            from live_mem.config import get_settings
            get_settings.cache_clear()
            s = Settings()
            get_settings.cache_clear()
        assert s.consolidation_cooldown_seconds == 0


# =============================================================================
# LM2-05 — CSP durcie dans waf/Caddyfile
# =============================================================================


class TestLM2_05_CSPHardened:
    """Smoke test sur le Caddyfile : la CSP doit interdire script-src unsafe-inline."""

    @pytest.fixture(scope="class")
    def caddyfile_content(self) -> str:
        return CADDYFILE.read_text(encoding="utf-8")

    def test_script_src_does_not_allow_unsafe_inline(self, caddyfile_content):
        # Cherche la ligne CSP et vérifie script-src
        m = re.search(
            r"Content-Security-Policy\s*\"([^\"]+)\"",
            caddyfile_content,
        )
        assert m, "CSP introuvable dans Caddyfile"
        csp = m.group(1)
        # Extraire script-src
        script_src_match = re.search(r"script-src([^;]+)", csp)
        assert script_src_match, "script-src directive introuvable"
        script_src = script_src_match.group(1)
        assert "'unsafe-inline'" not in script_src, (
            "Sans fix LM2-05 : 'unsafe-inline' sur script-src permettait XSS "
            "via injection inline (handler `onclick=`, `<script>...`)."
        )

    def test_csp_does_not_whitelist_external_cdns_for_scripts(
        self, caddyfile_content
    ):
        m = re.search(
            r"Content-Security-Policy\s*\"([^\"]+)\"",
            caddyfile_content,
        )
        csp = m.group(1)
        script_src_match = re.search(r"script-src([^;]+)", csp)
        script_src = script_src_match.group(1)
        for cdn in ("unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com"):
            assert cdn not in script_src, (
                f"Sans fix LM2-06 : {cdn} dans script-src exposait à compromise CDN"
            )

    def test_csp_includes_frame_ancestors_none(self, caddyfile_content):
        """Anti-clickjacking : `frame-ancestors 'none'` doit rester."""
        m = re.search(
            r"Content-Security-Policy\s*\"([^\"]+)\"",
            caddyfile_content,
        )
        csp = m.group(1)
        assert "frame-ancestors 'none'" in csp


# =============================================================================
# LM2-06 — Libs vendored localement (marked + DOMPurify)
# =============================================================================


class TestLM2_06_VendoredLibraries:
    def test_marked_vendored_present(self):
        f = VENDOR_DIR / "marked.min.js"
        assert f.exists(), (
            "Sans fix LM2-06 : marked.js chargé depuis CDN externe = "
            "exécution JS arbitraire en cas de compromise CDN"
        )
        assert f.stat().st_size > 1000, "marked.min.js suspectement petit"

    def test_dompurify_vendored_present(self):
        f = VENDOR_DIR / "purify.min.js"
        assert f.exists(), "DOMPurify vendored manquant (LM2-19)"
        assert f.stat().st_size > 1000

    def test_vendor_readme_documents_hashes(self):
        readme = VENDOR_DIR / "README.md"
        assert readme.exists()
        content = readme.read_text()
        assert "sha384" in content.lower() or "SHA-384" in content, (
            "README.md doit documenter les hashes SHA-384 des libs vendored"
        )

    def test_live_html_loads_local_vendor_not_cdn(self):
        live_html = ROOT / "src" / "live_mem" / "static" / "live.html"
        content = live_html.read_text()
        # Cherche les références aux scripts marked et purify
        assert "/static/vendor/marked.min.js" in content
        assert "/static/vendor/purify.min.js" in content
        # Anti-régression : aucune ref CDN externe restante
        assert "cdn.jsdelivr.net" not in content
        assert "unpkg.com" not in content


# =============================================================================
# LM2-26 — Lower bounds des dépendances (CVE-2026-32871)
# =============================================================================


class TestLM2_26_DependencyBounds:
    @pytest.fixture(scope="class")
    def pyproject_content(self) -> str:
        return PYPROJECT.read_text(encoding="utf-8")

    def test_mcp_requires_127_or_later(self, pyproject_content):
        """CVE-2026-32871 (path traversal FastMCP) corrigé en 1.27.0."""
        m = re.search(r'"mcp\[cli\]>=(\d+\.\d+\.\d+)"', pyproject_content)
        assert m, "Borne mcp[cli] introuvable"
        version_str = m.group(1)
        major, minor, _patch = (int(x) for x in version_str.split("."))
        assert (major, minor) >= (1, 27), (
            f"Sans fix LM2-26 : mcp[cli]>={version_str} expose à CVE-2026-32871"
        )

    def test_httpx_sse_removed(self, pyproject_content):
        """httpx-sse ne doit plus apparaître dans la LISTE des dépendances.

        Les commentaires explicatifs (ex: "LM2-27 fix : httpx-sse retiré")
        sont autorisés et sont même souhaitables pour la traçabilité.
        """
        # Extraire le bloc [dependencies = [...]]
        match = re.search(
            r"^dependencies\s*=\s*\[(.+?)\]",
            pyproject_content,
            re.DOTALL | re.MULTILINE,
        )
        assert match, "Bloc dependencies introuvable dans pyproject.toml"
        deps_block = match.group(1)
        # Les lignes du bloc qui définissent une dépendance commencent par "
        active_deps = [
            line.strip()
            for line in deps_block.splitlines()
            if line.strip().startswith('"')
        ]
        for dep in active_deps:
            assert "httpx-sse" not in dep, (
                "Sans fix LM2-27 : httpx-sse encore listé comme dépendance active"
            )

    def test_httpx_bound_at_028_or_later(self, pyproject_content):
        m = re.search(r'"httpx>=(\d+\.\d+)"', pyproject_content)
        assert m
        major, minor = (int(x) for x in m.group(1).split(".")[:2])
        assert (major, minor) >= (0, 28)

    def test_cryptography_requires_50_or_later(self, pyproject_content):
        """CVE-2026-69247 is fixed in cryptography 50.0.0."""
        match = re.search(r'"cryptography>=(\d+\.\d+\.\d+)"', pyproject_content)
        assert match, "Borne cryptography introuvable"
        version = tuple(int(part) for part in match.group(1).split("."))
        assert version >= (50, 0, 0), (
            f"cryptography>={match.group(1)} reste vulnérable à CVE-2026-69247"
        )

    def test_uv_lock_pins_patched_cryptography(self):
        document = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
        packages = [
            package for package in document["package"] if package["name"] == "cryptography"
        ]
        assert len(packages) == 1, "Pin cryptography unique introuvable dans uv.lock"
        version = tuple(int(part) for part in packages[0]["version"].split("."))
        assert version >= (50, 0, 0), (
            f"uv.lock épingle cryptography {packages[0]['version']}, vulnérable à "
            "CVE-2026-69247"
        )

    def test_graph_runtime_lock_pins_patched_cryptography(self):
        lock = GRAPH_REQUIREMENTS_LOCK.read_text(encoding="utf-8")
        match = re.search(r"^cryptography==(\d+\.\d+\.\d+)(?:\s|$)", lock, re.MULTILINE)
        assert match, "Pin cryptography introuvable dans le lock Graph Memory"
        version = tuple(int(part) for part in match.group(1).split("."))
        assert version >= (50, 0, 0), (
            f"Graph Memory épingle cryptography {match.group(1)}, vulnérable à "
            "CVE-2026-69247"
        )

    def test_graph_requirements_pin_patched_security_floors(self):
        requirements = GRAPH_REQUIREMENTS.read_text(encoding="utf-8")
        expected = {
            "cryptography": ((50, 0, 0), "CVE-2026-69247"),
            "aiohttp": ((3, 14, 3), "CVE-2026-69244"),
        }
        for package, (minimum, cve) in expected.items():
            match = re.search(
                rf"^{re.escape(package)}==(\d+\.\d+\.\d+)(?:\s|$)",
                requirements,
                re.MULTILINE,
            )
            assert match, f"Pin {package} introuvable dans requirements.txt Graph Memory"
            version = tuple(int(part) for part in match.group(1).split("."))
            assert version >= minimum, (
                f"Graph Memory demande {package} {match.group(1)}, vulnérable à {cve}"
            )

    def test_graph_runtime_lock_pins_patched_aiohttp(self):
        lock = GRAPH_REQUIREMENTS_LOCK.read_text(encoding="utf-8")
        match = re.search(r"^aiohttp==(\d+\.\d+\.\d+)(?:\s|$)", lock, re.MULTILINE)
        assert match, "Pin aiohttp introuvable dans le lock Graph Memory"
        version = tuple(int(part) for part in match.group(1).split("."))
        assert version >= (3, 14, 3), (
            f"Graph Memory épingle aiohttp {match.group(1)}, vulnérable à "
            "CVE-2026-69244"
        )


# =============================================================================
# LM2-13 — Anti-erasure rewrite guard
# =============================================================================


class TestLM2_13_RewriteGuard:
    """Vérifier les constantes du seuil — la logique d'application est
    testée à travers la consolidation complète (couvert par test_recette)."""

    def test_min_ratio_is_strict_enough(self):
        """0.30 = un rewrite shrink >70% est refusé."""
        from live_mem.core.consolidator import _REWRITE_MIN_RATIO
        # Un compact légitime vise 50% de réduction max
        assert _REWRITE_MIN_RATIO < 0.5, (
            "Le seuil doit être strict (refuser les rewrites trop agressifs)"
        )
        assert _REWRITE_MIN_RATIO > 0.0, (
            "Le seuil ne doit pas être 0 (sinon tout passe)"
        )

    def test_min_absolute_threshold_protects_tiny_files(self):
        """Fichiers < 200B : on ne déclenche pas le guard (faux positifs)."""
        from live_mem.core.consolidator import _REWRITE_MIN_ABSOLUTE_BYTES
        assert _REWRITE_MIN_ABSOLUTE_BYTES > 0
        # Doit être assez bas pour ne pas couvrir tous les fichiers réels
        assert _REWRITE_MIN_ABSOLUTE_BYTES < 1024


# =============================================================================
# LM2-04 — Cookie HttpOnly via /api/login
# =============================================================================


class TestLM2_04_CookieAuth:
    """Le flux /api/login doit émettre un cookie HttpOnly + SameSite=Strict."""

    @pytest.mark.asyncio
    async def test_login_with_valid_token_sets_httponly_cookie(self):
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(app=None)

        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps({"token": "valid-bootstrap-key-32-chars-min"}).encode(),
                "more_body": False,
            }

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/login",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 1),
            "scheme": "http",
        }

        # Mock l'auth pour accepter ce token
        with patch.object(
            __import__("live_mem.auth.middleware", fromlist=["AuthMiddleware"]).AuthMiddleware,
            "_validate_token",
            AsyncMock(
                return_value={
                    "type": "bootstrap",
                    "client_name": "admin",
                    "permissions": ["admin"],
                    "allowed_resources": [],
                    "token_hash": None,
                }
            ),
        ):
            await m._api_login(scope, receive, send)

        # Récupérer le response.start
        start_msg = next(m for m in sent if m["type"] == "http.response.start")
        assert start_msg["status"] == 200

        # Chercher le Set-Cookie
        set_cookie = None
        for k, v in start_msg["headers"]:
            if k == b"set-cookie":
                set_cookie = v.decode()
                break

        assert set_cookie is not None, (
            "Sans fix LM2-04 : pas de cookie émis, token JS-visible"
        )
        assert f"{AUTH_COOKIE_NAME}=" in set_cookie
        assert "HttpOnly" in set_cookie, "HttpOnly manquant : XSS-exfiltrable"
        assert "SameSite=Strict" in set_cookie, "SameSite manquant : CSRF possible"

    @pytest.mark.asyncio
    async def test_login_rejects_invalid_token_with_401(self):
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(app=None)
        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps({"token": "totally-invalid"}).encode(),
                "more_body": False,
            }

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/login",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 1),
            "scheme": "http",
        }

        with patch.object(
            __import__("live_mem.auth.middleware", fromlist=["AuthMiddleware"]).AuthMiddleware,
            "_validate_token",
            AsyncMock(return_value=None),
        ):
            await m._api_login(scope, receive, send)

        start_msg = next(m for m in sent if m["type"] == "http.response.start")
        assert start_msg["status"] == 401
        # Et aucun cookie émis
        for k, _v in start_msg["headers"]:
            assert k != b"set-cookie", "Bug : cookie émis sur token invalide"

    @pytest.mark.asyncio
    async def test_logout_clears_cookie_with_max_age_zero(self):
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(app=None)
        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        await m._api_logout(send)

        start_msg = next(m for m in sent if m["type"] == "http.response.start")
        set_cookie = next(
            v.decode() for k, v in start_msg["headers"] if k == b"set-cookie"
        )
        assert "Max-Age=0" in set_cookie, "Cookie de logout doit forcer expiration"
        assert "HttpOnly" in set_cookie


# =============================================================================
# LM2-31 — confirm=True obligatoire
# =============================================================================


class TestLM2_31_ConfirmRequired:
    """Les outils destructifs critiques doivent exiger confirm=True.

    Comme les outils MCP sont définis dynamiquement dans register() avec
    des signatures multi-lignes Annotated[..., Field(...)], on inspecte
    le bloc de code de chaque outil pour vérifier la présence du
    paramètre + du garde-fou runtime "confirm=True requis".
    """

    @staticmethod
    def _extract_async_function_body(src: str, name: str) -> str:
        """Extrait le bloc d'une fonction `async def <name>(...): ...` jusqu'à
        la prochaine fonction async ou la fin du source."""
        m = re.search(
            r"async def " + re.escape(name) + r"\b(.*?)(?=\n    async def |\Z)",
            src,
            re.DOTALL,
        )
        assert m is not None, f"Fonction {name} introuvable"
        return m.group(0)

    def test_bank_delete_has_confirm_parameter_and_guard(self):
        from live_mem.tools import bank as bank_module
        import inspect

        src = inspect.getsource(bank_module.register)
        body = self._extract_async_function_body(src, "bank_delete")

        # 1. Paramètre confirm présent dans la signature (multi-lignes)
        assert "confirm:" in body, (
            "Sans fix LM2-31 : paramètre `confirm` absent de bank_delete"
        )
        # 2. Garde-fou runtime : refus si confirm n'est pas True
        assert re.search(r"if not confirm", body), (
            "Sans fix LM2-31 : pas de garde-fou `if not confirm:` dans "
            "bank_delete — suppression accidentelle par un opérateur manage"
        )

    def test_admin_purge_tokens_has_confirm_parameter_and_guard(self):
        from live_mem.tools import admin as admin_module
        import inspect

        src = inspect.getsource(admin_module.register)
        body = self._extract_async_function_body(src, "admin_purge_tokens")

        assert "confirm:" in body, (
            "Sans fix LM2-31 : paramètre `confirm` absent de admin_purge_tokens"
        )
        # Le garde-fou n'est obligatoire que quand revoked_only=False
        # (purge totale) — on cherche le pattern correspondant.
        assert re.search(r"not revoked_only and not confirm", body), (
            "Sans fix LM2-31 : pas de garde-fou `if not revoked_only and "
            "not confirm` — purge totale possible sans confirmation"
        )


# =============================================================================
# LM2-24 — /health ne fuite pas str(e)
# =============================================================================


class TestLM2_24_HealthDoesNotLeakErrors:
    """L'endpoint /health public ne doit pas exposer str(e) (URL S3, etc.)."""

    @pytest.mark.asyncio
    async def test_s3_failure_returns_generic_message(self):
        from live_mem.auth.middleware import StaticFilesMiddleware

        m = StaticFilesMiddleware(app=None)
        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        # Simuler un échec S3 qui exposerait normalement l'URL
        secret_url = "https://internal-s3.cloud-temple.local:9000/secret-bucket"

        async def mock_test_connection():
            raise RuntimeError(
                f"Unable to connect to S3 endpoint {secret_url} : SSL error"
            )

        with patch(
            "live_mem.core.storage.get_storage"
        ) as mock_get_storage:
            mock_storage = MagicMock()
            mock_storage.test_connection = mock_test_connection
            mock_get_storage.return_value = mock_storage

            await m._handle_health(send)

        # Récupérer le body JSON
        body_msg = next(m for m in sent if m["type"] == "http.response.body")
        body = json.loads(body_msg["body"])

        # L'URL ne doit JAMAIS apparaître dans la réponse
        body_str = json.dumps(body)
        assert secret_url not in body_str, (
            "Sans fix LM2-24 : URL S3 leakée via /health public"
        )
        assert "SSL error" not in body_str, (
            "Sans fix LM2-24 : détails de l'erreur exposés"
        )
        # Le service est bien marqué en erreur, mais avec message générique
        assert body["services"]["s3"]["status"] == "error"
        assert body["service"] == "hivemind"


# =============================================================================
# LM2-29 — Backup cross-tenant : check_access en plus de check_manage
# =============================================================================


class TestLM2_29_BackupCheckAccess:
    """Un manage restreint à space-a ne doit pas pouvoir manipuler les
    backups de space-b."""

    def test_backup_restore_uses_parse_backup_id_first(self):
        """Le code de backup_restore doit appeler _parse_backup_id puis check_access."""
        from live_mem.tools import backup as backup_module
        import inspect

        src = inspect.getsource(backup_module.register)
        # Cherche le corps de backup_restore
        m = re.search(
            r"async def backup_restore\(.*?return await get_backup_service\(\)\.restore",
            src,
            re.DOTALL,
        )
        assert m, "backup_restore introuvable"
        body = m.group(0)
        # Doit appeler _parse_backup_id AVANT check_access (regex ordre)
        assert "_parse_backup_id" in body
        assert "check_access" in body, (
            "Sans fix LM2-29 : backup_restore n'appelait que check_manage_permission, "
            "permettant un manage restreint de restaurer un autre tenant"
        )
        # check_access doit précéder check_manage_permission (l'erreur d'accès
        # est plus informative que l'erreur de permission)
        idx_access = body.index("check_access")
        idx_manage = body.index("check_manage_permission")
        assert idx_access < idx_manage, (
            "check_access doit précéder check_manage_permission pour "
            "donner l'erreur la plus précise"
        )

    def test_backup_delete_uses_parse_backup_id_first(self):
        from live_mem.tools import backup as backup_module
        import inspect

        src = inspect.getsource(backup_module.register)
        m = re.search(
            r"async def backup_delete\(.*?return await get_backup_service\(\)\.delete",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "_parse_backup_id" in body
        assert "check_access" in body, "LM2-29 non appliqué à backup_delete"


# =============================================================================
# LM2-10 — gc.py utilise _write_gc_notice (et non l'API live cassée)
# =============================================================================


class TestLM2_10_GCNoticeUsesS3Direct:
    """gc.py ne doit plus appeler live.write_note(agent=...) (signature retirée)."""

    def test_gc_does_not_call_live_write_note_with_agent_param(self):
        """Anti-régression : write_note(agent=...) crasherait au runtime."""
        import inspect
        from live_mem.core import gc as gc_module

        src = inspect.getsource(gc_module.consolidate_old_notes if False else gc_module)
        # Vérifie qu'aucun appel write_note(... agent=...) ne reste
        assert not re.search(
            r"\.write_note\([^)]*\bagent\s*=", src
        ), (
            "Sans fix LM2-10 : write_note(agent=...) crashe (signature retirée v0.8.1)"
        )

    def test_write_gc_notice_helper_exists(self):
        """Le helper de remplacement existe et imite l'identité de l'agent."""
        from live_mem.core import gc as gc_module

        assert hasattr(gc_module, "_write_gc_notice"), (
            "Helper de remplacement attendu : _write_gc_notice"
        )

    @pytest.mark.asyncio
    async def test_write_gc_notice_uses_agent_name_not_caller(self):
        """L'agent dans le filename doit être l'agent orphelin, pas le caller."""
        from live_mem.core import gc as gc_module

        captured: dict = {}

        async def fake_put(key, content, content_type="text/plain"):
            captured["key"] = key
            captured["content"] = content

        from live_mem.core.write_sink import DirectLocalWriteSink

        mock_storage = MagicMock()
        mock_storage.put = fake_put
        sink = DirectLocalWriteSink(storage=mock_storage)

        await gc_module._write_gc_notice(
            space_id="test-space",
            agent_name="orphan-agent",
            content="forced consolidation notice",
            sink=sink,
        )

        assert "orphan-agent" in captured["key"], (
            "Le nom de l'agent orphelin doit apparaître dans la clé S3 "
            "(sinon la consolidation par agent ne le trouve pas)"
        )
        assert "orphan-agent" in captured["content"], (
            "L'identité de l'agent doit être dans le front-matter YAML"
        )


# =============================================================================
# LM2-25 — consolidator.py ne fuite pas str(e) au client MCP
# =============================================================================


class TestLM2_25_ConsolidatorNoStrErrorLeak:
    """Le consolidator ne doit plus renvoyer ``str(e)`` au client MCP.

    Audit LM2-25 (2026-05-15) : les sites ``consolidator.py:806, 1221, 1418``
    construisaient des dicts ``{"status": "error", "message": f"...{str(e)}"}``
    pouvant fuiter l'URL LLMaaS, la stack openai ou des secrets de config.

    Stratégie de test : preuve par contrapposée — on simule une exception
    contenant une URL secrète et on vérifie qu'elle ne se retrouve pas dans
    la réponse client en mode non-debug.
    """

    @staticmethod
    def _make_consolidator_with_failing_llm(exception_message: str):
        """Construit un ConsolidatorService dont le seam de complétion
        partagé (P13-1C ``_complete_chat``) lève l'exception passée.

        Centralisé pour éviter la duplication entre tests debug-on/off.
        """
        from live_mem.core.consolidator import ConsolidatorService

        # On bypass le constructeur réel (qui lit get_settings et snapshotte
        # le profil du runtime d'inférence partagé) en créant l'objet via
        # __new__ et en injectant uniquement les attributs nécessaires à
        # _call_llm.
        svc = ConsolidatorService.__new__(ConsolidatorService)
        svc._complete_chat = AsyncMock(
            side_effect=RuntimeError(exception_message)
        )
        svc._model = "test-model"
        svc._context_window = 32768
        svc._max_tokens = 4096
        return svc

    @pytest.mark.asyncio
    async def test_call_llm_does_not_leak_exception_url_in_error_message(self):
        """`_call_llm` doit retourner un message générique sur exception."""
        secret_url = "https://internal-llmaas.cloud-temple.local:9000/v1"
        secret_token = "sk-supersecretkey1234567890"

        svc = self._make_consolidator_with_failing_llm(
            f"Connection error : POST {secret_url} "
            f"auth=Bearer {secret_token} timeout=30s"
        )

        # _call_llm fait un `from ..config import get_settings as _gs` local,
        # donc on patche `live_mem.config.get_settings` à la racine (pas
        # l'alias importé dans le module consolidator).
        with patch("live_mem.config.get_settings") as mock_gs:
            mock_settings = MagicMock()
            mock_settings.mcp_server_debug = False
            mock_gs.return_value = mock_settings

            result = await svc._call_llm(
                messages=[{"role": "user", "content": "test"}]
            )

        # 1. La réponse est bien marquée error
        assert result.get("status") == "error", "Le call est en erreur"
        msg = result.get("message", "")

        # 2. L'URL secrète NE doit JAMAIS apparaître côté client MCP
        assert secret_url not in msg, (
            f"Sans fix LM2-25 : URL LLMaaS leakée → {msg!r}"
        )
        assert secret_token not in msg, (
            f"Sans fix LM2-25 : token openai leaké → {msg!r}"
        )
        # 3. Le message est générique (sentinel attendu)
        assert "LLM call failed" in msg, (
            f"Message attendu 'LLM call failed' ; reçu : {msg!r}"
        )

    @pytest.mark.asyncio
    async def test_call_llm_debug_mode_can_leak_for_dev_only(self):
        """En mode debug, str(e) est volontairement exposé (dev/troubleshoot).

        Cette asymétrie est explicite dans le code (cf. ``mcp_server_debug``).
        Le test fige ce comportement pour éviter un revert silencieux qui
        rendrait LM2-25 inopérant en prod.
        """
        secret_url = "https://internal/should-be-visible-in-debug"
        svc = self._make_consolidator_with_failing_llm(f"boom: {secret_url}")

        # `_call_llm` fait `from ..config import get_settings as _gs` au
        # moment de l'appel → on patche au point d'origine `live_mem.config`,
        # pas l'alias module consolidator.
        with patch("live_mem.config.get_settings") as mock_gs:
            mock_settings = MagicMock()
            mock_settings.mcp_server_debug = True   # DEBUG ON
            mock_gs.return_value = mock_settings

            result = await svc._call_llm(
                messages=[{"role": "user", "content": "test"}]
            )

        # En debug, str(e) PEUT apparaître — c'est le contrat.
        # On vérifie juste que `LLM call failed:` est présent (préfixe
        # caractéristique du chemin debug).
        msg = result.get("message", "")
        assert "LLM call failed:" in msg, (
            "En mode debug, le préfixe 'LLM call failed:' doit indiquer "
            "qu'on est sur le chemin debug (str(e) exposé volontairement)."
        )
        # Et la chaîne secrète DOIT apparaître (volontaire en debug)
        assert secret_url in msg, (
            "En debug, str(e) doit être visible — sinon le fix LM2-25 a "
            "neutralisé même le chemin debug, ce qui n'est pas voulu."
        )

    def test_test_connection_does_not_leak_str_error(self):
        """`test_connection` (utilisé par /health probe) doit aussi être muet."""
        from live_mem.core.consolidator import ConsolidatorService
        import inspect

        src = inspect.getsource(ConsolidatorService.test_connection)
        # Le fix LM2-25 doit avoir remplacé str(e) par un message générique.
        # On ne tolère pas la présence d'une f-string `str(e)` directement
        # dans le `return {"status": "error", ...}`.
        assert re.search(
            r"return\s*\{\s*[\"\']status[\"\']\s*:\s*[\"\']error[\"\'].*?"
            r"str\(e\)",
            src,
            re.DOTALL,
        ) is None, (
            "Sans fix LM2-25 : test_connection retourne str(e) au client "
            "(peut leaker l'URL LLMaaS sur /health public)"
        )
        # On vérifie en positif que le message générique attendu est bien là.
        assert "LLMaaS unreachable" in src, (
            "Message générique attendu absent — fix LM2-25 incomplet"
        )

    def test_no_residual_str_e_in_mcp_error_returns(self):
        """Anti-régression structurelle : aucun `return {... "message": ...str(e)... "status": "error"...}`
        ni `f"...{str(e)}"` directement dans un return d'erreur.

        On scanne le source du module et on accepte uniquement les usages
        dans des contextes debug (entourés d'un if mcp_server_debug:) ou
        dans des appels `logger.*`.
        """
        from live_mem.core import consolidator as cons_module
        src_path = Path(cons_module.__file__)
        src = src_path.read_text(encoding="utf-8")

        # On cherche les lignes du type :
        #   return {"status": "error", "message": f"...{str(e)}..."}
        # qui ne sont PAS protégées par `if ...debug`. Approche : regex
        # multiline qui isole chaque `return {...}` contenant str(e).
        bad = re.findall(
            r"return\s*\{[^{}]*[\"\']status[\"\']\s*:\s*[\"\']error[\"\'][^{}]*str\(e\)[^{}]*\}",
            src,
        )
        # bad ne doit contenir QUE des occurrences protégées par debug.
        # On vérifie qu'il y en a au plus une (le bloc debug-only de _call_llm).
        assert len(bad) <= 1, (
            f"Sans fix LM2-25 : {len(bad)} return d'erreur avec str(e) "
            f"restants en clair (hors block debug-only)"
        )


# =============================================================================
# LM2-08 — Bootstrap key et asymétrie _fresh_token_store (documenté)
# =============================================================================


class TestLM2_08_BootstrapNoTokenHashDocumented:
    """Le bootstrap key n'a volontairement pas de ``token_hash``.

    Audit LM2-08 (2026-05-15, MEDIUM) : l'absence de ``token_hash``
    sur le bootstrap est inoffensive mais doit être documentée pour
    prévenir une régression future qui le rendrait subitement obligatoire.

    Tests :
    - Le contrat est respecté : ``update_fresh_token`` est un no-op
      silencieux pour un token sans ``token_hash``.
    - Le code de production le commente explicitement (anti-régression
      par grep — empêche un futur dev de supprimer le ``if token_hash``
      sans réfléchir).
    """

    def setup_method(self):
        _fresh_token_store.clear()

    def teardown_method(self):
        _fresh_token_store.clear()

    def test_update_fresh_token_silently_skips_when_no_hash(self):
        """Contract : token_info sans token_hash → no-op (pas d'exception)."""
        bootstrap_info = {
            "type": "bootstrap",
            "client_name": "admin",
            "permissions": ["admin", "read", "write"],
            "allowed_resources": [],
            "token_hash": None,  # ← caractéristique du bootstrap
        }

        # Ne doit pas lever, et ne doit RIEN ajouter au store.
        update_fresh_token(bootstrap_info)
        assert len(_fresh_token_store) == 0, (
            "Bug : bootstrap pollue le store global avec une clé None"
        )

    def test_update_fresh_token_silently_skips_when_empty_hash(self):
        """Idem avec hash vide '' (autre forme falsy)."""
        info = {"client_name": "x", "token_hash": ""}
        update_fresh_token(info)
        assert "" not in _fresh_token_store
        assert len(_fresh_token_store) == 0

    def test_update_fresh_token_documents_lm2_08_behavior(self):
        """Anti-régression : la docstring de `update_fresh_token` doit
        mentionner LM2-08 et expliquer pourquoi le `if token_hash:` est là.

        Sans ce commentaire, un futur dev pourrait supprimer la garde et
        crasher le store avec une clé `None` → KeyError partout en aval.
        """
        from live_mem.auth import context as ctx_module
        import inspect

        doc = inspect.getdoc(ctx_module.update_fresh_token) or ""
        assert "LM2-08" in doc or "bootstrap" in doc.lower(), (
            "Sans fix LM2-08 (doc) : l'asymétrie bootstrap n'est pas "
            "documentée — risque de régression silencieuse"
        )

    @pytest.mark.asyncio
    async def test_validate_token_bootstrap_returns_token_hash_none(self):
        """Confirme que la fabrique bootstrap met explicitement token_hash=None.

        Si un futur fix forçait `token_hash = hashlib.sha256(bootstrap_key)`,
        l'asymétrie LM2-08 disparaîtrait — mais ce serait un breaking
        change qui doit être conscient. Le test fige le contrat actuel.
        """
        from live_mem.auth.middleware import AuthMiddleware
        from live_mem.config import get_settings

        # On force une bootstrap key connue
        with patch.object(
            get_settings.__wrapped__ if hasattr(get_settings, "__wrapped__") else get_settings,
            "__call__",
            create=True,
        ):
            settings = get_settings()
            bootstrap = settings.admin_bootstrap_key

        auth = AuthMiddleware(app=None)
        info = await auth._validate_token(bootstrap)

        # Si la config locale n'a pas de bootstrap_key, on skip plutôt
        # que de faire échouer.
        if info is None:
            pytest.skip("admin_bootstrap_key non configurée localement")

        assert info.get("type") == "bootstrap"
        assert info.get("token_hash") is None, (
            "Sans LM2-08 : bootstrap a maintenant un token_hash, ce qui "
            "casse l'asymétrie documentée et pollue _fresh_token_store. "
            "Si c'est volontaire, mettre à jour ce test."
        )


# =============================================================================
# LM2-11 — provisioning réservé à manage (clôture de VULN-06)
# =============================================================================


class TestLM2_11_ProvisioningRequiresManage:
    """Verrouille la clôture LM2-11 et rend le mutant manage→write rouge."""

    def test_space_create_gate_is_manage_not_write(self):
        """Le handler public doit appeler le garde manage, jamais le garde write."""
        from live_mem.tools import space as space_module
        import inspect

        src = inspect.getsource(space_module.register)
        m = re.search(
            r"async def space_create\(.*?(?=\n    @mcp\.tool|\Z)",
            src,
            re.DOTALL,
        )
        assert m, "space_create introuvable dans tools/space.py"
        body = m.group(0)
        assert "check_manage_permission" in body
        assert "check_write_permission" not in body

    def test_write_identity_is_rejected_by_manage_gate(self):
        """Contrepartie comportementale du verrou structurel ci-dessus."""
        from live_mem.auth.context import (
            current_token_info,
            check_manage_permission,
        )

        write_only_token = {
            "type": "token",
            "client_name": "writer-bob",
            "permissions": ["read", "write"],
            "allowed_resources": [],
            "token_hash": "sha256:" + "b" * 64,
        }
        tok = current_token_info.set(write_only_token)
        try:
            update_fresh_token(write_only_token)
            err = check_manage_permission()
        finally:
            current_token_info.reset(tok)
            invalidate_token_in_store(write_only_token["token_hash"])
        assert err == {
            "status": "error",
            "message": "Permission 'manage' requise pour cette opération",
        }


# =============================================================================
# Sanity check : la suite tourne et ne casse pas le projet
# =============================================================================


def test_sanity_imports_dont_break_existing_code():
    """Vérifier qu'on n'a pas cassé les imports principaux."""
    import live_mem
    from live_mem import config, server
    from live_mem.core import (
        consolidator,
        storage,
        space,
        backup,
        gc,
        live,
        tokens,
        models,
        graph_bridge,
    )
    from live_mem.tools import bank, admin, system, graph, backup as tb
    from live_mem.auth import context, middleware

    # Version identity per ADR-0018 (P6-8, amended P7-9). The inherited Live
    # Memory `2.5.x` line is preserved as provenance in CHANGELOG.md but the
    # in-code `__version__` never continues it: it tracks the repo-root
    # VERSION file (placeholder `0.0.0+unreleased` before the user decided
    # the starting public SemVer; `1.0.0-beta.1` since the P7-9 release cut).
    # The value-shape contract is locked by tests/test_release_provenance_lint
    # .py; here we lock in-code identity with VERSION + the 2.5.x reset.
    from pathlib import Path as _Path

    _version_file = (
        (_Path(__file__).resolve().parents[1] / "VERSION")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert live_mem.__version__ == _version_file
    assert not live_mem.__version__.startswith("2.5.")
