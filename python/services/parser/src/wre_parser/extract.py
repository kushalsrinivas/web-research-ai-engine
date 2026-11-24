"""Format-specific extraction. Raw extracted text is kept separate from cleaning."""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import feedparser  # type: ignore[import-untyped]
import trafilatura
from charset_normalizer import from_bytes
from defusedxml import ElementTree as SafeET  # type: ignore[import-untyped]
from docx import Document as open_docx
from lxml import html as lxml_html  # type: ignore[import-untyped]
from pymupdf import Document as open_pdf


class ExtractionError(ValueError):
    """The payload is present but cannot be parsed."""


@dataclass(slots=True)
class Extraction:
    kind: str
    text: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    published_raw: str | None = None
    updated_raw: str | None = None
    description: str = ""
    canonical_url: str | None = None
    tags: list[str] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    headings: int = 0
    visible_chars: int = 0
    extractor: str = ""
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\w+", self.text, flags=re.UNICODE))


_DOCX_TYPES = ("application/vnd.openxmlformats-officedocument.wordprocessingml",)


def detect_kind(content_type: str, payload: bytes) -> str:
    media = (content_type or "").split(";")[0].strip().lower()
    head = payload[:2048].lstrip()
    if head.startswith(b"%PDF-") or media == "application/pdf":
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        if media in _DOCX_TYPES:
            return "docx"
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                if "word/document.xml" in archive.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            pass
    if media in {"text/html", "application/xhtml+xml"} or head[:15].lower().startswith(
        (b"<!doctype html", b"<html")
    ):
        return "html"
    if (
        media in {"application/rss+xml", "application/atom+xml"}
        or b"<rss" in head.lower()
        or b"<feed" in head.lower()
    ):
        return "feed"
    if media in {"application/json", "application/ld+json"}:
        return "json"
    if head.startswith((b"{", b"[")):
        return "json"
    if "xml" in media or head.startswith(b"<?xml"):
        return "xml"
    if media.startswith("text/") or media in {"", "application/octet-stream"}:
        if b"<html" in head.lower() or b"<!doctype html" in head.lower():
            return "html"
        if b"<rss" in head.lower() or b"<feed" in head.lower():
            return "feed"
        if b"<urlset" in head.lower() or b"<sitemapindex" in head.lower():
            return "xml"
        return "text"
    return "text"


def extract(payload: bytes, content_type: str, url: str) -> Extraction:
    if not payload:
        raise ExtractionError("empty payload")
    kind = detect_kind(content_type, payload)
    try:
        if kind == "html":
            return _extract_html(payload, content_type, url)
        if kind == "pdf":
            return _extract_pdf(payload)
        if kind == "docx":
            return _extract_docx(payload)
        if kind == "feed":
            return _extract_feed(payload, url)
        if kind == "json":
            return _extract_json(payload)
        if kind == "xml":
            return _extract_xml(payload, url)
        return _extract_text(payload, content_type)
    except ExtractionError:
        raise
    except Exception as exc:  # third-party parsers fail in many shapes
        raise ExtractionError(f"{kind} extraction failed: {exc}") from exc


def _extract_html(payload: bytes, content_type: str, url: str) -> Extraction:
    text_payload = _decode_html(payload, content_type)
    warnings: list[str] = []
    extracted = None
    try:
        extracted = trafilatura.bare_extraction(
            text_payload,
            url=url,
            with_metadata=True,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            deduplicate=True,
        )
    except Exception:
        warnings.append("trafilatura_failed")

    tree = _html_tree(text_payload)
    visible_chars = 0
    canonical_url = None
    headings = 0
    images: list[dict[str, str]] = []
    links: list[dict[str, str]] = []
    if tree is not None:
        for bad in tree.xpath("//script|//style|//noscript|//iframe|//svg"):
            bad.getparent().remove(bad)
        visible_chars = len(" ".join(tree.text_content().split()))
        canonical = tree.xpath(
            "//link[translate(@rel,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='canonical']/@href"
        )
        canonical_url = urljoin(url, canonical[0]) if canonical else None
        headings = len(tree.xpath("//h1|//h2|//h3"))
        for image in tree.xpath("//img[@src]"):
            source = urljoin(url, image.get("src", ""))
            if source:
                images.append({"url": source, "caption": (image.get("alt") or "").strip()})
            if len(images) >= 50:
                break
        for anchor in tree.xpath("//a[@href]"):
            href = urljoin(url, anchor.get("href", ""))
            if href.startswith(("http://", "https://")):
                links.append({"url": href, "text": " ".join(anchor.text_content().split())[:200]})
            if len(links) >= 200:
                break

    text = ""
    title = ""
    authors: list[str] = []
    published = updated = description = None
    tags: list[str] = []
    if extracted is not None:
        text = getattr(extracted, "text", "") or getattr(extracted, "raw_text", "") or ""
        title = getattr(extracted, "title", "") or ""
        author = getattr(extracted, "author", "") or ""
        authors = _split_people(author)
        published = getattr(extracted, "date", None)
        description = getattr(extracted, "description", None)
        tags = list(getattr(extracted, "tags", []) or []) + list(
            getattr(extracted, "categories", []) or []
        )
        if text and title and text.startswith(title):
            text = text[len(title) :].lstrip()

    if not text.strip() and tree is not None:
        text = " ".join(tree.text_content().split())
        warnings.append("fallback_extraction")
    if not title and tree is not None:
        heading = tree.xpath("//h1//text()|//title//text()")
        if heading:
            title = " ".join(str(heading[0]).split())

    return Extraction(
        kind="html",
        text=text,
        title=title.strip(),
        authors=authors,
        published_raw=published,
        updated_raw=updated,
        description=description or "",
        canonical_url=canonical_url,
        tags=_dedupe(tags),
        images=images,
        links=links,
        headings=headings,
        visible_chars=visible_chars,
        extractor="trafilatura",
        warnings=warnings,
        metadata={"sitename": getattr(extracted, "sitename", None) if extracted else None},
    )


