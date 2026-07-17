"""Read-only vault graph quality assessment by explicit note role."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .config import ConfigError, load_vault_path, note_path_allowed
from .git import run_git


class QualityAuditError(RuntimeError):
    """Raised when a vault quality audit cannot be produced safely."""


_WIKILINK = re.compile(r"!?(?:\[\[)([^\]\n]+)(?:\]\])")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(.*?)(\1)")
_ROLE_DEFAULTS = {
    "concept": {"note", "concept", "topic"},
    "source": {"source"},
    "index": {"index", "moc"},
    "excluded": {"audit", "prompt-template", "prompt-archive"},
}
GRAPH_RULESET_VERSION = 1
GRAPH_AUDIT_SCHEMA_VERSION = 2
GRAPH_AUDIT_PAYLOAD_FIELDS = (
    "schema_version",
    "audit",
    "git_commit",
    "config_sha256",
    "rules_sha256",
    "evidence",
    "metrics",
    "note_inventory",
    "resolved_edges",
    "findings",
)
AUTONOMOUS_RULE_CAPABILITIES = {
    "broken_wikilink": "repair_broken_wikilink",
    "source_without_synthesis_link": "repair_source_synthesis_link",
}
AUTONOMOUS_CAPABILITY_ACTIONS = {
    "repair_broken_wikilink": frozenset({"create", "update"}),
    "repair_source_synthesis_link": frozenset({"create", "update"}),
}


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def graph_audit_payload_sha256(payload: dict[str, Any]) -> str:
    """Hash the closed deterministic portion of a graph audit."""
    if set(payload) != set(GRAPH_AUDIT_PAYLOAD_FIELDS):
        raise QualityAuditError(
            "graph audit payload has unknown or missing fields"
        )
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def validate_graph_audit_digest(audit: dict[str, Any]) -> str:
    """Validate a graph audit's closed schema and content-bound digest."""
    expected_fields = {
        *GRAPH_AUDIT_PAYLOAD_FIELDS,
        "generated_at",
        "git_clean",
        "audit_sha256",
    }
    if (
        set(audit) != expected_fields
        or audit.get("schema_version") != GRAPH_AUDIT_SCHEMA_VERSION
        or audit.get("audit") != "graph"
        or not isinstance(audit.get("generated_at"), str)
        or not audit["generated_at"].strip()
        or not isinstance(audit.get("git_clean"), bool)
    ):
        raise QualityAuditError("graph audit schema is invalid")
    payload = {
        field: audit[field] for field in GRAPH_AUDIT_PAYLOAD_FIELDS
    }
    digest = graph_audit_payload_sha256(payload)
    if audit.get("audit_sha256") != digest:
        raise QualityAuditError("graph audit digest does not match its content")
    return digest


