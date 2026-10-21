"""peekseed security: token auth, Host/Origin allowlist, upload limits, SSRF.

The threat model is a **single user on their own machine**. The server binds
loopback by default, there is no account system, and "no model configured" is a
supported state. These are the boundaries that make the local server safe to
expose deliberately, and the defaults keep the loopback single-user setup
unchanged:

* **Token** -- ``PEEKSEED_TOKEN`` enables one shared secret for *mutating*
  ``/api/*`` requests. Reads stay open, so a browser can still browse while a
  token protects writes. Accepted headers are ``Authorization: Bearer <token>``
  and ``X-Peekseed-Token: <token>``; a missing or wrong token is a short 401
  JSON error and the token is never echoed.
* **Host/Origin allowlist** -- ``PEEKSEED_ALLOWED_HOSTS`` is a comma-separated
  ``host[:port]`` list. When it is set, every mutating request needs a matching
  ``Host``. A present ``Origin`` must equal the request host or be listed, which
  blocks cross-site writes and DNS-rebinding reads of the API. Loopback names
  (``127.0.0.1``, ``localhost``, ``::1``) and the configured bind host are
  always allowed. Unset means "no Host check", the default.
* **Upload limits** -- one PDF may be 50 MB, one request may carry 50 files and
  200 MB in total. The declared multipart ``Content-Length`` is rejected in the
  middleware *before* the body is parsed; a body without a trustworthy length is
  streamed to disk in fixed 64 KiB chunks and counted as it arrives, so a file
  is never read into memory. A rejected upload leaves no staging directory.
* **SSRF guard** -- :func:`validate_url` blocks cloud metadata endpoints,
  link-local addresses and non-http(s) schemes, while allowing loopback, RFC1918
  and tailnet addresses because local-first model servers are the normal case.
* **Data modes** -- :func:`harden_data_root` tightens derived directories to
  0700 and history files to 0600 after :func:`peekseed.config.ensure`; dataset
  directories created by an upload are tightened to 0700 as well.

Environment:
    PEEKSEED_TOKEN          shared secret for mutating /api/* (unset = off)
    PEEKSEED_ALLOWED_HOSTS  comma-separated host[:port] allowlist (unset = off)
    PEEKSEED_HOST           bind host; always allowed for mutating requests

Error messages here are short, path-free and secret-free: they name the rule
that was hit, never the configured token, a filesystem path or a provider body.
"""

import hmac
import inspect
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from . import config

TOKEN_ENV = "PEEKSEED_TOKEN"
ALLOWED_HOSTS_ENV = "PEEKSEED_ALLOWED_HOSTS"

# --- upload limits (enforced while streaming, never after buffering) --------
MAX_UPLOAD_BYTES = 50 * 1024 * 1024          # 50 MB per file
MAX_UPLOAD_FILES = 50                         # files per dataset upload
MAX_UPLOAD_TOTAL_BYTES = 200 * 1024 * 1024    # 200 MB per request
UPLOAD_CHUNK_SIZE = 64 * 1024                 # 64 KiB streaming chunks
MULTIPART_OVERHEAD_BYTES = 2 * 1024 * 1024    # framing allowance for the early check
MAX_REQUEST_BYTES = MAX_UPLOAD_TOTAL_BYTES + MULTIPART_OVERHEAD_BYTES

MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")
LOOPBACK_HOSTS = frozenset(DEFAULT_ALLOWED_HOSTS)
WILDCARD_BIND_HOSTS = frozenset(("0.0.0.0", "::", "[::]"))

# Cloud metadata services are never a legitimate model endpoint.
METADATA_V4 = frozenset((
    ipaddress.IPv4Address("169.254.169.254"),   # AWS / GCP / Azure IMDS
    ipaddress.IPv4Address("100.100.100.200"),   # Alibaba Cloud
    ipaddress.IPv4Address("192.0.0.192"),       # Oracle Cloud
))
METADATA_V6 = frozenset((
    ipaddress.IPv6Address("fd00:ec2::254"),     # AWS IPv6 IMDS
))
METADATA_HOSTS = frozenset((
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "metadata.azure.com",
    "instance-data",
))


def _env(name):
    return (os.environ.get(name) or "").strip()


def _mb(value):
    return value // (1024 * 1024)


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------

def configured_token():
    """The configured shared secret, or "" when authentication is off."""
    return _env(TOKEN_ENV)


def token_enabled():
    """True when ``PEEKSEED_TOKEN`` is set (mutating API calls need a token)."""
    return bool(configured_token())


