# -*- coding: utf-8 -*-
"""Deterministic in-memory storage fake shared by Graph/Long test suites.

This deliberately small stand-in implements only the common Graph bridge
surface. Scenario-specific operations (for example, deletion, copy semantics,
or controlled failure injection) stay in the suite that needs them.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


class GraphLongFakeStorage:
    """Minimal in-memory ``StorageService`` stand-in. No S3, deterministic."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.objects[key] = content

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str) -> str | None:
        return self.objects.get(key)

    async def get_json(self, key: str) -> dict | None:
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        # The original Graph/Long suite fakes deliberately ignored ``max_keys``.
        # The watermark suite overrides this method because it pins bounded-list
        # behavior; keeping that distinction preserves each suite's contract.
        objects: list[dict] = []
        for key in sorted(self.objects):
            if key.startswith(prefix):
                objects.append(
                    {"Key": key, "Size": len(self.objects[key]), "LastModified": ""}
                )
        return objects

    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        results: list[dict] = []
        for obj in await self.list_objects(prefix):
            key = obj["Key"]
            if exclude_keep and key.endswith(".keep"):
                continue
            content = self.objects.get(key)
            if content is not None:
                results.append(
                    {
                        "key": key,
                        "content": content,
                        "size": obj["Size"],
                        "last_modified": "",
                    }
                )
        return results

    def snapshot(self) -> dict[str, str]:
        return deepcopy(self.objects)
