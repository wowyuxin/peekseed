# Vendored PDF.js viewer (Apache-2.0) — local patches

`peekseed/web/pdfjs/` is an upstream **PDF.js 6.3.289** distribution (`build/` +
`web/`, generic build) trimmed for this app: only the en-US/zh-CN/zh-TW locales
are shipped, and the `.map` files, debugger, sample PDF and other locales are
removed. It is mounted at `/pdfjs/` by `peekseed/app.py`; search/ask/agent
citations deep-link to
`/pdfjs/web/viewer.html?file=%2Fpdf%2F<dataset>%2F<file>#page=N`.

## Range-only loading (`web/viewer.mjs`)

Two `AppOptions` defaults are flipped from `false` to `true`:

| option | upstream | here | why |
|---|---|---|---|
| `disableAutoFetch` | `false` | `true` | upstream keeps requesting every 64 KiB chunk until the whole file has been pulled |
| `disableStream` | `false` | `true` | with streaming on, PDF.js keeps the full-file reader and ignores range requests |

Together they make the viewer fetch **only the byte ranges the rendered page
needs** instead of the whole document. `GET /pdf/{dataset}/{filename}` answers
`Range` requests (`206 Partial Content`, `Accept-Ranges: bytes`), so this is a
pure viewer-side switch; a client that still wants the old behaviour moves the
two values back to `false`.

Measured in the previous deployment (smart-search, 2026-10-11; headless Chrome,
over Tailscale):

| deep link | before | after |
|---|---|---|
| `pcie` (31 MB, **not** linearized) `#page=536` | 16.7 s to paint, 32.0 MB | 3.1 s to paint, 1.6 MB |
| `cxl` (12 MB, linearized) `#page=536` | 7.7 s to paint, 12.1 MB | 1.7 s to paint, 0.6 MB |

Local (loopback) measurement of the same CXL PDF through this deployment:
`#page=700` paints in ~0.3 s with ~0.26 MB of the 12 MB PDF fetched (plus the
viewer assets, cached after the first open). The page and its neighbours are
rendered as they scroll into view; nothing else is downloaded.

### Known characteristic: `#search=` links still read the whole document

`PDFFindController.#extractText()` extracts the text of *every* page before a
URL-hash search settles, so a link carrying `#search=` still transfers the
entire document in the background — one 64 KiB range request at a time. The
target page and its highlight are on screen long before that finishes, and the
traffic stops as soon as the page is navigated away. peekseed citation links do
not pass `#search=`; only `#page=N` is used.

`disableStream`/`disableAutoFetch` are also `PREFERENCE` options, but this build
never writes `pdfjs.preferences` to `localStorage` (no UI calls
`preferences.set`), so no stored value overrides these defaults.

## Re-applying on refresh

`web/viewer.mjs` carries the two patch comments inline (`LOCAL PATCH`); when
updating the vendored distribution, re-apply the two `value: true` flips above
and re-trim the locales. `LICENSE` is the upstream Apache-2.0 text; the Adobe
CMap resources under `web/cmaps/` are covered by their own copyright (same
license) — see `THIRD-PARTY-NOTICES.md` in the repository root.
