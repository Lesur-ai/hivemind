# -*- coding: utf-8 -*-
"""#306 integration proofs for the two process ASGI stacks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import warnings

import pytest
import uvicorn
from uvicorn.lifespan.on import LifespanOn

from hivemind_inference.asgi_lifespan import LifespanGuard

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GRAPH_ENVIRONMENT = {
    "S3_ENDPOINT_URL": "http://s3.test.invalid:9000",
    "S3_ACCESS_KEY_ID": "test-access-key",
    "S3_SECRET_ACCESS_KEY": "test-secret-key",
    "S3_BUCKET_NAME": "test-bucket",
    "S3_REGION_NAME": "fr1",
    "LLMAAS_API_KEY": "test-llm-key",
    "LLMAAS_API_URL": "http://llm.test.invalid/v1",
    "NEO4J_PASSWORD": "test-neo4j-password",
}


class _RecordingLifespanOn(LifespanOn):
    def __init__(self, config):
        super().__init__(config)
        self.sent = []

    async def send(self, message):
        self.sent.append(dict(message))
        await super().send(message)


def _lifespan(app) -> _RecordingLifespanOn:
    return _RecordingLifespanOn(
        uvicorn.Config(app, lifespan="auto", log_config=None)
    )


def _inner_app():
    async def inner(scope, receive, send):
        while True:
            message = await receive()
            phase = message["type"].rsplit(".", 1)[-1]
            await send({"type": f"lifespan.{phase}.complete"})
            if phase == "shutdown":
                return

    return inner


def _set_graph_memory_environment(monkeypatch) -> None:
    for key, value in _GRAPH_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)


def _prepare_core_session_preflight(monkeypatch):
    from live_mem.core import embedded_secret
    from live_mem.core import space as space_module
    from live_mem.core import tokens as tokens_module

    class Spaces:
        async def list_spaces(self):
            return {"status": "ok", "spaces": []}

    class Tokens:
        async def migrate_empty_space_ids(self, ids):
            return {
                "status": "ok",
                "migrated": 0,
                "already_migrated": True,
            }

        async def register_internal_long_token(self, token):
            return {
                "status": "ok",
                "registered": False,
                "current_active": True,
                "rotated_out": 0,
            }

    monkeypatch.setattr(space_module, "get_space_service", lambda: Spaces())
    monkeypatch.setattr(tokens_module, "get_token_service", lambda: Tokens())
    monkeypatch.setattr(
        embedded_secret,
        "resolve_embedded_token",
        lambda *_args, **_kwargs: "durable-test-token",
    )


class TestCoreProcessScope:
    async def test_n_mcp_sessions_do_not_close_process_resource(
        self,
        monkeypatch,
    ):
        from live_mem import server

        _prepare_core_session_preflight(monkeypatch)
        closes = []
        process_phases = []

        async def close_process_resource():
            closes.append("process-close")

        async def process_inner(scope, receive, send):
            while True:
                message = await receive()
                phase = message["type"].rsplit(".", 1)[-1]
                process_phases.append(phase)
                await send({"type": f"lifespan.{phase}.complete"})
                if phase == "shutdown":
                    return

        monkeypatch.setattr(
            server,
            "_close_core_process_resources",
            close_process_resource,
        )
        monkeypatch.setattr(
            server,
            "_reject_weak_bootstrap_key",
            lambda key: None,
        )
        monkeypatch.setattr(
            server.mcp,
            "streamable_http_app",
            lambda: process_inner,
        )
        monkeypatch.setattr(
            server.settings,
            "hivemind_mesh_enabled",
            "false",
        )

        app = server.create_app()
        assert isinstance(app, LifespanGuard)
        assert app._redact is server.redact_proxy_secrets
        assert app._report is server._report_lifespan

        process = _lifespan(app)
        await asyncio.wait_for(process.startup(), timeout=2.0)
        assert closes == []

        # FastMCP enters this context for each MCP session. Neither session is
        # allowed to close the shared consolidator transport.
        for _ in range(3):
            async with server._lifespan(None):
                assert closes == []

        await asyncio.wait_for(process.shutdown(), timeout=2.0)
        assert closes == ["process-close"]
        assert process_phases == ["startup", "shutdown"]
        assert not process.shutdown_failed

    def test_three_in_process_mcp_sessions_share_one_process_close(
        self,
        monkeypatch,
    ):
        """Three TestClient MCP flows enter three in-process session lifespans,
        while the process-owned resource closes only at ASGI shutdown."""

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from starlette.testclient import TestClient

        from mcp.server.transport_security import TransportSecuritySettings
        import live_mem.auth.middleware as auth_middleware
        import live_mem.core.consolidator as consolidator_module
        from live_mem import server
        from live_mem.tools.exposure import HivemindFastMCP

        _prepare_core_session_preflight(monkeypatch)
        session_events = []
        process_closes = []

        @asynccontextmanager
        async def counted_session_lifespan(app):
            session_events.append("open")
            try:
                async with server._lifespan(app):
                    yield
            finally:
                session_events.append("close")

        async def close_process_resource():
            process_closes.append("process-close")

        fresh_mcp = HivemindFastMCP(
            name="issue-306-real-sessions",
            lifespan=counted_session_lifespan,
            json_response=True,
            transport_security=TransportSecuritySettings(
                allowed_hosts=["testserver"]
            ),
        )
        monkeypatch.setattr(
            consolidator_module,
            "close_consolidator_if_initialized",
            close_process_resource,
        )
        monkeypatch.setattr(server, "mcp", fresh_mcp)
        monkeypatch.setattr(
            server,
            "_reject_weak_bootstrap_key",
            lambda _key: None,
        )
        monkeypatch.setattr(server.settings, "hivemind_mesh_enabled", "false")
        monkeypatch.setattr(
            auth_middleware,
            "AuthMiddleware",
            lambda inner, **_kwargs: inner,
        )
        app = server.create_app()
        assert isinstance(app, LifespanGuard)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

        with TestClient(app) as client:
            for request_id in range(1, 4):
                response = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {
                                "name": f"issue-306-{request_id}",
                                "version": "1",
                            },
                        },
                    },
                )
                assert response.status_code == 200
                assert "mcp-session-id" in response.headers

                session_headers = {
                    **headers,
                    "mcp-session-id": response.headers["mcp-session-id"],
                    "mcp-protocol-version": "2025-06-18",
                }
                terminated = client.delete("/mcp", headers=session_headers)
                assert terminated.status_code == 200

                async def wait_until_session_closed():
                    for _ in range(200):
                        if session_events.count("close") == request_id:
                            return
                        await asyncio.sleep(0.01)
                    raise AssertionError("MCP session lifespan did not close")

                client.portal.call(wait_until_session_closed)
                assert process_closes == []

            assert session_events == [
                "open",
                "close",
                "open",
                "close",
                "open",
                "close",
            ]

        assert session_events == [
            "open",
            "close",
            "open",
            "close",
            "open",
            "close",
        ]
        assert process_closes == ["process-close"]


class TestGraphMemoryProcessScope:
    def test_docker_layout_imports_the_shared_guard(self, tmp_path):
        """Recreate the Dockerfile's /app COPY layout and import its entrypoint.

        This is the faithful local packaging proof when the Docker daemon is
        unavailable; CI still performs the actual image build.
        """

        app_root = tmp_path / "app"
        shutil.copytree(
            _REPO_ROOT / "services" / "graph-memory" / "src",
            app_root / "src",
        )
        shutil.copytree(
            _REPO_ROOT / "src" / "hivemind_inference",
            app_root / "hivemind_inference",
        )
        shutil.copytree(
            _REPO_ROOT / "services" / "graph-memory" / "ONTOLOGIES",
            app_root / "ONTOLOGIES",
        )
        for filename in ("VERSION", "requirements.txt", "requirements.lock"):
            shutil.copy2(
                _REPO_ROOT / "services" / "graph-memory" / filename,
                app_root / filename,
            )
        program = textwrap.dedent(
            """
            import json
            from pathlib import Path

            import hivemind_inference.asgi_lifespan as shared
            import src.mcp_memory.server as graph

            app = graph._create_app()
            payload = {
                "guard_module": str(Path(shared.__file__).resolve()),
                "graph_module": str(Path(graph.__file__).resolve()),
                "same_guard": isinstance(app, shared.LifespanGuard),
            }
            print(json.dumps(payload))
            if not payload["same_guard"]:
                raise SystemExit(17)
            """
        )
        environment = os.environ.copy()
        environment.update(_GRAPH_ENVIRONMENT)
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(app_root),
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=app_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout.splitlines()[-1])
        assert payload == {
            "guard_module": str(
                (app_root / "hivemind_inference" / "asgi_lifespan.py").resolve()
            ),
            "graph_module": str(
                (app_root / "src" / "mcp_memory" / "server.py").resolve()
            ),
            "same_guard": True,
        }

    async def test_module_and_server_singleton_aliases_close_once_by_identity(
        self,
        monkeypatch,
    ):
        _set_graph_memory_environment(monkeypatch)
        import mcp_memory.core.embedder as embedder_module
        import mcp_memory.core.extractor as extractor_module
        import mcp_memory.server as server

        events = []

        class Service:
            def __init__(self, name):
                self.name = name
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1
                events.append(self.name)

        extractor = Service("extractor")
        embedder = Service("embedder")
        monkeypatch.setattr(extractor_module, "_extractor_service", extractor)
        monkeypatch.setattr(embedder_module, "_embedding_service", embedder)
        monkeypatch.setattr(server, "_extractor_service", extractor)
        monkeypatch.setattr(server, "_embedding_service", embedder)

        await server._close_llm_singletons()
        await server._close_llm_singletons()

        assert events == ["extractor", "embedder"]
        assert extractor.close_calls == 1
        assert embedder.close_calls == 1
        assert extractor_module._extractor_service is None
        assert embedder_module._embedding_service is None
        assert server._extractor_service is None
        assert server._embedding_service is None

    async def test_equal_but_distinct_singletons_each_close_once(self, monkeypatch):
        _set_graph_memory_environment(monkeypatch)
        import mcp_memory.core.embedder as embedder_module
        import mcp_memory.core.extractor as extractor_module
        import mcp_memory.server as server

        events = []

        class EqualService:
            def __init__(self, name):
                self.name = name
                self.close_calls = 0

            def __eq__(self, other):
                return isinstance(other, EqualService)

            async def close(self):
                self.close_calls += 1
                events.append(self.name)

        module_service = EqualService("module")
        local_service = EqualService("local")
        monkeypatch.setattr(
            extractor_module,
            "_extractor_service",
            module_service,
        )
        monkeypatch.setattr(embedder_module, "_embedding_service", None)
        monkeypatch.setattr(server, "_extractor_service", local_service)
        monkeypatch.setattr(server, "_embedding_service", None)

        await server._close_llm_singletons()

        assert events == ["module", "local"]
        assert module_service.close_calls == 1
        assert local_service.close_calls == 1

    @pytest.mark.parametrize(
        ("first_outcome", "failure_type"),
        [
            ("error", RuntimeError),
            ("cancel", asyncio.CancelledError),
        ],
    )
    async def test_distinct_singleton_registries_all_close_once_after_failure(
        self,
        monkeypatch,
        first_outcome,
        failure_type,
    ):
        _set_graph_memory_environment(monkeypatch)
        import mcp_memory.core.embedder as embedder_module
        import mcp_memory.core.extractor as extractor_module
        import mcp_memory.server as server

        events = []

        class Service:
            def __init__(self, name, outcome="ok"):
                self.name = name
                self.outcome = outcome
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1
                events.append(self.name)
                if self.outcome == "error":
                    raise RuntimeError("first close failed")
                if self.outcome == "cancel":
                    raise asyncio.CancelledError()

        services = (
            Service("module-extractor", first_outcome),
            Service("module-embedder"),
            Service("local-extractor"),
            Service("local-embedder"),
        )
        monkeypatch.setattr(
            extractor_module,
            "_extractor_service",
            services[0],
        )
        monkeypatch.setattr(
            embedder_module,
            "_embedding_service",
            services[1],
        )
        monkeypatch.setattr(server, "_extractor_service", services[2])
        monkeypatch.setattr(server, "_embedding_service", services[3])

        with pytest.raises(failure_type):
            await server._close_llm_singletons()
        assert events == [
            "module-extractor",
            "module-embedder",
            "local-extractor",
            "local-embedder",
        ]
        assert [service.close_calls for service in services] == [1, 1, 1, 1]
        assert extractor_module._extractor_service is None
        assert embedder_module._embedding_service is None
        assert server._extractor_service is None
        assert server._embedding_service is None

    async def test_real_graph_stack_reports_cleanup_failure(
        self,
        monkeypatch,
        capsys,
        request,
    ):
        _set_graph_memory_environment(monkeypatch)
        import mcp_memory.server as server

        # The test deliberately drives a terminal shutdown failure, which
        # correctly retains process ownership.  Register restoration here as
        # well as in the session-wide fixture because this may be the first
        # test that lazily imports ``mcp_memory.server`` after that fixture took
        # its startup snapshot.
        process_window_snapshot = server._process_window.snapshot_for_tests()
        request.addfinalizer(
            lambda: server._process_window.restore_for_tests(
                process_window_snapshot
            )
        )

        async def failed_close():
            raise RuntimeError("proxy://user:secret@example.invalid")

        monkeypatch.setattr(server, "_close_llm_singletons", failed_close)
        # This test owns the shutdown-failure contract, not the independent
        # Neo4j schema-startup contract.  The top-level/public test environment
        # intentionally omits Graph Memory's service-only dependencies.
        monkeypatch.setattr(
            server,
            "_initialize_graph_document_schema",
            lambda: None,
        )
        monkeypatch.setattr(server.mcp, "streamable_http_app", _inner_app)
        app = server._create_app()

        assert isinstance(app, LifespanGuard)
        assert app._redact is server.redact_proxy_secrets
        assert app._report is server._report_egress_lifespan

        process = _lifespan(app)
        await asyncio.wait_for(process.startup(), timeout=2.0)
        await asyncio.wait_for(process.shutdown(), timeout=2.0)
        assert process.shutdown_failed
        assert process.should_exit
        shutdown = [
            message
            for message in process.sent
            if message["type"].startswith("lifespan.shutdown.")
        ]
        assert [message["type"] for message in shutdown] == [
            "lifespan.shutdown.failed"
        ]
        captured = capsys.readouterr()
        assert "⚠️ [Egress]" in captured.err

    def test_graph_shutdown_failure_keeps_uvicorn_process_exit_zero(self):
        program = textwrap.dedent(
            """
            import asyncio
            import json
            import mcp_memory.server as graph
            import uvicorn

            closed = 0

            async def failed_close():
                global closed
                closed += 1
                raise RuntimeError("close failed")

            async def initialize_schema():
                return None

            async def inner(scope, receive, send):
                while True:
                    message = await receive()
                    phase = message["type"].rsplit(".", 1)[-1]
                    await send({"type": f"lifespan.{phase}.complete"})
                    if phase == "shutdown":
                        return

            async def main():
                graph._close_llm_singletons = failed_close
                graph._initialize_graph_document_schema = initialize_schema
                graph.mcp.streamable_http_app = lambda: inner
                app = graph._create_app()
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host="127.0.0.1",
                        port=0,
                        lifespan="on",
                        log_config=None,
                        access_log=False,
                    )
                )

                async def request_stop():
                    for _ in range(1000):
                        if server.started:
                            server.should_exit = True
                            return
                        await asyncio.sleep(0.001)
                    raise RuntimeError("server did not start")

                stopper = asyncio.create_task(request_stop())
                await asyncio.wait_for(server.serve(), timeout=10.0)
                await stopper
                payload = {"closed": closed, "started": server.started}
                print(json.dumps(payload))
                if payload != {"closed": 1, "started": True}:
                    raise SystemExit(9)

            asyncio.run(main())
            """
        )
        environment = os.environ.copy()
        environment.update(_GRAPH_ENVIRONMENT)
        environment["PYTHONPATH"] = os.pathsep.join(
            [
                str(_REPO_ROOT / "src"),
                str(_REPO_ROOT / "services/graph-memory/src"),
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=_REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout.splitlines()[-1]) == {
            "closed": 1,
            "started": True,
        }


class TestOperatorLifecycleContract:
    def test_production_docs_name_fail_closed_modes_and_recovery(self):
        deployment = (
            _REPO_ROOT / "docs" / "DEPLOYMENT.md"
        ).read_text(encoding="utf-8")
        lifecycle_start = deployment.index("### ASGI process lifecycle")
        lifecycle_end = deployment.index("\n## WAF route table", lifecycle_start)
        lifecycle = " ".join(
            deployment[lifecycle_start:lifecycle_end].split()
        )
        mesh_start = deployment.index("\n## Project Mesh deployment")

        assert lifecycle_start < lifecycle_end < mesh_start
        for required in (
            "`uvicorn --lifespan off`",
            "refuses every route",
            "MCP, health, and metrics",
            "`lifespan.shutdown.failed`",
            "status zero",
            "may remain listening",
            "`restart: unless-stopped`",
            "restart/recreate",
            "health-aware external supervisor",
        ):
            assert required in lifecycle

        security = (
            _REPO_ROOT / "docs" / "SECURITY.md"
        ).read_text(encoding="utf-8")
        section_start = security.index(
            "### 3.12 Process lifecycle failures are fail-closed, "
            "not self-healing"
        )
        section_end = security.index("\n---", section_start)
        lifecycle_security = " ".join(
            security[section_start:section_end].split()
        )
        assert "known, documented availability residual" in lifecycle_security
        assert "requires explicit deployment acceptance" in lifecycle_security
        assert "pending the explicit human merge decision" not in lifecycle_security
        assert "availability loss is accepted" not in lifecycle_security

    def test_release_notes_name_operator_visible_lifecycle_change(self):
        changelogs = [_REPO_ROOT / "CHANGELOG.md"]
        private_overlay = (
            _REPO_ROOT / "release" / "public-overlay" / "CHANGELOG.md"
        )
        if private_overlay.exists():
            changelogs.append(private_overlay)

        for changelog_path in changelogs:
            changelog = changelog_path.read_text(encoding="utf-8")
            lifecycle_heading = "## [1.4.0] — 2026-08-07"
            section_start = changelog.index(lifecycle_heading)
            section_end = changelog.index("\n## [", section_start + 1)
            current = " ".join(
                changelog[section_start:section_end].split()
            )
            for required in (
                "Process-scoped ASGI lifecycle is now explicit and fail-closed",
                "`lifespan.shutdown.failed`",
                "refuse every request",
                "may remain listening",
                "health-aware supervisor",
            ):
                assert required in current
