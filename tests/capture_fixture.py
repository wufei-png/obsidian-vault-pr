from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vault_pr.capture import source_identity_digest


def source_note_text(source_id: str, body: str = "Captured") -> str:
    return (
        "---\n"
        "title: Source\n"
        "type: source\n"
        f'source_id: "{source_id}"\n'
        "tags: []\n"
        "aliases: []\n"
        "---\n\n"
        f"# Source\n\n{body}\n"
    )


def write_capture(
    vault: Path,
    *,
    source_note: str = "knowledgebase/sources/source.md",
    requested_url: str = "https://example.com/source",
    title: str = "Source Fixture",
    extracted_content: bytes | None = None,
    secondary_files: dict[str, tuple[bytes, str, str]] | None = None,
) -> str:
    raw = b"<main>raw source fixture</main>\n"
    extracted = (
        extracted_content
        if extracted_content is not None
        else b"# Source Fixture\n\n" + b"knowledge " * 100
    )
    file_records = [
        {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "mime": mime,
            "role": role,
        }
        for path, (content, mime, role) in sorted(
            (secondary_files or {}).items()
        )
    ]
    identity = {
        "schema_version": 3,
        "source": {
            "kind": "url",
            "access_kind": "public",
            "requested_url": requested_url,
            "final_url": requested_url,
        },
        "primary": {
            "path": "source.html",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "mime": "text/html",
        },
        "files": file_records,
    }
    source_id = f"src_{source_identity_digest(identity)[:20]}"
    capture = vault / ".vault-pr" / "sources" / "public" / source_id
    capture.mkdir(parents=True)
    (capture / "source.html").write_bytes(raw)
    (capture / "extracted.md").write_bytes(extracted)
    for path, (content, _mime, _role) in (secondary_files or {}).items():
        artifact = capture / path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(content)
    note_path = vault / source_note
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(source_note_text(source_id))
    manifest = {
        **identity,
        "source_id": source_id,
        "captured_at": "2026-07-16T00:00:00+00:00",
        "title": title,
        "derived": {
            "extracted": {
                "path": "extracted.md",
                "sha256": hashlib.sha256(extracted).hexdigest(),
                "size": len(extracted),
                "mime": "text/markdown",
            }
        },
        "acquisition": {"adapter": "http"},
    }
    (capture / "manifest.json").write_text(json.dumps(manifest) + "\n")
    return source_id
