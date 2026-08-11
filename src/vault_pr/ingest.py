"""Ingest URLs, local files, and Snapshot Bundles as schema-v3 captures."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capture import (
    CaptureError,
    load_validated_capture,
    source_identity_digest,
    source_identity_payload,
)
from .config import (
    ConfigError,
    load_vault_path,
    note_path_allowed,
    source_archive_dir,
    source_notes_dir,
)
from .errors import IngestError
from .git import GitSafetyError, run_git
from .mutation import (
    MutationError,
    execute_journaled_mutation,
    inspect_journaled_mutation,
    mutation_response,
)
from .recovery import committed_file_matches_worktree
from .acquisition import (
    FetchedSource,
    canonicalize_url,
    extract_source,
    fetch_url,
    normalize_title,
)
from .snapshot_bundle import inspect_single_snapshot, validate_snapshot_bundle
from .source_files import materialize_regular_tree, sha256_regular_file_identity
from .source_note import (
    build_source_note,
    resolve_source_note,
    safe_local_note_filename,
    source_note_has_id,
    source_note_path,
)


def _git_dir(vault: Path) -> Path:
    result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        raise IngestError("unable to resolve vault Git directory")
    return Path(result.stdout.strip()).resolve()


def _limits(config: dict[str, Any]) -> dict[str, int | float]:
    sources = config.get("sources", {})
    try:
        values: dict[str, int | float] = {
            "timeout": float(sources.get("timeout_seconds", 30)),
            "primary_bytes": int(sources.get("max_download_bytes", 10_000_000)),
            "asset_files": int(sources.get("max_asset_files", 200)),
            "asset_bytes": int(sources.get("max_asset_bytes", 50_000_000)),
            "extraction_timeout": float(
                sources.get("extraction_timeout_seconds", 60)
            ),
            "pdf_pages": int(sources.get("max_pdf_pages", 500)),
            "characters": int(
                sources.get("max_extracted_characters", 2_000_000)
            ),
            "memory": int(
                sources.get("max_extraction_memory_bytes", 536_870_912)
            ),
        }
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError("invalid source fetch or extraction limits") from exc
    if any(value <= 0 for value in values.values()):
        raise ConfigError("source fetch and extraction limits must be positive")
    return values


def _acquisition(
    adapter: str, provider: dict[str, str] | None
) -> dict[str, Any]:
    return {
        "adapter": adapter,
        **({"provider": provider} if provider is not None else {}),
    }


def _install_note(path: Path, content: str, staging: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging.write_text(content, encoding="utf-8")
        staging.chmod(0o644)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


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


def ingest_source(
    *,
    config: dict[str, Any],
    source_reference: str,
    apply: bool,
    title_override: str | None = None,
    snapshot_path: Path | None = None,
    adapter: str = "auto",
    logical_name: str | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Prepare or write one immutable Capture and one visible Source Note."""
    vault = load_vault_path(config)
    if not vault.is_dir():
        raise ConfigError("configured vault path does not exist")
    limits = _limits(config)

    with tempfile.TemporaryDirectory(prefix="vault-pr-ingest-") as temporary:
        work = Path(temporary)
        capture_stage = work / "capture"
        capture_stage.mkdir()
        extraction_stage = work / "extraction"
        extraction_stage.mkdir()
        effective_adapter = adapter
        if effective_adapter == "auto":
            if snapshot_path is not None:
                route_url = canonicalize_url(source_reference)
                effective_adapter = (
                    "wechat"
                    if urllib.parse.urlsplit(route_url).hostname
                    == "mp.weixin.qq.com"
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
                    "WeChat adapter requires a local file or Snapshot Bundle "
                    "--snapshot"
                )

        captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        provider: dict[str, str] | None = None
        secondary: list[dict[str, Any]] = []
        source_kind = "url"
        access_kind = "public"
        primary: dict[str, Any] | None = None

        if effective_adapter == "http":
            assert canonical is not None
            fetched = fetch_url(
                canonical,
                timeout_seconds=float(limits["timeout"]),
                max_bytes=int(limits["primary_bytes"]),
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
            extracted = extract_source(
                fetched,
                capture_stage,
                extraction_timeout_seconds=float(limits["extraction_timeout"]),
                max_pdf_pages=int(limits["pdf_pages"]),
                max_extracted_characters=int(limits["characters"]),
                max_extraction_memory_bytes=int(limits["memory"]),
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
                bundle = validate_snapshot_bundle(
                    local_input,
                    max_primary_bytes=int(limits["primary_bytes"]),
                    max_files=int(limits["asset_files"]),
                    max_total_bytes=int(limits["asset_bytes"]),
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
                primary = inspect_single_snapshot(
                    local_input,
                    max_bytes=int(limits["primary_bytes"]),
                    destination=capture_stage / destination_name,
                )
                primary["path"] = destination_name
                primary_path = capture_stage / destination_name
            source_kind = (
                "local_file"
                if effective_adapter == "local_file"
                else "snapshot_bundle"
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
            extracted = extract_source(
                fetched,
                extraction_stage,
                extraction_timeout_seconds=float(limits["extraction_timeout"]),
                max_pdf_pages=int(limits["pdf_pages"]),
                max_extracted_characters=int(limits["characters"]),
                max_extraction_memory_bytes=int(limits["memory"]),
            )
            shutil.copy2(
                extraction_stage / extracted.extracted_filename,
                capture_stage / "extracted.md",
            )
            extracted = type(extracted)(
                title=extracted.title,
                raw_filename=str(primary["path"]),
                extracted_filename="extracted.md",
                extractor=extracted.extractor,
                extractor_version=extracted.extractor_version,
            )

        raw_path = capture_stage / extracted.raw_filename
        extracted_path = capture_stage / extracted.extracted_filename
        raw_sha256, raw_identity = sha256_regular_file_identity(
            raw_path, "primary"
        )
        extracted_sha256, extracted_identity = sha256_regular_file_identity(
            extracted_path, "extracted"
        )
        if raw_identity == extracted_identity:
            raise IngestError("capture primary and extracted artifacts alias")
        if primary is None:
            primary = {
                "path": extracted.raw_filename,
                "sha256": raw_sha256,
                "size": raw_path.stat().st_size,
                "mime": fetched.headers.get(
                    "content-type", "application/octet-stream"
                )
                .split(";", 1)[0]
                .strip(),
            }
        else:
            primary = {
                **primary,
                "path": extracted.raw_filename,
                "sha256": raw_sha256,
                "size": raw_path.stat().st_size,
            }
        extracted_record = {
            "path": extracted.extracted_filename,
            "sha256": extracted_sha256,
            "size": extracted_path.stat().st_size,
            "mime": "text/markdown",
        }
        source: dict[str, Any] = {
            "kind": source_kind,
            "access_kind": access_kind,
        }
        if effective_adapter == "local_file":
            source["logical_name"] = normalize_title(
                logical_name or Path(source_reference).name
            )
        else:
            assert canonical is not None
            source["requested_url"] = canonical
            source["final_url"] = (
                canonicalize_url(fetched.final_url)
                if effective_adapter == "http"
                else canonical
            )
        if supersedes is not None:
            if not re.fullmatch(r"src_[0-9a-f]{20}", supersedes):
                raise IngestError("supersedes must be a valid source_id")
            try:
                load_validated_capture(
                    source_archive_dir(config, vault) / supersedes,
                    source_id=supersedes,
                    vault=vault,
                )
            except CaptureError as exc:
                raise IngestError(f"supersedes capture is invalid: {exc}") from exc
            source["supersedes"] = supersedes

        identity = {
            "schema_version": 3,
            "source": source,
            "primary": primary,
            "files": secondary,
        }
        identity_sha256 = source_identity_digest(source_identity_payload(identity))
        source_id = f"src_{identity_sha256[:20]}"
        if supersedes == source_id:
            return {
                "status": "source_not_changed",
                "source_id": source_id,
                "supersedes": supersedes,
            }
        title = normalize_title(title_override or extracted.title)
        archive_root = source_archive_dir(config, vault)
        capture_dir = archive_root / source_id
        note_dir = source_notes_dir(config, vault)
        note_path = (
            note_dir / safe_local_note_filename(title, source_id)
            if effective_adapter == "local_file"
            else source_note_path(
                note_dir,
                title,
                str(source["requested_url"]),
                source_id=source_id,
            )
        )
        if not note_path_allowed(config, vault, note_path):
            raise ConfigError(
                "sources.visible_notes_dir must produce an allowed Markdown note path"
            )
        acquisition = _acquisition(effective_adapter, provider)
        manifest = {
            **identity,
            "source_id": source_id,
            "captured_at": captured_at,
            "title": title,
            "derived": {"extracted": extracted_record},
            "acquisition": acquisition,
        }
        note_content = build_source_note(
            title=title,
            source_id=source_id,
            source=source,
            captured_at=captured_at,
            acquisition=acquisition,
            primary=primary,
            extracted=extracted_record,
            note_path=note_path,
            capture_dir=capture_dir,
        )
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        (capture_stage / "manifest.json").write_bytes(manifest_bytes)
        staged_hashes = {
            Path(str(primary["path"])): str(primary["sha256"]),
            Path(str(extracted_record["path"])): str(extracted_record["sha256"]),
            **{
                Path(str(record["path"])): str(record["sha256"])
                for record in secondary
            },
            Path("manifest.json"): hashlib.sha256(manifest_bytes).hexdigest(),
        }
        materialize_regular_tree(capture_stage, staged_hashes)
        result = {
            "status": "planned",
            "source_id": source_id,
            "title": title,
            "capture_dir": str(capture_dir.relative_to(vault)),
            "source_note": str(note_path.relative_to(vault)),
        }
        if not apply:
            return result

        transaction_id = f"txn_ingest_{source_id.removeprefix('src_')}"
        request_sha256 = source_identity_digest(
            {
                "identity_sha256": identity_sha256,
                "title": title,
                "source_note": str(note_path.relative_to(vault)),
                "acquisition": acquisition,
                "primary": primary,
                "files": secondary,
                "derived": {"extracted": extracted_record},
            }
        )
        git_dir = _git_dir(vault)
        try:
            existing = inspect_journaled_mutation(
                vault=vault,
                git_dir=git_dir,
                operation="ingest",
                transaction_id=transaction_id,
                transaction_sha256=identity_sha256,
                request_sha256=request_sha256,
            )
        except MutationError as exc:
            raise IngestError(str(exc)) from exc
        if existing is not None:
            if existing["status"] == "already_committed":
                try:
                    load_validated_capture(
                        capture_dir,
                        source_id=source_id,
                        vault=vault,
                    )
                    existing_note = resolve_source_note(config, vault, source_id)
                except (CaptureError, IngestError) as exc:
                    raise IngestError(
                        f"existing capture integrity failed: {exc}"
                    ) from exc
                if not source_note_has_id(existing_note, source_id):
                    raise IngestError("existing Source Note lost its source_id")
            return mutation_response(existing, applied_status="captured")

        if capture_dir.exists():
            try:
                existing_manifest = load_validated_capture(
                    capture_dir,
                    source_id=source_id,
                    vault=vault,
                )
                existing_note = resolve_source_note(config, vault, source_id)
            except (CaptureError, IngestError) as exc:
                raise IngestError(
                    f"existing capture is incomplete or inconsistent: {exc}"
                ) from exc
            expected_without_capture_time = {
                key: value for key, value in manifest.items() if key != "captured_at"
            }
            existing_without_capture_time = {
                key: value
                for key, value in existing_manifest.items()
                if key != "captured_at"
            }
            if existing_without_capture_time != expected_without_capture_time:
                raise IngestError(
                    "existing capture metadata does not match the adoption request"
                )
            adoption_result = {
                **result,
                "status": "planned",
                "title": existing_manifest["title"],
                "source_note": str(existing_note.relative_to(vault)),
                "adopted_existing": True,
            }
            head = run_git(vault, "rev-parse", "--verify", "HEAD")
            if head.returncode != 0:
                raise IngestError(
                    "existing capture adoption requires a committed Git HEAD"
                )
            adoption_base = head.stdout.strip()
            adoption_result.update(
                {
                    "adopted_from_commit": adoption_base,
                    "capture_manifest_sha256": hashlib.sha256(
                        (capture_dir / "manifest.json").read_bytes()
                    ).hexdigest(),
                    "source_note_sha256": hashlib.sha256(
                        existing_note.read_bytes()
                    ).hexdigest(),
                }
            )

            def validate_adoption() -> None:
                current_head = run_git(vault, "rev-parse", "--verify", "HEAD")
                if (
                    current_head.returncode != 0
                    or current_head.stdout.strip() != adoption_base
                ):
                    raise IngestError("vault HEAD changed before capture adoption")
                current_manifest = load_validated_capture(
                    capture_dir,
                    source_id=source_id,
                    vault=vault,
                )
                current_without_capture_time = {
                    key: value
                    for key, value in current_manifest.items()
                    if key != "captured_at"
                }
                if current_without_capture_time != expected_without_capture_time:
                    raise IngestError(
                        "existing capture changed before adoption"
                    )
                current_note = resolve_source_note(config, vault, source_id)
                if current_note != existing_note or not source_note_has_id(
                    current_note, source_id
                ):
                    raise IngestError(
                        "existing Source Note changed before adoption"
                    )
                governed_paths = {
                    capture_dir / relative for relative in staged_hashes
                } | {existing_note}
                if not all(
                    committed_file_matches_worktree(vault, path)
                    for path in governed_paths
                ):
                    raise IngestError(
                        "existing capture adoption requires every artifact and "
                        "Source Note to match HEAD"
                    )

            validate_adoption()
            try:
                outcome = execute_journaled_mutation(
                    vault=vault,
                    git_dir=git_dir,
                    transaction_id=transaction_id,
                    transaction_sha256=identity_sha256,
                    request_sha256=request_sha256,
                    operation="ingest",
                    receipt_data=adoption_result,
                    affected=set(),
                    expected_original_sha256={},
                    expected_original_mode={},
                    final_sha256={},
                    final_mode={},
                    write=lambda _active_dir: None,
                    precondition=validate_adoption,
                    postcondition=lambda _active_dir: validate_adoption(),
                    completion_path=str(
                        (capture_dir / "manifest.json").relative_to(vault)
                    ),
                )
            except (GitSafetyError, MutationError) as exc:
                raise IngestError(
                    f"unable to adopt existing capture: {exc}"
                ) from exc
            return mutation_response(outcome, applied_status="adopted")
        if note_path.exists() and not source_note_has_id(note_path, source_id):
            raise IngestError(
                "source note already exists for different content: "
                + str(note_path.relative_to(vault))
            )
        affected = {capture_dir / relative for relative in staged_hashes}
        affected.add(note_path)
        final_sha256 = {
            capture_dir / relative: digest for relative, digest in staged_hashes.items()
        }
        final_sha256[note_path] = hashlib.sha256(
            note_content.encode("utf-8")
        ).hexdigest()
        final_mode = {path: "100644" for path in affected}

        def write(active_dir: Path) -> None:
            staged_capture = active_dir / "capture-staging"
            materialize_regular_tree(
                capture_stage,
                staged_hashes,
                destination=staged_capture,
            )
            capture_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_capture, capture_dir)
            _install_note(
                note_path,
                note_content,
                active_dir / "write-staging" / "source-note",
            )

        def postcondition(_active_dir: Path) -> None:
            load_validated_capture(
                capture_dir,
                source_id=source_id,
                vault=vault,
                expected_manifest=manifest,
            )
            if not source_note_has_id(note_path, source_id):
                raise IngestError("written Source Note lost its source_id")

        try:
            outcome = execute_journaled_mutation(
                vault=vault,
                git_dir=git_dir,
                transaction_id=transaction_id,
                transaction_sha256=identity_sha256,
                request_sha256=request_sha256,
                operation="ingest",
                receipt_data=result,
                affected=affected,
                expected_original_sha256={path: None for path in affected},
                expected_original_mode={path: None for path in affected},
                final_sha256=final_sha256,
                final_mode=final_mode,
                write=write,
                postcondition=postcondition,
                completion_path=str(
                    (capture_dir / "manifest.json").relative_to(vault)
                ),
            )
        except (GitSafetyError, MutationError) as exc:
            detail = str(exc)
            if detail.startswith("mutation failed:"):
                detail = "failed to write ingest artifacts:" + detail.removeprefix(
                    "mutation failed:"
                )
            raise IngestError(detail) from exc
        return mutation_response(outcome, applied_status="captured")
