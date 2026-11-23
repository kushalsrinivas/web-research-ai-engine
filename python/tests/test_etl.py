from __future__ import annotations

import io
from datetime import UTC, datetime

import pymupdf
import pytest
from docx import Document as DocxDocument
from wre_parser import canonical, normalize, quality
from wre_parser.extract import Extraction, ExtractionError, detect_kind, extract

ARTICLE = """
<html><head><title>Gujarat semiconductor plant</title>
<link rel="canonical" href="https://example.com/semiconductors/gujarat">
<meta name="author" content="Jane Doe, John Roe">
<meta property="article:published_time" content="2026-01-02T03:04:05Z">
</head><body>
<nav>Home | Markets | Contact</nav>
<article><h1>Gujarat semiconductor plant</h1>
<p>The company announced plans to invest fifteen thousand crore rupees in a semiconductor
facility in Gujarat. The facility is expected to begin production in 2028, subject to approvals.</p>
<p>Officials said the project would create several thousand jobs and support the local supply chain
over the next decade, with construction starting later this year.</p>
<p>Analysts cautioned that announced investment is not the same as committed capital spending.</p>
</article><footer>Copyright example</footer></body></html>
"""


def test_detect_kind_sniffs_formats() -> None:
    assert detect_kind("application/pdf", b"%PDF-1.7") == "pdf"
    assert detect_kind("text/html; charset=utf-8", b"<!doctype html><p>x") == "html"
    assert detect_kind("", b'{"a":1}') == "json"
    assert detect_kind("", b"<rss><channel></channel></rss>") == "feed"
    assert detect_kind("text/plain", b"hello") == "text"


def test_html_extraction_keeps_metadata_and_strips_boilerplate() -> None:
    result = extract(ARTICLE.encode(), "text/html", "https://example.com/semiconductors/gujarat")
    assert result.kind == "html"
    assert result.title == "Gujarat semiconductor plant"
    assert "Jane Doe" in result.authors
    assert "fifteen thousand crore" in result.text
    assert "Copyright example" not in result.text
    assert result.canonical_url == "https://example.com/semiconductors/gujarat"
    assert result.headings >= 1


def test_pdf_and_docx_are_processed() -> None:
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Semiconductor investment in Gujarat. " * 20)
    pdf_bytes = pdf.tobytes()
    pdf.close()
    pdf_result = extract(pdf_bytes, "application/pdf", "https://example.com/report.pdf")
    assert pdf_result.kind == "pdf"
    assert "Semiconductor investment" in pdf_result.text

    docx = DocxDocument()
    docx.add_paragraph("Factory construction began in Gujarat." * 10)
    docx.core_properties.title = "Factory update"
    buffer = io.BytesIO()
    docx.save(buffer)
    docx_result = extract(
        buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "https://example.com/report.docx",
    )
    assert docx_result.kind == "docx"
    assert docx_result.title == "Factory update"
    assert "Factory construction" in docx_result.text


def test_json_feed_and_text() -> None:
    json_result = extract(
        b'{"title":"Update","content":"A long body","author":"A B"}',
        "application/json",
        "https://x.test",
    )
    assert json_result.title == "Update"
    assert json_result.text == "A long body"
    assert json_result.authors == ["A B"]

    feed = b"""<rss><channel><title>Feed</title><item><title>Item one</title>
    <description>Summary text here</description><link>https://x.test/1</link></item></channel></rss>"""
    feed_result = extract(feed, "application/rss+xml", "https://x.test/feed")
    assert feed_result.title == "Feed"
    assert "Item one" in feed_result.text

    text_result = extract(b"Hello world.", "text/plain", "https://x.test/a.txt")
    assert text_result.text == "Hello world."

    with pytest.raises(ExtractionError):
        extract(b"\x00\x01\x02", "application/pdf", "https://x.test/bad.pdf")


