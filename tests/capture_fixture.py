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
    domain: str = "tech",
    source_note: str = "knowledgebase/sources/source.md",
) -> str:
    raw = b"raw source fixture\n"
    extracted = b"# Source Fixture\n\n" + b"knowledge " * 100
    identity = {
        "schema_version": 1,
        "requested_url": "https://example.com/source",
        "final_url": "https://example.com/source",
        "adapter": "http",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "extracted_sha256": hashlib.sha256(extracted).hexdigest(),
        "asset_sha256": {},
        "domain": domain,
        "artifacts": {
            "raw": "source.html",
            "extracted": "extracted.md",
            "source_note": source_note,
        },
    }
    source_id = f"src_{source_identity_digest(identity)[:20]}"
    capture = vault / ".vault-pr" / "sources" / "public" / source_id
    capture.mkdir(parents=True)
    (capture / "source.html").write_bytes(raw)
    (capture / "extracted.md").write_bytes(extracted)
    note_path = vault / source_note
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(source_note_text(source_id))
    manifest = {
        **identity,
        "source_id": source_id,
        "title": "Source Fixture",
    }
    (capture / "manifest.json").write_text(json.dumps(manifest) + "\n")
    return source_id
