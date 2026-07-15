"""Prepare immutable URL captures and deterministic Obsidian source notes."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .capture import (
    CaptureError,
    load_validated_capture,
    source_identity_digest,
    source_note_has_id,
)
from .config import (
    ConfigError,
    load_vault_path,
    note_path_allowed,
    source_archive_dir,
    source_notes_dir,
)
from .git import require_clean_git_root, run_git, vault_write_lock
from .process import (
    LimitedProcessResult,
    LimitedProcessTimeout,
    run_limited_process,
)
from .recovery import (
    RecoveryError,
    mark_active_applied,
    prepare_active_transaction,
    rollback_active_transaction,
)


class IngestError(RuntimeError):
    """Raised when a source cannot be safely prepared."""


def _git_dir(vault: Path) -> Path:
    result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        raise IngestError("unable to resolve vault Git directory")
    return Path(result.stdout.strip()).resolve()


def _atomic_note_write(path: Path, content: str, staging_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging_path.write_text(content, encoding="utf-8")
        staging_path.chmod(0o644)
        os.replace(staging_path, path)
    finally:
        staging_path.unlink(missing_ok=True)


SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-language",
    "content-length",
    "content-type",
    "etag",
    "last-modified",
}
SUPPORTED_CONTENT_TYPES = {
    "application/pdf",
    "application/xhtml+xml",
    "text/html",
    "text/markdown",
    "text/plain",
}
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "accesskeyid",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "awsaccesskeyid",
    "credential",
    "googleaccessid",
    "jwt",
    "key",
    "keypairid",
    "passwd",
    "password",
    "secret",
    "securitytoken",
    "sig",
    "signature",
    "token",
}
SENSITIVE_QUERY_KEY_PARTS = {
    "auth",
    "authorization",
    "credential",
    "jwt",
    "key",
    "passwd",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5


@dataclass(frozen=True)
class FetchedSource:
    """An HTTP response captured in memory before any vault write."""

    requested_url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    captured_at: str
    adapter: str = "http"


@dataclass(frozen=True)
class ExtractedSource:
    """Local artifacts produced from a fetched source."""

    title: str
    raw_filename: str
    extracted_filename: str
    extractor: str
    extractor_version: str


@dataclass(frozen=True)
class _ResolvedTarget:
    canonical_url: str
    parsed: urllib.parse.SplitResult
    address: tuple[int, int, int, str, tuple[Any, ...]]


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
        key for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    }
    sensitive_keys = sorted(key for key in query_keys if _is_sensitive_query_key(key))
    if sensitive_keys:
        raise IngestError(
            "URL query contains sensitive parameters: " + ", ".join(sensitive_keys)
        )
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    display_hostname = f"[{hostname}]" if ":" in hostname else hostname
    netloc = (
        display_hostname
        if port in {None, default_port}
        else f"{display_hostname}:{port}"
    )
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), netloc, path, parsed.query, "")
    )


def _is_sensitive_query_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    compact = normalized.replace("_", "")
    parts = {part for part in normalized.split("_") if part}
    return (
        normalized in SENSITIVE_QUERY_KEYS
        or compact in SENSITIVE_QUERY_KEYS
        or bool(parts & SENSITIVE_QUERY_KEY_PARTS)
    )


def _resolve_public_target(url: str) -> _ResolvedTarget:
    """Resolve once, reject every non-public answer, and retain a pinned address."""
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
    if not addresses:
        raise IngestError(f"URL hostname has no addresses: {parsed.hostname}")
    return _ResolvedTarget(canonical, parsed, addresses[0])


def require_public_url(url: str) -> str:
    """Reject local and non-global URL targets before fetching."""
    return _resolve_public_target(url).canonical_url


def _pinned_socket(
    address: tuple[int, int, int, str, tuple[Any, ...]], timeout_seconds: float
) -> socket.socket:
    family, socktype, proto, _canonical_name, sockaddr = address
    connection = socket.socket(family, socktype, proto)
    try:
        connection.settimeout(timeout_seconds)
        connection.connect(sockaddr)
        return connection
    except Exception:
        connection.close()
        raise


def _open_pinned_connection(
    target: _ResolvedTarget, timeout_seconds: float
) -> http.client.HTTPConnection:
    """Open HTTP(S) to the validated address while preserving TLS hostname checks."""
    hostname = target.parsed.hostname
    assert hostname is not None
    port = target.parsed.port or (443 if target.parsed.scheme == "https" else 80)
    if target.parsed.scheme == "https":
        connection = http.client.HTTPSConnection(
            hostname,
            port,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )

        def connect() -> None:
            raw_socket = _pinned_socket(target.address, timeout_seconds)
            try:
                connection.sock = connection._context.wrap_socket(  # noqa: SLF001
                    raw_socket, server_hostname=hostname
                )
            except Exception:
                raw_socket.close()
                raise

    else:
        connection = http.client.HTTPConnection(hostname, port, timeout=timeout_seconds)

        def connect() -> None:
            connection.sock = _pinned_socket(target.address, timeout_seconds)

    connection.connect = connect  # type: ignore[method-assign]
    return connection


def fetch_url(url: str, timeout_seconds: float, max_bytes: int) -> FetchedSource:
    """Fetch a public URL with address pinning, redirect, and size safeguards."""
    requested_url = canonicalize_url(url)
    current_url = requested_url
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            target = _resolve_public_target(current_url)
            current_url = target.canonical_url
            request_target = urllib.parse.urlunsplit(
                ("", "", target.parsed.path or "/", target.parsed.query, "")
            )
            connection = _open_pinned_connection(target, timeout_seconds)
            try:
                connection.request(
                    "GET",
                    request_target,
                    headers={
                        "Accept": "text/html,text/markdown,application/pdf,text/plain;q=0.9,*/*;q=0.1",
                        "User-Agent": "obsidian-vault-pr/0.1",
                    },
                )
                response = connection.getresponse()
                if response.status in REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    if not location:
                        raise IngestError("redirect response has no Location header")
                    if redirect_count >= MAX_REDIRECTS:
                        raise IngestError(f"response exceeds {MAX_REDIRECTS} redirects")
                    current_url = urllib.parse.urljoin(current_url, location)
                    continue
                if not 200 <= response.status < 300:
                    raise IngestError(
                        f"HTTP request failed with status {response.status}"
                    )
                content_length = response.getheader("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            raise IngestError(f"response exceeds {max_bytes} bytes")
                    except ValueError as exc:
                        raise IngestError(
                            "response has an invalid Content-Length"
                        ) from exc
                body = response.read(max_bytes + 1)
                response_headers = list(response.headers.items())
                response_status = response.status
            finally:
                connection.close()
            if len(body) > max_bytes:
                raise IngestError(f"response exceeds {max_bytes} bytes")
            headers = {
                key.lower(): value
                for key, value in response_headers
                if key.lower() in SAFE_RESPONSE_HEADERS
            }
            media_type = (
                headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            if media_type and media_type not in SUPPORTED_CONTENT_TYPES:
                raise IngestError(f"unsupported response content type: {media_type}")
            return FetchedSource(
                requested_url=requested_url,
                final_url=current_url,
                status=response_status,
                headers=headers,
                body=body,
                captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        raise AssertionError("redirect loop did not return or raise")
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


def _defuddle_version(
    executable: str,
    timeout_seconds: float,
    max_memory_bytes: int,
    max_file_bytes: int,
) -> str:
    try:
        result = run_limited_process(
            [executable, "--version"],
            timeout_seconds=timeout_seconds,
            max_memory_bytes=max_memory_bytes,
            max_file_bytes=max_file_bytes,
        )
    except LimitedProcessTimeout:
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def snapshot_source(
    *,
    url: str,
    snapshot_path: Path,
    adapter: str,
    max_bytes: int,
) -> FetchedSource:
    """Load a browser/tool-produced snapshot without storing authentication data."""
    canonical = canonicalize_url(url)
    expanded_snapshot = snapshot_path.expanduser()
    if expanded_snapshot.is_symlink():
        raise IngestError("snapshot file must not be a symbolic link")
    snapshot = expanded_snapshot.resolve()
    if not snapshot.is_file():
        raise IngestError(f"snapshot file does not exist: {snapshot}")
    body = snapshot.read_bytes()
    if len(body) > max_bytes:
        raise IngestError(f"snapshot exceeds {max_bytes} bytes")

    suffix = snapshot.suffix.lower()
    content_types = {
        ".html": "text/html",
        ".htm": "text/html",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".pdf": "application/pdf",
    }
    content_type = content_types.get(suffix)
    if not content_type:
        raise IngestError("snapshot must be HTML, Markdown, or PDF")
    return FetchedSource(
        requested_url=canonical,
        final_url=canonical,
        status=0,
        headers={"content-type": content_type},
        body=body,
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        adapter=adapter,
    )


def _copy_snapshot_assets(
    snapshot_path: Path,
    output_dir: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> dict[str, str]:
    """Copy a converter's sibling image directory and return content hashes."""
    images = snapshot_path.parent / "images"
    if not images.is_dir():
        return {}
    if images.is_symlink() or any(path.is_symlink() for path in images.rglob("*")):
        raise IngestError("snapshot image directory must not contain symlinks")
    assets = sorted(path for path in images.rglob("*") if path.is_file())
    if len(assets) > max_files:
        raise IngestError(f"snapshot assets exceed the {max_files}-file limit")
    total_bytes = sum(path.stat().st_size for path in assets)
    if total_bytes > max_bytes:
        raise IngestError(f"snapshot assets exceed the {max_bytes}-byte limit")
    if not assets:
        return {}
    destination = output_dir / "images"
    for asset in assets:
        relative = asset.relative_to(images)
        copied_asset = destination / relative
        copied_asset.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(asset, copied_asset)
    hashes: dict[str, str] = {}
    for asset in sorted(path for path in destination.rglob("*") if path.is_file()):
        relative = str(asset.relative_to(output_dir))
        hashes[relative] = hashlib.sha256(asset.read_bytes()).hexdigest()
    return hashes


