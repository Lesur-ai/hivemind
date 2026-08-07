# -*- coding: utf-8 -*-
"""P13-1A (#274) — owned outbound transport and secret redaction (ADR-0027,
P12-3 egress parity).

Proven here: the owned httpx client never honors ambient proxy environment
(``trust_env=False``) on either the proxied or the direct path, an explicit
``PROXY_URL`` injects an owned proxy transport, and free-form text / proxy-URL
rendering strip credentials, query, and fragment. No network is performed.
"""

from __future__ import annotations

import pytest

from hivemind_inference.egress import (
    build_owned_async_http_client,
    display_proxy_url,
    redact_proxy_secrets,
)


# --------------------------------------------------------------------------- #
# redact_proxy_secrets                                                        #
# --------------------------------------------------------------------------- #


class TestRedactProxySecrets:
    def test_fully_redacts_authority_userinfo_query_fragment(self):
        # ADR-0027: provider/proxy endpoints (host AND port) are sensitive even
        # without credentials, so the WHOLE URL collapses to scheme://<redacted>.
        text = "connect to https://user:pass@host.example.com:8443/v1?token=abc#frag failed"
        redacted = redact_proxy_secrets(text)
        assert "https://<redacted>" in redacted
        assert "host.example.com" not in redacted
        assert "8443" not in redacted
        assert "user:pass" not in redacted
        assert "token=abc" not in redacted
        assert "#frag" not in redacted

    def test_password_containing_at_sign_is_fully_stripped(self):
        redacted = redact_proxy_secrets("see https://u:p@ss@host.example.com/x now")
        assert "p@ss" not in redacted
        assert "host.example.com" not in redacted
        assert redacted == "see https://<redacted> now"

    def test_multiple_urls_all_redacted(self):
        text = "a https://x:y@h1.com/p?q=1 b http://m:n@h2.com/#z c"
        redacted = redact_proxy_secrets(text)
        assert "x:y" not in redacted and "m:n" not in redacted
        assert "h1.com" not in redacted and "h2.com" not in redacted
        assert "https://<redacted>" in redacted and "http://<redacted>" in redacted

    def test_idempotent(self):
        text = "https://user:pass@host.example.com/v1?token=abc#frag"
        once = redact_proxy_secrets(text)
        assert once == "https://<redacted>"
        assert redact_proxy_secrets(once) == once

    def test_noop_on_text_without_urls(self):
        assert redact_proxy_secrets("nothing to see here") == "nothing to see here"

    def test_bare_endpoint_without_credentials_is_still_redacted(self):
        # Even a credential-free proxy/provider endpoint is sensitive (host/port).
        assert redact_proxy_secrets("https://host.example.com/v1") == "https://<redacted>"
        assert (
            redact_proxy_secrets("proxy https://proxy.local:8080 down")
            == "proxy https://<redacted> down"
        )

    @pytest.mark.parametrize(
        "text, secret",
        [
            # A quote inside userinfo/path must NOT terminate the URL match and
            # leave the host/port/suffix exposed (the URL regex no longer treats
            # ' or " as a delimiter).
            ("err https://user:pa'ss@secret-host.example:8443/v1 end", "secret-host.example"),
            ('err https://user:pa"ss@secret-host.example:8443/v1 end', "secret-host.example"),
            ("path https://secret-host.example/a'b/c'd end", "secret-host.example"),
            ("user https://SECRET'USER:pw@h.example/v1 end", "SECRET'USER"),
        ],
    )
    def test_quotes_inside_url_do_not_bypass_redaction(self, text, secret):
        redacted = redact_proxy_secrets(text)
        assert secret not in redacted
        assert "8443" not in redacted
        assert "<redacted>" in redacted

    @pytest.mark.parametrize(
        "text",
        [
            "HTTPS://user:CREDENTIAL@proxy.example/p?token=T0KEN#FRAG",
            "HtTp://u:CREDENTIAL@proxy.example/x?token=T0KEN#FRAG",
        ],
    )
    def test_mixed_case_scheme_urls_are_redacted(self, text):
        # URI schemes are case-insensitive; an uppercase scheme must still redact
        # and the surviving placeholder scheme is normalized to lowercase.
        redacted = redact_proxy_secrets(text)
        assert "CREDENTIAL" not in redacted
        assert "token=T0KEN" not in redacted
        assert "FRAG" not in redacted
        assert "proxy.example" not in redacted
        assert "<redacted>" in redacted


