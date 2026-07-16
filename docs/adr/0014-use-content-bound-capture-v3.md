# Use content-bound Capture schema v3

Capture v3 is the only runtime-supported Source Capture schema during
incubation. A Capture identity binds the acquisition trust context, normalized
URL facts when present, and the Kernel-verified primary and declared secondary
files. It does not bind the visible Source Note path, title, provider, logical
name, capture time, `supersedes`, or derived extraction.

A single local file is inspected directly by the Governance Kernel. A directory
or multi-file provider output must contain a closed `snapshot-manifest.json`;
implicit sibling discovery is forbidden. The Source Note is resolved
independently by exact `source_id` and must be unique.

This keeps immutable evidence stable when a Source Note is renamed, makes local
content deduplication deterministic, and prevents provider conventions from
expanding the trusted file set. Capture v2 requires the explicit journaled
`migrate captures-v3` operation; runtime does not carry v1/v2 compatibility
branches.
