"""#540: exercise Graph's public static boundary with isolated marker files."""

from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, unquote

import httpx
import pytest


@pytest.fixture
def static_site(tmp_path, monkeypatch):
    # Import the real middleware without requiring any backing service.
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-neo4j-password")
    from mcp_memory.auth.middleware import AuthMiddleware, StaticFilesMiddleware

    root = tmp_path / "static"
    root.mkdir()
    (root / "css").mkdir()
    (root / "css" / "app.css").write_bytes(b"body { color: green; }")
    (root / "graph.html").write_bytes(b"<h1>Graph</h1>")
    (root / "admin.html").write_bytes(b"<h1>Admin</h1>")
    outside = tmp_path / "static-sibling"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"OUTSIDE-STATIC-SYNTHETIC-MARKER")
    (root / "escape.txt").symlink_to(sentinel)
    (root / "escape-dir").symlink_to(outside, target_is_directory=True)
    (root / "inside.css").symlink_to(root / "css" / "app.css")
    (root / "loop").symlink_to(root / "loop")
    fallback = AsyncMock()
    middleware = StaticFilesMiddleware(fallback)
    middleware._static_dir = str(root)
    return middleware, AuthMiddleware(middleware), fallback, root, sentinel


async def _request(app, raw_path, query=b""):
    """Send the decoded ASGI path separately from its original wire spelling."""
    sent = []

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http", "method": "GET", "path": unquote(raw_path),
            "raw_path": raw_path.encode(), "query_string": query,
            "headers": [], "client": ("192.0.2.1", 1234),
        },
        AsyncMock(),
        send,
    )
    return sent


def _response(sent):
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


def _assert_safe_404(sent):
    status, headers, body = _response(sent)
    assert status == 404
    assert body == b"<h1>404 Not Found</h1>"
    assert headers[b"content-length"] == str(len(body)).encode()
    assert headers[b"content-type"].startswith(b"text/html")
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert b"default-src 'none'" in headers[b"content-security-policy"]
    assert b"frame-ancestors 'none'" in headers[b"content-security-policy"]
    assert b"base-uri 'none'" in headers[b"content-security-policy"]
    assert b"form-action 'none'" in headers[b"content-security-policy"]
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert b"camera=()" in headers[b"permissions-policy"]


@pytest.mark.parametrize("attack", [
    "absolute", "root-absolute", "encoded-absolute", "encoded-separators", "traversal", "in-root-traversal",
    "encoded-traversal", "double-encoded-traversal", "backslash", "drive",
    "symlink", "directory-symlink", "empty", "directory", "nul", "loop",
    "missing-html", "encoded-question",
])
async def test_public_static_paths_fail_closed_without_open(static_site, attack):
    middleware, app, fallback, root, sentinel = static_site
    paths = {
        "absolute": "/static/" + str(sentinel),
        "root-absolute": "/static/" + str(root / "css" / "app.css"),
        "encoded-absolute": "/static/" + quote(str(sentinel), safe=""),
        "encoded-separators": "/static/" + str(sentinel).replace("/", "%2f"),
        "traversal": "/static/../static-sibling/sentinel.txt",
        "in-root-traversal": "/static/css/../admin.html",
        "encoded-traversal": "/static/%2e%2e%2fstatic-sibling%2fsentinel.txt",
        "double-encoded-traversal": "/static/%252e%252e%252fstatic-sibling%252fsentinel.txt",
        "backslash": "/static/%5c%5cserver%5cshare%5csentinel.txt",
        "drive": "/static/C:%5csentinel.txt",
        "symlink": "/static/escape.txt",
        "directory-symlink": "/static/escape-dir/sentinel.txt",
        "empty": "/static/",
        "directory": "/static/css",
        "nul": "/static/css/app.css%00",
        "loop": "/static/loop",
        "missing-html": "/static/%3Cscript%3Ealert(1)%3C%2Fscript%3E.js",
        # ASGI query_string is separate: an encoded '?' is a filename byte.
        "encoded-question": "/static/css/app.css%3Fmissing",
    }
    with patch("builtins.open", wraps=open) as opened:
        sent = await _request(app, paths[attack])
    _assert_safe_404(sent)
    opened.assert_not_called()
    fallback.assert_not_awaited()
    assert sentinel.read_bytes() not in _response(sent)[2]