def extract_source(
    fetched: FetchedSource,
    output_dir: Path,
    *,
    extraction_timeout_seconds: float = 60,
    max_pdf_pages: int = 500,
    max_extracted_characters: int = 2_000_000,
    max_extraction_memory_bytes: int = 536_870_912,
) -> ExtractedSource:
    """Write the raw response and extract deterministic Markdown."""
    content_type = fetched.headers.get("content-type", "").lower()
    url_path = urllib.parse.urlsplit(fetched.final_url).path.lower()
    is_pdf = url_path.endswith(".pdf") or "application/pdf" in content_type
    is_markdown = url_path.endswith(".md") or "text/markdown" in content_type
    raw_filename = (
        "source.pdf" if is_pdf else "source.md" if is_markdown else "source.html"
    )
    raw_path = output_dir / raw_filename
    raw_path.write_bytes(fetched.body)
    extracted_path = output_dir / "extracted.md"

    if is_pdf:
        try:
            extraction = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "vault_pr.pdf_extract",
                    str(raw_path),
                    str(extracted_path),
                    "--max-pages",
                    str(max_pdf_pages),
                    "--max-characters",
                    str(max_extracted_characters),
                    "--max-memory-bytes",
                    str(max_extraction_memory_bytes),
                    "--cpu-seconds",
                    str(extraction_timeout_seconds),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=extraction_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise IngestError(
                f"PDF extraction exceeded {extraction_timeout_seconds} seconds"
            ) from exc
        if extraction.returncode != 0 or not extracted_path.is_file():
            detail = (extraction.stderr.strip() or extraction.stdout.strip())[:2000]
            raise IngestError(f"PDF extraction failed: {detail}")
        try:
            metadata = json.loads(extraction.stdout)
            title = normalize_title(metadata["title"])
            extractor_version = str(metadata["extractor_version"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise IngestError("PDF extractor returned invalid metadata") from exc
        return ExtractedSource(
            title=title,
            raw_filename=raw_filename,
            extracted_filename=extracted_path.name,
            extractor="pypdf",
            extractor_version=extractor_version,
        )

    if is_markdown:
        text = fetched.body.decode("utf-8", errors="replace")
        if len(text) > max_extracted_characters:
            raise IngestError(
                f"Markdown exceeds {max_extracted_characters} extracted characters"
            )
        extracted_path.write_text(text, encoding="utf-8")
        title = _markdown_title(text) or Path(url_path).stem or "Untitled source"
        return ExtractedSource(
            title=title,
            raw_filename=raw_filename,
            extracted_filename=extracted_path.name,
            extractor=(
                "wechat-markdown-snapshot"
                if fetched.adapter == "wechat"
                else "authenticated-snapshot"
                if fetched.adapter == "authenticated"
                else "direct-markdown"
            ),
            extractor_version="1",
        )

    executable = shutil.which("defuddle")
    if not executable:
        raise IngestError("required tool missing: defuddle")
    try:
        extraction = run_limited_process(
            [executable, "parse", str(raw_path), "--md", "-o", str(extracted_path)],
            timeout_seconds=extraction_timeout_seconds,
            max_memory_bytes=max_extraction_memory_bytes,
            max_file_bytes=max(max_extracted_characters * 4, 16_384),
        )
    except LimitedProcessTimeout as exc:
        raise IngestError(
            f"Defuddle extraction exceeded {extraction_timeout_seconds} seconds"
        ) from exc
    if extraction.returncode != 0 or not extracted_path.is_file():
        detail = extraction.stderr.strip() or extraction.stdout.strip()
        raise IngestError(f"Defuddle extraction failed: {detail}")
    if extracted_path.stat().st_size > max_extracted_characters * 4:
        raise IngestError("Defuddle output exceeds the extracted content limit")
    extracted_text = extracted_path.read_text(encoding="utf-8")
    if len(extracted_text) > max_extracted_characters:
        raise IngestError("Defuddle output exceeds the extracted content limit")
    if len(extracted_text.strip()) < 100:
        raise IngestError("Defuddle output is too short to trust")

    try:
        title_result = run_limited_process(
            [executable, "parse", str(raw_path), "-p", "title"],
            timeout_seconds=extraction_timeout_seconds,
            max_memory_bytes=max_extraction_memory_bytes,
            max_file_bytes=16_384,
        )
    except LimitedProcessTimeout:
        title_result = LimitedProcessResult(1, "", "title extraction timed out")
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
        extractor_version=_defuddle_version(
            executable,
            extraction_timeout_seconds,
            max_extraction_memory_bytes,
            16_384,
        ),
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


def _source_note_has_id(path: Path, source_id: str) -> bool:
    try:
        return source_note_has_id(path, source_id)
    except CaptureError as exc:
        raise IngestError(str(exc)) from exc


def _source_note_candidates(note_dir: Path, url: str) -> list[Path]:
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


def _versioned_source_note_path(
    note_dir: Path, title: str, url: str, version: str
) -> Path:
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

    candidates = _source_note_candidates(note_dir, url)
    matching: list[Path] = []
    for candidate in candidates:
        if _source_note_has_id(candidate, source_id):
            matching.append(candidate)
    if len(matching) > 1:
        raise IngestError(f"multiple source notes contain source id {source_id}")
    if matching:
        return matching[0]
    if not candidates:
        return note_dir / safe_note_filename(title, url)
    version = source_id.removeprefix("src_")
    versioned = _versioned_source_note_path(note_dir, title, url, version)
    if versioned.exists() or versioned.is_symlink():
        raise IngestError(
            f"versioned source note path is already occupied: {versioned}"
        )
    return versioned


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
    apply: bool,
    title_override: str | None = None,
    snapshot_path: Path | None = None,
    adapter: str = "auto",
) -> dict[str, Any]:
    """Prepare a URL capture and optionally write it to the configured vault."""
    vault = load_vault_path(config)
    if not vault.is_dir():
        raise ConfigError(f"vault path does not exist: {vault}")
    if apply:
        require_clean_git_root(vault)

    sources_config = config.get("sources", {})
    try:
        timeout_seconds = float(sources_config.get("timeout_seconds", 30))
        max_bytes = int(sources_config.get("max_download_bytes", 10_000_000))
        max_asset_files = int(sources_config.get("max_asset_files", 200))
        max_asset_bytes = int(sources_config.get("max_asset_bytes", 50_000_000))
        extraction_timeout_seconds = float(
            sources_config.get("extraction_timeout_seconds", 60)
        )
        max_pdf_pages = int(sources_config.get("max_pdf_pages", 500))
        max_extracted_characters = int(
            sources_config.get("max_extracted_characters", 2_000_000)
        )
        max_extraction_memory_bytes = int(
            sources_config.get("max_extraction_memory_bytes", 536_870_912)
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError("invalid source fetch or extraction limits") from exc
    if (
        timeout_seconds <= 0
        or max_bytes < 1
        or max_asset_files < 1
        or max_asset_bytes < 1
        or extraction_timeout_seconds <= 0
        or max_pdf_pages < 1
        or max_extracted_characters < 1
        or max_extraction_memory_bytes < 1
    ):
        raise ConfigError("source fetch and extraction limits must be positive")
    with tempfile.TemporaryDirectory(prefix="vault-pr-ingest-") as temp_dir_string:
        temp_dir = Path(temp_dir_string)
        canonical = canonicalize_url(url)
        hostname = urllib.parse.urlsplit(canonical).hostname
        effective_snapshot = snapshot_path
        effective_adapter = adapter
        if effective_adapter == "auto":
            effective_adapter = (
                "wechat"
                if hostname == "mp.weixin.qq.com"
                else "authenticated"
                if snapshot_path
                else "http"
            )
        if effective_adapter not in {"http", "wechat", "authenticated"}:
            raise IngestError(f"unsupported source adapter: {effective_adapter}")
        if hostname == "mp.weixin.qq.com" and effective_adapter != "wechat":
            raise IngestError(
                "mp.weixin.qq.com sources must use the offline WeChat snapshot adapter"
            )
        if effective_adapter == "http" and effective_snapshot is not None:
            raise IngestError("http adapter does not accept --snapshot")
        if effective_adapter == "authenticated" and effective_snapshot is None:
            raise IngestError("authenticated adapter requires --snapshot")
        if effective_adapter == "wechat":
            if hostname != "mp.weixin.qq.com":
                raise IngestError(
                    "WeChat adapter only accepts mp.weixin.qq.com source URLs"
                )
            if effective_snapshot is None:
                raise IngestError(
                    "WeChat adapter requires a local HTML, Markdown, or PDF --snapshot; "
                    "vault-pr does not invoke URL-fetching converters"
                )
        if effective_snapshot is not None:
            fetched = snapshot_source(
                url=canonical,
                snapshot_path=effective_snapshot,
                adapter=effective_adapter,
                max_bytes=max_bytes,
            )
        else:
            fetched = fetch_url(
                canonical, timeout_seconds=timeout_seconds, max_bytes=max_bytes
            )
        fetched = replace(
            fetched,
            requested_url=canonicalize_url(fetched.requested_url),
            final_url=canonicalize_url(fetched.final_url),
        )
        extracted = extract_source(
            fetched,
            temp_dir,
            extraction_timeout_seconds=extraction_timeout_seconds,
            max_pdf_pages=max_pdf_pages,
            max_extracted_characters=max_extracted_characters,
            max_extraction_memory_bytes=max_extraction_memory_bytes,
        )
        asset_hashes = (
            _copy_snapshot_assets(
                effective_snapshot,
                temp_dir,
                max_files=max_asset_files,
                max_bytes=max_asset_bytes,
            )
            if effective_snapshot is not None and effective_adapter == "wechat"
            else {}
        )
        title = normalize_title(title_override or extracted.title)
        raw_path = temp_dir / extracted.raw_filename
        extracted_path = temp_dir / extracted.extracted_filename
        raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        extracted_sha256 = hashlib.sha256(extracted_path.read_bytes()).hexdigest()
        identity_base = {
            "schema_version": 2,
            "requested_url": fetched.requested_url,
            "final_url": fetched.final_url,
            "adapter": fetched.adapter,
            "raw_sha256": raw_sha256,
            "extracted_sha256": extracted_sha256,
            "asset_sha256": asset_hashes,
            "artifacts": {
                "raw": extracted.raw_filename,
                "extracted": extracted.extracted_filename,
            },
        }
        archive_root = source_archive_dir(config, vault)
        note_dir = source_notes_dir(config, vault)
        candidates = _source_note_candidates(note_dir, fetched.requested_url)
        matched: list[tuple[Path, str, str, dict[str, Any]]] = []
        for candidate in candidates:
            candidate_identity = {
                **identity_base,
                "artifacts": {
                    **identity_base["artifacts"],
                    "source_note": str(candidate.relative_to(vault)),
                },
            }
            candidate_digest = source_identity_digest(candidate_identity)
            candidate_source_id = f"src_{candidate_digest[:20]}"
            if _source_note_has_id(candidate, candidate_source_id):
                matched.append(
                    (
                        candidate,
                        candidate_digest,
                        candidate_source_id,
                        candidate_identity,
                    )
                )
            elif (archive_root / candidate_source_id).exists():
                raise IngestError(
                    f"source note linkage is invalid for capture: {candidate_source_id}"
                )
        if len(matched) > 1:
            raise IngestError("multiple source notes match the same immutable source")
        if matched:
            note_path, source_identity, source_id, identity_payload = matched[0]
        else:
            content_version = source_identity_digest(identity_base)[:20]
            note_path = (
                note_dir / safe_note_filename(title, fetched.requested_url)
                if not candidates
                else _versioned_source_note_path(
                    note_dir, title, fetched.requested_url, content_version
                )
            )
            identity_payload = {
                **identity_base,
                "artifacts": {
                    **identity_base["artifacts"],
                    "source_note": str(note_path.relative_to(vault)),
                },
            }
            source_identity = source_identity_digest(identity_payload)
            source_id = f"src_{source_identity[:20]}"
        if not note_path_allowed(config, vault, note_path):
            raise ConfigError(
                "sources.visible_notes_dir must produce an allowed Markdown note path"
            )
        capture_dir = archive_root / source_id
        note_content = build_source_note(
            title=title,
            source_id=source_id,
            fetched=fetched,
            extracted=extracted,
            raw_sha256=raw_sha256,
            extracted_sha256=extracted_sha256,
            note_path=note_path,
            capture_dir=capture_dir,
        )
        manifest = {
            "schema_version": 2,
            "source_id": source_id,
            "requested_url": fetched.requested_url,
            "final_url": fetched.final_url,
            "captured_at": fetched.captured_at,
            "http_status": fetched.status,
            "response_headers": fetched.headers,
            "title": title,
            "raw_sha256": raw_sha256,
            "extracted_sha256": extracted_sha256,
            "extractor": extracted.extractor,
            "extractor_version": extracted.extractor_version,
            "adapter": fetched.adapter,
            "asset_sha256": asset_hashes,
            "artifacts": identity_payload["artifacts"],
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for staged_file in temp_dir.rglob("*"):
            if staged_file.is_file():
                staged_file.chmod(0o644)

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
                if not _source_note_has_id(note_path, source_id):
                    raise IngestError(
                        f"source note already exists for different content: {note_path}"
                    )
            capture_exists = capture_dir.exists()
            note_exists = note_path.exists()
            if capture_exists:
                try:
                    load_validated_capture(
                        capture_dir,
                        source_id=source_id,
                        vault=vault,
                        expected_manifest=manifest,
                    )
                except CaptureError as exc:
                    raise IngestError(str(exc)) from exc

            capture_files = [
                path for path in sorted(temp_dir.rglob("*")) if path.is_file()
            ]
            affected: set[Path] = set()
            final_sha256: dict[Path, str | None] = {}
            final_mode: dict[Path, str | None] = {}
            if not capture_exists:
                for staged_file in capture_files:
                    destination = capture_dir / staged_file.relative_to(temp_dir)
                    affected.add(destination)
                    final_sha256[destination] = hashlib.sha256(
                        staged_file.read_bytes()
                    ).hexdigest()
                    final_mode[destination] = "100644"
            if not note_exists:
                affected.add(note_path)
                final_sha256[note_path] = hashlib.sha256(
                    note_content.encode("utf-8")
                ).hexdigest()
                final_mode[note_path] = "100644"
            if not affected:
                result["status"] = "captured"
                return result

            try:
                active_dir = prepare_active_transaction(
                    vault=vault,
                    git_dir=_git_dir(vault),
                    transaction_id=f"txn_ingest_{source_id.removeprefix('src_')}",
                    transaction_sha256=source_identity,
                    affected=affected,
                    expected_original_sha256={path: None for path in affected},
                    expected_original_mode={path: None for path in affected},
                    final_sha256=final_sha256,
                    final_mode=final_mode,
                )
            except RecoveryError as exc:
                raise IngestError(str(exc)) from exc

            try:
                if not capture_exists:
                    staged_capture = active_dir / "capture-staging"
                    shutil.copytree(temp_dir, staged_capture)
                    for staged_file in capture_files:
                        relative = staged_file.relative_to(temp_dir)
                        copied = staged_capture / relative
                        destination = capture_dir / relative
                        copied_sha256 = hashlib.sha256(copied.read_bytes()).hexdigest()
                        copied_mode = (
                            "100755" if copied.stat().st_mode & 0o111 else "100644"
                        )
                        if (
                            copied_sha256 != final_sha256[destination]
                            or copied_mode != final_mode[destination]
                        ):
                            raise IngestError(
                                f"failed to verify staged capture file: {relative}"
                            )
                    capture_dir.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged_capture, capture_dir)

                if not note_exists:
                    _atomic_note_write(
                        note_path,
                        note_content,
                        active_dir / "write-staging" / "source-note",
                    )
                mark_active_applied(active_dir)
            except Exception as exc:
                try:
                    rollback_active_transaction(vault, active_dir)
                except RecoveryError as recovery_exc:
                    raise IngestError(
                        "ingest failed; durable recovery is required from "
                        f"{active_dir}: {recovery_exc}"
                    ) from recovery_exc
                if isinstance(exc, IngestError):
                    raise
                if isinstance(exc, RecoveryError):
                    raise IngestError(str(exc)) from exc
                raise IngestError(f"failed to write ingest artifacts: {exc}") from exc

        result["status"] = "captured"
        result["transaction_id"] = active_dir.name
        result["requires_commit"] = True
        return result
