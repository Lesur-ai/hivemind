# -*- coding: utf-8 -*-
"""
P12-3 (#268) — Graph Memory `PROXY_URL`: configuration, S3 mappings, redaction,
bypass guards.

The vendored Graph Memory runtime (v3.2.0 baseline) had no `PROXY_URL` support:
its AsyncOpenAI inference clients, both document-storage botocore clients, and
the shared token-store reader always connected directly. P12-3 gives the
embedded service the same outbound-proxy contract as the Hivemind core, with an
explicit, statically frozen external-vs-internal classification (never runtime
DNS/IP heuristics):

- proxied when `PROXY_URL` is set: extraction/embedding/provider-health LLM
  clients, document-storage SigV2+SigV4 clients, token-validator S3 reader;
- always direct: Neo4j (bolt), Qdrant, the Hivemind→GM MCP bridge, GM's own
  local health surface, and every unclassified library — enforced by never
  exporting `HTTP_PROXY`/`HTTPS_PROXY` and by injecting the proxy only at the
  classified construction sites.

This file covers the deterministic non-network surfaces:

- GM `Settings.proxy_url` normalization/validation, mirroring the core
  contract (same accepted schemes, same empty-value normalization, fail-closed
  startup on an invalid value);
- botocore `proxies` mappings for the SigV2/SigV4 document-storage clients in
  both signature modes and for the token-validator reader, plus the untouched
  no-proxy baseline;
- proxy-secret redaction helpers and their choke points (health output,
  storage exceptions);
- structural/bypass guards: no proxy env-var export, AsyncOpenAI construction
  only through the shared egress helper, botocore modules wired through the
  egress helper, compose GM service free of global proxy variables.

Runtime fake-proxy evidence (extraction/embeddings/provider-health through a
recording proxy, direct-network trap, client lifecycle) lives in
``tests/test_p12_3_gm_proxy_runtime.py``.

Import strategy: same as ``tests/test_p7_9_vendored_storage_signature.py`` —
the vendored GM package is imported directly with deterministic env-only
settings; no network I/O happens here.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GM_PKG = _REPO_ROOT / "services" / "graph-memory" / "src" / "mcp_memory"

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _set_gm_env(monkeypatch, proxy_url):
    """Set every env var the GM settings import path needs (order-independent).

    Mirrors the P7-9 helper: importing ``mcp_memory.config`` executes a
    module-level ``Settings()`` with required fields, so credentials must be in
    the environment before the first import, and explicit env vars take
    precedence over any local ``.env``.
    """
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://s3.test.invalid:9000")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_REGION_NAME", "fr1")
    monkeypatch.setenv("LLMAAS_API_KEY", "test-llm-key")
    # P13-1C: the shared resolver needs the COMPLETE legacy pair (GM no longer
    # carries its own default endpoint), exactly like a real deployment.
    monkeypatch.setenv("LLMAAS_API_URL", "http://llm.p12-3-hivemind.invalid/v1")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-neo4j-password")
    monkeypatch.setenv("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    if proxy_url is None:
        monkeypatch.delenv("PROXY_URL", raising=False)
    else:
        monkeypatch.setenv("PROXY_URL", proxy_url)


def _gm_settings(monkeypatch, proxy_url):
    """Build fresh GM Settings from env only, clearing the lru_cache around it."""
    _set_gm_env(monkeypatch, proxy_url)
    from mcp_memory.config import get_settings

    get_settings.cache_clear()
    try:
        return get_settings()
    finally:
        get_settings.cache_clear()


def _make_storage(monkeypatch, proxy_url, mode=None):
    """Build a real StorageService under the given proxy/signature env."""
    _set_gm_env(monkeypatch, proxy_url)
    if mode is None:
        monkeypatch.delenv("S3_SIGNATURE_MODE", raising=False)
    else:
        monkeypatch.setenv("S3_SIGNATURE_MODE", mode)

    from mcp_memory.config import get_settings
    from mcp_memory.core.storage import StorageService

    get_settings.cache_clear()
    try:
        return StorageService()
    finally:
        get_settings.cache_clear()


def _proxies_of(client):
    """The proxies mapping a boto3 client will actually use."""
    return client.meta.config.proxies


# --------------------------------------------------------------------------- #
# GM Settings — PROXY_URL normalization/validation (core parity)              #
# --------------------------------------------------------------------------- #

class TestGmProxyUrlSettings:
    def test_unset_is_none(self, monkeypatch):
        assert _gm_settings(monkeypatch, None).proxy_url is None

    def test_empty_normalized_to_none(self, monkeypatch):
        assert _gm_settings(monkeypatch, "").proxy_url is None

    def test_whitespace_normalized_to_none(self, monkeypatch):
        assert _gm_settings(monkeypatch, "   ").proxy_url is None

    def test_http_accepted_and_stripped(self, monkeypatch):
        s = _gm_settings(monkeypatch, "  http://proxy.example:3128  ")
        assert s.proxy_url == "http://proxy.example:3128"

    def test_https_accepted(self, monkeypatch):
        s = _gm_settings(monkeypatch, "https://proxy.example:3128")
        assert s.proxy_url == "https://proxy.example:3128"

    def test_invalid_scheme_fails_closed_at_startup(self, monkeypatch):
        """An invalid PROXY_URL must refuse GM settings construction (the
        module-level ``Settings()`` makes this a startup failure), never a
        silent direct connection. R4: RuntimeError (not ValueError) so
        pydantic can never wrap it into a raw-input-echoing
        ValidationError."""
        with pytest.raises(RuntimeError, match="PROXY_URL must start"):
            _gm_settings(monkeypatch, "socks5://proxy.example:1080")

    def test_bare_host_fails_closed(self, monkeypatch):
        with pytest.raises(RuntimeError, match="PROXY_URL must start"):
            _gm_settings(monkeypatch, "proxy.example.com:3128")

    def test_invalid_scheme_error_never_echoes_credentials(self, monkeypatch):
        """R2/R3/R5 (Codex rounds 2-5): a credential-bearing INVALID value —
        including a password containing raw '@' characters — must not leak
        its userinfo NOR its query/fragment (access_token=...) into the
        startup error message, on any pydantic echo surface — same rule as
        the core."""
        from pydantic import ValidationError

        with pytest.raises(RuntimeError) as excinfo:
            _gm_settings(
                monkeypatch,
                "socks5://svc-user:s3cr3t@pw@proxy.internal:1080"
                "?access_token=qs3cr3t#fr4g",
            )
        assert not isinstance(excinfo.value, ValidationError)
        message = str(excinfo.value)
        assert "PROXY_URL must start" in message
        assert "s3cr3t" not in message
        assert "pw@" not in message
        assert "svc-user" not in message
        assert "qs3cr3t" not in message
        assert "access_token" not in message
        assert "fr4g" not in message
        assert "proxy.internal:1080" in message

    def test_direct_settings_construction_never_carries_secrets(
        self, monkeypatch
    ):
        """R3/R4: a DIRECT ``Settings()`` construction bypasses the
        ``get_settings`` redaction wrapper, so the raw value must never reach
        a pydantic ValidationError at all — the field validator raises an
        unwrapped RuntimeError whose text (the ONLY payload that exists) is
        free of userinfo and query/fragment secrets. No ``errors()``
        structured payload can echo the raw input."""
        _set_gm_env(monkeypatch, None)
        from pydantic import ValidationError

        from mcp_memory.config import Settings

        with pytest.raises(RuntimeError) as excinfo:
            Settings(
                _env_file=None,
                s3_access_key_id="k",
                s3_secret_access_key="s",
                llmaas_api_key="k",
                neo4j_password="p",
                proxy_url=(
                    "socks5://svc-user:s3cr3t-pw@proxy.internal:1080"
                    "?access_token=qs3cr3t#fr4g"
                ),
            )
        assert not isinstance(excinfo.value, ValidationError)
        assert not hasattr(excinfo.value, "errors")
        message = str(excinfo.value)
        assert "PROXY_URL must start" in message
        assert "s3cr3t-pw" not in message
        assert "svc-user" not in message
        assert "qs3cr3t" not in message
        assert "access_token" not in message
        assert "fr4g" not in message

    @pytest.mark.parametrize(
        "raw",
        [None, "", "   ", "http://p:3128", " https://p:3128 ", "tcp://p:1", "p:1"],
    )
    def test_mirror_contract_with_core_settings(self, monkeypatch, raw):
        """One shared .env, one contract: for the same PROXY_URL input the GM
        view must accept/reject and normalize exactly like the Hivemind core
        (`live_mem.config.Settings`), so the two services can never diverge on
        an install."""
        from live_mem.config import Settings as CoreSettings

        core_base = {
            "s3_endpoint_url": "",
            "s3_access_key_id": "",
            "s3_secret_access_key": "",
            "llmaas_api_url": "",
            "llmaas_api_key": "",
            "admin_bootstrap_key": "x" * 40,
        }
        core_error = gm_error = None
        core_value = gm_value = None
        try:
            core_value = CoreSettings.model_validate(
                {**core_base, "proxy_url": raw}
            ).proxy_url
        except (ValueError, RuntimeError) as e:
            core_error = type(e).__name__
        try:
            gm_value = _gm_settings(
                monkeypatch, raw if raw is not None else None
            ).proxy_url
        except (ValueError, RuntimeError) as e:
            gm_error = type(e).__name__
        assert core_error == gm_error
        assert core_value == gm_value


# --------------------------------------------------------------------------- #
# Document-storage botocore clients — proxies mapping                          #
# --------------------------------------------------------------------------- #

_PROXY = "http://proxy.example:3128"
_EXPECTED_MAPPING = {"http": _PROXY, "https": _PROXY}


class TestStorageProxyMapping:
    def test_dual_mode_both_clients_get_proxies(self, monkeypatch):
        svc = _make_storage(monkeypatch, _PROXY, mode="dual")
        assert _proxies_of(svc._client_v4) == _EXPECTED_MAPPING
        assert _proxies_of(svc._client_v2) == _EXPECTED_MAPPING
        assert svc._client_v2 is not svc._client_v4

    def test_sigv4_mode_single_client_gets_proxies(self, monkeypatch):
        svc = _make_storage(monkeypatch, _PROXY, mode="sigv4")
        assert svc._client_v2 is svc._client_v4
        assert _proxies_of(svc._client_v4) == _EXPECTED_MAPPING

    def test_no_proxy_keeps_direct_baseline(self, monkeypatch):
        """Without PROXY_URL the botocore configs must not carry any proxies
        key — byte-compatible with the vendored direct behavior."""
        svc = _make_storage(monkeypatch, None, mode="dual")
        assert not _proxies_of(svc._client_v4)
        assert not _proxies_of(svc._client_v2)

    def test_proxy_does_not_change_signature_modes(self, monkeypatch):
        """The proxy mapping must not weaken the #135 signature-mode contract."""
        svc = _make_storage(monkeypatch, _PROXY, mode="dual")
        assert svc._client.meta.config.signature_version == "s3"
        assert svc._client_v4.meta.config.signature_version == "s3v4"
        svc4 = _make_storage(monkeypatch, _PROXY, mode="sigv4")
        assert svc4._client.meta.config.signature_version == "s3v4"

    def test_proxy_construction_does_not_export_env(self, monkeypatch):
        """Building the proxied storage must never export HTTP(S)_PROXY-style
        variables that would reroute unclassified libraries (Qdrant, urllib
        healthchecks, neo4j tooling)."""
        _make_storage(monkeypatch, _PROXY, mode="dual")
        for key in _PROXY_ENV_KEYS:
            assert key not in os.environ


