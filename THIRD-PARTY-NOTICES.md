# Third-party notices

peekseed itself is licensed under the MIT License (see `LICENSE`).

It depends on the following third-party Python packages. They are installed by
the user's package manager and are not vendored into this repository; the
licenses below are recorded for convenience and must be verified against each
package's own distribution when redistributing a bundled environment.

| Package | License | Role |
|---|---|---|
| FastAPI | MIT | HTTP API |
| Uvicorn | BSD-3-Clause | ASGI server |
| Pydantic | MIT | request models |
| HTTPX | BSD-3-Clause | outbound model calls |
| NumPy | BSD-3-Clause | vector storage/scoring |
| pypdf | BSD-3-Clause | PDF text extraction |
| python-multipart | Apache-2.0 | multipart upload parsing |
| PyMuPDF (optional extra) | AGPL-3.0 or commercial | optional PDF extractor/renderer |

Vendored static assets:

| Asset | License | Role |
|---|---|---|
| PDF.js 6.3.289 (`peekseed/web/pdfjs/`, generic build + web viewer) | Apache-2.0 | page viewer for citations; range-only loading (see `peekseed/web/pdfjs/LOCAL-PATCHES.md`) |
| Adobe CMap resources (`peekseed/web/pdfjs/web/cmaps/`) | BSD-3-Clause (Adobe) | CJK font CMaps used by PDF.js |

Notes:

- **PyMuPDF is optional** and is not required by the default install. Its AGPL
  license is not compatible with distributing a proprietary combined work
  without a commercial license; users who enable it are responsible for that
  choice.
- The single-file web UI (`peekseed/web/index.html`) is original to this
  project. Page citations open the vendored PDF.js viewer at `/pdfjs/` (served
  as static files, no build step); the viewer ships with its upstream Apache-2.0
  `LICENSE` and the PDF.js icons under `web/images/`.
- The PDF.js wasm decoders under `peekseed/web/pdfjs/web/wasm/` ship their own
  upstream license files (`LICENSE_OPENJPEG`, `LICENSE_JBIG2`, `LICENSE_QCMS`,
  ...) next to the binaries; they are permissive (BSD-2-Clause-style) and are
  included unmodified.
- No third-party document corpus, model weights, fonts or sample data are
  included in this repository.
