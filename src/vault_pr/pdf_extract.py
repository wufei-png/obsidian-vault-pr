"""Resource-bounded PDF-to-Markdown worker used by the ingest process."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


class PdfExtractionError(RuntimeError):
    """Raised when a PDF exceeds extraction policy or cannot be parsed."""


def _apply_resource_limits(max_memory_bytes: int, cpu_seconds: float) -> None:
    if os.name != "posix":
        return
    import resource

    for limit_name in ("RLIMIT_AS", "RLIMIT_DATA"):
        limit = getattr(resource, limit_name, None)
        if limit is None:
            continue
        try:
            resource.setrlimit(limit, (max_memory_bytes, max_memory_bytes))
        except (OSError, ValueError):
            pass
    cpu_limit = max(1, math.ceil(cpu_seconds))
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 1))
    except (OSError, ValueError):
        pass


def extract_pdf(
    raw_path: Path,
    extracted_path: Path,
    *,
    max_pages: int,
    max_characters: int,
) -> dict[str, Any]:
    """Extract bounded page text and return stable metadata."""
    import pypdf

    reader = pypdf.PdfReader(raw_path)
    page_count = len(reader.pages)
    if page_count > max_pages:
        raise PdfExtractionError(f"PDF has {page_count} pages; maximum is {max_pages}")
    metadata_title = reader.metadata.title if reader.metadata else None
    title = " ".join(str(metadata_title or raw_path.stem or "Untitled PDF").split())
    title = title[:200] or "Untitled PDF"
    parts = [f"# {title}"]
    character_count = len(parts[0])
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        section = f"## Page {page_number}\n\n{text}"
        character_count += len(section) + 2
        if character_count > max_characters:
            raise PdfExtractionError(
                f"PDF extracted text exceeds {max_characters} characters"
            )
        parts.append(section)
    extracted_text = "\n\n".join(parts)
    if len(extracted_text.strip()) < 100:
        raise PdfExtractionError(
            "PDF contains too little extractable text; provide an OCR Markdown snapshot"
        )
    extracted_path.write_text(extracted_text, encoding="utf-8")
    return {"title": title, "extractor_version": pypdf.__version__}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw_path", type=Path)
    parser.add_argument("extracted_path", type=Path)
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--max-characters", type=int, required=True)
    parser.add_argument("--max-memory-bytes", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=float, required=True)
    args = parser.parse_args()
    _apply_resource_limits(args.max_memory_bytes, args.cpu_seconds)
    try:
        result = extract_pdf(
            args.raw_path,
            args.extracted_path,
            max_pages=args.max_pages,
            max_characters=args.max_characters,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