@pytest.mark.parametrize("filename", ["absolute", "relative", "symlink", "directory-symlink"])
async def test_direct_file_sink_rejects_escape(static_site, filename):
    middleware, _, _, _, sentinel = static_site
    requested = {
        "absolute": str(sentinel),
        "relative": "../static-sibling/sentinel.txt",
        "symlink": "escape.txt",
        "directory-symlink": "escape-dir/sentinel.txt",
    }[filename]
    sent = []

    async def send(message):
        sent.append(message)

    with patch("builtins.open", wraps=open) as opened:
        await middleware._serve_file(send, requested, "text/plain")
    _assert_safe_404(sent)
    opened.assert_not_called()


async def test_http_transport_decodes_encoded_absolute_path(static_site):
    _, app, fallback, _, sentinel = static_site
    # Exercise a real ASGI client as well as the raw scope matrix above.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://graph.test") as client:
        response = await client.get("/static/" + quote(str(sentinel), safe=""))
    assert response.status_code == 404
    assert response.content == b"<h1>404 Not Found</h1>"
    fallback.assert_not_awaited()


@pytest.mark.parametrize("path,expected,content_type", [
    ("/static/css/app.css", b"body { color: green; }", b"text/css; charset=utf-8"),
    ("/static/css%2fapp.css", b"body { color: green; }", b"text/css; charset=utf-8"),
    ("/static/inside.css", b"body { color: green; }", b"text/css; charset=utf-8"),
    ("/graph", b"<h1>Graph</h1>", b"text/html"),
    ("/graph/", b"<h1>Graph</h1>", b"text/html"),
    ("/admin", b"<h1>Admin</h1>", b"text/html; charset=utf-8"),
    ("/admin/", b"<h1>Admin</h1>", b"text/html; charset=utf-8"),
])
async def test_legitimate_assets_and_cache_busting_unchanged(static_site, path, expected, content_type):
    _, app, fallback, _, _ = static_site
    status, headers, body = _response(await _request(app, path, query=b"v=123"))
    assert status == 200
    assert body == expected
    assert headers[b"content-type"] == content_type
    assert headers[b"content-length"] == str(len(expected)).encode()
    assert headers[b"cache-control"] == b"no-store, no-cache, must-revalidate, max-age=0"
    assert headers[b"pragma"] == b"no-cache"
    assert headers[b"expires"] == b"0"
    fallback.assert_not_awaited()


async def test_configured_root_may_be_symlink(static_site, tmp_path):
    middleware, app, _, root, _ = static_site
    alias = tmp_path / "root-alias"
    alias.symlink_to(root, target_is_directory=True)
    middleware._static_dir = str(alias)
    assert _response(await _request(app, "/static/css/app.css"))[2] == b"body { color: green; }"
    _assert_safe_404(await _request(app, "/static/escape.txt"))


async def test_file_open_error_is_constant_and_does_not_echo_path(static_site):
    _, app, _, _, _ = static_site
    with patch("builtins.open", side_effect=PermissionError("<script>path-error</script>")):
        _assert_safe_404(await _request(app, "/static/css/app.css"))


@pytest.mark.parametrize("path", ["/graph", "/admin", "/static/css/graph.css", "/static/js/app.js"])
async def test_shipped_graph_assets_are_still_served(static_site, path):
    middleware, app, fallback, _, _ = static_site
    root = Path(__file__).resolve().parents[1] / "services/graph-memory/src/mcp_memory/static"
    middleware._static_dir = str(root)
    filename = {"/graph": "graph.html", "/admin": "admin.html"}.get(path, path.removeprefix("/static/"))
    status, _, body = _response(await _request(app, path))
    assert status == 200
    assert body == (root / filename).read_bytes()
    fallback.assert_not_awaited()
