"""Tests for format extraction and the extraction cache contract (peekseed/extract.py)."""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.fixtures import make_pdf

from peekseed import contracts, extract
from peekseed.ingest.pdf import IngestError

SHA = "0123456789abcdef" * 4  # 64 characters

OPTIONAL_MODULES = ("pymupdf", "fitz", "pytesseract", "PIL", "docx", "pptx", "openpyxl")


def build_epub(path, chapters):
    """Write a tiny but structurally valid EPUB with one spine item per chapter."""
    items = "\n".join(
        '    <item id="ch%d" href="ch%d.xhtml" media-type="application/xhtml+xml"/>'
        % (index, index)
        for index in range(1, len(chapters) + 1)
    )
    spine = "\n".join(
        '    <itemref idref="ch%d"/>' % index for index in range(1, len(chapters) + 1)
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )
    package = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:identifier id="bookid">urn:uuid:peekseed-test</dc:identifier>\n'
        "    <dc:title>Test Book</dc:title>\n"
        "    <dc:language>en</dc:language>\n"
        "  </metadata>\n"
        "  <manifest>\n"
        "%s\n"
        "  </manifest>\n"
        "  <spine>\n"
        "%s\n"
        "  </spine>\n"
        "</package>\n" % (items, spine)
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        for index, chapter in enumerate(chapters, start=1):
            archive.writestr("OEBPS/ch%d.xhtml" % index, chapter)
    return path


