"""Bounded S3 inventory must bound both result size and backend pagination."""

from types import SimpleNamespace

import pytest

from live_mem.core.storage import (
    StorageInventoryLimitError,
    StorageInventoryMetadataError,
    StorageService,
    inventory_object_size,
)


def _storage_with_pages(pages):
    service = object.__new__(StorageService)
    service.bucket = "test"
    service._client_meta = SimpleNamespace(list_objects_v2=object())
    calls = []

    async def run(_operation, **params):
        calls.append(params)
        index = len(calls) - 1
        return pages[index] if index < len(pages) else pages[-1]

    service._run = run
    return service, calls


async def test_bounded_prefix_inventory_rejects_empty_truncated_pages() -> None:
    service, calls = _storage_with_pages(
        [
            {
                "IsTruncated": True,
                "KeyCount": 0,
                "NextContinuationToken": "page-2",
            },
            {
                "IsTruncated": True,
                "KeyCount": 0,
                "NextContinuationToken": "page-3",
            },
        ]
    )

    with pytest.raises(StorageInventoryLimitError):
        await service.list_prefixes("", max_prefixes=2)
    assert len(calls) == 2
    assert [call["MaxKeys"] for call in calls] == [2, 1]


async def test_bounded_object_inventory_rejects_nonconverging_empty_pages() -> None:
    service, calls = _storage_with_pages(
        [
            {
                "IsTruncated": True,
                "NextContinuationToken": f"page-{index}",
            }
            for index in range(1, 5)
        ]
    )

    with pytest.raises(StorageInventoryLimitError):
        await service.list_objects("alpha/", max_keys=2)
    # max_keys+1 backend pages is the hard convergence budget.
    assert len(calls) == 3
    assert all(call["MaxKeys"] == 2 for call in calls)


async def test_object_inventory_preserves_missing_size_as_untrusted() -> None:
    service, _calls = _storage_with_pages(
        [{"Contents": [{"Key": "alpha/members.json"}], "IsTruncated": False}]
    )

    objects = await service.list_objects("alpha/", max_keys=1)
    assert objects == [
        {"Key": "alpha/members.json", "Size": None, "LastModified": ""}
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"Size": None},
        {"Size": True},
        {"Size": "1"},
        {"Size": -1},
    ],
)
def test_inventory_size_strict_mode_rejects_missing_or_invalid_metadata(
    entry: dict,
) -> None:
    with pytest.raises(StorageInventoryMetadataError):
        inventory_object_size(entry)


def test_inventory_size_informational_mode_normalizes_only_missing_metadata() -> None:
    assert inventory_object_size({"Size": None}, missing_as_zero=True) == 0
    assert inventory_object_size({"Size": 7}, missing_as_zero=True) == 7
    with pytest.raises(StorageInventoryMetadataError):
        inventory_object_size({"Size": False}, missing_as_zero=True)


async def test_list_and_get_normalizes_missing_size_without_emitting_none() -> None:
    service = object.__new__(StorageService)

    async def list_objects(_prefix: str) -> list[dict]:
        return [{"Key": "alpha/bank/context.md", "Size": None}]

    async def get(_key: str) -> str:
        return "context"

    service.list_objects = list_objects
    service.get = get

    assert await service.list_and_get("alpha/bank/") == [
        {
            "key": "alpha/bank/context.md",
            "content": "context",
            "size": 0,
            "last_modified": "",
        }
    ]
