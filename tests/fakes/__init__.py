# -*- coding: utf-8 -*-
"""Deterministic, offline test fakes shared across the P4 engine and P13
inference suites.

These fakes substitute real transports/clients so the whole live-mem → Graph
Memory chain (P4-4 and downstream P4-5 / P4-7 / P4-8 / P4-9) and the P13
provider-neutral inference boundary (#275) are testable with NO network / S3 /
Neo4j / Qdrant / LLM.
"""

from .fake_graph_transport import FakeGraphTransport, RecordedCall
from .fake_storage import GraphLongFakeStorage
from .inference_emulator import InferenceEmulator

__all__ = [
    "FakeGraphTransport",
    "GraphLongFakeStorage",
    "InferenceEmulator",
    "RecordedCall",
]
