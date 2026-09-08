"""Real parser compatibility checks for the locked document dependencies."""

from io import BytesIO

import pytest
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import RequestReceived
from h2.exceptions import ProtocolError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


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