# --------------------------------------------------------------------------- #
# Token-validator reader — proxies mapping                                     #
# --------------------------------------------------------------------------- #

class TestTokenValidatorProxyMapping:
    def _captured_reader_config(self, monkeypatch, proxy_url):
        """Run the real S3 reader with boto3.client captured; return kwargs."""
        _set_gm_env(monkeypatch, proxy_url)

        import boto3

        from mcp_memory.auth.s3_token_validator import S3TokenValidator
        from mcp_memory.config import get_settings

        captured = {}

        class _FakeBody:
            def read(self):
                return b"{}"

        def _capture_client(service, **kwargs):
            captured["service"] = service
            captured["kwargs"] = kwargs
            class _FakeClient:
                def get_object(self, **_kw):
                    return {"Body": _FakeBody()}
            return _FakeClient()

        monkeypatch.setattr(boto3, "client", _capture_client)
        get_settings.cache_clear()
        try:
            import asyncio

            validator = S3TokenValidator()
            asyncio.run(validator._read_tokens_json_from_s3())
        finally:
            get_settings.cache_clear()
        return captured

    def test_reader_config_gets_proxies(self, monkeypatch):
        captured = self._captured_reader_config(monkeypatch, _PROXY)
        assert captured["service"] == "s3"
        assert captured["kwargs"]["config"].proxies == _EXPECTED_MAPPING

    def test_reader_config_direct_without_proxy(self, monkeypatch):
        captured = self._captured_reader_config(monkeypatch, None)
        assert not captured["kwargs"]["config"].proxies

    def test_reader_keeps_signature_mode_mirror(self, monkeypatch):
        """The proxy change must not weaken the P7-4 signature-mode mirror."""
        monkeypatch.setenv("S3_SIGNATURE_MODE", "sigv4")
        captured = self._captured_reader_config(monkeypatch, _PROXY)
        assert captured["kwargs"]["config"].signature_version == "s3v4"


