"""Prepare immutable URL captures and deterministic Obsidian source notes."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .config import (
    ConfigError,
    domain_source_notes_dir,
    load_vault_path,
    source_archive_dir,
)
from .git import require_clean_git_root, vault_write_lock


class IngestError(RuntimeError):
    """Raised when a source cannot be safely prepared."""


SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-language",
    "content-length",
    "content-type",
    "etag",
    "last-modified",
}
SUPPORTED_CONTENT_TYPES = {
    "application/xhtml+xml",
    "text/html",
    "text/markdown",
    "text/plain",
}
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "key",
    "password",
    "sig",
    "signature",
    "token",
}


@dataclass(frozen=True)
class FetchedSource:
    """An HTTP response captured in memory before any vault write."""

    requested_url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    captured_at: str


@dataclass(frozen=True)
class ExtractedSource:
    """Local artifacts produced from a fetched source."""

    title: str
    raw_filename: str
    extracted_filename: str
    extractor: str
    extractor_version: str


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._inside_title = False

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self.parts.append(data)


def canonicalize_url(url: str) -> str:
    """Normalize an HTTP URL without changing query semantics."""
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise IngestError("only http and https URLs are supported")
    if not parsed.hostname:
        raise IngestError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise IngestError("credentials embedded in URLs are not supported")

    hostname = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise IngestError("URL contains an invalid port") from exc
    query_keys = {
        key.lower()
        for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    }
    sensitive_keys = sorted(query_keys & SENSITIVE_QUERY_KEYS)
    if sensitive_keys:
        raise IngestError(
            "URL query contains sensitive parameters: " + ", ".join(sensitive_keys)
        )
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    netloc = hostname if port in {None, default_port} else f"{hostname}:{port}"
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), netloc, path, parsed.query, "")
    )


def require_public_url(url: str) -> str:
    """Reject local and non-global URL targets before fetching."""
    canonical = canonicalize_url(url)
    parsed = urllib.parse.urlsplit(canonical)
    assert parsed.hostname is not None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise IngestError(f"unable to resolve URL hostname: {parsed.hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise IngestError(f"URL resolves to a non-public address: {ip}")
    return canonical


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        require_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, timeout_seconds: float, max_bytes: int) -> FetchedSource:
    """Fetch a public URL once with redirect and size safeguards."""
    canonical = require_public_url(url)
    request = urllib.request.Request(
        canonical,
        headers={
            "Accept": "text/html,text/markdown,text/plain;q=0.9,*/*;q=0.1",
            "User-Agent": "obsidian-vault-pr/0.1",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            final_url = require_public_url(response.geturl())
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise IngestError(f"response exceeds {max_bytes} bytes")
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in SAFE_RESPONSE_HEADERS
            }
            media_type = (
                headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            if media_type and media_type not in SUPPORTED_CONTENT_TYPES:
                raise IngestError(f"unsupported response content type: {media_type}")
            return FetchedSource(
                requested_url=canonical,
                final_url=final_url,
                status=response.status,
                headers=headers,
                body=body,
                captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(f"failed to fetch URL: {exc}") from exc


def _markdown_title(text: str) -> str | None:
    match = re.search(r"^#\s+(.+?)\s*$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _html_title(text: str) -> str | None:
    parser = _TitleParser()
    parser.feed(text)
    title = " ".join(part.strip() for part in parser.parts if part.strip())
    return title or None


def _defuddle_version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"], check=False, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def extract_source(fetched: FetchedSource, output_dir: Path) -> ExtractedSource:
    """Write the raw response and extract deterministic Markdown."""
    content_type = fetched.headers.get("content-type", "").lower()
    url_path = urllib.parse.urlsplit(fetched.final_url).path.lower()
    is_markdown = url_path.endswith(".md") or "text/markdown" in content_type
    raw_filename = "source.md" if is_markdown else "source.html"
    raw_path = output_dir / raw_filename
    raw_path.write_bytes(fetched.body)
    extracted_path = output_dir / "extracted.md"

    if is_markdown:
        text = fetched.body.decode("utf-8", errors="replace")
        extracted_path.write_text(text, encoding="utf-8")
        title = _markdown_title(text) or Path(url_path).stem or "Untitled source"
        return ExtractedSource(
            title=title,
            raw_filename=raw_filename,
            extracted_filename=extracted_path.name,
            extractor="direct-markdown",
            extractor_version="1",
        )

    executable = shutil.which("defuddle")
    if not executable:
        raise IngestError("required tool missing: defuddle")
    extraction = subprocess.run(
        [executable, "parse", str(raw_path), "--md", "-o", str(extracted_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if extraction.returncode != 0 or not extracted_path.is_file():
        detail = extraction.stderr.strip() or extraction.stdout.strip()
        raise IngestError(f"Defuddle extraction failed: {detail}")
    extracted_text = extracted_path.read_text(encoding="utf-8")
    if len(extracted_text.strip()) < 100:
        raise IngestError("Defuddle output is too short to trust")

    title_result = subprocess.run(
        [executable, "parse", str(raw_path), "-p", "title"],
        check=False,
        capture_output=True,
        text=True,
    )
    raw_text = fetched.body.decode("utf-8", errors="replace")
    title = (
        title_result.stdout.strip()
        if title_result.returncode == 0 and title_result.stdout.strip()
        else _html_title(raw_text)
    )
    return ExtractedSource(
        title=title
        or urllib.parse.urlsplit(fetched.final_url).hostname
        or "Untitled source",
        raw_filename=raw_filename,
        extracted_filename=extracted_path.name,
        extractor="defuddle",
        extractor_version=_defuddle_version(executable),
    )


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


def source_note_path(note_dir: Path, title: str, url: str) -> Path:
    """Reuse a stable URL-suffixed note even if the extracted title changes."""
    url_id = hashlib.sha256(url.encode()).hexdigest()[:8]
    existing = sorted(note_dir.glob(f"*--{url_id}.md")) if note_dir.is_dir() else []
    if len(existing) > 1:
        raise IngestError(f"multiple source notes share URL id {url_id}")
    return existing[0] if existing else note_dir / safe_note_filename(title, url)


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_source_note(
    *,
    title: str,
    domain: str,
    source_id: str,
    fetched: FetchedSource,
    extracted: ExtractedSource,
    raw_sha256: str,
    extracted_sha256: str,
    note_path: Path,
    capture_dir: Path,
) -> str:
    """Build a valid Obsidian source-note draft with provenance properties."""
    raw_link = os.path.relpath(capture_dir / extracted.raw_filename, note_path.parent)
    extracted_link = os.path.relpath(
        capture_dir / extracted.extracted_filename, note_path.parent
    )
    return "\n".join(
        [
            "---",
            f"title: {_yaml_string(title)}",
            "type: source",
            f"domain: {_yaml_string(domain)}",
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
            f"# {title}",
            "",
            "> [!info] 来源记录",
            f"> - 原始链接：[{fetched.final_url}]({fetched.final_url})",
            f"> - 捕获时间：{fetched.captured_at}",
            f"> - 原始快照：`{raw_link}`",
            f"> - 提取正文：`{extracted_link}`",
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


def ingest_url(
    *,
    config: dict[str, Any],
    url: str,
    domain: str,
    apply: bool,
    title_override: str | None = None,
) -> dict[str, Any]:
    """Prepare a URL capture and optionally write it to the configured vault."""
    vault = load_vault_path(config)
    if not vault.is_dir():
        raise ConfigError(f"vault path does not exist: {vault}")
    if apply:
        require_clean_git_root(vault)

    sources_config = config.get("sources", {})
    timeout_seconds = float(sources_config.get("timeout_seconds", 30))
    max_bytes = int(sources_config.get("max_download_bytes", 10_000_000))
    fetched = fetch_url(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)

    with tempfile.TemporaryDirectory(prefix="vault-pr-ingest-") as temp_dir_string:
        temp_dir = Path(temp_dir_string)
        extracted = extract_source(fetched, temp_dir)
        title = normalize_title(title_override or extracted.title)
        raw_path = temp_dir / extracted.raw_filename
        extracted_path = temp_dir / extracted.extracted_filename
        raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        extracted_sha256 = hashlib.sha256(extracted_path.read_bytes()).hexdigest()
        source_identity = hashlib.sha256(
            f"{fetched.requested_url}\0{extracted_sha256}".encode()
        ).hexdigest()
        source_id = f"src_{source_identity[:20]}"

        archive_root = source_archive_dir(config, vault)
        capture_dir = archive_root / source_id
        note_dir = domain_source_notes_dir(config, vault, domain)
        note_path = source_note_path(note_dir, title, fetched.requested_url)
        note_content = build_source_note(
            title=title,
            domain=domain,
            source_id=source_id,
            fetched=fetched,
            extracted=extracted,
            raw_sha256=raw_sha256,
            extracted_sha256=extracted_sha256,
            note_path=note_path,
            capture_dir=capture_dir,
        )
        manifest = {
            "schema_version": 1,
            "source_id": source_id,
            "requested_url": fetched.requested_url,
            "final_url": fetched.final_url,
            "captured_at": fetched.captured_at,
            "http_status": fetched.status,
            "response_headers": fetched.headers,
            "title": title,
            "domain": domain,
            "raw_sha256": raw_sha256,
            "extracted_sha256": extracted_sha256,
            "extractor": extracted.extractor,
            "extractor_version": extracted.extractor_version,
            "artifacts": {
                "raw": extracted.raw_filename,
                "extracted": extracted.extracted_filename,
                "source_note": str(note_path.relative_to(vault)),
            },
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = {
            "status": "planned",
            "source_id": source_id,
            "title": title,
            "capture_dir": str(capture_dir.relative_to(vault)),
            "source_note": str(note_path.relative_to(vault)),
        }
        if not apply:
            return result

        with vault_write_lock(vault):
            require_clean_git_root(vault)
            if note_path.exists():
                existing = note_path.read_text(encoding="utf-8")
                if f'source_id: "{source_id}"' not in existing:
                    raise IngestError(
                        f"source note already exists for different content: {note_path}"
                    )
            capture_created = False
            try:
                if capture_dir.exists():
                    existing_manifest = capture_dir / "manifest.json"
                    if not existing_manifest.is_file():
                        raise IngestError(
                            f"capture directory is incomplete: {capture_dir}"
                        )
                else:
                    capture_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(temp_dir, capture_dir)
                    capture_created = True

                if not note_path.exists():
                    note_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary_note = note_path.with_suffix(note_path.suffix + ".tmp")
                    temporary_note.write_text(note_content, encoding="utf-8")
                    temporary_note.replace(note_path)
            except Exception as exc:
                if capture_created and not note_path.exists():
                    shutil.rmtree(capture_dir, ignore_errors=True)
                if isinstance(exc, IngestError):
                    raise
                raise IngestError(f"failed to write ingest artifacts: {exc}") from exc

        result["status"] = "captured"
        return result