def _html_tree(payload: bytes | str) -> lxml_html.HtmlElement | None:
    try:
        return lxml_html.fromstring(
            payload, parser=lxml_html.HTMLParser(encoding=None, recover=True)
        )
    except Exception:
        return None


def _extract_pdf(payload: bytes) -> Extraction:
    document: Any = open_pdf(stream=payload, filetype="pdf")  # type: ignore[no-untyped-call]
    try:
        if document.needs_pass:
            raise ExtractionError("encrypted PDF")
        pages = [document.load_page(index).get_text("text") for index in range(document.page_count)]
        text = "\n\n".join(part.strip() for part in pages if part.strip())
        metadata = document.metadata or {}
        page_count = document.page_count
    finally:
        document.close()
    warnings = [] if text.strip() else ["pdf_no_text"]
    return Extraction(
        kind="pdf",
        text=text,
        title=str(metadata.get("title") or "").strip(),
        authors=_split_people(str(metadata.get("author") or "")),
        published_raw=_pdf_date(metadata.get("creationDate")),
        updated_raw=_pdf_date(metadata.get("modDate")),
        description=str(metadata.get("subject") or ""),
        extractor="pymupdf",
        warnings=warnings,
        metadata={"pages": page_count},
    )


def _extract_docx(payload: bytes) -> Extraction:
    document = open_docx(io.BytesIO(payload))
    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    props = document.core_properties
    text = "\n\n".join(parts)
    warnings = [] if text.strip() else ["docx_no_text"]
    return Extraction(
        kind="docx",
        text=text,
        title=(props.title or "").strip(),
        authors=_split_people(props.author or ""),
        published_raw=props.created.isoformat() if props.created else None,
        updated_raw=props.modified.isoformat() if props.modified else None,
        description=(props.subject or "").strip(),
        extractor="python-docx",
        warnings=warnings,
        metadata={"paragraphs": len(parts), "images": len(document.inline_shapes)},
    )


def _extract_feed(payload: bytes, url: str) -> Extraction:
    feed = feedparser.parse(payload)
    entries = list(feed.entries or [])
    warnings = []
    if not entries:
        warnings.append("feed_no_entries")
    parts: list[str] = []
    entries_metadata: list[dict[str, Any]] = []
    for entry in entries[:200]:
        entry_title = str(entry.get("title", "")).strip()
        summary = str(entry.get("summary", "") or "").strip()
        parts.append("\n".join(part for part in (entry_title, summary) if part))
        entries_metadata.append(
            {
                "title": entry_title,
                "url": entry.get("link"),
                "published": entry.get("published"),
            }
        )
    title = str(feed.feed.get("title", "")).strip() if getattr(feed, "feed", None) else ""
    description = (
        str(feed.feed.get("subtitle", "") or feed.feed.get("description", "")).strip()
        if getattr(feed, "feed", None)
        else ""
    )
    published = (
        _struct_time(feed.feed.get("published_parsed")) if getattr(feed, "feed", None) else None
    )
    updated = _struct_time(feed.feed.get("updated_parsed")) if getattr(feed, "feed", None) else None
    authors = _split_people(str(feed.feed.get("author", ""))) if getattr(feed, "feed", None) else []
    return Extraction(
        kind="feed",
        text="\n\n".join(part for part in parts if part) or description or title,
        title=title,
        authors=authors,
        published_raw=published,
        updated_raw=updated,
        description=description,
        canonical_url=str(feed.feed.get("link", "") or url),
        tags=_dedupe([str(tag.get("term", "")) for tag in feed.feed.get("tags", []) or []]),
        extractor="feedparser",
        warnings=warnings + (["feed_parse_error"] if getattr(feed, "bozo", False) else []),
        metadata={"entries": entries_metadata},
    )


