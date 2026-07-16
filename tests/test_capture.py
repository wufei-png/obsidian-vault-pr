from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.capture_fixture import write_capture
from vault_pr.capture import (
    CaptureError,
    load_validated_capture,
    source_identity_digest,
    source_identity_payload,
)


class CaptureValidationTests(unittest.TestCase):
    def test_rejects_acquisition_adapter_inconsistent_with_source_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            source_id = write_capture(vault)
            capture = vault / ".vault-pr" / "sources" / "public" / source_id
            manifest_path = capture / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["acquisition"]["adapter"] = "local_file"
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                CaptureError, "acquisition does not match"
            ):
                load_validated_capture(
                    capture,
                    source_id=source_id,
                    vault=vault,
                )

    def test_rejects_file_mime_inconsistent_with_declared_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            old_source_id = write_capture(vault)
            old_capture = (
                vault / ".vault-pr" / "sources" / "public" / old_source_id
            )
            manifest_path = old_capture / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            metadata = b'{"kind":"supplement"}\n'
            (old_capture / "metadata.json").write_bytes(metadata)
            manifest["files"].append(
                {
                    "path": "metadata.json",
                    "sha256": hashlib.sha256(metadata).hexdigest(),
                    "size": len(metadata),
                    "mime": "application/json",
                    "role": "asset",
                }
            )
            new_source_id = (
                "src_"
                + source_identity_digest(source_identity_payload(manifest))[:20]
            )
            manifest["source_id"] = new_source_id
            new_capture = old_capture.with_name(new_source_id)
            old_capture.rename(new_capture)
            (new_capture / "manifest.json").write_text(
                json.dumps(manifest) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CaptureError, "invalid for its role"):
                load_validated_capture(
                    new_capture,
                    source_id=new_source_id,
                    vault=vault,
                )


if __name__ == "__main__":
    unittest.main()
