"""PDF.js page-viewer wiring (fast citation deep links).

Citations deep-link into the vendored PDF.js viewer
(`/pdfjs/web/viewer.html?file=/pdf/<dataset>/<file>#page=N`) instead of handing
the whole PDF to the browser's native viewer. The viewer is configured for
range-only loading (`disableAutoFetch`/`disableStream` in
`peekseed/web/pdfjs/web/viewer.mjs`), so it paints the cited page from the byte
ranges it needs; `/pdf` answers Range requests. These tests cover the static
assets, the citation URL shape, the native-viewer fallback and Range serving.
"""

import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.fixtures import make_pdf, temp_data_root

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "peekseed" / "web" / "pdfjs"


class PdfViewerTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from peekseed import app as app_module
        app_module.reset_state()
        return TestClient(app_module.create_app())

    def test_vendored_viewer_assets_are_served(self):
        self.assertTrue((VIEWER / "web" / "viewer.html").is_file())
        with temp_data_root():
            client = self._client()
            page = client.get("/pdfjs/web/viewer.html")
            self.assertEqual(page.status_code, 200, page.text)
            self.assertIn("PDF.js viewer", page.text)
            script = client.get("/pdfjs/build/pdf.mjs")
            self.assertEqual(script.status_code, 200)
            self.assertIn("javascript", script.headers["content-type"])
            worker = client.get("/pdfjs/build/pdf.worker.mjs")
            self.assertEqual(worker.status_code, 200)
            locale = client.get("/pdfjs/web/locale/zh-CN/viewer.ftl")
            self.assertEqual(locale.status_code, 200)
            cmap = client.get("/pdfjs/web/cmaps/Adobe-Japan1-UCS2.bcmap")
            self.assertEqual(cmap.status_code, 200)

    def test_pdf_url_deep_links_into_viewer_with_page(self):
        from peekseed import app as app_module

        url = app_module._pdf_url("ds 1", {"source": "a b.pdf",
                                           "page_start": 7})
        parts = urlsplit(url)
        self.assertEqual(parts.path, "/pdfjs/web/viewer.html")
        self.assertEqual(parse_qs(parts.query)["file"],
                         ["/pdf/ds%201/a%20b.pdf"])
        self.assertEqual(parts.fragment, "page=7")
        # a chunk without a source/page has no link rather than a dead one
        self.assertEqual(app_module._pdf_url("ds", {"source": "a.pdf"}), "")
        self.assertEqual(app_module._pdf_url("ds", {"page_start": 3}), "")

    def test_native_viewer_fallback_when_assets_are_absent(self):
        from peekseed import app as app_module

        with mock.patch.object(app_module, "_pdfjs_available",
                               return_value=False):
            url = app_module._pdf_url("ds", {"source": "a.pdf",
                                             "page_start": 3})
        self.assertEqual(url, "/pdf/ds/a.pdf#page=3")

    def test_pdf_route_is_inline_and_answers_range_requests(self):
        with temp_data_root() as root:
            client = self._client()
            pdf = make_pdf(root / "manual.pdf.tmp", pages=["hello"])
            data = pdf.read_bytes()
            created = client.post(
                "/api/datasets", data={"name": "Docs"},
                files=[("files", ("manual.pdf", data, "application/pdf"))])
            self.assertEqual(created.status_code, 201, created.text)
            dataset_id = created.json()["dataset"]["id"]
            full = client.get("/pdf/%s/manual.pdf" % dataset_id)
            self.assertEqual(full.status_code, 200, full.text)
            self.assertIn("inline", full.headers["content-disposition"])
            self.assertEqual(full.headers.get("accept-ranges"), "bytes")
            self.assertEqual(full.content, data)
            partial = client.get("/pdf/%s/manual.pdf" % dataset_id,
                                 headers={"Range": "bytes=0-99"})
            self.assertEqual(partial.status_code, 206, partial.text)
            self.assertEqual(partial.headers["content-range"],
                             "bytes 0-99/%d" % len(data))
            self.assertEqual(partial.content, data[:100])


if __name__ == "__main__":
    unittest.main()