# --------------------------------------------------------------------------- #
# display_proxy_url                                                           #
# --------------------------------------------------------------------------- #


class TestDisplayProxyUrl:
    def test_authority_is_fully_redacted_scheme_preserved(self):
        # ADR-0027: the proxy endpoint (host/port) is sensitive even without
        # credentials, so only the scheme survives.
        rendered = display_proxy_url("http://user:pass@proxy.local:8080/path?q=1")
        assert rendered == "http://<redacted>"
        assert "proxy.local" not in rendered
        assert "8080" not in rendered

    def test_no_port_still_redacts_host(self):
        rendered = display_proxy_url("https://proxy.internal.example")
        assert rendered == "https://<redacted>"
        assert "proxy.internal.example" not in rendered

    def test_credentials_and_host_never_rendered(self):
        rendered = display_proxy_url("http://secretuser:secretpass@secret-proxy.local:3128")
        assert "secretuser" not in rendered
        assert "secretpass" not in rendered
        assert "secret-proxy.local" not in rendered
        assert "3128" not in rendered
        assert rendered == "http://<redacted>"

    def test_malformed_port_is_redacted_not_leaked(self):
        # urlsplit's lazy .port raises ValueError embedding the configured
        # value; the log-safe helper must swallow it and never echo it.
        rendered = display_proxy_url("http://proxy.local:configured-secret")
        assert "configured-secret" not in rendered
        assert rendered == "http://<redacted>"

    def test_malformed_ipv6_authority_is_redacted(self):
        rendered = display_proxy_url("http://[bad::v6::secret]:99999/x")
        assert "secret" not in rendered
        assert "<redacted>" in rendered

    def test_no_scheme_malformed_input_is_fully_redacted(self):
        rendered = display_proxy_url("://:configured-secret")
        assert "configured-secret" not in rendered

    @pytest.mark.parametrize(
        "proxy_url",
        [
            "credential-token://proxy.local:8080",
            "weird-scheme://secret@host",
            "ftp://proxy.local",
        ],
    )
    def test_non_http_scheme_is_fully_redacted(self, proxy_url):
        # Only http/https survive; a data-bearing or unexpected scheme is fully
        # redacted (no scheme echoed) so it cannot carry information out.
        rendered = display_proxy_url(proxy_url)
        assert rendered == "<redacted>"

    @pytest.mark.parametrize("proxy_url", ["HTTP://Proxy.Local:8080", "HTTPS://h"])
    def test_scheme_is_case_normalized(self, proxy_url):
        rendered = display_proxy_url(proxy_url)
        assert rendered in ("http://<redacted>", "https://<redacted>")


# --------------------------------------------------------------------------- #
# build_owned_async_http_client                                              #
# --------------------------------------------------------------------------- #


class TestOwnedHttpClient:
    async def test_direct_client_never_trusts_env(self):
        client = build_owned_async_http_client(None, timeout=5.0)
        try:
            assert client.trust_env is False
            assert client._mounts == {}  # no proxy mount on the direct path
        finally:
            await client.aclose()

    async def test_proxied_client_never_trusts_env(self):
        client = build_owned_async_http_client("http://proxy.local:8080", timeout=5.0)
        try:
            assert client.trust_env is False
        finally:
            await client.aclose()

    async def test_proxy_url_injects_owned_proxy_transport(self):
        client = build_owned_async_http_client("http://proxy.local:8080", timeout=5.0)
        try:
            assert client._mounts, "an explicit PROXY_URL must inject a proxy transport"
            proxy_urls = []
            for transport in client._mounts.values():
                pool = getattr(transport, "_pool", None)
                proxy_url = getattr(pool, "_proxy_url", None)
                if proxy_url is not None:
                    proxy_urls.append((proxy_url.host, proxy_url.port))
            assert (b"proxy.local", 8080) in proxy_urls
        finally:
            await client.aclose()

    async def test_empty_proxy_url_is_treated_as_direct(self):
        # ADR: absent PROXY_URL keeps the historical direct transport. An empty
        # string is falsy and must not create a broken proxy mount.
        client = build_owned_async_http_client("", timeout=5.0)
        try:
            assert client.trust_env is False
            assert client._mounts == {}
        finally:
            await client.aclose()