def _extract_json(payload: bytes) -> Extraction:
    try:
        value = json.loads(_decode(payload))
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"invalid json: {exc}") from exc
    title = ""
    text = ""
    description = ""
    authors: list[str] = []
    published: str | None = None
    updated: str | None = None
    if isinstance(value, dict):
        title = str(value.get("title") or value.get("headline") or value.get("name") or "").strip()
        for key in ("content", "text", "body", "article", "description", "summary"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                text = candidate
                break
        description = str(value.get("description") or value.get("summary") or "").strip()
        authors = _split_people(value.get("author") or value.get("authors") or "")
        published = _string_or_none(
            value.get("published_at") or value.get("published") or value.get("date")
        )
        updated = _string_or_none(value.get("updated_at") or value.get("updated"))
    if not text:
        text = _flatten_json(value)
    warnings = [] if text.strip() else ["json_no_text"]
    return Extraction(
        kind="json",
        text=text,
        title=title,
        authors=authors,
        published_raw=published,
        updated_raw=updated,
        description=description,
        extractor="json",
        warnings=warnings,
        metadata={"root_type": type(value).__name__},
    )


def _extract_xml(payload: bytes, url: str) -> Extraction:
    try:
        root = SafeET.fromstring(payload)
    except Exception as exc:
        raise ExtractionError(f"invalid xml: {exc}") from exc
    tag = _local_name(root.tag)
    texts = [" ".join("".join(node.itertext()).split()) for node in root.iter()]
    text = "\n".join(part for part in texts if part)
    title = ""
    for node in root.iter():
        if _local_name(node.tag) == "title" and node.text:
            title = " ".join(node.text.split())
            break
    links = []
    for node in root.iter():
        if _local_name(node.tag) in {"loc", "link"} and node.text:
            links.append({"url": node.text.strip(), "text": ""})
    return Extraction(
        kind="xml",
        text=text,
        title=title,
        canonical_url=url,
        links=links[:200],
        extractor="lxml",
        warnings=[] if text.strip() else ["xml_no_text"],
        metadata={"root": tag},
    )


def _extract_text(payload: bytes, content_type: str = "") -> Extraction:
    text = _decode_html(payload, content_type)
    return Extraction(
        kind="text",
        text=text,
        title="",
        extractor="native-text",
        warnings=[] if text.strip() else ["text_empty"],
        metadata={},
    )


def _decode_html(payload: bytes, content_type: str) -> str:
    charset = ""
    for part in (content_type or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key.casefold() == "charset":
            charset = value.strip().strip('"').strip("'")
            break
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return payload.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return _decode(payload)


def _decode(payload: bytes) -> str:
    match = from_bytes(payload).best()
    if match is not None:
        return str(match)
    return payload.decode("utf-8", errors="replace")


def _flatten_json(value: Any, prefix: str = "", depth: int = 0) -> str:
    if depth > 6:
        return ""
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            label = f"{prefix}.{key}" if prefix else str(key)
            lines.append(_flatten_json(item, label, depth + 1))
    elif isinstance(value, list):
        for index, item in enumerate(value[:200]):
            lines.append(_flatten_json(item, f"{prefix}[{index}]", depth + 1))
    elif value is not None:
        lines.append(f"{prefix}: {value}" if prefix else str(value))
    return "\n".join(line for line in lines if line)


def _split_people(value: Any) -> list[str]:
    raw = [str(item) for item in value] if isinstance(value, list) else [str(value or "")]
    people: list[str] = []
    for part in raw:
        for candidate in re.split(r"\s*(?:;|\||&|\band\b)\s*", part):
            candidate = " ".join(candidate.split())
            if candidate and len(candidate) <= 200:
                people.append(candidate)
    return _dedupe(people)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).split())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _pdf_date(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.match(r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", raw)
    if not match:
        return None
    parts = [int(part) if part else 1 for part in match.groups()]
    try:
        return datetime(
            parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], tzinfo=UTC
        ).isoformat()
    except ValueError:
        return None


def _struct_time(value: Any) -> str | None:
    if not value:
        return None
    try:
        parts = [int(part) for part in value[:6]]
        return datetime(
            parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], tzinfo=UTC
        ).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()