def relevant_quality_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return only config fields that affect deterministic graph findings."""
    return {
        "notes": config.get("notes", {}),
        "note_roles": config.get("quality", {}).get("note_roles", {})
        if isinstance(config.get("quality", {}), dict)
        else {},
        "graph_ruleset_version": GRAPH_RULESET_VERSION,
    }


def _finding(
    rule_id: str,
    *,
    severity: str,
    path: str,
    owner_sha256: str | None,
    target: str | None = None,
    blocking_scope: str = "none",
) -> dict[str, Any]:
    identity = {
        "rule_id": rule_id,
        "path": path,
        "target": target,
    }
    digest = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
    result: dict[str, Any] = {
        "finding_id": f"finding_{digest[:20]}",
        "rule_id": rule_id,
        "severity": severity,
        "path": path,
        "owner_sha256": owner_sha256,
        "blocking_scope": blocking_scope,
    }
    if target is not None:
        result["target"] = target
    capability = AUTONOMOUS_RULE_CAPABILITIES.get(rule_id)
    if capability is not None:
        result["autonomous_capability"] = capability
    return result


@dataclass(frozen=True)
class Note:
    """The graph-relevant subset of one tracked Markdown note."""

    path: str
    stem: str
    role: str
    sha256: str
    aliases: tuple[str, ...]
    links: tuple[str, ...]


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def note_role_sets(config: dict[str, Any]) -> dict[str, set[str]]:
    """Return configured normalized note types grouped by graph role."""
    quality = config.get("quality", {})
    if not isinstance(quality, dict):
        raise ConfigError("quality must be a table")
    raw_roles = quality.get("note_roles", {})
    if not isinstance(raw_roles, dict):
        raise ConfigError("quality.note_roles must be a table")
    roles: dict[str, set[str]] = {}
    seen: set[str] = set()
    for role, defaults in _ROLE_DEFAULTS.items():
        raw = raw_roles.get(f"{role}_types", sorted(defaults))
        if not isinstance(raw, list) or not all(
            isinstance(value, str) and value.strip() for value in raw
        ):
            raise ConfigError(
                f"quality.note_roles.{role}_types must be an array of strings"
            )
        values = {_normalize(value) for value in raw}
        overlap = seen & values
        if overlap:
            raise ConfigError(
                "quality note-role types overlap: " + ", ".join(sorted(overlap))
            )
        roles[role] = values
        seen.update(values)
    return roles


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
        parsed = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise QualityAuditError("note contains invalid YAML frontmatter") from exc
    return parsed if isinstance(parsed, dict) else {}


def note_role_for_type(raw_type: Any, roles: dict[str, set[str]]) -> str:
    """Classify one raw frontmatter type using the configured role mapping."""
    if not isinstance(raw_type, str) or not raw_type.strip():
        return "unclassified"
    normalized = _normalize(raw_type)
    for role in ("concept", "source", "index", "excluded"):
        if normalized in roles[role]:
            return role
    return "unclassified"


def _note_role(frontmatter: dict[str, Any], roles: dict[str, set[str]]) -> str:
    return note_role_for_type(frontmatter.get("type"), roles)


def _aliases(frontmatter: dict[str, Any]) -> tuple[str, ...]:
    raw = frontmatter.get("aliases", [])
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [value for value in raw if isinstance(value, str)]
    else:
        values = []
    return tuple(sorted({_normalize(value) for value in values if value.strip()}))


def _link_targets(content: str) -> tuple[str, ...]:
    targets: set[str] = set()
    fence: tuple[str, int] | None = None
    frontmatter = False
    for line_number, line in enumerate(content.splitlines()):
        if line_number == 0 and line.strip() == "---":
            frontmatter = True
            continue
        if frontmatter:
            if line.strip() == "---":
                frontmatter = False
            continue
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            marker_type = marker[0]
            if fence is None:
                fence = (marker_type, len(marker))
            elif marker_type == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        semantic_line = _INLINE_CODE.sub("", line)
        for match in _WIKILINK.finditer(semantic_line):
            raw_target = match.group(1)
            if raw_target != raw_target.strip() or re.match(
                r"\(https?://", semantic_line[match.end() :]
            ):
                continue
            target = raw_target.split("|", 1)[0].split("#", 1)[0].strip()
            if not target:
                continue
            suffix = PurePosixPath(target).suffix.casefold()
            if suffix and suffix != ".md":
                continue
            if suffix == ".md":
                target = target[:-3]
            targets.add(target)
    return tuple(sorted(targets))


def _tracked_notes(
    config: dict[str, Any], vault: Path, roles: dict[str, set[str]]
) -> tuple[list[Note], list[dict[str, Any]]]:
    listed = run_git(vault, "ls-files", "-z", "--", "*.md")
    if listed.returncode != 0:
        raise QualityAuditError("unable to enumerate tracked Markdown notes")
    notes: list[Note] = []
    findings: list[dict[str, Any]] = []
    raw_paths = [raw for raw in listed.stdout.split("\0") if raw]
    if len(raw_paths) > 10_000:
        raise QualityAuditError("vault exceeds the 10000-note audit limit")
    for relative in sorted(raw_paths):
        path = vault / relative
        if not note_path_allowed(config, vault, path):
            continue
        if path.is_symlink() or not path.is_file():
            raise QualityAuditError("tracked note is not a regular file")
        if path.stat().st_size > 2_000_000:
            findings.append(
                _finding(
                    "note_too_large",
                    severity="warning",
                    path=relative,
                    owner_sha256=None,
                )
            )
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise QualityAuditError("tracked Markdown note is not UTF-8") from exc
        try:
            metadata = _frontmatter(content)
        except QualityAuditError as exc:
            raise QualityAuditError(
                f"note contains invalid YAML frontmatter: {relative}"
            ) from exc
        role = _note_role(metadata, roles)
        notes.append(
            Note(
                path=relative,
                stem=Path(relative).stem,
                role=role,
                sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                aliases=_aliases(metadata),
                links=_link_targets(content),
            )
        )
    return notes, findings


def _resolvers(notes: list[Note]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    exact: dict[str, list[str]] = defaultdict(list)
    short: dict[str, list[str]] = defaultdict(list)
    for note in notes:
        without_suffix = note.path.removesuffix(".md")
        exact[_normalize(without_suffix)].append(note.path)
        short[_normalize(note.stem)].append(note.path)
        for alias in note.aliases:
            short[alias].append(note.path)
    return dict(exact), dict(short)


def _resolve_link(
    source: Note,
    raw_target: str,
    exact: dict[str, list[str]],
    short: dict[str, list[str]],
) -> tuple[str, str | None]:
    def collapse(raw: str) -> str | None:
        parts: list[str] = []
        for part in PurePosixPath(raw.replace("\\", "/")).parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    return None
                parts.pop()
            elif part == "/":
                return None
            else:
                parts.append(part)
        return "/".join(parts)

    normalized_path = collapse(raw_target)
    source_parent = PurePosixPath(source.path).parent
    relative_path = collapse(str(source_parent / raw_target))
    candidates: set[str] = set(
        exact.get(_normalize(relative_path), []) if relative_path else []
    )
    if normalized_path is not None:
        normalized = _normalize(normalized_path)
        candidates.update(exact.get(normalized, []))
        if "/" not in raw_target:
            candidates.update(short.get(normalized, []))
    if not candidates:
        return "broken", None
    if len(candidates) > 1:
        return "ambiguous", None
    return "resolved", next(iter(candidates))


def _components(paths: set[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = {path: set() for path in paths}
    for source, target in edges:
        if source in adjacency and target in adjacency:
            adjacency[source].add(target)
            adjacency[target].add(source)
    remaining = set(paths)
    components: list[list[str]] = []
    while remaining:
        start = min(remaining)
        queue = deque([start])
        remaining.remove(start)
        component: list[str] = []
        while queue:
            current = queue.popleft()
            component.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
        components.append(sorted(component))
    return sorted(components, key=lambda item: (-len(item), item[0]))


def audit_graph(config: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic graph findings without writing the vault."""
    vault = load_vault_path(config)
    roles = note_role_sets(config)
    notes, findings = _tracked_notes(config, vault, roles)
    exact, short = _resolvers(notes)
    by_path = {note.path: note for note in notes}
    incoming: Counter[str] = Counter()
    incoming_from: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    edges: set[tuple[str, str]] = set()
    graph_roles = {"concept", "source", "index"}
    for note in notes:
        if note.role not in graph_roles:
            continue
        for target in note.links:
            status, resolved = _resolve_link(note, target, exact, short)
            if status != "resolved" or resolved is None:
                findings.append(
                    _finding(
                        f"{status}_wikilink",
                        severity="error",
                        path=note.path,
                        owner_sha256=note.sha256,
                        target=target,
                        blocking_scope="owning_transaction",
                    )
                )
                continue
            if by_path.get(resolved) is None or by_path[resolved].role not in graph_roles:
                continue
            if resolved == note.path:
                continue
            incoming[resolved] += 1
            incoming_from[resolved].add(note.path)
            outgoing[note.path].add(resolved)
            edges.add((note.path, resolved))

    graph_paths = {note.path for note in notes if note.role in graph_roles}
    for note in notes:
        if note.role == "excluded":
            continue
        if note.role == "unclassified":
            findings.append(
                _finding(
                    "unclassified_note_role",
                    severity="warning",
                    path=note.path,
                    owner_sha256=note.sha256,
                )
            )
        elif note.role == "concept" and not incoming[note.path] and not outgoing[note.path]:
            findings.append(
                _finding(
                    "isolated_concept",
                    severity="warning",
                    path=note.path,
                    owner_sha256=note.sha256,
                )
            )
        elif note.role == "source" and not any(
            by_path[owner].role in {"concept", "index"}
            for owner in incoming_from[note.path]
            if owner in by_path
        ):
            findings.append(
                _finding(
                    "source_without_synthesis_link",
                    severity="warning",
                    path=note.path,
                    owner_sha256=note.sha256,
                )
            )
        elif note.role == "index" and not outgoing[note.path]:
            findings.append(
                _finding(
                    "index_without_coverage",
                    severity="warning",
                    path=note.path,
                    owner_sha256=note.sha256,
                )
            )

    components = _components(graph_paths, edges)
    head = run_git(vault, "rev-parse", "HEAD")
    status = run_git(vault, "status", "--porcelain=v1")
    role_counts = Counter(note.role for note in notes)
    relevant_config = relevant_quality_config(config)
    config_digest = hashlib.sha256(
        _canonical_json_bytes(relevant_config)
    ).hexdigest()
    rules_digest = hashlib.sha256(
        _canonical_json_bytes(
            {
                "graph_ruleset_version": GRAPH_RULESET_VERSION,
                "autonomous_capabilities": AUTONOMOUS_RULE_CAPABILITIES,
            }
        )
    ).hexdigest()
    findings.sort(
        key=lambda item: (
            {"error": 0, "warning": 1}.get(str(item.get("severity")), 2),
            str(item.get("path", "")),
            str(item.get("rule_id", "")),
        )
    )
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metrics = {
        "tracked_notes": len(notes),
        "graph_notes": len(graph_paths),
        "roles": dict(sorted(role_counts.items())),
        "resolved_edges": len(edges),
        "components": len(components),
        "largest_component": len(components[0]) if components else 0,
        "isolated_graph_notes": sum(len(item) == 1 for item in components),
        "findings": len(findings),
    }
    inventory = [
        {
            "path": note.path,
            "role": note.role,
            "sha256": note.sha256,
            "outgoing": sorted(outgoing[note.path]),
        }
        for note in sorted(notes, key=lambda item: item.path)
    ]
    resolved_edges = [
        {"source": source, "target": target} for source, target in sorted(edges)
    ]
    inventory_digest = hashlib.sha256(_canonical_json_bytes(inventory)).hexdigest()
    findings_digest = hashlib.sha256(_canonical_json_bytes(findings)).hexdigest()
    audit_evidence = [
        {"kind": "config", "sha256": config_digest},
        {"kind": "graph_rules", "sha256": rules_digest},
        {"kind": "note_inventory", "sha256": inventory_digest},
        {"kind": "graph_findings", "sha256": findings_digest},
    ]
    audit_payload = {
        "schema_version": GRAPH_AUDIT_SCHEMA_VERSION,
        "audit": "graph",
        "git_commit": head.stdout.strip() if head.returncode == 0 else None,
        "config_sha256": config_digest,
        "rules_sha256": rules_digest,
        "evidence": audit_evidence,
        "metrics": metrics,
        "note_inventory": inventory,
        "resolved_edges": resolved_edges,
        "findings": findings,
    }
    audit_digest = graph_audit_payload_sha256(audit_payload)
    return {
        **audit_payload,
        "generated_at": generated_at,
        "git_clean": status.returncode == 0 and not status.stdout,
        "audit_sha256": audit_digest,
    }
