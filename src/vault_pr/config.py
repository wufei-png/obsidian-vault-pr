"""Configuration loading and vault-relative path validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when the local project configuration is invalid."""


@dataclass(frozen=True)
class DomainSpec:
    """One semantic domain and its physical vault/index mapping."""

    name: str
    root_dir: Path
    source_notes_dir: Path
    qmd_collection: str | None
    qmd_context: str | None


def load_config(config_path: Path) -> dict[str, Any]:
    """Load a TOML configuration file."""
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML config: {config_path}") from exc
    return config


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


def domain_source_notes_dir(config: dict[str, Any], vault: Path, domain: str) -> Path:
    """Resolve the visible source-note directory for a domain."""
    try:
        return domain_specs(config, vault)[domain].source_notes_dir
    except KeyError as exc:
        raise ConfigError(f"unknown domain: {domain}") from exc


def domain_specs(config: dict[str, Any], vault: Path) -> dict[str, DomainSpec]:
    """Load every configured domain and validate its vault/index mapping."""
    raw_domains = config.get("domains")
    if not isinstance(raw_domains, dict) or not raw_domains:
        raise ConfigError("domains must be a non-empty table")

    specs: dict[str, DomainSpec] = {}
    collection_names: set[str] = set()
    for name, raw_domain in raw_domains.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(raw_domain, dict)
        ):
            raise ConfigError("each domain must be a named table")
        root_dir = vault_relative_path(
            vault, raw_domain.get("root_dir"), f"domains.{name}.root_dir"
        )
        source_notes_dir = vault_relative_path(
            vault,
            raw_domain.get("source_notes_dir"),
            f"domains.{name}.source_notes_dir",
        )
        if not source_notes_dir.is_relative_to(root_dir):
            raise ConfigError(f"domains.{name}.source_notes_dir must be under root_dir")

        collection = raw_domain.get("qmd_collection")
        if collection is not None:
            if not isinstance(collection, str) or not collection.strip():
                raise ConfigError(
                    f"domains.{name}.qmd_collection must be a non-empty string"
                )
            collection = collection.strip()
            if collection in collection_names:
                raise ConfigError(f"duplicate qmd collection name: {collection}")
            collection_names.add(collection)

        context = raw_domain.get("qmd_context")
        if context is not None and (
            not isinstance(context, str) or not context.strip()
        ):
            raise ConfigError(f"domains.{name}.qmd_context must be a non-empty string")

        specs[name] = DomainSpec(
            name=name,
            root_dir=root_dir,
            source_notes_dir=source_notes_dir,
            qmd_collection=collection,
            qmd_context=context.strip() if isinstance(context, str) else None,
        )

    roots = list(specs.values())
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if left.root_dir.is_relative_to(
                right.root_dir
            ) or right.root_dir.is_relative_to(left.root_dir):
                raise ConfigError(
                    f"domain roots must not overlap: {left.name}, {right.name}"
                )
    return specs


def max_touched_notes(config: dict[str, Any]) -> int:
    """Return the transaction note limit."""
    try:
        maximum = int(config.get("ingest", {}).get("max_touched_notes", 15))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError("ingest.max_touched_notes must be an integer") from exc
    if maximum < 1:
        raise ConfigError("ingest.max_touched_notes must be positive")
    if maximum > 15:
        raise ConfigError("ingest.max_touched_notes cannot exceed the hard limit of 15")
    return maximum


def source_archive_dir(config: dict[str, Any], vault: Path) -> Path:
    """Resolve the immutable public-source archive directory."""
    try:
        raw_path = config["sources"]["public_archive_dir"]
    except (KeyError, TypeError) as exc:
        raise ConfigError("missing sources.public_archive_dir") from exc
    return vault_relative_path(vault, raw_path, "sources.public_archive_dir")
