# -*- coding: utf-8 -*-
"""Deterministic, offline test fakes shared across the P4 engine suites.

These fakes substitute real transports/clients so the whole live-mem → Graph
Memory chain is testable with NO network / S3 / Neo4j / Qdrant / LLM. Reused by
P4-4 (this wave) and the downstream P4-5 / P4-7 / P4-8 / P4-9 suites.
"""

from .fake_graph_transport import FakeGraphTransport, RecordedCall

__all__ = ["FakeGraphTransport", "RecordedCall"]