class ExtractTestCase(unittest.TestCase):
    """A per-test temporary directory with small file-writing helpers."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="peekseed-extract-")
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def write(self, name, text=None, data=None, encoding="utf-8"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if data is not None:
            path.write_bytes(data)
        else:
            path.write_text(text, encoding=encoding)
        return path

    def write_email(self, name, headers, body):
        message = headers + "\n\n" + body + "\n"
        return self.write(name, data=message.encode("utf-8"))


class PdfTests(ExtractTestCase):
    def setUp(self):
        super().setUp()
        self.pdf = make_pdf(
            self.root / "manual.pdf",
            pages=[
                "Widget installation guide. Mount the widget on the frame with two screws.",
                "Widget operation. Press the green button to start the widget.",
            ],
        )

    def test_pdf_pages_in_order(self):
        result = extract.extract(self.pdf)
        self.assertEqual(
            set(result), {"pages", "extractor", "pages_count", "needs_ocr"}
        )
        self.assertEqual(set(result["pages"][0]), {"page", "text"})
        self.assertEqual(result["pages_count"], 2)
        self.assertEqual(result["extractor"], extract.extractor_identity_for(self.pdf))
        self.assertEqual([page["page"] for page in result["pages"]], [1, 2])
        self.assertIn("Widget installation", result["pages"][0]["text"])
        self.assertIn("green button", result["pages"][1]["text"])
        self.assertFalse(result["needs_ocr"])

    def test_pdf_identity_is_pypdf_version(self):
        import pypdf

        identity = extract.extractor_identity_for(self.pdf)
        self.assertEqual(identity["name"], "pypdf")
        self.assertEqual(identity["version"], pypdf.__version__)
        self.assertEqual(set(identity), {"name", "version"})

    def test_pdf_extraction_is_deterministic(self):
        self.assertEqual(extract.extract(self.pdf), extract.extract(self.pdf))

    def test_encrypted_pdf_is_a_user_facing_error(self):
        import pypdf

        source = make_pdf(
            self.root / "source.pdf",
            pages=["Secret page text that is long enough to be extracted."],
        )
        writer = pypdf.PdfWriter(clone_from=str(source))
        writer.encrypt("password")
        encrypted = self.root / "encrypted.pdf"
        with encrypted.open("wb") as handle:
            writer.write(handle)
        with self.assertRaises(IngestError) as caught:
            extract.extract(encrypted)
        message = str(caught.exception)
        self.assertIn("encrypted.pdf", message)
        self.assertIn("password", message.lower())
        self.assertNotIn(str(self.root), message)

    def test_corrupt_pdf_is_a_user_facing_error(self):
        broken = self.write("broken.pdf", data=b"%PDF-1.4\nnot really a pdf")
        with self.assertRaises(IngestError) as caught:
            extract.extract(broken)
        message = str(caught.exception)
        self.assertIn("broken.pdf", message)
        self.assertNotIn(str(self.root), message)


class ScannedPdfTests(ExtractTestCase):
    def test_textless_pdf_raises_ocr_required_naming_file_and_page(self):
        path = make_pdf(self.root / "scanned.pdf", pages=["", "   "])
        with self.assertRaises(extract.OcrRequired) as caught:
            extract.extract(path)
        message = str(caught.exception)
        self.assertIn("scanned.pdf", message)
        self.assertIn("page 1", message)
        self.assertIn("peekseed[ocr]", message)
        self.assertNotIn(str(self.root), message)
        self.assertEqual(caught.exception.file, "scanned.pdf")
        self.assertEqual(caught.exception.page, 1)

    def test_ocr_required_is_an_ingest_error(self):
        self.assertTrue(issubclass(extract.OcrRequired, IngestError))

    @unittest.skipIf(extract.ocr_available(), "OCR tools installed change the outcome")
    def test_partial_scan_is_returned_with_needs_ocr(self):
        path = make_pdf(
            self.root / "mixed.pdf",
            pages=[
                "The first page has a normal text layer with plenty of readable words.",
                "   ",
            ],
        )
        result = extract.extract(path)
        self.assertEqual(result["pages_count"], 2)
        self.assertTrue(result["needs_ocr"])
        self.assertEqual(result["pages"][1]["text"], "")

    @unittest.skipIf(extract.ocr_available(), "OCR tools installed change the outcome")
    def test_ocr_false_never_runs_ocr(self):
        path = make_pdf(
            self.root / "mixed.pdf",
            pages=[
                "The first page has a normal text layer with plenty of readable words.",
                "   ",
            ],
        )
        result = extract.extract(path, ocr=False)
        self.assertTrue(result["needs_ocr"])

    @unittest.skipIf(extract.ocr_available(), "OCR tools installed change the outcome")
    def test_ocr_true_without_tools_raises(self):
        path = make_pdf(
            self.root / "mixed.pdf",
            pages=[
                "The first page has a normal text layer with plenty of readable words.",
                "   ",
            ],
        )
        with self.assertRaises(extract.OcrRequired):
            extract.extract(path, ocr=True)


class MarkdownTests(ExtractTestCase):
    def test_headings_are_preserved_as_lines(self):
        path = self.write(
            "guide.md",
            "# Widget Guide\n\nIntro paragraph.\n\n## Installation\n\nUse two screws.\n",
        )
        result = extract.extract(path)
        self.assertEqual(result["pages_count"], 1)
        self.assertEqual(result["pages"][0]["page"], 1)
        text = result["pages"][0]["text"]
        lines = text.splitlines()
        self.assertIn("# Widget Guide", lines)
        self.assertIn("## Installation", lines)
        self.assertIn("Use two screws.", text)
        self.assertEqual(result["extractor"]["name"], "markdown")
        self.assertEqual(
            set(result), {"pages", "extractor", "pages_count", "needs_ocr"}
        )

    def test_markdown_extension_is_supported(self):
        path = self.write("notes.markdown", "## Notes\n\nBody text.\n")
        self.assertEqual(extract.extract(path)["pages_count"], 1)


class HtmlTests(ExtractTestCase):
    def test_visible_text_only(self):
        path = self.write(
            "page.html",
            "<!doctype html><html><head><title>Hidden title</title>"
            "<script>window.alert('x')</script><style>.x{color:red}</style></head>"
            "<body><nav>Home | Docs</nav><h1>Widget Manual</h1>"
            "<p>Mount &amp; tighten.</p></body></html>",
        )
        result = extract.extract(path)
        self.assertEqual(result["pages_count"], 1)
        text = result["pages"][0]["text"]
        self.assertIn("Widget Manual", text)
        self.assertIn("Mount & tighten.", text)
        self.assertNotIn("Hidden title", text)
        self.assertNotIn("window.alert", text)
        self.assertNotIn("Home | Docs", text)
        self.assertNotIn("color:red", text)
        self.assertEqual(result["extractor"]["name"], "html")

    def test_htm_extension_is_supported(self):
        path = self.write("page.htm", "<html><body><p>Short visible text.</p></body></html>")
        self.assertEqual(extract.extract(path)["pages_count"], 1)


class TextTests(ExtractTestCase):
    def test_text_is_normalised(self):
        path = self.write("notes.txt", "First line.\r\n\r\n\r\n\r\nSecond paragraph.\n")
        result = extract.extract(path)
        self.assertEqual(result["pages_count"], 1)
        self.assertEqual(result["pages"][0]["text"], "First line.\n\nSecond paragraph.")
        self.assertEqual(result["extractor"]["name"], "text")

    def test_non_utf8_bytes_are_replaced_not_fatal(self):
        path = self.write("latin.txt", data=b"caf\xe9 text\n")
        result = extract.extract(path)
        self.assertIn("caf", result["pages"][0]["text"])
        self.assertIn("text", result["pages"][0]["text"])


class EpubTests(ExtractTestCase):
    def test_one_page_per_spine_item_in_order(self):
        path = build_epub(
            self.root / "book.epub",
            [
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title>'
                "</head><body><h1>Chapter One</h1><p>First chapter text.</p>"
                "<script>alert(1)</script></body></html>",
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                "<h1>Chapter Two</h1><p>Second chapter text.</p></body></html>",
            ],
        )
        result = extract.extract(path)
        self.assertEqual(result["pages_count"], 2)
        self.assertEqual([page["page"] for page in result["pages"]], [1, 2])
        self.assertIn("Chapter One", result["pages"][0]["text"])
        self.assertIn("First chapter text.", result["pages"][0]["text"])
        self.assertIn("Chapter Two", result["pages"][1]["text"])
        self.assertNotIn("alert(1)", result["pages"][0]["text"])
        self.assertEqual(result["extractor"]["name"], "epub")

    def test_invalid_epub_is_a_user_facing_error(self):
        path = self.write("broken.epub", data=b"this is not a zip archive")
        with self.assertRaises(IngestError) as caught:
            extract.extract(path)
        message = str(caught.exception)
        self.assertIn("broken.epub", message)
        self.assertNotIn(str(self.root), message)


class EmailTests(ExtractTestCase):
    def test_envelope_and_plain_body(self):
        path = self.write_email(
            "report.eml",
            "From: Ada <ada@example.com>\n"
            "To: team@example.com\n"
            "Subject: Widget report\n"
            "Date: Tue, 20 Oct 2026 10:00:00 +0000\n"
            "MIME-Version: 1.0\n"
            "Content-Type: text/plain; charset=utf-8",
            "Body text here.",
        )
        result = extract.extract(path)
        self.assertEqual(result["pages_count"], 1)
        text = result["pages"][0]["text"]
        self.assertIn("Subject: Widget report", text)
        self.assertIn("From: Ada <ada@example.com>", text)
        self.assertIn("Date: Tue, 20 Oct 2026 10:00:00 +0000", text)
        self.assertIn("Body text here.", text)
        self.assertEqual(result["extractor"]["name"], "eml")

    def test_html_body_is_stripped(self):
        path = self.write_email(
            "html.eml",
            "From: Ada <ada@example.com>\n"
            "Subject: Html\n"
            "MIME-Version: 1.0\n"
            "Content-Type: text/html; charset=utf-8",
            "<html><body><p>HTML body text.</p><script>evil()</script></body></html>",
        )
        text = extract.extract(path)["pages"][0]["text"]
        self.assertIn("HTML body text.", text)
        self.assertNotIn("evil()", text)

    def test_multipart_prefers_text_plain(self):
        path = self.write_email(
            "multi.eml",
            "From: Ada <ada@example.com>\n"
            "Subject: Multi\n"
            "MIME-Version: 1.0\n"
            'Content-Type: multipart/alternative; boundary="BOUND"',
            "--BOUND\n"
            "Content-Type: text/plain; charset=utf-8\n"
            "\n"
            "Plain alternative body.\n"
            "--BOUND\n"
            "Content-Type: text/html; charset=utf-8\n"
            "\n"
            "<html><body><p>HTML alternative body.</p></body></html>\n"
            "--BOUND--",
        )
        text = extract.extract(path)["pages"][0]["text"]
        self.assertIn("Plain alternative body.", text)
        self.assertNotIn("HTML alternative body.", text)


class UnsupportedTests(ExtractTestCase):
    def test_unsupported_extension_names_extension_and_core_formats(self):
        path = self.write("photo.xyz", "some bytes")
        with self.assertRaises(IngestError) as caught:
            extract.extract(path)
        message = str(caught.exception)
        self.assertIn("'.xyz'", message)
        self.assertIn("PDF", message)
        self.assertIn("Markdown", message)
        self.assertIn("TXT", message)

    def test_extractor_identity_for_unsupported_extension_raises(self):
        with self.assertRaises(IngestError):
            extract.extractor_identity_for("photo.xyz")

    def test_missing_file_is_a_user_facing_error(self):
        with self.assertRaises(IngestError) as caught:
            extract.extract(self.root / "gone.pdf")
        message = str(caught.exception)
        self.assertIn("gone.pdf", message)
        self.assertNotIn(str(self.root), message)


class CacheKeyTests(unittest.TestCase):
    def test_cache_key_is_one_file_per_content_hash(self):
        first = {"name": "pypdf", "version": "1.0"}
        second = {"name": "pypdf", "version": "2.0"}
        self.assertEqual(extract.cache_key(SHA, first), SHA + ".json")
        # A parser upgrade must reuse the same path; the identity lives inside.
        self.assertEqual(extract.cache_key(SHA, second), SHA + ".json")

    def test_cache_key_lowercases_the_hash(self):
        identity = {"name": "pypdf", "version": "1"}
        self.assertEqual(extract.cache_key(SHA.upper(), identity), SHA + ".json")

    def test_cache_key_rejects_a_bad_hash(self):
        with self.assertRaises(contracts.ContractError):
            extract.cache_key("not-a-hash", {"name": "pypdf", "version": "1"})

    def test_cache_key_requires_an_extractor_identity(self):
        with self.assertRaises(contracts.ContractError):
            extract.cache_key(SHA, {})


class OptionalDependencyTests(ExtractTestCase):
    def test_importing_extract_does_not_import_optional_dependencies(self):
        root = Path(__file__).resolve().parent.parent
        code = (
            "import sys\n"
            "import peekseed.extract\n"
            "forbidden = %r\n"
            "found = [name for name in forbidden if name in sys.modules]\n"
            "print('\\n'.join(found))\n" % (OPTIONAL_MODULES,)
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(), "", "optional modules imported: " + completed.stdout
        )

    @unittest.skipIf(
        importlib.util.find_spec("docx") is not None, "python-docx is installed"
    )
    def test_docx_without_office_extra_is_a_clear_error(self):
        path = self.write("report.docx", data=b"not a real docx")
        with self.assertRaises(IngestError) as caught:
            extract.extract(path)
        message = str(caught.exception)
        self.assertIn("report.docx", message)
        self.assertIn("peekseed[office]", message)

    @unittest.skipIf(
        importlib.util.find_spec("pptx") is not None, "python-pptx is installed"
    )
    def test_pptx_without_office_extra_is_a_clear_error(self):
        path = self.write("deck.pptx", data=b"not a real pptx")
        with self.assertRaises(IngestError) as caught:
            extract.extract(path)
        message = str(caught.exception)
        self.assertIn("deck.pptx", message)
        self.assertIn("peekseed[office]", message)

    @unittest.skipIf(
        importlib.util.find_spec("openpyxl") is not None, "openpyxl is installed"
    )
    def test_xlsx_without_tables_extra_is_a_clear_error(self):
        path = self.write("sheet.xlsx", data=b"not a real xlsx")
        with self.assertRaises(IngestError) as caught:
            extract.extract(path)
        message = str(caught.exception)
        self.assertIn("sheet.xlsx", message)
        self.assertIn("peekseed[tables]", message)

    @unittest.skipIf(
        importlib.util.find_spec("pytesseract") is not None, "pytesseract is installed"
    )
    def test_ocr_tools_report_unavailable(self):
        self.assertFalse(extract.ocr_available())


class AvailabilityTests(unittest.TestCase):
    def test_supported_extensions_cover_core_formats(self):
        for extension in (".pdf", ".md", ".markdown", ".html", ".htm", ".txt", ".epub", ".eml"):
            self.assertIn(extension, extract.SUPPORTED_EXTENSIONS)
        for extension in (".docx", ".pptx", ".xlsx"):
            self.assertIn(extension, extract.SUPPORTED_EXTENSIONS)

    def test_extractor_api_version(self):
        self.assertEqual(extract.EXTRACTOR_API, 1)

    def test_available_extractors_are_sorted_labels(self):
        labels = extract.available_extractors()
        self.assertTrue(labels)
        self.assertEqual(labels, sorted(labels))
        for label in labels:
            self.assertIsInstance(label, str)
            self.assertIn("@", label)
        for label in ("eml@1", "epub@1", "html@1", "markdown@1", "text@1"):
            self.assertIn(label, labels)
        if importlib.util.find_spec("pypdf") is not None:
            self.assertTrue(any(label.startswith("pypdf@") for label in labels))

    def test_identity_for_stdlib_formats(self):
        expected = {
            "guide.md": "markdown",
            "page.html": "html",
            "notes.txt": "text",
            "book.epub": "epub",
            "mail.eml": "eml",
        }
        for filename, name in expected.items():
            identity = extract.extractor_identity_for(filename)
            self.assertEqual(identity["name"], name)
            self.assertEqual(identity["version"], "1")

    def test_ocr_available_is_a_boolean(self):
        self.assertIsInstance(extract.ocr_available(), bool)

    def test_every_supported_extension_has_a_backend(self):
        for extension in extract.SUPPORTED_EXTENSIONS:
            backend = extract.get_extractor("document" + extension)
            self.assertIn(extension, backend.extensions)


class FakeBackend:
    """A minimal third-party backend used to test the registry."""

    name = "fake"
    version = "9"
    extensions = (".fakefmt",)

    def extract(self, path, *, ocr=None):
        return [
            {"page": 2, "text": "Second fake page.\r\n"},
            {"page": 1, "text": "First fake page."},
        ]


class NeedsOcrBackend:
    name = "dict"
    version = "1"
    extensions = (".dictfmt",)

    def extract(self, path, *, ocr=None):
        return {"pages": [{"page": 1, "text": "Dict backend body."}], "needs_ocr": True}


class RegistryTests(ExtractTestCase):
    def test_register_and_extract_custom_format(self):
        path = self.write("custom.fakefmt", "some bytes")
        extract.register_extractor(FakeBackend())
        backend = extract.get_extractor(path)
        self.assertEqual(backend.identity(), {"name": "fake", "version": "9"})
        result = extract.extract(path)
        self.assertEqual(result["pages_count"], 2)
        self.assertEqual([page["page"] for page in result["pages"]], [1, 2])
        self.assertEqual(result["pages"][0]["text"], "First fake page.")
        self.assertEqual(result["pages"][1]["text"], "Second fake page.")
        self.assertEqual(result["extractor"], {"name": "fake", "version": "9"})
        self.assertIn("fake@9", extract.available_extractors())

    def test_registered_dict_result_carries_needs_ocr(self):
        path = self.write("custom.dictfmt", "some bytes")
        extract.register_extractor(NeedsOcrBackend())
        result = extract.extract(path)
        self.assertTrue(result["needs_ocr"])
        self.assertEqual(result["pages_count"], 1)

    def test_backend_with_a_wrong_api_version_is_rejected(self):
        class StaleBackend:
            api = 0
            name = "stale"
            version = "1"
            extensions = (".stalefmt",)

            def extract(self, path, *, ocr=None):
                return []

        with self.assertRaises(IngestError) as caught:
            extract.register_extractor(StaleBackend())
        self.assertIn("interface version", str(caught.exception))

    def test_unknown_extension_still_raises(self):
        with self.assertRaises(IngestError) as caught:
            extract.get_extractor("file.unknown")
        self.assertIn("'.unknown'", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
