"""peekseed configuration: one data root owns every mutable path.

The framework is meant to be shared and run on someone else's machine, so no
path, host, model or document name is baked into the code. Everything the user
creates at runtime -- model links, datasets, indexes, search history -- lives
under ONE configurable root:

    PEEKSEED_DATA   (default: <workspace>/.peekseed)

The default is derived rather than relative: :mod:`peekseed.workspace` resolves
the user's workspace directory (``./workspace`` by default) and the derived tree
lives in its hidden ``.peekseed`` child, so one directory can be backed up. The
environment variable still overrides the derived root on its own, which keeps
older deployments and tests working unchanged.

Layout under that root (created on first use):

    <root>/manifest.json           scan state; schema 1
    <root>/config/links.json       model links; 0600; keys masked in responses
    <root>/config/settings.json    optional UI title/tagline/examples
    <root>/datasets/<id>/          uploaded sources, chunks.jsonl, index/<space>/
    <root>/extracted/<sha256>.json extraction cache
    <root>/history/                search + dialog history
    <root>/jobs/<id>.json          persisted job records
    <root>/uploads/                temporary staging for an upload in progress

Keeping the data root separate from the code is what makes updates safe: a new
checkout of peekseed never touches a user's datasets, and a user's data can be
backed up or moved as one directory.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8030
DATA_ENV = "PEEKSEED_DATA"
HOST_ENV = "PEEKSEED_HOST"
PORT_ENV = "PEEKSEED_PORT"

# The UI reads these; a user can override the copy in config/settings.json
# without editing code. The defaults are deliberately plain: the framework
# does not ship a product identity or sample questions about any domain.
DEFAULT_SETTINGS = {
    "version": 1,
    "title": "peekseed",
    "tagline": "Search, ask and research your own documents",
    "examples": [],
}


class Paths(object):
    """Resolved paths for one data root. Immutable for a given process.

    ``root`` is the derived tree (``<workspace>/.peekseed`` by default or the
    ``PEEKSEED_DATA`` override). ``workspace_root``/``documents_dir`` name the
    user-owned side so callers never have to re-derive them.
    """

    __slots__ = ("root", "workspace_root", "documents_dir", "config_dir",
                 "links_file", "settings_file", "datasets_dir", "extracted_dir",
                 "manifest_file", "history_dir", "jobs_dir", "uploads_dir")

    def __init__(self, root, workspace_root=None, documents_dir=None):
        root = Path(root)
        self.root = root
        self.workspace_root = Path(workspace_root) if workspace_root else root
        self.documents_dir = (
            Path(documents_dir)
            if documents_dir
            else self.workspace_root / "documents"
        )
        self.config_dir = root / "config"
        self.links_file = self.config_dir / "links.json"
        self.settings_file = self.config_dir / "settings.json"
        self.datasets_dir = root / "datasets"
        self.extracted_dir = root / "extracted"
        self.manifest_file = root / "manifest.json"
        self.history_dir = root / "history"
        self.jobs_dir = root / "jobs"
        self.uploads_dir = root / "uploads"

    def as_dict(self):
        return {
            "root": str(self.root),
            "workspace_root": str(self.workspace_root),
            "documents_dir": str(self.documents_dir),
            "config_dir": str(self.config_dir),
            "links_file": str(self.links_file),
            "settings_file": str(self.settings_file),
            "datasets_dir": str(self.datasets_dir),
            "extracted_dir": str(self.extracted_dir),
            "manifest_file": str(self.manifest_file),
            "history_dir": str(self.history_dir),
            "jobs_dir": str(self.jobs_dir),
            "uploads_dir": str(self.uploads_dir),
        }


_CACHE = None
_CACHE_ROOT = None
_CACHE_LOCK = threading.RLock()


def data_root():
    """The configured data root (the derived tree), resolved absolute.

    A relative ``PEEKSEED_DATA`` is resolved against the current working
    directory, which is what a user typing ``PEEKSEED_DATA=./data peekseed
    serve`` expects. Without it the root follows the resolved workspace.
    """
    raw = os.environ.get(DATA_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    from . import workspace

    return workspace.resolve().derived_dir


def paths():
    """Paths for the current data root (cached until the root changes)."""
    global _CACHE, _CACHE_ROOT
    raw = os.environ.get(DATA_ENV)
    if raw:
        root = Path(raw).expanduser().resolve()
        workspace_root = root
        documents_dir = root / "documents"
    else:
        from . import workspace

        resolved = workspace.resolve()
        root = resolved.derived_dir
        workspace_root = resolved.root
        documents_dir = resolved.documents_dir
    with _CACHE_LOCK:
        if _CACHE is None or _CACHE_ROOT != root:
            _CACHE = Paths(root, workspace_root=workspace_root,
                           documents_dir=documents_dir)
            _CACHE_ROOT = root
        return _CACHE


def reset_cache():
    """Drop the cached paths. Tests call this after changing PEEKSEED_DATA."""
    global _CACHE, _CACHE_ROOT
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_ROOT = None


def ensure():
    """Create the data root layout if needed and return its paths.

    Called by the server at startup and by every write path that might run
    before the server does (the CLI). mkdir is idempotent, so this is cheap.
    ``documents/`` is user-owned and created by :func:`peekseed.workspace.init`,
    not here.
    """
    p = paths()
    for directory in (p.config_dir, p.datasets_dir, p.history_dir,
                      p.extracted_dir, p.jobs_dir, p.uploads_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return p


def resolve_env_file(path=None):
    """Find the .env a CLI run should load, or None.

    Candidates, in order: an explicit ``path`` or ``PEEKSEED_ENV_FILE``, the
    current working directory, the project root (the directory that contains the
    package). The server is normally started through ``python -m peekseed`` from
    the project directory, so the project root is where a user's `.env` lives.
    """
    explicit = path or os.environ.get("PEEKSEED_ENV_FILE")
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    for candidate in (Path.cwd() / ".env",
                      Path(__file__).resolve().parent.parent / ".env"):
        if candidate.is_file():
            return candidate
    return None


def load_env_file(path=None):
    """Apply KEY=VALUE defaults from a .env file to os.environ.

    Real environment variables win (setdefault semantics), so
    ``PEEKSEED_PORT=9000 python -m peekseed serve`` still overrides the file.
    Format: comments, an optional ``export`` prefix, optional surrounding
    quotes, and ``#`` inline comments on unquoted values.
    """
    env_file = resolve_env_file(path)
    if env_file is None:
        return None
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value
    return env_file


def read_json(path, default=None):
    """Read a JSON file, returning ``default`` when it is missing or unreadable.

    A corrupt file is not silently replaced on read: callers that care (the link
    store) raise their own error so the user is told what is wrong. This helper
    is for optional, non-critical files such as settings.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data, mode=0o644):
    """Write JSON with a same-directory temp file and os.replace.

    A crash can leave the temp file behind but can never leave a half-written
    config that the next request would read as corrupt. Secrets (the link store)
    pass mode=0o600 so the file is never group/world readable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_settings():
    """The effective settings: built-in defaults merged with settings.json."""
    data = dict(DEFAULT_SETTINGS)
    stored = read_json(paths().settings_file, default=None)
    if isinstance(stored, dict):
        for key in ("title", "tagline"):
            value = stored.get(key)
            if isinstance(value, str) and value.strip():
                data[key] = value.strip()
        examples = stored.get("examples")
        if isinstance(examples, list):
            data["examples"] = [str(x) for x in examples if str(x).strip()]
    data["data_root"] = str(paths().root)
    return data


def save_settings(data):
    """Persist the editable subset of settings; returns the effective settings.

    Unknown keys are ignored rather than stored: settings.json is a small,
    user-editable file, not a second config surface.
    """
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    current = load_settings()
    title = data.get("title", current["title"])
    tagline = data.get("tagline", current["tagline"])
    examples = data.get("examples", current["examples"])
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(tagline, str):
        raise ValueError("tagline must be a string")
    if not isinstance(examples, list):
        raise ValueError("examples must be a list of strings")
    clean = {
        "version": 1,
        "title": title.strip(),
        "tagline": tagline.strip(),
        "examples": [str(x).strip() for x in examples if str(x).strip()],
    }
    write_json_atomic(paths().settings_file, clean)
    return load_settings()


def host():
    """Bind host: loopback by default because this runs on the user's machine.

    Set PEEKSEED_HOST=0.0.0.0 deliberately to expose it on a LAN; there is no
    authentication in this step, so the safe default is not exposed.
    """
    value = (os.environ.get(HOST_ENV) or "").strip()
    return value or DEFAULT_HOST


def port():
    """Bind port: PEEKSEED_PORT, else 8030."""
    raw = (os.environ.get(PORT_ENV) or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("PEEKSEED_PORT must be an integer")
    if not (0 < value < 65536):
        raise ValueError("PEEKSEED_PORT must be between 1 and 65535")
    return value