# --------------------------------------------------------------------------- #
# Redaction helpers and choke points                                           #
# --------------------------------------------------------------------------- #

_SECRET_URL = "http://svc-user:s3cr3t-pw@proxy.internal:3128"


class TestProxyRedaction:
    def test_redact_strips_userinfo(self):
        from mcp_memory.core.egress import redact_proxy_secrets

        msg = f'Failed to connect to proxy URL: "{_SECRET_URL}"'
        redacted = redact_proxy_secrets(msg)
        assert "s3cr3t-pw" not in redacted
        assert "svc-user" not in redacted
        assert "proxy.internal:3128" in redacted

    def test_redact_strips_query_parameters(self):
        from mcp_memory.core.egress import redact_proxy_secrets

        msg = "error for https://s3.example/bucket/key?X-Amz-Signature=abc123&t=2"
        redacted = redact_proxy_secrets(msg)
        assert "abc123" not in redacted
        assert "X-Amz-Signature" not in redacted
        assert "https://s3.example/bucket/key" in redacted

    def test_redact_strips_fragments(self):
        """R4: a VALID proxy URL may carry a credential in its fragment —
        the redactor cuts at the first '?' OR '#'."""
        from mcp_memory.core.egress import redact_proxy_secrets

        msg = f'Failed to connect to proxy URL: "{_SECRET_URL}#frag-t0ken"'
        redacted = redact_proxy_secrets(msg)
        assert "frag-t0ken" not in redacted
        assert "s3cr3t-pw" not in redacted
        assert "proxy.internal:3128" in redacted

    def test_redact_strips_multi_at_userinfo(self):
        """R5: URL parsers use the FINAL '@' as the authority delimiter, so a
        password containing raw '@' characters must be stripped through the
        last one — no partial-password suffix may survive."""
        from mcp_memory.core.egress import redact_proxy_secrets

        msg = 'proxy error for "http://svc:pa@ss@proxy.internal:3128/x"'
        redacted = redact_proxy_secrets(msg)
        assert "pa@" not in redacted
        assert "ss@" not in redacted
        assert "svc" not in redacted
        assert "proxy.internal:3128" in redacted

    def test_redact_is_idempotent_and_noop_on_clean_text(self):
        from mcp_memory.core.egress import redact_proxy_secrets

        clean = "S3 unreachable at http://s3.internal:9000/bucket (timeout)"
        assert redact_proxy_secrets(clean) == clean
        once = redact_proxy_secrets(f"x {_SECRET_URL} y")
        assert redact_proxy_secrets(once) == once

    def test_display_proxy_url_is_origin_only(self):
        from mcp_memory.core.egress import display_proxy_url

        shown = display_proxy_url(f"{_SECRET_URL}/path?token=abc")
        assert shown == "http://proxy.internal:3128"

    def test_storage_exceptions_are_redacted(self, monkeypatch):
        """A botocore proxy failure must surface without the credential-bearing
        proxy URL while keeping its exception type (fail-closed, no leak)."""
        from botocore.exceptions import ProxyConnectionError

        svc = _make_storage(monkeypatch, _SECRET_URL, mode="dual")

        def _boom(**_kw):
            raise ProxyConnectionError(
                proxy_url=f"{_SECRET_URL}?access_token=qs3cr3t#fr4g"
            )

        monkeypatch.setattr(svc._client_v2, "put_object", _boom)
        import asyncio

        with pytest.raises(ProxyConnectionError) as excinfo:
            asyncio.run(
                svc.upload_document(
                    memory_id="m1", filename="f.txt", content=b"x"
                )
            )
        assert "s3cr3t-pw" not in str(excinfo.value)
        assert "svc-user" not in str(excinfo.value)
        assert "qs3cr3t" not in str(excinfo.value)
        assert "fr4g" not in str(excinfo.value)

    def test_check_documents_recovered_proxy_error_is_redacted(self, monkeypatch):
        """R1 fix (Codex #270 round 1): ``check_documents`` RECOVERS every S3
        failure into its returned payload instead of re-raising, so the
        method-level decorator never sees it — the recovered
        ``ProxyConnectionError`` text (raw credential-bearing proxy URL) must
        be redacted before it reaches ``details`` and the ``storage_check``
        MCP response built from them."""
        from botocore.exceptions import ProxyConnectionError

        svc = _make_storage(monkeypatch, _SECRET_URL, mode="dual")

        def _boom(**_kw):
            raise ProxyConnectionError(
                proxy_url=f"{_SECRET_URL}?access_token=qs3cr3t#fr4g"
            )

        monkeypatch.setattr(svc._client_v4, "head_object", _boom)
        import asyncio

        result = asyncio.run(
            svc.check_documents(["s3://test-bucket/m1/documents/a.txt"])
        )
        assert result["errors"] == 1
        detail = result["details"][0]
        assert detail["status"] == "error"
        assert "s3cr3t-pw" not in detail["error"]
        assert "svc-user" not in detail["error"]
        assert "qs3cr3t" not in detail["error"]
        assert "fr4g" not in detail["error"]
        assert "proxy.internal:3128" in detail["error"]

    def test_raised_client_error_redacts_log_and_message(
        self, monkeypatch, capsys
    ):
        """R6 (Codex round 6): the ``except ClientError`` blocks log ``{e}``
        BEFORE the decorator can rewrite the exception — both the stderr log
        emitted inside ``upload_document`` and the re-raised ``str()`` (the
        ingestion/MCP-facing text) must be free of userinfo, query, and
        fragment secrets."""
        from botocore.exceptions import ClientError

        svc = _make_storage(monkeypatch, _SECRET_URL, mode="dual")
        secret_msg = (
            "denied via http://svc-user:s3cr3t@pw@proxy.internal:3128/x"
            "?access_token=qs3cr3t#fr4g"
        )

        def _boom(**_kw):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": secret_msg}},
                "PutObject",
            )

        monkeypatch.setattr(svc._client_v2, "put_object", _boom)
        import asyncio

        with pytest.raises(ClientError) as excinfo:
            asyncio.run(
                svc.upload_document(
                    memory_id="m1", filename="f.txt", content=b"x"
                )
            )
        for surface in (capsys.readouterr().err, str(excinfo.value)):
            assert "s3cr3t" not in surface
            assert "pw@" not in surface
            assert "svc-user" not in surface
            assert "qs3cr3t" not in surface
            assert "fr4g" not in surface
        assert "AccessDenied" in str(excinfo.value)

    def test_recovered_delete_and_list_paths_redact_logs(
        self, monkeypatch, capsys
    ):
        """R6: ``delete_objects`` and ``list_documents`` RECOVER ClientError
        (per-object log + counters / empty list) — their stderr logs must be
        clean too."""
        from botocore.exceptions import ClientError

        svc = _make_storage(monkeypatch, _SECRET_URL, mode="dual")
        secret_msg = (
            "denied via http://svc-user:s3cr3t@pw@proxy.internal:3128/x"
            "?access_token=qs3cr3t#fr4g"
        )

        def _boom(**_kw):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": secret_msg}},
                "DeleteObject",
            )

        monkeypatch.setattr(svc._client_v2, "delete_object", _boom)
        monkeypatch.setattr(svc._client_v4, "list_objects_v2", _boom)
        import asyncio

        result = asyncio.run(svc.delete_objects(["m1/documents/a.txt"]))
        assert result["error_count"] == 1
        listed = asyncio.run(svc.list_documents("m1"))
        assert listed == []
        err_output = capsys.readouterr().err
        assert "s3cr3t" not in err_output
        assert "pw@" not in err_output
        assert "svc-user" not in err_output
        assert "qs3cr3t" not in err_output
        assert "fr4g" not in err_output

    def test_check_documents_client_error_branch_is_redacted(self, monkeypatch):
        """The recovered ClientError branch formats server-provided message
        text into the payload — it goes through the same redaction (no-op on
        normal messages, strips URL secrets if a message embeds them)."""
        from botocore.exceptions import ClientError

        svc = _make_storage(monkeypatch, _SECRET_URL, mode="dual")
        err = ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": f"denied via {_SECRET_URL}/x?token=abc",
                }
            },
            "HeadObject",
        )

        def _boom(**_kw):
            raise err

        monkeypatch.setattr(svc._client_v4, "head_object", _boom)
        import asyncio

        result = asyncio.run(
            svc.check_documents(["s3://test-bucket/m1/documents/a.txt"])
        )
        detail = result["details"][0]
        assert detail["status"] == "error"
        assert "AccessDenied" in detail["error"]
        assert "s3cr3t-pw" not in detail["error"]
        assert "token=abc" not in detail["error"]

    def test_generate_answer_recovered_error_is_redacted(self, monkeypatch):
        """``generate_answer`` returns its recovered error text to the client
        (Q&A answer string) — same redaction requirement as the R1 fix."""
        _set_gm_env(monkeypatch, _SECRET_URL)
        from mcp_memory.config import get_settings
        from mcp_memory.core.extractor import ExtractorService
        from tests.fakes.inference_fakes import gm_inference_runtime

        import asyncio

        get_settings.cache_clear()
        try:
            with gm_inference_runtime(proxy_url=_SECRET_URL):
                svc = ExtractorService()

                async def _boom(*_a, **_kw):
                    raise RuntimeError(
                        f'Failed to connect to proxy URL: "{_SECRET_URL}"'
                    )

                monkeypatch.setattr(svc, "_complete", _boom)
                answer = asyncio.run(svc.generate_answer("question"))
                asyncio.run(svc.close())
        finally:
            get_settings.cache_clear()
        assert "s3cr3t-pw" not in answer
        assert "svc-user" not in answer
        # P12-3 baseline preserved verbatim: Graph Memory's own
        # ``redact_proxy_secrets`` strips USERINFO and keeps the origin, which
        # is the accepted contract this lot must not change. (The shared
        # boundary applies the stricter ADR-0027 rendering to the text IT
        # produces — see hivemind_inference.egress.display_proxy_url.)
        assert "proxy.internal:3128" in answer

    def test_shared_runtime_proxy_log_is_display_safe(self, monkeypatch, caplog):
        """P13-1C: the P12-3 operator signal ("egress goes through a proxy")
        survives the migration, but moves to the ONE place that now owns the
        transport — the shared runtime. The message must exist (otherwise
        operators silently lose the signal) and must carry no credential, host,
        or port.
        """
        import logging

        from hivemind_inference import InferenceRuntime
        from tests.fakes.inference_fakes import make_inference_config

        with caplog.at_level(logging.INFO, logger="hivemind_inference.runtime"):
            InferenceRuntime(
                make_inference_config(chat=True, embedding=True),
                proxy_url=_SECRET_URL,
            )
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "hivemind_inference.runtime"
        ]
        assert any("via proxy" in message for message in messages)
        joined = "\n".join(messages)
        assert "s3cr3t-pw" not in joined
        assert "svc-user" not in joined
        assert "proxy.internal" not in joined
        assert "3128" not in joined

    def test_migrated_inference_services_print_nothing_on_construction(
        self, monkeypatch, capsys
    ):
        """The per-service proxy banners are gone with the per-service
        transports: construction is now a pure profile snapshot, so no
        credential-bearing value can reach stderr from these paths at all."""
        _set_gm_env(monkeypatch, _SECRET_URL)
        from mcp_memory.config import get_settings
        from mcp_memory.core.embedder import EmbeddingService
        from mcp_memory.core.extractor import ExtractorService
        from tests.fakes.inference_fakes import gm_inference_runtime

        get_settings.cache_clear()
        try:
            with gm_inference_runtime(proxy_url=_SECRET_URL):
                ExtractorService()
                EmbeddingService()
        finally:
            get_settings.cache_clear()
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""


