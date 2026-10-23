"""Real parser and JWT compatibility checks for the locked dependencies."""

import base64
import re
import threading
from io import BytesIO

import jwt
import pytest
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import RequestReceived
from h2.exceptions import ProtocolError
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


# CPython 3.14 derives its C recursion guard from the real stack size, so the
# JSON nesting depth that overflows follows the process stack rlimit (a 64 MiB
# or unlimited runner stack parses 100k levels; 3.14.8 parses 50k on 8 MiB).
# A small fixed thread stack keeps the recursion boundary independent of the
# host: 2 MiB already overflows below 20k levels, a 5x margin for 100k.
_BOUNDED_STACK_BYTES = 2 * 1024 * 1024


def _call_on_bounded_stack(call):
    outcome = {}

    def target():
        try:
            call()
        except BaseException as exc:  # re-raised on the test thread below
            outcome["error"] = exc

    previous = threading.stack_size(_BOUNDED_STACK_BYTES)
    try:
        worker = threading.Thread(target=target)
        worker.start()
    finally:
        threading.stack_size(previous)
    worker.join()
    if "error" in outcome:
        raise outcome["error"]


@pytest.mark.parametrize("path", ["decode", "jwks"])
def test_jwt_recursive_payload_keeps_documented_error_boundary(path, monkeypatch):
    """CVE-2026-101918: an unsigned payload must not leak RecursionError."""
    def b64url(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    token = b".".join((
        b64url(b'{"alg":"HS256","typ":"JWT"}'),
        # 100k levels overflow the bounded 2 MiB thread stack on every host.
        b64url(b"[" * 100_000 + b"]" * 100_000),
        b64url(b"forged-signature"),
    )).decode()
    client = jwt.PyJWKClient("https://jwks.example.invalid")

    def unexpected_network(*args, **kwargs):
        pytest.fail("recursive payload must be rejected before JWKS access")

    def decode():
        if path == "jwks":
            client.get_signing_key_from_jwt(token)
        else:
            jwt.decode(token, options={"verify_signature": False})

    monkeypatch.setattr(client, "fetch_data", unexpected_network)
    with pytest.raises(jwt.DecodeError) as excinfo:
        _call_on_bounded_stack(decode)
    assert isinstance(excinfo.value.__cause__, RecursionError)


def test_jwt_normal_and_padded_signatures_keep_verification_contract():
    """PyJWT 2.15.1 restores library padding compatibility and verification."""
    key = "test-jwt-compatibility-key-32-bytes"
    payload = {"sub": "dependency-test"}
    token = jwt.encode(payload, key, algorithm="HS256")
    for candidate in (token, token + "="):
        assert jwt.decode(candidate, key, algorithms=["HS256"]) == payload
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(candidate, "different-test-jwt-key-32-bytes!!", algorithms=["HS256"])
    assert jwt.decode(token, options={"verify_signature": False}) == payload


@pytest.fixture
def extract_text(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-document-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-document-secret")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-document-password")
    from mcp_memory.server import _extract_text

    return _extract_text


def _pdf(cmap_line=None):
    """Create a small in-memory PDF; no network or external fixture needed."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    if cmap_line is not None:
        mapping = DecodedStreamObject()
        mapping.set_data(b"1 beginbfrange\n" + cmap_line + b"\nendbfrange\n")
        font[NameObject("/ToUnicode")] = mapping
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 20 100 Td (A) Tj ET")
    page[NameObject("/Contents")] = content
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("filename", ["notes.pdf", "NOTES.PDF"])
def test_document_pdf_text_extraction_keeps_normal_content(extract_text, filename):
    assert extract_text(_pdf(), filename).strip() == "A"


def test_document_pdf_normal_unicode_mapping_is_preserved(extract_text):
    assert extract_text(_pdf(b"<41> <41> <0042>"), "notes.pdf").strip() == "B"


@pytest.mark.parametrize("cmap_line", [
    b"<000000000000000041> <000000000000000041> <0041>",
    b"<41> <41> [<" + b"0041" * 257 + b">]",
], ids=["oversized-code", "oversized-string"])
def test_document_pdf_oversized_unicode_token_is_rejected_by_parser_bound(
    extract_text, cmap_line, capsys,
):
    document = _pdf(cmap_line)
    assert len(document) < 4096
    assert extract_text(document, "notes.pdf") is None
    # Intentionally coupled to the pinned pypdf diagnostic: a generic failure
    # could pass without exercising the new /ToUnicode bound. Recheck this
    # refusal path, not just the wording, when upgrading the parser.
    assert "Maximum /ToUnicode" in capsys.readouterr().err


@pytest.mark.parametrize("content", [b"", b"not a PDF"])
def test_document_pdf_malformed_input_keeps_failure_contract(extract_text, content):
    assert extract_text(content, "notes.pdf") is None


class _FullReadCountingStream:
    """Count full-file scans without supplying BytesIO's getbuffer shortcut."""

    def __init__(self, content):
        self.stream = BytesIO(content)
        self.full_reads = 0

    def read(self, size=-1):
        if size == -1:
            self.full_reads += 1
        return self.stream.read(size)

    def seek(self, *args):
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()


def test_document_pdf_repeated_missing_object_scans_only_once():
    """CVE-2026-115572: cached missing objects must not rescan the file."""
    stream = _FullReadCountingStream(_pdf())
    reader = PdfReader(stream)
    stream.full_reads = 0
    for _ in range(3):
        assert reader.get_object(9999) is None
    assert stream.full_reads == 1


def test_document_pdf_xref_count_is_bounded_by_input_size(extract_text, capsys):
    """CVE-2026-115580: a tiny PDF cannot declare a disproportionate xref."""
    document = _pdf()
    # Leave room for the longer replacement header itself in the byte limit.
    declared = len(document) // 20 + 2
    malicious, replacements = re.subn(
        rb"xref\n0 \d+\n", f"xref\n0 {declared}\n".encode(), document, count=1,
    )
    assert replacements == 1 and len(malicious) < 4096
    assert extract_text(malicious, "notes.pdf") is None
    assert f"Declared size {declared} exceeds allowed limit" in capsys.readouterr().err


def test_document_pdf_malformed_xref_entries_share_one_recovery_scan():
    """CVE-2026-115585: each invalid entry must reuse the recovery scan."""
    body = bytearray(b"%PDF-1.7\n")
    body += b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    body += b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    xref_offset = len(body)
    body += b"xref\n0 8\n" + b"x" * 20 * 8
    body += b"trailer\n<< /Root 1 0 R /Size 3 >>\nstartxref\n"
    body += str(xref_offset).encode() + b"\n%%EOF\n"
    stream = _FullReadCountingStream(bytes(body))
    reader = PdfReader(stream)
    assert stream.full_reads == 1
    assert len(reader.pages) == 0


def _receive_request(hosts):
    """Only the sender validation is disabled to construct invalid wire input."""
    client = H2Connection(config=H2Configuration(
        client_side=True,
        validate_outbound_headers=False,
        normalize_outbound_headers=False,
    ))
    server = H2Connection(config=H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    headers = [
        (":method", "GET"), (":scheme", "https"),
        (":authority", "example.test"), (":path", "/"),
        *(("host", host) for host in hosts),
    ]
    client.send_headers(1, headers, end_stream=True)
    return server.receive_data(client.data_to_send())


@pytest.mark.parametrize("hosts", [[], ["example.test"]])
def test_http2_normal_request_is_preserved(hosts):
    requests = [event for event in _receive_request(hosts) if isinstance(event, RequestReceived)]
    assert len(requests) == 1
    assert requests[0].stream_id == 1
    assert (b":authority", b"example.test") in requests[0].headers


@pytest.mark.parametrize("hosts", [
    ["different.test", "example.test"],
    ["example.test", "example.test"],
])
def test_http2_duplicate_host_headers_are_rejected(hosts):
    # The pinned h2 diagnostic distinguishes duplicate-Host rejection from an
    # unrelated protocol failure; preserve that evidence when updating h2.
    with pytest.raises(ProtocolError, match="multiple Host headers"):
        _receive_request(hosts)
