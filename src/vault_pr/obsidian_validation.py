"""Deterministic validation of Obsidian references in proposed vault states."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from .config import note_path_allowed
from .git import run_git


class ObsidianValidationError(ValueError):
    """Raised when a semantic transaction introduces invalid Obsidian state."""


_WIKILINK = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]\n]*\]\(([^)\n]+)\)")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(.*?)(\1)")
_COMMENT = re.compile(r"%%.*?%%")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BLOCK = re.compile(r"(?:^|\s)\^([A-Za-z0-9_-]+)\s*$")


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def _semantic_text(content: str) -> str:
    lines: list[str] = []
    fence: tuple[str, int] | None = None
    frontmatter = False
    for number, line in enumerate(content.splitlines()):
        if number == 0 and line.strip() == "---":
            frontmatter = True
            continue
        if frontmatter:
            if line.strip() == "---":
                frontmatter = False
            continue
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        line = _COMMENT.sub("", line)
        line = _INLINE_CODE.sub("", line)
        lines.append(line)
    return "\n".join(lines)


def _frontmatter(content: str) -> dict[str, Any]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next(
        (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if end is None:
        return {}
    try:
        value = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _aliases(content: str) -> set[str]:
    raw = _frontmatter(content).get("aliases", [])
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return {
        _normalize(value)
        for value in values
        if isinstance(value, str) and value.strip()
    }


def _collapse(raw: str) -> str | None:
    parts: list[str] = []
    for part in PurePosixPath(raw.replace("\\", "/")).parts:
        if part in {"", "."}:
            continue
        if part in {"/", ".."}:
            if part == ".." and parts:
                parts.pop()
                continue
            return None
        parts.append(part)
    return "/".join(parts)


def _note_resolvers(
    contents: dict[str, str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    exact: dict[str, list[str]] = defaultdict(list)
    short: dict[str, list[str]] = defaultdict(list)
    for path, content in contents.items():
        without_suffix = path.removesuffix(".md")
        exact[_normalize(without_suffix)].append(path)
        short[_normalize(Path(path).stem)].append(path)
        for alias in _aliases(content):
            short[alias].append(path)
    return dict(exact), dict(short)


def _resolve_note(
    source_path: str,
    raw_target: str,
    exact: dict[str, list[str]],
    short: dict[str, list[str]],
) -> tuple[str, str | None]:
    normalized_target = raw_target.removesuffix(".md")
    if not normalized_target:
        return "resolved", source_path
    source_parent = PurePosixPath(source_path).parent
    candidates: set[str] = set()
    relative = _collapse(str(source_parent / normalized_target))
    absolute = _collapse(normalized_target)
    if relative is not None:
        candidates.update(exact.get(_normalize(relative), []))
    if absolute is not None:
        candidates.update(exact.get(_normalize(absolute), []))
        if "/" not in normalized_target:
            candidates.update(short.get(_normalize(absolute), []))
    if not candidates:
        return "broken", None
    if len(candidates) > 1:
        return "ambiguous", None
    return "resolved", next(iter(candidates))


def _anchor_exists(content: str, anchor: str) -> bool:
    if anchor.startswith("^"):
        target = anchor[1:]
        return any(
            match.group(1) == target
            for line in _semantic_text(content).splitlines()
            if (match := _BLOCK.search(line))
        )
    normalized = _normalize(anchor)
    return any(
        _normalize(match.group(2).strip("#").strip()) == normalized
        for line in _semantic_text(content).splitlines()
        if (match := _HEADING.match(line))
    )


def _listed_files(vault: Path) -> set[str]:
    result = run_git(vault, "ls-files", "-z")
    if result.returncode != 0:
        raise ObsidianValidationError("unable to enumerate tracked vault files")
    return {value for value in result.stdout.split("\0") if value}


def _markdown_contents(
    config: dict[str, Any],
    vault: Path,
    tracked: set[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in sorted(tracked):
        path = vault / relative
        if path.suffix.casefold() != ".md" or not note_path_allowed(
            config, vault, path
        ):
            continue
        if path.is_symlink() or not path.is_file():
            raise ObsidianValidationError("tracked Markdown note is unsafe")
        try:
            result[relative] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ObsidianValidationError(
                f"tracked Markdown note is not UTF-8: {relative}"
            ) from exc
    return result


def _prospective_state(
    config: dict[str, Any],
    vault: Path,
    operations: list[dict[str, Any]],
    content_snapshots: dict[Path, bytes] | None = None,
) -> tuple[dict[str, str], set[str]]:
    tracked = _listed_files(vault)
    contents = _markdown_contents(config, vault, tracked)
    existing_files = {
        relative
        for relative in tracked
        if (vault / relative).is_file() and not (vault / relative).is_symlink()
    }

    def proposed_text(path: Path) -> str:
        try:
            content = (
                content_snapshots[path]
                if content_snapshots is not None
                else path.read_bytes()
            )
            return content.decode("utf-8")
        except KeyError as exc:
            raise ObsidianValidationError(
                "proposed content is missing from the frozen plan bundle"
            ) from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise ObsidianValidationError(
                "proposed Markdown content is not readable UTF-8"
            ) from exc

    for operation in operations:
        action = operation["action"]
        target = str(operation["path"].relative_to(vault))
        if action == "delete":
            contents.pop(target, None)
            existing_files.discard(target)
        elif action == "rename":
            source = str(operation["from"].relative_to(vault))
            source_content = contents.pop(source, None)
            existing_files.discard(source)
            if operation.get("content_file") is not None:
                source_content = proposed_text(operation["content_file"])
            if source_content is not None:
                contents[target] = source_content
            existing_files.add(target)
        else:
            content_file = operation.get("content_file")
            if content_file is not None:
                contents[target] = proposed_text(content_file)
            existing_files.add(target)
    return contents, existing_files


def _file_candidates(
    source_path: str,
    raw_target: str,
    files: set[str],
) -> list[str]:
    decoded = unquote(raw_target.split("|", 1)[0].split("#", 1)[0].strip())
    parsed = urlsplit(decoded)
    if parsed.scheme in {"http", "https", "data"}:
        return []
    if parsed.scheme or decoded.startswith("/"):
        return []
    source_parent = PurePosixPath(source_path).parent
    relative = _collapse(str(source_parent / decoded))
    exact = _collapse(decoded)
    candidates = {
        value
        for value in (relative, exact)
        if value is not None and value in files
    }
    if "/" not in decoded:
        candidates.update(path for path in files if Path(path).name == decoded)
    return sorted(candidates)


def _references_internal_path(
    source_path: str,
    raw_target: str,
    candidates: list[str],
) -> bool:
    decoded = unquote(raw_target.split("|", 1)[0].split("#", 1)[0].strip())
    source_parent = PurePosixPath(source_path).parent
    possible = {
        candidate
        for candidate in (
            _collapse(str(source_parent / decoded)),
            _collapse(decoded),
            *candidates,
        )
        if candidate is not None
    }
    return any(
        PurePosixPath(candidate).parts
        and _normalize(PurePosixPath(candidate).parts[0])
        in {".git", ".obsidian", ".vault-pr"}
        for candidate in possible
    )


def _markdown_destination(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<"):
        end = value.find(">")
        return value[1:end] if end > 0 else value
    return value.split(maxsplit=1)[0] if value else value


def _validate_base(vault: Path, relative: str) -> None:
    path = vault / relative
    if path.is_symlink() or not path.is_file():
        raise ObsidianValidationError(f"Base dependency is missing or unsafe: {relative}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ObsidianValidationError(f"Base dependency is invalid: {relative}") from exc
    if not isinstance(value, dict):
        raise ObsidianValidationError(f"Base dependency must be a YAML mapping: {relative}")
    if "views" in value and not isinstance(value["views"], list):
        raise ObsidianValidationError(f"Base views must be an array: {relative}")
    if "formulas" in value and not isinstance(value["formulas"], dict):
        raise ObsidianValidationError(f"Base formulas must be a mapping: {relative}")
    for view in value.get("views", []):
        if not isinstance(view, dict) or view.get("type") not in {
            "table",
            "cards",
            "list",
            "map",
        }:
            raise ObsidianValidationError(f"Base contains an invalid view: {relative}")


def _issues(
    vault: Path,
    contents: dict[str, str],
    files: set[str],
    *,
    anchor_validation_paths: set[str] | None = None,
) -> tuple[
    list[dict[str, Any]],
    dict[str, list[str]],
    dict[str, list[str]],
]:
    exact, short = _note_resolvers(contents)
    issues: list[dict[str, Any]] = []
    resolved_by_path: dict[str, list[str]] = defaultdict(list)
    resolved_assets_by_path: dict[str, list[str]] = defaultdict(list)
    for source_path, content in sorted(contents.items()):
        semantic = _semantic_text(content)
        for match in _WIKILINK.finditer(semantic):
            embed = bool(match.group(1))
            value = match.group(2).split("|", 1)[0].strip()
            target, separator, anchor = value.partition("#")
            suffix = PurePosixPath(target).suffix.casefold()
            if embed and suffix and suffix != ".md":
                candidates = _file_candidates(source_path, target, files)
                if _references_internal_path(source_path, target, candidates):
                    issues.append(
                        {
                            "kind": "asset_promotion_required",
                            "path": source_path,
                            "target": target,
                        }
                    )
                elif len(candidates) != 1:
                    issues.append(
                        {
                            "kind": "broken_attachment"
                            if not candidates
                            else "ambiguous_attachment",
                            "path": source_path,
                            "target": target,
                        }
                    )
                else:
                    resolved_assets_by_path[source_path].append(candidates[0])
                    if suffix == ".base":
                        try:
                            _validate_base(vault, candidates[0])
                        except ObsidianValidationError:
                            issues.append(
                                {
                                    "kind": "invalid_base",
                                    "path": source_path,
                                    "target": candidates[0],
                                }
                            )
                continue
            status, resolved = _resolve_note(source_path, target, exact, short)
            if status != "resolved" or resolved is None:
                issues.append(
                    {
                        "kind": f"{status}_wikilink",
                        "path": source_path,
                        "target": target,
                    }
                )
                continue
            resolved_by_path[source_path].append(resolved)
            if separator and (
                anchor_validation_paths is None
                or resolved in anchor_validation_paths
            ):
                if not _anchor_exists(contents[resolved], anchor):
                    issues.append(
                        {
                            "kind": "broken_anchor",
                            "path": source_path,
                            "target": value,
                        }
                    )
        for match in _MARKDOWN_IMAGE.finditer(semantic):
            raw = _markdown_destination(match.group(1))
            if urlsplit(raw).scheme in {"http", "https", "data"}:
                continue
            candidates = _file_candidates(source_path, raw, files)
            if _references_internal_path(source_path, raw, candidates):
                issues.append(
                    {
                        "kind": "asset_promotion_required",
                        "path": source_path,
                        "target": raw,
                    }
                )
            elif len(candidates) != 1:
                issues.append(
                    {
                        "kind": "broken_attachment"
                        if not candidates
                        else "ambiguous_attachment",
                        "path": source_path,
                        "target": raw,
                    }
                )
            else:
                resolved_assets_by_path[source_path].append(candidates[0])
    issues.sort(key=lambda item: (item["path"], item["kind"], item["target"]))
    return (
        issues,
        {path: sorted(values) for path, values in resolved_by_path.items()},
        {
            path: sorted(set(values))
            for path, values in resolved_assets_by_path.items()
        },
    )


def validate_proposed_obsidian_state(
    config: dict[str, Any],
    vault: Path,
    operations: list[dict[str, Any]],
    content_snapshots: dict[Path, bytes] | None = None,
) -> dict[str, Any]:
    """Reject only reference defects newly introduced by a transaction."""
    tracked = _listed_files(vault)
    baseline_contents = _markdown_contents(config, vault, tracked)
    baseline_files = {
        relative
        for relative in tracked
        if (vault / relative).is_file() and not (vault / relative).is_symlink()
    }
    baseline_issues, _baseline_links, _baseline_assets = _issues(
        vault,
        baseline_contents,
        baseline_files,
    )
    prospective_contents, prospective_files = _prospective_state(
        config, vault, operations, content_snapshots
    )
    modified_anchor_paths = {
        str(operation["path"].relative_to(vault))
        for operation in operations
        if operation["action"] != "delete"
        and operation["path"].suffix.casefold() == ".md"
        and str(operation["path"].relative_to(vault)) in prospective_contents
    }
    prospective_issues, resolved, resolved_assets = _issues(
        vault,
        prospective_contents,
        prospective_files,
        anchor_validation_paths=modified_anchor_paths,
    )
    baseline_ids = {
        (item["kind"], item["path"], item["target"]) for item in baseline_issues
    }
    introduced = [
        item
        for item in prospective_issues
        if (item["kind"], item["path"], item["target"]) not in baseline_ids
    ]
    if introduced:
        first = introduced[0]
        raise ObsidianValidationError(
            f"transaction introduces {first['kind']}: "
            f"{first['path']} -> {first['target']}"
        )
    return {
        "status": "valid",
        "existing_reference_debt": baseline_issues,
        "prospective_reference_debt": prospective_issues,
        "resolved_links_by_path": resolved,
        "resolved_assets_by_path": resolved_assets,
        "prospective_note_sha256": {
            path: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for path, content in sorted(prospective_contents.items())
        },
        "prospective_note_roles": {
            path: str(_frontmatter(content).get("type", "")).strip().casefold()
            for path, content in sorted(prospective_contents.items())
        },
    }