# --------------------------------------------------------------------------- #
# Structural / bypass guards                                                   #
# --------------------------------------------------------------------------- #

def _gm_module_trees():
    for path in sorted(_GM_PKG.rglob("*.py")):
        yield path.relative_to(_REPO_ROOT).as_posix(), ast.parse(
            path.read_text(encoding="utf-8")
        )


def _module_calls_name(tree: ast.AST, names: set[str]) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None
            )
            if called in names:
                return True
    return False


def _module_imports_egress(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "egress" or node.module.endswith(".egress")
        ):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name.endswith("egress") for alias in node.names
        ):
            return True
    return False


class TestBypassGuards:
    def test_no_gm_module_exports_proxy_env_vars(self):
        """The classification is per-client injection; a global env export
        would reroute Qdrant/urllib/neo4j tooling and is forbidden."""
        forbidden = {k.lower() for k in _PROXY_ENV_KEYS}
        offenders = []
        for rel, tree in _gm_module_trees():
            for node in ast.walk(tree):
                # os.environ["HTTP_PROXY"] = ... / os.environ.setdefault(...)
                if isinstance(node, ast.Subscript) and isinstance(
                    node.ctx, ast.Store
                ):
                    sl = node.slice
                    if (
                        isinstance(sl, ast.Constant)
                        and isinstance(sl.value, str)
                        and sl.value.lower() in forbidden
                    ):
                        offenders.append(rel)
                if isinstance(node, ast.Call):
                    func = node.func
                    called = func.attr if isinstance(func, ast.Attribute) else None
                    if called in ("setdefault", "putenv", "setenv") and any(
                        isinstance(a, ast.Constant)
                        and isinstance(a.value, str)
                        and a.value.lower() in forbidden
                        for a in node.args
                    ):
                        offenders.append(rel)
        assert offenders == []

    def test_no_gm_module_constructs_a_provider_sdk_client(self):
        """P13-1C (ADR-0027) supersedes the P12-3 "wire the SDK through the
        egress helper" rule with a stronger one: the embedded runtime builds NO
        provider client at all. Every chat/embedding/discovery call goes through
        the shared boundary, whose registered adapters are the single provider
        construction seam — so an unclassified direct client is not merely
        discouraged, it has nowhere to be written.
        """
        forbidden = {"AsyncOpenAI", "OpenAI", "AsyncAnthropic", "Anthropic"}
        offenders = [
            rel
            for rel, tree in _gm_module_trees()
            if _module_calls_name(tree, forbidden)
        ]
        assert offenders == []

    def test_gm_inference_modules_consume_the_shared_boundary(self):
        """Non-vacuity companion to the guard above: the two inference services
        must still be in scope and must reach the provider through the shared
        package, not through some third path that happens to avoid the SDK
        names."""
        for rel in (
            "services/graph-memory/src/mcp_memory/core/extractor.py",
            "services/graph-memory/src/mcp_memory/core/embedder.py",
        ):
            source = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "from hivemind_inference" in source, rel
            assert "from .inference_runtime import get_inference_runtime" in source, rel

    def test_boto3_modules_read_egress_proxy_mapping(self):
        """Every GM module building boto3 clients must consult the egress
        proxies helper (one branch of the shared classification, never an
        unconditional direct config)."""
        builders = []
        offenders = []
        for rel, tree in _gm_module_trees():
            has_boto3_client = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "client"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "boto3"
                for node in ast.walk(tree)
            )
            if not has_boto3_client:
                continue
            builders.append(rel)
            if not _module_calls_name(tree, {"botocore_proxies"}):
                offenders.append(rel)
        assert "services/graph-memory/src/mcp_memory/core/storage.py" in builders
        assert (
            "services/graph-memory/src/mcp_memory/auth/s3_token_validator.py"
            in builders
        )
        assert offenders == []

    def test_compose_gm_service_defines_no_global_proxy_vars(self):
        """Root compose stays the configuration authority: `PROXY_URL` flows
        through the shared env_file, and neither the GM nor the Hivemind
        service may define container-level HTTP(S)_PROXY variables that would
        reroute unclassified processes (healthchecks, datastore clients)."""
        compose = yaml.safe_load(
            (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        for service in ("graph-memory", "hivemind"):
            env_entries = compose["services"][service].get("environment", [])
            for entry in env_entries:
                key = entry.split("=", 1)[0] if isinstance(entry, str) else entry
                assert key not in _PROXY_ENV_KEYS, (
                    f"{service} must not define global proxy env var {key}"
                )
            assert compose["services"][service].get("env_file") == ".env"

    def test_qdrant_and_neo4j_clients_stay_unclassified_direct(self):
        """Internal datastores must never gain proxy wiring: the vector-store
        and graph modules stay free of egress/proxy references."""
        for rel, tree in _gm_module_trees():
            if rel.endswith(("core/vector_store.py", "core/graph.py")):
                assert not _module_imports_egress(tree), rel
                src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
                assert "PROXY_URL" not in src, rel
                assert "proxies" not in src, rel
