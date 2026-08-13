"""Acquire and extract untrusted source documents without writing a vault."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .errors import IngestError
from .process import (
    LimitedProcessResult,
    LimitedProcessTimeout,
    run_limited_process,
)
from .source_files import read_bounded_regular_file as _read_bounded_regular_file
from .source_note import normalize_title


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
MAX_PINNED_ADDRESS_ATTEMPTS = 8


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
    addresses: tuple[tuple[int, int, int, str, tuple[Any, ...]], ...]


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
    """Resolve once, reject every non-public answer, and retain pinned addresses."""
    canonical = canonicalize_url(url)
    parsed = urllib.parse.urlsplit(canonical)
    assert parsed.hostname is not None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise IngestError(f"unable to resolve URL hostname: {parsed.hostname}") from exc
    unique_addresses: list[tuple[int, int, int, str, tuple[Any, ...]]] = []
    seen: set[tuple[int, int, int, tuple[Any, ...]]] = set()
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise IngestError(f"URL resolves to a non-public address: {ip}")
        identity = (address[0], address[1], address[2], address[4])
        if identity not in seen:
            seen.add(identity)
            unique_addresses.append(address)
    if not addresses:
        raise IngestError(f"URL hostname has no addresses: {parsed.hostname}")
    return _ResolvedTarget(
        canonical,
        parsed,
        tuple(unique_addresses[:MAX_PINNED_ADDRESS_ATTEMPTS]),
    )


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


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout("source fetch timeout exceeded")
    return remaining


def _open_pinned_connection(
    target: _ResolvedTarget, timeout_seconds: float
) -> http.client.HTTPConnection:
    """Open HTTP(S) to the validated address while preserving TLS hostname checks."""
    deadline = time.monotonic() + timeout_seconds
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
            last_error: OSError | ssl.SSLError | None = None
            for address in target.addresses:
                remaining = _remaining_timeout(deadline)
                try:
                    raw_socket = _pinned_socket(address, remaining)
                    try:
                        raw_socket.settimeout(_remaining_timeout(deadline))
                        connection.sock = connection._context.wrap_socket(  # noqa: SLF001
                            raw_socket, server_hostname=hostname
                        )
                    except Exception:
                        raw_socket.close()
                        raise
                    return
                except (OSError, ssl.SSLError) as exc:
                    last_error = exc
            assert last_error is not None
            raise last_error

    else:
        connection = http.client.HTTPConnection(hostname, port, timeout=timeout_seconds)

        def connect() -> None:
            last_error: OSError | None = None
            for address in target.addresses:
                remaining = _remaining_timeout(deadline)
                try:
                    connection.sock = _pinned_socket(address, remaining)
                    return
                except OSError as exc:
                    last_error = exc
            assert last_error is not None
            raise last_error

    connection.connect = connect  # type: ignore[method-assign]
    return connection


def fetch_url(url: str, timeout_seconds: float, max_bytes: int) -> FetchedSource:
    """Fetch a public URL with address pinning, redirect, and size safeguards."""
    requested_url = canonicalize_url(url)
    current_url = requested_url
    deadline = time.monotonic() + timeout_seconds
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            target = _resolve_public_target(current_url)
            current_url = target.canonical_url
            request_target = urllib.parse.urlunsplit(
                ("", "", target.parsed.path or "/", target.parsed.query, "")
            )
            connection = _open_pinned_connection(
                target,
                _remaining_timeout(deadline),
            )
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
    snapshot = Path(os.path.abspath(snapshot_path.expanduser()))
    body = _read_bounded_regular_file(
        snapshot,
        max_bytes=max_bytes,
        label="snapshot",
    )

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
    content_type = (
        fetched.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    url_path = urllib.parse.urlsplit(fetched.final_url).path.lower()
    if content_type == "application/pdf":
        is_pdf = True
        is_markdown = False
    elif content_type == "text/markdown":
        is_pdf = False
        is_markdown = True
    elif content_type in {"text/html", "application/xhtml+xml"}:
        is_pdf = False
        is_markdown = False
    else:
        # Missing and text/plain response types are common for raw static files.
        # Only these weak signals may be refined by the final URL suffix.
        is_pdf = url_path.endswith(".pdf")
        is_markdown = url_path.endswith((".md", ".markdown"))
    raw_filename = (
        "source.pdf" if is_pdf else "source.md" if is_markdown else "source.html"
    )
    raw_path = output_dir / raw_filename
    raw_path.write_bytes(fetched.body)
    extracted_path = output_dir / "extracted.md"

    if is_pdf:
        try:
            extraction = run_limited_process(
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
                timeout_seconds=extraction_timeout_seconds,
                max_memory_bytes=max_extraction_memory_bytes,
                max_file_bytes=max(max_extracted_characters * 4, 16_384),
            )
        except LimitedProcessTimeout as exc:
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
    if not extracted_text.strip():
        raise IngestError("Defuddle output is empty")

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