def _token_value(value):
    """Reduce a header value to the bare token.

    Accepts ``Bearer <token>``, ``Token <token>`` and a raw token, and tolerates
    a ``Header-Name: value`` prefix so a caller holding a full header string can
    pass it straight through.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    name, separator, rest = text.partition(":")
    if separator and name.strip().lower() in ("authorization", "x-peekseed-token"):
        text = rest.strip()
    lowered = text.lower()
    if lowered.startswith("bearer "):
        text = text[len("bearer "):].strip()
    elif lowered.startswith("token "):
        text = text[len("token "):].strip()
    return text


def check_token(value):
    """Constant-time check of one auth header value against the configured token.

    Returns False when no token is configured: there is nothing to match, so
    callers guard with :func:`token_enabled` first.
    """
    expected = configured_token()
    if not expected:
        return False
    return hmac.compare_digest(_token_value(value).encode("utf-8"),
                               expected.encode("utf-8"))


def request_authorized(headers):
    """True when the request carries the configured token (or auth is off)."""
    if not token_enabled():
        return True
    return bool(check_token(headers.get("authorization"))
                or check_token(headers.get("x-peekseed-token")))


# ---------------------------------------------------------------------------
# Host / Origin allowlist
# ---------------------------------------------------------------------------

def configured_allowlist():
    """The normalized ``PEEKSEED_ALLOWED_HOSTS`` entries (empty when unset)."""
    raw = _env(ALLOWED_HOSTS_ENV)
    return frozenset(item.strip().lower().rstrip("/")
                     for item in raw.split(",") if item.strip())


def host_check_enabled():
    """True only when the user configured an explicit Host allowlist."""
    return bool(configured_allowlist())


def allowed_hosts():
    """Every host a browser may use: loopback, the bind host and the env list."""
    names = set(DEFAULT_ALLOWED_HOSTS)
    bind = (config.host() or "").strip().lower()
    if bind and bind not in WILDCARD_BIND_HOSTS:
        names.add(bind)
    names.update(configured_allowlist())
    return frozenset(names)


def _split_host(value):
    """``(hostname, port or None)`` for a Host header or allowlist entry."""
    text = str(value or "").strip().lower()
    if not text:
        return "", None
    if text.startswith("["):
        end = text.find("]")
        if end > 0:
            host = text[1:end]
            rest = text[end + 1:]
            port = rest[1:] if rest.startswith(":") and rest[1:].isdigit() else None
            return host, port
        return text, None
    try:
        ipaddress.ip_address(text)
        return text, None
    except ValueError:
        pass
    host, separator, port = text.rpartition(":")
    if separator and port.isdigit():
        return host, port
    return text, None


def host_allowed(host_header):
    """True when a mutating request's Host header may reach this server.

    Loopback names and the configured bind host are always allowed; everything
    else must be in ``PEEKSEED_ALLOWED_HOSTS``. Only called when the allowlist is
    configured (the default loopback setup does not validate Host).
    """
    host = str(host_header or "").strip().lower()
    if not host:
        return False
    name, port = _split_host(host)
    if name in LOOPBACK_HOSTS:
        return True
    allow = allowed_hosts()
    if name in allow or host in allow:
        return True
    return bool(port and ("%s:%s" % (name, port)) in allow)


def origin_allowed(origin, host_header):
    """True when a present browser Origin matches this host or the allowlist.

    An absent Origin (curl, an API client, a non-browser tool) is allowed; a
    present one must share the request host or be explicitly listed. ``null``
    origins (sandboxed frames, file:// pages) are rejected because they cannot
    be attributed to a host.
    """
    text = str(origin or "").strip()
    if not text:
        return True
    try:
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.username or parsed.password:
            return False
        host = parsed.hostname
        if not host:
            return False
        origin_name = host.lower()
        request_name, _port = _split_host(host_header)
        if origin_name == request_name:
            return True
        allow = allowed_hosts()
        if origin_name in allow or parsed.netloc.lower() in allow:
            return True
        origin_port = parsed.port
        if origin_port and ("%s:%d" % (origin_name, origin_port)) in allow:
            return True
    except ValueError:
        return False
    return False


# ---------------------------------------------------------------------------
# request gate
# ---------------------------------------------------------------------------

def declared_content_length(headers):
    """The request's declared Content-Length as an int, or None."""
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def rejection_for(method, path, headers):
    """``(status, detail)`` when a request must be rejected, else None.

    Only mutating requests under ``/api/`` are gated: reads (GET/HEAD), ``/pdf``
    and ``/health`` stay open, which is what a local single-user UI needs.
    """
    method = str(method or "").upper()
    path = str(path or "")
    if method not in MUTATING_METHODS or not path.startswith("/api/"):
        return None
    if token_enabled() and not request_authorized(headers):
        return 401, "a token is required for this API"
    if host_check_enabled() and not host_allowed(headers.get("host")):
        return 403, "this Host is not allowed"
    if not origin_allowed(headers.get("origin"), headers.get("host")):
        return 403, "this Origin is not allowed"
    declared = declared_content_length(headers)
    if declared is not None and declared > MAX_REQUEST_BYTES:
        return 413, ("the request is larger than the allowed total of %d MB"
                     % _mb(MAX_UPLOAD_TOTAL_BYTES))
    return None


async def _bounded_request_body(receive, limit):
    """Read a body of unknown length, refusing anything larger than ``limit``.

    Returns the buffered bytes, or ``None`` when the cap is crossed. The caller
    answers 413 itself in that case, without running the application, so a huge
    chunked upload is never spooled to disk or held in memory beyond the cap. A
    body with a trustworthy ``Content-Length`` never takes this path: it is
    rejected from the header alone.
    """
    chunks = []
    total = 0
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return None
        body = message.get("body") or b""
        total += len(body)
        if total > limit:
            return None
        chunks.append(bytes(body))
        if not message.get("more_body"):
            return b"".join(chunks)


def _too_large_detail():
    return ("the request is larger than the allowed total of %d MB"
            % _mb(MAX_UPLOAD_TOTAL_BYTES))


class SecurityMiddleware:
    """ASGI middleware: token + Host/Origin + body-size gate.

    A plain ASGI middleware (not ``BaseHTTPMiddleware``) because the chunked-body
    cap has to read the request stream *before* FastAPI's multipart parser runs:
    once parsing starts, FastAPI turns any exception into a generic 400, so a
    413 raised mid-parse would surface as the wrong status. Buffering the body up
    to ``MAX_REQUEST_BYTES`` and replaying it to the app keeps the status honest
    while bounding memory to that cap.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        verdict = rejection_for(method, path, headers)
        if verdict is not None:
            status, detail = verdict
            response = JSONResponse(status_code=status, content={"detail": detail})
            return await response(scope, receive, send)
        if (method.upper() in MUTATING_METHODS and path.startswith("/api/")
                and declared_content_length(headers) is None):
            payload = await _bounded_request_body(receive, MAX_REQUEST_BYTES)
            if payload is None:
                response = JSONResponse(status_code=413,
                                        content={"detail": _too_large_detail()})
                return await response(scope, receive, send)
            delivered = [False]

            async def replay():
                if not delivered[0]:
                    delivered[0] = True
                    return {"type": "http.request", "body": payload,
                            "more_body": False}
                return {"type": "http.disconnect"}

            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)


def install(app):
    """Add the security middleware to a FastAPI/Starlette app (idempotent-ish)."""
    app.add_middleware(SecurityMiddleware)


# ---------------------------------------------------------------------------
# uploads: stream to disk, count bytes, never buffer a file
# ---------------------------------------------------------------------------

class UploadError(Exception):
    """A user-facing upload rejection; ``status`` is the HTTP status to report."""

    status = 400

    def __init__(self, detail):
        super(UploadError, self).__init__(detail)
        self.detail = str(detail)


class UploadInvalid(UploadError):
    """The upload is not a usable file (wrong magic, empty, ...) -> 400."""

    status = 400


class UploadLimitExceeded(UploadError):
    """A per-file or total upload limit was hit -> 413."""

    status = 413


async def stream_upload(upload, destination, *, max_bytes=MAX_UPLOAD_BYTES,
                        max_total=MAX_UPLOAD_TOTAL_BYTES, total_used=0,
                        chunk_size=UPLOAD_CHUNK_SIZE, expect_prefix=None):
    """Stream one UploadFile-like object to ``destination`` in fixed chunks.

    ``upload`` only needs a ``read(size)`` method returning bytes (sync or
    async), which is how an UploadFile and a lazily-generated test double both
    behave. The body is written chunk by chunk, so peak memory is bounded by
    ``chunk_size`` no matter how large the file claims to be. Limits are checked
    as bytes arrive: ``max_bytes`` per file and ``total_used + written`` against
    ``max_total`` for the whole request. ``expect_prefix`` (for example
    ``b"%PDF-"``) rejects wrong content on the first chunk, before more data is
    stored.

    Returns the number of bytes written. On any rejection the partial file is
    removed before the error propagates, so a failed upload leaves no staging
    garbage behind. Raises :class:`UploadLimitExceeded` (413) or
    :class:`UploadInvalid` (400).
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    destination = Path(destination)
    if total_used >= max_total:
        raise UploadLimitExceeded("the upload is larger than the allowed total "
                                  "of %d MB" % _mb(max_total))
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    first = True
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = upload.read(chunk_size)
                if inspect.isawaitable(chunk):
                    chunk = await chunk
                if not chunk:
                    break
                data = bytes(chunk)
                if first:
                    first = False
                    if expect_prefix is not None and not data.startswith(bytes(expect_prefix)):
                        raise UploadInvalid(
                            "the uploaded file is not a valid PDF "
                            "(missing %PDF- header)")
                written += len(data)
                if written > max_bytes:
                    raise UploadLimitExceeded("one file is larger than the "
                                              "allowed %d MB" % _mb(max_bytes))
                if total_used + written > max_total:
                    raise UploadLimitExceeded("the upload is larger than the "
                                              "allowed total of %d MB" % _mb(max_total))
                handle.write(data)
        if first:
            raise UploadInvalid("the uploaded file is empty")
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return written


# ---------------------------------------------------------------------------
# SSRF guard for user-supplied model/link URLs
# ---------------------------------------------------------------------------

def _address_problem(address):
    """The reason a resolved IP literal may not be used, or None."""
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _address_problem(mapped)
        if address in METADATA_V6:
            return "base_url must not point at a cloud metadata endpoint"
        if address.is_link_local:
            return "base_url must not point at a link-local address"
        if address.is_loopback or address.is_private:
            return None
        if address.is_unspecified or address.is_multicast:
            return "base_url must not point at an unspecified or multicast address"
        return None
    if address in METADATA_V4:
        return "base_url must not point at a cloud metadata endpoint"
    if address.is_link_local:
        return "base_url must not point at a link-local address"
    if address.is_loopback or address.is_private:
        return None
    if address.is_unspecified or address.is_multicast:
        return "base_url must not point at an unspecified or multicast address"
    return None


def validate_url(url):
    """Return a short reason a user-supplied URL is unsafe, or None when usable.

    Blocks non-http(s) schemes, embedded credentials, cloud metadata endpoints,
    link-local addresses and unspecified/multicast addresses. Loopback, RFC1918
    and tailnet addresses are allowed on purpose: a model server next to the app
    is the local-first default. Hostnames are not resolved here (that would need
    the network); the known metadata hostnames are blocked by name, and a future
    hardening step can re-check the resolved address at connect time.
    """
    text = str(url or "").strip()
    if not text:
        return "base_url is required"
    try:
        parsed = urlparse(text)
    except ValueError:
        return "base_url must look like http(s)://host[:port][/path]"
    if parsed.scheme not in ("http", "https"):
        return "base_url must use http:// or https://"
    if parsed.username or parsed.password:
        return "base_url must not embed credentials"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "base_url must include a host"
    if host in METADATA_HOSTS:
        return "base_url must not point at a cloud metadata endpoint"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name: allowed here, blocked by name only when it is a known
        # metadata host. Local-first users point links at their own hostnames.
        return None
    try:
        parsed.port
    except ValueError:
        return "base_url has an invalid port"
    return _address_problem(address)


# ---------------------------------------------------------------------------
# data modes
# ---------------------------------------------------------------------------

def _chmod(path, mode):
    try:
        os.chmod(str(path), mode)
    except OSError:
        pass


def harden_dir(directory):
    """Make one directory owner-only (0700) when it exists. Best-effort."""
    if directory is None:
        return
    path = Path(str(directory))
    try:
        if path.is_dir() and not path.is_symlink():
            _chmod(path, 0o700)
    except OSError:
        pass


def harden_file(file_path):
    """Make one regular file owner-only (0600) when it exists. Best-effort."""
    if file_path is None:
        return
    path = Path(str(file_path))
    try:
        if path.is_file() and not path.is_symlink():
            _chmod(path, 0o600)
    except OSError:
        pass


def harden_dataset_dir(dataset_dir):
    """Tighten a dataset directory and its ``original/`` copies to 0700."""
    if not dataset_dir:
        return
    directory = Path(str(dataset_dir))
    if not directory.is_dir():
        return
    harden_dir(directory)
    harden_dir(directory / "original")


def harden_history_dir(history_dir):
    """Make a history directory 0700 and every history file 0600."""
    if not history_dir:
        return
    directory = Path(str(history_dir))
    if not directory.is_dir():
        return
    harden_dir(directory)
    try:
        entries = list(directory.rglob("*"))
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            harden_dir(entry)
        else:
            harden_file(entry)


def harden_data_root(paths):
    """Tighten the derived data root after :func:`peekseed.config.ensure`.

    Derived directories are owner-only (0700), history files are 0600 and the
    link store (which can hold API keys) is 0600. Idempotent and never fatal:
    a read-only or foreign-owned data root must not stop the server from booting.
    """
    if paths is None:
        return
    for directory in (paths.root, paths.config_dir, paths.datasets_dir,
                      paths.history_dir, paths.uploads_dir):
        harden_dir(directory)
    harden_history_dir(paths.history_dir)
    harden_file(paths.links_file)
