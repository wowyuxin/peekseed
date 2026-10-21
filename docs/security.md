# Security model (0.1.0)

peekseed runs **on your machine, for you**. The default bind is loopback and
there are no accounts, tenants or remote users. The security work is therefore
about keeping that single-user server safe *by default* and making it safe to
expose deliberately: a browser write must come from a page you actually opened,
a model link must not be able to reach cloud metadata, and an upload must never
exhaust memory or escape the dataset directory.

## Threat model

Defended:

* **Cross-site writes** — a page the user visits must not be able to POST to the
  local API (CSRF): a mutating request whose `Origin` host differs from its
  `Host` is rejected. When `PEEKSEED_ALLOWED_HOSTS` is configured, a DNS name
  that resolves to loopback (DNS rebinding) is rejected too because the `Host`
  is pinned; without an allowlist, same-name rebinding is not distinguishable and
  writes are protected by the token (or by binding loopback only).
* **Deliberate LAN/remote exposure** — a shared token gates every mutating
  `/api/*` call, and an explicit Host allowlist pins the names the service may be
  reached by.
* **Hostile uploads** — zip-slip filenames, traversal and symlinked `/pdf`
  reads, oversized or too-many files, and wrong file types are rejected before
  they can write outside `original/` or consume RAM.
* **Model links as an SSRF path** — a user-supplied `base_url` may not point at
  cloud metadata endpoints, link-local addresses or non-http(s) schemes.
* **Accidental disclosure of provider responses** — a failed link probe reports
  a status, never the provider body, API key or machine path.

Not defended (out of scope for a single-user local tool):

* Multiple users, per-user permissions, and audit logging.
* A compromised local account or a process that can already read the data root.
* TLS termination: bind to loopback or put the server behind a reverse proxy if
  it must leave the machine.
* DNS rebinding that resolves a hostname *after* validation; see the SSRF note.

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `PEEKSEED_TOKEN` | Shared secret required on mutating `/api/*` requests | unset = auth off |
| `PEEKSEED_ALLOWED_HOSTS` | Comma-separated `host[:port]` allowlist for `Host`/`Origin` | unset = Host check off |
| `PEEKSEED_HOST` | Bind host; always accepted for mutating requests | `127.0.0.1` |
| `PEEKSEED_DATA` | Derived data root (created 0700) | `./data` |

With **both** `PEEKSEED_TOKEN` and `PEEKSEED_ALLOWED_HOSTS` unset, behaviour is
the plain local single-user loopback server: reads and writes are open, and only
a hostile `Origin` (when a browser sends one) is rejected.

When `PEEKSEED_TOKEN` is set, mutating routes (`POST`/`PUT`/`PATCH`/`DELETE`
under `/api/`) must send one of:

```
Authorization: Bearer <token>
X-Peekseed-Token: <token>
```

A missing or wrong token is `401` with a short JSON detail; the token is never
echoed. `GET`/`HEAD` requests stay open, so browsing the UI never needs the
token. The bundled web UI does not yet attach the token itself, so with a token
set, browser writes need a small UI change (tracked as a follow-up) or a reverse
proxy that injects the header.

When `PEEKSEED_ALLOWED_HOSTS` is set (for example `192.168.1.10:8030,box.lan`),
every mutating request must carry a matching `Host`. Loopback names
(`127.0.0.1`, `localhost`, `::1`) and the configured bind host are always
allowed. If a request carries an `Origin`, its host must equal the request host
or be in the allowlist; `Origin: null` and non-http(s) origins are rejected. An
absent `Origin` (curl, API clients) is allowed.

## Upload limits

Constants live in [`peekseed/security.py`](../peekseed/security.py):

| Limit | Value |
|---|---|
| `MAX_UPLOAD_BYTES` (one file) | 50 MB (`50 * 1024 * 1024`) |
| `MAX_UPLOAD_FILES` (one dataset) | 50 |
| `MAX_UPLOAD_TOTAL_BYTES` (one request) | 200 MB (`200 * 1024 * 1024`) |
| `UPLOAD_CHUNK_SIZE` (streaming) | 64 KiB |
| `MULTIPART_OVERHEAD_BYTES` (framing allowance) | 2 MB |

The middleware rejects a declared multipart `Content-Length` larger than
`MAX_UPLOAD_TOTAL_BYTES + MULTIPART_OVERHEAD_BYTES` with `413` **before the body
is parsed**, so a 10 GB request never spools to disk. Bodies without a
trustworthy length are streamed to the uploads staging area in 64 KiB chunks and
counted as they arrive (`400` for wrong magic/empty, `413` when a limit is hit);
the partial file and the staging directory are removed on every path. Filenames
are sanitised before any path is built (zip-slip safe), the extension and `%PDF-`
magic are checked before more data is stored, and the dataset directory is
created only after every file passed validation.

## `/pdf` is inline and read-only

`GET /pdf/<dataset>/<file>` serves the dataset's own copy from `original/`
inline (`Content-Disposition: inline`) so a browser opens its PDF viewer and
`#page=N` jumps to the chunk's page. Names containing path separators, `..`,
absolute paths, encoded traversal or NUL are rejected; the resolved path must
stay inside `original/` (`Path.relative_to` on resolved paths), so a symlink that
escapes the dataset is never served. Reads are not gated by the token.

## File modes

`config.ensure()` creates the derived tree; startup then tightens it
(idempotently, best-effort): the data root and its derived directories
(`config/`, `datasets/`, `history/`, `uploads/`) are `0700`, history files are
`0600`, and the link store (which can hold API keys) is `0600`. Dataset
directories are created by an upload with `0700` for the dataset and its
`original/` copies. A failed chmod is logged nowhere and never stops boot: a
read-only data root must not take the UI down.

## SSRF note for model links

`security.validate_url()` is applied to `base_url` on link create/update. It
blocks cloud metadata addresses (`169.254.169.254`, `fd00:ec2::254`,
`100.100.100.200`, `192.0.0.192` and their hostnames), link-local and
unspecified/multicast addresses, embedded credentials and non-http(s) schemes.
Loopback, RFC1918 and tailnet (`100.64.0.0/10`) addresses are deliberately
allowed — a model server next to the app is the local-first default.

Hostnames are not resolved during validation (that would need the network), so a
name that resolves to a metadata address later is not caught here. `peekseed.links`
still owns URL syntax validation; two follow-ups belong there rather than in the
app: (1) apply `validate_url` to links seeded from `PEEKSEED_*` environment
variables at first-run, and (2) re-check the resolved address at connect time to
close the DNS-rebinding gap.
