from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vault_pr.pdf_extract import PdfExtractionError, extract_pdf


class PdfExtractionTests(unittest.TestCase):
    def test_rejects_pdf_over_page_limit_before_page_extraction(self) -> None:
        class Page:
            def extract_text(self) -> str:
                raise AssertionError("page text should not be extracted")

        class Reader:
            metadata = None
            pages = [Page(), Page()]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "source.pdf"
            raw.write_bytes(b"%PDF fixture")
            with patch("pypdf.PdfReader", return_value=Reader()):
                with self.assertRaisesRegex(PdfExtractionError, "maximum is 1"):
                    extract_pdf(
                        raw,
                        root / "extracted.md",
                        max_pages=1,
                        max_characters=1000,
                    )

    def test_rejects_pdf_over_extracted_character_limit(self) -> None:
        class Page:
            def extract_text(self) -> str:
                return "large text " * 100

        class Reader:
            metadata = None
            pages = [Page()]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "source.pdf"
            raw.write_bytes(b"%PDF fixture")
            with patch("pypdf.PdfReader", return_value=Reader()):
                with self.assertRaisesRegex(PdfExtractionError, "exceeds 100"):
                    extract_pdf(
                        raw,
                        root / "extracted.md",
                        max_pages=10,
                        max_characters=100,
                    )
