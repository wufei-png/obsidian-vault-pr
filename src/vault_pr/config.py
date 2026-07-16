"""Configuration loading and vault-relative path validation."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when the local project configuration is invalid."""


_CONFIG_FIELDS: dict[str, set[str]] = {
    "vault": {"path"},
    "sources": {
        "capture_archive_dir",
        "visible_notes_dir",
        "timeout_seconds",
        "max_download_bytes",
        "max_asset_files",
        "max_asset_bytes",
        "extraction_timeout_seconds",
        "max_pdf_pages",
        "max_extracted_characters",
        "max_extraction_memory_bytes",
    },
    "semantic": {"max_touched_notes"},
    "notes": {"include", "exclude"},
    "index": {
        "name",
        "collection",
        "mask",
        "context",
        "embed",
        "max_docs_per_batch",
        "max_retries",
        "retry_backoff_seconds",
    },
    "review": {
        "review_all_semantic_transactions",
        "max_revision_rounds",
    },
    "quality": {"rag_cases_path", "note_roles"},
    "autonomous_repair": {"enabled"},
}

_NOTE_ROLE_FIELDS = {
    "concept_types",
    "source_types",
    "index_types",
    "excluded_types",
}


@dataclass(frozen=True)
class IndexSpec:
    """The single qmd collection governed for one configured vault."""

    root_dir: Path
    collection: str
    context: str | None
    mask: str


@dataclass(frozen=True)
class ReviewPolicy:
    """Vault-wide semantic review requirements."""

    review_all_semantic_transactions: bool
    max_revision_rounds: int


def load_config(config_path: Path) -> dict[str, Any]:
    """Load a TOML configuration file."""
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML config: {config_path}") from exc
    _validate_config_shape(config)
    return config


def _validate_config_shape(config: dict[str, Any]) -> None:
    unknown_tables = sorted(set(config) - set(_CONFIG_FIELDS))
    if unknown_tables:
        raise ConfigError("unknown config table: " + ", ".join(unknown_tables))
    for table, allowed_fields in _CONFIG_FIELDS.items():
        value = config.get(table)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"{table} must be a table")
        unknown_fields = sorted(set(value) - allowed_fields)
        if unknown_fields:
            raise ConfigError(
                f"unknown {table} field: " + ", ".join(unknown_fields)
            )
    quality = config.get("quality")
    if isinstance(quality, dict) and "note_roles" in quality:
        note_roles = quality["note_roles"]
        if not isinstance(note_roles, dict):
            raise ConfigError("quality.note_roles must be a table")
        unknown_roles = sorted(set(note_roles) - _NOTE_ROLE_FIELDS)
        if unknown_roles:
            raise ConfigError(
                "unknown quality.note_roles field: " + ", ".join(unknown_roles)
            )
        for field, values in note_roles.items():
            if (
                not isinstance(values, list)
                or not all(
                    isinstance(value, str) and value.strip() for value in values
                )
            ):
                raise ConfigError(
                    f"quality.note_roles.{field} must be an array of strings"
                )
    for table, field in (
        ("review", "review_all_semantic_transactions"),
        ("autonomous_repair", "enabled"),
        ("index", "embed"),
    ):
        value = config.get(table, {}).get(field)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(f"{table}.{field} must be boolean")
    for table, field in (
        ("sources", "max_download_bytes"),
        ("sources", "max_asset_files"),
        ("sources", "max_asset_bytes"),
        ("sources", "max_pdf_pages"),
        ("sources", "max_extracted_characters"),
        ("sources", "max_extraction_memory_bytes"),
        ("semantic", "max_touched_notes"),
        ("index", "max_docs_per_batch"),
        ("index", "max_retries"),
        ("review", "max_revision_rounds"),
    ):
        value = config.get(table, {}).get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise ConfigError(f"{table}.{field} must be an integer")
    for field in ("timeout_seconds", "extraction_timeout_seconds"):
        value = config.get("sources", {}).get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ConfigError(f"sources.{field} must be a number")
    backoff = config.get("index", {}).get("retry_backoff_seconds")
    if backoff is not None and (
        not isinstance(backoff, list)
        or not all(
            not isinstance(value, bool) and isinstance(value, (int, float))
            for value in backoff
        )
    ):
        raise ConfigError(
            "index.retry_backoff_seconds must be an array of numbers"
        )


def load_vault_path(config: dict[str, Any]) -> Path:
    """Load and validate the configured target vault path."""
    try:
        raw_path = config["vault"]["path"]
    except (KeyError, TypeError) as exc:
        raise ConfigError("missing vault.path") from exc
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError("vault.path must be a non-empty string")
    return Path(raw_path).expanduser().resolve()


