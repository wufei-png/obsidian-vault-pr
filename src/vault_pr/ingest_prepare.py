"""Typed adapter routing and local preparation for one Capture."""

from __future__ import annotations

import os
import shutil
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .acquisition import ExtractedSource, FetchedSource, canonicalize_url
from .config import ConfigError
from .errors import IngestError


FetchSource = Callable[..., FetchedSource]
ExtractSource = Callable[..., ExtractedSource]
ValidateBundle = Callable[..., dict[str, Any]]
InspectSnapshot = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class SourceLimits:
    timeout_seconds: float
    primary_bytes: int
    asset_files: int
    asset_bytes: int
    extraction_timeout_seconds: float
    pdf_pages: int
    extracted_characters: int
    extraction_memory_bytes: int


@dataclass(frozen=True)
class AcquiredCapture:
    adapter: str
    canonical_url: str | None
    captured_at: str
    provider: dict[str, str] | None
    secondary_files: list[dict[str, Any]]
    source_kind: str
    access_kind: str
    primary: dict[str, Any] | None
    fetched: FetchedSource
    extracted: ExtractedSource


def source_limits(config: dict[str, Any]) -> SourceLimits:
    sources = config.get("sources", {})
    try:
        limits = SourceLimits(
            timeout_seconds=float(sources.get("timeout_seconds", 30)),
            primary_bytes=int(sources.get("max_download_bytes", 10_000_000)),
            asset_files=int(sources.get("max_asset_files", 200)),
            asset_bytes=int(sources.get("max_asset_bytes", 50_000_000)),
            extraction_timeout_seconds=float(
                sources.get("extraction_timeout_seconds", 60)
            ),
            pdf_pages=int(sources.get("max_pdf_pages", 500)),
            extracted_characters=int(
                sources.get("max_extracted_characters", 2_000_000)
            ),
            extraction_memory_bytes=int(
                sources.get("max_extraction_memory_bytes", 536_870_912)
            ),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError("invalid source fetch or extraction limits") from exc
    if any(
        value <= 0
        for value in (
            limits.timeout_seconds,
            limits.primary_bytes,
            limits.asset_files,
            limits.asset_bytes,
            limits.extraction_timeout_seconds,
            limits.pdf_pages,
            limits.extracted_characters,
            limits.extraction_memory_bytes,
        )
    ):
        raise ConfigError("source fetch and extraction limits must be positive")
    return limits


def _local_fetched(
    *,
    primary_path: Path,
    mime: str,
    display_name: str,
    adapter: str,
    captured_at: str,
) -> FetchedSource:
    return FetchedSource(
        requested_url=display_name,
        final_url=display_name,
        status=0,
        headers={"content-type": mime},
        body=primary_path.read_bytes(),
        captured_at=captured_at,
        adapter=adapter,
    )


def acquire_capture(
    *,
    source_reference: str,
    snapshot_path: Path | None,
    adapter: str,
    logical_name: str | None,
    capture_stage: Path,
    extraction_stage: Path,
    limits: SourceLimits,
    fetcher: FetchSource,
    extractor: ExtractSource,
    bundle_validator: ValidateBundle,
    snapshot_inspector: InspectSnapshot,
) -> AcquiredCapture:
    """Resolve one adapter and materialize its declared artifacts into staging."""
    effective_adapter = adapter
    if effective_adapter == "auto":
        if snapshot_path is not None:
            route_url = canonicalize_url(source_reference)
            effective_adapter = (
                "wechat"
                if urllib.parse.urlsplit(route_url).hostname == "mp.weixin.qq.com"
                else "authenticated"
            )
        else:
            effective_adapter = (
                "local_file"
                if Path(source_reference).expanduser().is_file()
                else "http"
            )
    if effective_adapter not in {
        "http",
        "wechat",
        "authenticated",
        "local_file",
    }:
        raise IngestError(f"unsupported source adapter: {effective_adapter}")
    if effective_adapter == "local_file" and snapshot_path is not None:
        raise IngestError("local_file adapter does not accept --snapshot")

    canonical: str | None = None
    hostname: str | None = None
    if effective_adapter != "local_file":
        canonical = canonicalize_url(source_reference)
        hostname = urllib.parse.urlsplit(canonical).hostname
    if hostname == "mp.weixin.qq.com" and effective_adapter != "wechat":
        raise IngestError(
            "mp.weixin.qq.com sources must use the offline WeChat snapshot adapter"
        )
    if effective_adapter == "http" and snapshot_path is not None:
        raise IngestError("http adapter does not accept --snapshot")
    if effective_adapter == "authenticated" and snapshot_path is None:
        raise IngestError("authenticated adapter requires --snapshot")
    if effective_adapter == "wechat":
        if hostname != "mp.weixin.qq.com":
            raise IngestError(
                "WeChat adapter only accepts mp.weixin.qq.com source URLs"
            )
        if snapshot_path is None:
            raise IngestError(
                "WeChat adapter requires a local file or Snapshot Bundle --snapshot"
            )

    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    provider: dict[str, str] | None = None
    secondary: list[dict[str, Any]] = []
    source_kind = "url"
    access_kind = "public"
    primary: dict[str, Any] | None = None

    if effective_adapter == "http":
        assert canonical is not None
        fetched = fetcher(
            canonical,
            timeout_seconds=limits.timeout_seconds,
            max_bytes=limits.primary_bytes,
        )
        fetched = FetchedSource(
            requested_url=canonicalize_url(fetched.requested_url),
            final_url=canonicalize_url(fetched.final_url),
            status=fetched.status,
            headers=fetched.headers,
            body=fetched.body,
            captured_at=fetched.captured_at,
            adapter="http",
        )
        captured_at = fetched.captured_at
        extracted = extractor(
            fetched,
            capture_stage,
            extraction_timeout_seconds=limits.extraction_timeout_seconds,
            max_pdf_pages=limits.pdf_pages,
            max_extracted_characters=limits.extracted_characters,
            max_extraction_memory_bytes=limits.extraction_memory_bytes,
        )
    else:
        local_input = (
            Path(source_reference).expanduser()
            if effective_adapter == "local_file"
            else snapshot_path
        )
        assert local_input is not None
        local_input = Path(os.path.abspath(local_input.expanduser()))
        if local_input.is_dir():
            if effective_adapter == "local_file":
                raise IngestError("local_file adapter requires one regular file")
            bundle = bundle_validator(
                local_input,
                max_primary_bytes=limits.primary_bytes,
                max_files=limits.asset_files,
                max_total_bytes=limits.asset_bytes,
                destination=capture_stage,
            )
            (capture_stage / "snapshot-manifest.json").unlink(missing_ok=True)
            primary = dict(bundle["primary"])
            secondary = [dict(record) for record in bundle["files"]]
            provider = dict(bundle["provider"])
            primary_path = capture_stage / str(primary["path"])
        else:
            destination_name = (
                "source" + local_input.suffix.lower()
                if local_input.suffix
                else "source.bin"
            )
            primary = snapshot_inspector(
                local_input,
                max_bytes=limits.primary_bytes,
                destination=capture_stage / destination_name,
            )
            primary["path"] = destination_name
            primary_path = capture_stage / destination_name
        source_kind = (
            "local_file" if effective_adapter == "local_file" else "snapshot_bundle"
        )
        access_kind = (
            "local"
            if effective_adapter == "local_file"
            else "authenticated"
            if effective_adapter == "authenticated"
            else "public"
        )
        fetched = _local_fetched(
            primary_path=primary_path,
            mime=str(primary["mime"]),
            display_name=logical_name or local_input.name,
            adapter=effective_adapter,
            captured_at=captured_at,
        )
        extracted = extractor(
            fetched,
            extraction_stage,
            extraction_timeout_seconds=limits.extraction_timeout_seconds,
            max_pdf_pages=limits.pdf_pages,
            max_extracted_characters=limits.extracted_characters,
            max_extraction_memory_bytes=limits.extraction_memory_bytes,
        )
        shutil.copy2(
            extraction_stage / extracted.extracted_filename,
            capture_stage / "extracted.md",
        )
        extracted = ExtractedSource(
            title=extracted.title,
            raw_filename=str(primary["path"]),
            extracted_filename="extracted.md",
            extractor=extracted.extractor,
            extractor_version=extracted.extractor_version,
        )

    return AcquiredCapture(
        adapter=effective_adapter,
        canonical_url=canonical,
        captured_at=captured_at,
        provider=provider,
        secondary_files=secondary,
        source_kind=source_kind,
        access_kind=access_kind,
        primary=primary,
        fetched=fetched,
        extracted=extracted,
    )
