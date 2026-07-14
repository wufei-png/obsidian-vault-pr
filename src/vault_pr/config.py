"""Configuration loading and vault-relative path validation."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when the local project configuration is invalid."""


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
        raw_path = config["domains"][domain]["source_notes_dir"]
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"unknown or incomplete domain: {domain}") from exc
    return vault_relative_path(vault, raw_path, f"domains.{domain}.source_notes_dir")


def source_archive_dir(config: dict[str, Any], vault: Path) -> Path:
    """Resolve the immutable public-source archive directory."""
    try:
        raw_path = config["sources"]["public_archive_dir"]
    except (KeyError, TypeError) as exc:
        raise ConfigError("missing sources.public_archive_dir") from exc
    return vault_relative_path(vault, raw_path, "sources.public_archive_dir")
