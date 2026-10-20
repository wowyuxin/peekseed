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

Notes:

- **PyMuPDF is optional** and is not required by the default install. Its AGPL
  license is not compatible with distributing a proprietary combined work
  without a commercial license; users who enable it are responsible for that
  choice. The default extractor is `pypdf`, which is permissively licensed.
- The web UI is original to this project, is a single HTML file, and does not
  bundle a third-party PDF viewer. Page citations open the browser's native PDF
  viewer served from the local API.
- No third-party document corpus, model weights, fonts, icons, or sample data
  are included in this repository.