def test_cleaning_is_unicode_and_whitespace_stable() -> None:
    raw = "  \uff21\uff22\uff23\u200b   text \r\n\r\n\r\nnext\tline  "
    cleaned = normalize.clean_text(raw)
    assert cleaned == "ABC text\n\nnext line"
    assert normalize.word_count("one two three") == 3
    assert normalize.paragraphs("a\n\nb\n\n c ") == ["a", "b", "c"]


def test_timestamps_and_authors() -> None:
    assert normalize.parse_timestamp("2026-01-02") == datetime(2026, 1, 2, tzinfo=UTC)
    assert normalize.parse_timestamp("2026-01-02T03:04:05Z") == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )
    assert normalize.parse_timestamp("2 January 2026") == datetime(2026, 1, 2, tzinfo=UTC)
    assert normalize.parse_timestamp("not a date") is None
    assert normalize.normalize_authors("Jane Doe; john roe & JANE DOE") == ["Jane Doe", "john roe"]


def test_simhash_deterministic() -> None:
    text = "A company announced an investment in a semiconductor facility in Gujarat."
    first = normalize.simhash64(text)
    assert first == normalize.simhash64(text)
    assert first != normalize.simhash64("A completely different sentence about weather.")
    encoded = normalize.simhash_hex(first)
    assert len(encoded) == 18 and encoded.startswith("0x")


def test_quality_rejects_empty_and_rewards_structure() -> None:
    empty = quality.score(Extraction(kind="text", text=""), "", "und", 0.0, 0)
    assert empty["score"] == 0
    good = quality.score(
        Extraction(
            kind="html", text="x" * 2000, title="T", authors=["A"], published_raw="2026-01-02"
        ),
        "This is a paragraph.\n\nThis is another paragraph.\n\nA third paragraph.",
        "en",
        0.95,
        300,
    )
    assert good["score"] > 0.6


def test_canonical_document_validates_against_schema() -> None:
    extraction = extract(ARTICLE.encode(), "text/html", "https://example.com/a")
    text = normalize.clean_text(extraction.text)
    words = normalize.word_count(text)
    language, confidence = normalize.detect_language(text)
    score = quality.score(extraction, text, language, confidence, words)
    document = {
        "document_id": "01JB2Q1H7S5D9F4K2M8N6P3R0T",
        "tenant_id": "public",
        "canonical_url": "https://example.com/a",
        "created_at": datetime(2026, 1, 2, tzinfo=UTC),
    }
    document["revision"] = 1
    document["fetched_url"] = "https://example.com/a"
    document["http_status"] = 200
    document["content_type"] = "text/html"
    result = canonical.build(
        document=document,
        revision=document,
        source_id=None,
        extraction=extraction,
        text=text,
        language=language,
        language_confidence=confidence,
        quality=score,
        content_hash=normalize.content_hash(text),
        raw_uri="s3://wre-raw/raw/x",
        parsed_uri="s3://wre-parsed/parsed/x.json",
        processing_version="parser-1.0.0",
        parser_version="parser-1.0.0",
        normalizer_version="normalize-1.0.0",
        published_at=normalize.parse_timestamp(extraction.published_raw),
        updated_at=None,
        retrieved_at=document["created_at"],
        warnings=extraction.warnings,
    )
    assert canonical.validator().is_valid(result)
    result["content_hash"] = "not-a-hash"
    with pytest.raises(canonical.CanonicalError):
        canonical.build(
            document=document,
            revision=document,
            source_id=None,
            extraction=extraction,
            text=text,
            language=language,
            language_confidence=confidence,
            quality=score,
            content_hash=result["content_hash"],
            raw_uri="s3://wre-raw/raw/x",
            parsed_uri="s3://wre-parsed/parsed/x.json",
            processing_version="parser-1.0.0",
            parser_version="parser-1.0.0",
            normalizer_version="normalize-1.0.0",
            published_at=None,
            updated_at=None,
            retrieved_at=document["created_at"],
            warnings=[],
        )
