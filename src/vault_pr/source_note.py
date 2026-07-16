"""Visible Obsidian source-note naming, lookup, and inert rendering."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import unicodedata
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from .capture import CaptureError, source_note_has_id
from .errors import IngestError

if TYPE_CHECKING:
    from .ingest import ExtractedSource, FetchedSource


def safe_note_filename(title: str, url: str) -> str:
    """Create a stable, readable Obsidian filename for a source URL."""
    normalized = unicodedata.normalize("NFKC", title).strip()
    normalized = re.sub(r'[\\/:*?"<>|#^\[\]]+', "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .-")
    normalized = normalized[:80].rstrip(" .-") or "Untitled source"
    url_id = hashlib.sha256(url.encode()).hexdigest()[:8]
    return f"{normalized}--{url_id}.md"


def normalize_title(title: str) -> str:
    """Collapse untrusted title text into one bounded display line."""
    normalized = unicodedata.normalize("NFKC", title)
    normalized = " ".join(normalized.split())
    normalized = "".join(
        character for character in normalized if character.isprintable()
    )
    return normalized[:200].strip() or "Untitled source"


def source_note_candidates(note_dir: Path, url: str) -> list[Path]:
    """Return regular source-note candidates sharing the URL suffix."""
    url_id = hashlib.sha256(url.encode()).hexdigest()[:8]
    if not note_dir.is_dir():
        return []
    candidates = sorted(
        {*note_dir.glob(f"*--{url_id}.md"), *note_dir.glob(f"*--{url_id}--*.md")}
    )
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_file():
            raise IngestError(
                f"source note candidate is not a regular file: {candidate}"
            )
    return candidates


def versioned_source_note_path(
    note_dir: Path, title: str, url: str, version: str
) -> Path:
    """Create the deterministic path for a changed immutable source version."""
    base = safe_note_filename(title, url).removesuffix(".md")
    path = note_dir / f"{base}--{version}.md"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise IngestError(f"versioned source note path is occupied: {path}")
    return path


def source_note_path(
    note_dir: Path, title: str, url: str, source_id: str | None = None
) -> Path:
    """Resolve one visible note per immutable version of a source URL."""
    url_id = hashlib.sha256(url.encode()).hexdigest()[:8]
    base_notes = sorted(note_dir.glob(f"*--{url_id}.md")) if note_dir.is_dir() else []
    if source_id is None:
        if len(base_notes) > 1:
            raise IngestError(f"multiple source notes share URL id {url_id}")
        return (
            base_notes[0] if base_notes else note_dir / safe_note_filename(title, url)
        )

    candidates = source_note_candidates(note_dir, url)
    matching: list[Path] = []
    try:
        for candidate in candidates:
            if source_note_has_id(candidate, source_id):
                matching.append(candidate)
    except CaptureError as exc:
        raise IngestError(str(exc)) from exc
    if len(matching) > 1:
        raise IngestError(f"multiple source notes contain source id {source_id}")
    if matching:
        return matching[0]
    if not candidates:
        return note_dir / safe_note_filename(title, url)
    version = source_id.removeprefix("src_")
    versioned = versioned_source_note_path(note_dir, title, url, version)
    if versioned.exists() or versioned.is_symlink():
        raise IngestError(
            f"versioned source note path is already occupied: {versioned}"
        )
    return versioned


def _inert_obsidian_text(value: str) -> str:
    """Render untrusted text without creating Obsidian links, embeds, or tags."""
    escaped = html.escape(value, quote=False)
    for character, entity in {
        "\\": "&#92;",
        "`": "&#96;",
        "*": "&#42;",
        "_": "&#95;",
        "[": "&#91;",
        "]": "&#93;",
        "#": "&#35;",
        "!": "&#33;",
        "|": "&#124;",
        "^": "&#94;",
    }.items():
        escaped = escaped.replace(character, entity)
    return escaped


def _markdown_url_destination(url: str) -> str:
    return urllib.parse.quote(url, safe=":/?#@!$&'()*+,;=%")


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_source_note(
    *,
    title: str,
    source_id: str,
    fetched: FetchedSource,
    extracted: ExtractedSource,
    raw_sha256: str,
    extracted_sha256: str,
    note_path: Path,
    capture_dir: Path,
) -> str:
    """Build a valid Obsidian source-note draft with inert display metadata."""
    raw_link = os.path.relpath(capture_dir / extracted.raw_filename, note_path.parent)
    extracted_link = os.path.relpath(
        capture_dir / extracted.extracted_filename, note_path.parent
    )
    raw_display = _inert_obsidian_text(raw_link)
    extracted_display = _inert_obsidian_text(extracted_link)
    display_title = _inert_obsidian_text(title)
    source_destination = _markdown_url_destination(fetched.final_url)
    return "\n".join(
        [
            "---",
            f"title: {_yaml_string(title)}",
            "type: source",
            "status: captured",
            f"source_url: {_yaml_string(fetched.final_url)}",
            f"source_id: {_yaml_string(source_id)}",
            f"source_sha256: {_yaml_string(raw_sha256)}",
            f"extracted_sha256: {_yaml_string(extracted_sha256)}",
            f"captured_at: {_yaml_string(fetched.captured_at)}",
            f"extractor: {_yaml_string(extracted.extractor)}",
            f"extractor_version: {_yaml_string(extracted.extractor_version)}",
            "tags:",
            "  - source/article",
            "aliases: []",
            "---",
            "",
            f"# {display_title}",
            "",
            "> [!info] 来源记录",
            f"> - 原始链接：[打开原始链接](<{source_destination}>)",
            f"> - 捕获时间：{fetched.captured_at}",
            f"> - 原始快照：`{raw_display}`",
            f"> - 提取正文：`{extracted_display}`",
            "",
            "## 摘要",
            "",
            "> [!todo] 等待 Agent 完成语义整理并更新相关概念笔记。",
            "",
            "## 关键观点",
            "",
            "- 待整理。",
            "",
            "## 与既有知识的关系",
            "",
            "- 待检索并建立 wikilink。",
            "",
        ]
    )
