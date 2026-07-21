# -*- coding: utf-8 -*-
"""P7-3 — validation config du long embarqué (fail-closed sur sentinel/URL)."""

from __future__ import annotations

import pytest

from live_mem.config import Settings
from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL


def test_defaults_valid() -> None:
    s = Settings(long_embedded_url="http://graph-memory:8002", long_embedded_token="")
    assert s.long_embedded_url == "http://graph-memory:8002"
    assert s.long_embedded_token == ""


def test_rejects_token_equal_sentinel() -> None:
    with pytest.raises(ValueError):
        Settings(long_embedded_token=EMBEDDED_TOKEN_SENTINEL)


def test_rejects_non_http_url() -> None:
    with pytest.raises(ValueError):
        Settings(long_embedded_url="ftp://graph-memory:8002")
