"""Small real tar.gz fixtures for Graph restore resource budgets (#543)."""
import gzip
import io
import tarfile

import pytest

from tests.test_p13_qdrant_backup_identity import (
    BACKUP_ID, DOCUMENT_KEYS, MEMORY_ID, _artifact_set, _service, backup_module,
)


def archive_bytes(extra=(), *, long_name=False, global_pax=False):
    entries = list(_artifact_set().items()) + [("documents/document.txt", b"document")]
    if long_name:
        entries.append(("documents/" + "é" * 110 + ".txt", b"long filename"))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": "global"} if global_pax else None) as archive:
        for name, content in entries + list(extra):
            info = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo("backup/" + name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


async def invoke(module, content, route):
    service, operations, _ = _service(module)
    async def forbidden_lookup(*args, **kwargs):
        pytest.fail("archive budget failure reached graph lookup")
    service._graph.get_memory = forbidden_lookup
    try:
        if route == "namespace":
            return module.BackupService._archive_memory_id(content)
        if route == "service":
            return await service.restore_from_archive(content)
        return await service._restore_from_archive_unlocked(content)
    finally:
        # Even the vector preflight must not run on a budget failure.
        assert operations == []


@pytest.mark.parametrize("route", ["namespace", "service", "full"])
@pytest.mark.parametrize("limit", ["compressed", "inflated", "member", "count", "manifest", "json"])
async def test_archive_budgets_reject_before_backends(backup_module, monkeypatch, route, limit):
    content = archive_bytes()
    if limit == "compressed":
        name, value = "MAX_ARCHIVE_SIZE_BYTES", len(content) - 1
    elif limit == "inflated":
        name, value = "MAX_ARCHIVE_EXPANDED_SIZE_BYTES", len(gzip.decompress(content)) - 1
    elif limit == "member":
        content = archive_bytes([("documents/late.txt", b"x" * 3000)])
        name, value = "MAX_ARCHIVE_MEMBER_SIZE_BYTES", 2999
    elif limit == "count":
        name, value = "MAX_ARCHIVE_MEMBERS", 4
    elif limit == "manifest":
        name, value = "MAX_ARCHIVE_MANIFEST_SIZE_BYTES", len(_artifact_set()["manifest.json"]) - 1
    elif limit == "json":
        name, value = "MAX_ARCHIVE_JSON_SIZE_BYTES", len(_artifact_set()["graph_data.json"]) - 1
    monkeypatch.setattr(backup_module, name, value, raising=False)
    with pytest.raises(ValueError, match="limit|too large"):
        await invoke(backup_module, content, route)


@pytest.mark.parametrize("route", ["namespace", "full"])
async def test_pax_effective_member_size_is_checked(backup_module, monkeypatch, route):
    info = tarfile.TarInfo("backup/pax-sized.bin")
    info.pax_headers = {"size": "4000"}
    content = archive_bytes([(info, b"x")])
    monkeypatch.setattr(backup_module, "MAX_ARCHIVE_MEMBER_SIZE_BYTES", 3999, raising=False)
    with pytest.raises(ValueError, match="limit"):
        await invoke(backup_module, content, route)


@pytest.mark.parametrize("encoding", ["gnu", "pax00", "pax01", "pax10"])
async def test_sparse_rejected_before_internal_map_parsing(backup_module, monkeypatch, encoding):
    info = tarfile.TarInfo("backup/sparse.bin")
    if encoding == "gnu":
        info.type = tarfile.GNUTYPE_SPARSE
    else:
        info.pax_headers = {
            "pax00": {"GNU.sparse.size": "1"},
            "pax01": {"GNU.sparse.map": "0,1", "GNU.sparse.size": "1"},
            "pax10": {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"},
        }[encoding]
    content = archive_bytes([(info, b"x")])
    def forbidden(*args, **kwargs):
        pytest.fail("sparse input reached stdlib map materialization")
    for method in ("_proc_sparse", "_proc_gnusparse_00", "_proc_gnusparse_01", "_proc_gnusparse_10"):
        monkeypatch.setattr(tarfile.TarInfo, method, forbidden)
    with pytest.raises(ValueError, match="Unsupported.*sparse"):
        await invoke(backup_module, content, "namespace")


@pytest.mark.parametrize("limit,value", [("MAX_ARCHIVE_METADATA_SIZE_BYTES", 32), ("MAX_ARCHIVE_MEMBERS", 6)])
async def test_pax_header_is_bounded_before_stdlib_processing(backup_module, monkeypatch, limit, value):
    content = archive_bytes(long_name=True)  # six files plus one local PAX header
    monkeypatch.setattr(backup_module, limit, value, raising=False)
    with pytest.raises(ValueError, match="limit"):
        await invoke(backup_module, content, "namespace")


@pytest.mark.parametrize("case", ["duplicate", "symlink", "hardlink", "global_pax"])
async def test_archive_aliases_and_global_metadata_cannot_amplify_budget(backup_module, case):
    extra = []
    if case == "duplicate":
        extra = [("manifest.json", _artifact_set()["manifest.json"])]
    elif case in {"symlink", "hardlink"}:
        info = tarfile.TarInfo("backup/alias")
        info.type = tarfile.SYMTYPE if case == "symlink" else tarfile.LNKTYPE
        info.linkname = "backup/manifest.json"
        extra = [(info, b"")]
    content = archive_bytes(extra, global_pax=case == "global_pax")
    with pytest.raises(ValueError, match="Duplicate|Unsupported"):
        await invoke(backup_module, content, "namespace")


async def test_bounded_manifest_refuses_before_json_materialization(backup_module, monkeypatch):
    content = archive_bytes()
    monkeypatch.setattr(backup_module, "MAX_ARCHIVE_MANIFEST_SIZE_BYTES", 32, raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail("manifest reached JSON parser before its size budget")
    monkeypatch.setattr(backup_module.json, "loads", forbidden)
    with pytest.raises(ValueError, match="limit"):
        await invoke(backup_module, content, "namespace")


async def test_concatenated_gzip_and_trailing_expansion_are_counted(backup_module, monkeypatch):
    raw = gzip.decompress(archive_bytes())
    content = gzip.compress(raw) + gzip.compress(b"\0" * 1024)
    monkeypatch.setattr(backup_module, "MAX_ARCHIVE_EXPANDED_SIZE_BYTES", len(raw) + 1023, raising=False)
    with pytest.raises(ValueError, match="limit"):
        await invoke(backup_module, content, "namespace")


@pytest.mark.parametrize("layout", ["full", "light", "pax", "concatenated"])
async def test_legitimate_archives_keep_restore_behavior(backup_module, layout):
    service, operations, _ = _service(backup_module)
    if layout == "light":
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, content in _artifact_set().items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        content = output.getvalue()
    else:
        content = archive_bytes(long_name=layout == "pax")
        if layout == "concatenated":
            raw = gzip.decompress(content)
            content = gzip.compress(raw[:6000]) + gzip.compress(raw[6000:])
    assert backup_module.BackupService._archive_memory_id(content) == MEMORY_ID
    actual = await service.restore_from_archive(content)
    assert actual["status"] == "ok"
    assert actual["s3_documents_uploaded"] == {"light": 0, "pax": 2}.get(layout, 1)
    assert operations[0] == "vector.preflight"
    assert operations[-2:] == ["graph.import", "vector.import"]


@pytest.mark.parametrize("include_documents", [False, True])
@pytest.mark.parametrize("filename", [
    "document.txt", "é" * 110 + ".txt", "manifest.json", "graph_data.json",
    "document_keys.json", "qdrant_vectors.jsonl",
])
async def test_native_download_round_trips(backup_module, monkeypatch, include_documents, filename):
    # Documents with reserved basenames are data, not archive control files.
    monkeypatch.setattr(backup_module, "MAX_ARCHIVE_MANIFEST_SIZE_BYTES", 2048)
    monkeypatch.setattr(backup_module, "MAX_ARCHIVE_JSON_SIZE_BYTES", 1024)
    keys = [{**DOCUMENT_KEYS[0], "filename": filename}]
    exporter, _, objects = _service(
        backup_module, artifacts=_artifact_set(document_keys=keys),
    )
    objects[keys[0]["key"]] = b"x" * 4096
    content = await exporter.download_backup(BACKUP_ID, include_documents=include_documents)
    assert backup_module.BackupService._archive_memory_id(content) == MEMORY_ID
    restorer, operations, restored = _service(backup_module)
    result = await restorer.restore_from_archive(content)
    assert result["status"] == "ok"
    assert result["s3_documents_uploaded"] == int(include_documents)
    if include_documents:
        assert restored[keys[0]["key"]] == b"x" * 4096
    assert operations[-2:] == ["graph.import", "vector.import"]


async def test_document_cannot_stand_in_for_control_manifest(backup_module):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        content = _artifact_set()["manifest.json"]
        info = tarfile.TarInfo("backup/documents/manifest.json")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    with pytest.raises(ValueError, match="manifest.json.*not.*found"):
        await invoke(backup_module, output.getvalue(), "namespace")