def vault_relative_path(vault: Path, raw_path: object, field: str) -> Path:
    """Resolve a configured relative path without allowing vault escape."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError(f"{field} must be a non-empty relative path")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise ConfigError(f"{field} must be relative to the vault")
    resolved = (vault / relative).resolve()
    if not resolved.is_relative_to(vault):
        raise ConfigError(f"{field} escapes the vault")
    return resolved


def source_notes_dir(config: dict[str, Any], vault: Path) -> Path:
    """Resolve the visible source-note directory for the configured vault."""
    try:
        raw_path = config["sources"]["visible_notes_dir"]
    except (KeyError, TypeError) as exc:
        raise ConfigError("missing sources.visible_notes_dir") from exc
    return vault_relative_path(vault, raw_path, "sources.visible_notes_dir")


def index_spec(config: dict[str, Any], vault: Path) -> IndexSpec:
    """Load the configured vault-wide qmd collection."""
    raw_index = config.get("index")
    if not isinstance(raw_index, dict):
        raise ConfigError("index must be a table")
    if "root_dir" in raw_index:
        raise ConfigError(
            "index.root_dir is not supported; qmd always indexes the vault root"
        )
    collection = raw_index.get("collection")
    if not isinstance(collection, str) or not collection.strip():
        raise ConfigError("index.collection must be a non-empty string")
    context = raw_index.get("context")
    if context is not None and (not isinstance(context, str) or not context.strip()):
        raise ConfigError("index.context must be a non-empty string")
    mask = raw_index.get("mask", "**/*.md")
    if not isinstance(mask, str) or not mask.strip():
        raise ConfigError("index.mask must be a non-empty string")
    return IndexSpec(
        root_dir=vault,
        collection=collection.strip(),
        context=context.strip() if isinstance(context, str) else None,
        mask=mask.strip(),
    )


def _path_patterns(config: dict[str, Any], field: str, default: list[str]) -> list[str]:
    notes = config.get("notes", {})
    if not isinstance(notes, dict):
        raise ConfigError("notes must be a table")
    patterns = notes.get(field, default)
    if (
        not isinstance(patterns, list)
        or not patterns
        or not all(isinstance(pattern, str) and pattern.strip() for pattern in patterns)
    ):
        raise ConfigError(f"notes.{field} must be a non-empty array of strings")
    normalized = [pattern.strip() for pattern in patterns]
    if any(
        Path(pattern).is_absolute() or ".." in Path(pattern).parts
        for pattern in normalized
    ):
        raise ConfigError(f"notes.{field} patterns must stay within the vault")
    return normalized


def note_path_allowed(config: dict[str, Any], vault: Path, path: Path) -> bool:
    """Return whether a vault-relative Markdown path is eligible for semantic writes."""
    vault = vault.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(vault):
        return False
    relative = resolved.relative_to(vault).as_posix()
    include = _path_patterns(config, "include", ["**/*.md"])
    protected = [".git/**", ".obsidian/**", ".vault-pr/**"]
    exclude = protected + _path_patterns(
        config,
        "exclude",
        protected,
    )

    return any(
        _path_pattern_matches(relative, pattern) for pattern in include
    ) and not any(_path_pattern_matches(relative, pattern) for pattern in exclude)


def _path_pattern_matches(relative: str, pattern: str) -> bool:
    """Match an anchored vault path with segment-aware `*` and recursive `**`."""
    translated: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            translated.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            translated.append(".*")
            index += 2
        elif pattern[index] == "*":
            translated.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            translated.append("[^/]")
            index += 1
        else:
            translated.append(re.escape(pattern[index]))
            index += 1
    return re.fullmatch("".join(translated), relative) is not None


def review_policy(config: dict[str, Any]) -> ReviewPolicy:
    """Load review-all behavior and the bounded revision limit."""
    raw_review = config.get("review", {})
    if not isinstance(raw_review, dict):
        raise ConfigError("review must be a table")
    review_all = raw_review.get("review_all_semantic_transactions", True)
    if not isinstance(review_all, bool):
        raise ConfigError("review.review_all_semantic_transactions must be boolean")
    max_revisions = raw_review.get("max_revision_rounds", 2)
    if isinstance(max_revisions, bool) or not isinstance(max_revisions, int):
        raise ConfigError("review.max_revision_rounds must be an integer")
    if max_revisions < 0:
        raise ConfigError("review.max_revision_rounds must be non-negative")
    if max_revisions > 5:
        raise ConfigError(
            "review.max_revision_rounds cannot exceed the hard limit of 5"
        )
    return ReviewPolicy(review_all, max_revisions)


def max_touched_notes(config: dict[str, Any]) -> int:
    """Return the transaction note limit."""
    try:
        maximum = int(config.get("semantic", {}).get("max_touched_notes", 15))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError("semantic.max_touched_notes must be an integer") from exc
    if maximum < 1:
        raise ConfigError("semantic.max_touched_notes must be positive")
    if maximum > 15:
        raise ConfigError(
            "semantic.max_touched_notes cannot exceed the hard limit of 15"
        )
    return maximum


def source_archive_dir(config: dict[str, Any], vault: Path) -> Path:
    """Resolve the immutable source-capture archive directory."""
    try:
        raw_path = config["sources"]["capture_archive_dir"]
    except (KeyError, TypeError) as exc:
        raise ConfigError("missing sources.capture_archive_dir") from exc
    return vault_relative_path(vault, raw_path, "sources.capture_archive_dir")
