# -*- coding: utf-8 -*-
"""Pure parsing helpers for the live-note Markdown envelope.

The opening and closing YAML markers are delimiters only when they occupy a
complete physical line.  Splitting on the substring ``---`` corrupts valid
JSON-quoted metadata values such as an agent identity containing three hyphens.
This module is dependency-free within ``core`` so live reads, consolidation,
and Hivemind replication can share one boundary parser without import cycles.
"""

from __future__ import annotations

import json
import re


_LIVE_FRONT_MATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<front>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    flags=re.DOTALL,
)


def split_live_note_front_matter(raw_content: object) -> tuple[str, str] | None:
    """Return ``(front_matter, body)`` for a valid live-note envelope.

    ``None`` means the input is not a string, has no opening marker, or has no
    full-line closing marker.  Both returned sections follow the historical
    parser contract and are stripped of surrounding whitespace.
    """
    if not isinstance(raw_content, str) or not raw_content.startswith("---"):
        return None
    match = _LIVE_FRONT_MATTER_RE.match(raw_content)
    if match is None:
        return None
    return match.group("front").strip(), raw_content[match.end() :].strip()


def decode_live_note_string(raw_value: str) -> str:
    """Decode the JSON-compatible string scalars emitted by ``live_note``.

    Early notes also used simple unquoted or single-quoted scalar values, so
    those retain the historical tolerant stripping behavior.
    """
    value = raw_value.strip()
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                return decoded
        except json.JSONDecodeError:
            pass
        return value.strip('"')
    return value.strip("'")
