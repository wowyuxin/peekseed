"""Local model processes: optionally start/stop a machine's own model servers.

peekseed does not ship or supervise a model. Some deployments, though, run the
embedder and the reranker as local processes on the same machine, and a small
control panel should be able to start and stop them. This module is the
*optional*, machine-local half of that: a JSON file next to the model links
says which serve script owns each stage, on which port, and which embedding
models it can serve.

The file is READ-ONLY config (the API never writes it) and lives at
``<data_root>/config/local_models.json`` unless ``PEEKSEED_LOCAL_MODELS_FILE``
names another path. It is machine-local runtime state: it holds absolute paths
to scripts, not secrets.

Trust boundary. This file can name a command the service will run, so it is
treated as root-equivalent config for this feature: it must be a regular file
owned by the service user, not writable by group or others, in a directory that
is not writable by group or others. It is opened with ``O_NOFOLLOW`` and the
opened file is re-checked with ``fstat`` (a swap between the stat and the open
is refused). The same ownership/permission rules are applied to the serve
script, both when loading the config and again immediately before every
execution; a script whose inode changed since load is refused. A config file
that cannot be trusted is an error, never a silent fallback. A *missing* file
is the supported "no local models" state: every other feature works unchanged.

Schema (version 1)::

    {"version": 1, "stages": {
        "embed": {"label": "Embedding model",
                  "script": "/abs/path/serve-embedder.sh",
                  "port_env": "PORT",
                  "health_url": "http://127.0.0.1:8000/health",
                  "models": [{"id": "Qwen3-VL-Embedding-2B-8bit-mlx",
                              "label": "Qwen3-VL 2B - 8bit"}]},
        "rerank": {"label": "Reranker",
                   "script": "/abs/path/serve-reranker.sh",
                   "port_env": "RERANK_PORT",
                   "health_url": "http://127.0.0.1:8001/health"}}}

A stage entry is valid only when its script is a regular, owner-writable-only
executable file and its health URL is a loopback http(s) URL with an explicit
port. A link is "locally controllable" only when its role has an entry AND its
``base_url`` points at the same loopback port as the stage's health URL, so a
remote link can never be handed a local command.

A serve script is called with one action word (``start`` or ``stop``) and, for
the embed stage only, an optional model id as the next argument. The stage's
``port_env`` variable is set to the port taken from the link's ``base_url``,
which is how a link can move a process to another port without editing the
script. The child gets a minimal environment (PATH/HOME/LANG/TMPDIR plus the
port), never the service's own variables, so a script cannot echo service
secrets into an API error. ``start_timeout``/``stop_timeout`` (seconds) are
optional per stage.
"""

import copy
import json
import os
import re
import stat
import subprocess
import threading
import time
from urllib.parse import urlparse

import httpx

from . import config

FILE_ENV = "PEEKSEED_LOCAL_MODELS_FILE"
FILE_NAME = "local_models.json"
# Keep in step with links.STAGE_ROLES: answer links are remote endpoints by
# definition, so they are never locally controllable.
STAGE_ROLES = ("embed", "rerank")
DEFAULT_PORT_ENV = {"embed": "PORT", "rerank": "RERANK_PORT"}
DEFAULT_LABELS = {"embed": "Embedding model", "rerank": "Reranker"}
VERSION = 1
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PORT_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")
MAX_LABEL = 80
MAX_FILE_BYTES = 64 * 1024
MAX_TAIL = 200
HEALTH_TIMEOUT = 2.0
START_WAIT = 15.0
STOP_WAIT = 10.0
DEFAULT_START_TIMEOUT = 300
DEFAULT_STOP_TIMEOUT = 60
STAGE_FIELDS = ("label", "script", "port_env", "health_url", "models",
                "start_timeout", "stop_timeout")
# A start/stop request is a read-modify-write of external process state, and
# two panel tabs can issue one at the same time. The per-role lock covers the
# WHOLE operation (probe, script, post-check), so a second call sees the first
# one's outcome instead of racing it.
_LOCKS = {role: threading.Lock() for role in STAGE_ROLES}

# mtime+size(+inode) cache, keyed by path: the panel polls every few seconds,
# so an unchanged file must cost one lstat. The inode matters because a rewrite
# may keep the same size.
_CACHE = None
_CACHE_KEY = None
_CACHE_LOCK = threading.Lock()
# (dev, ino) of each script at load time; a change before execution is refused.
_SCRIPT_IDS = {}


class LocalModelsError(Exception):
    """The config file or a script cannot be trusted/read (HTTP 500).

    ``str`` names the file and the broken field, never the file's contents.
    """


class NotLocalError(ValueError):
    """A link is not a local process this service may start/stop (HTTP 400)."""


class StartError(RuntimeError):
    """A local process could not be started or stopped (HTTP 502 at the API)."""


def reset_cache():
    """Drop the file cache (tests point PEEKSEED_LOCAL_MODELS_FILE elsewhere)."""
    global _CACHE, _CACHE_KEY, _SCRIPT_IDS
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_KEY = None
        _SCRIPT_IDS = {}


def config_path():
    """The local-model file: PEEKSEED_LOCAL_MODELS_FILE, else <data_root>/config."""
    override = os.environ.get(FILE_ENV)
    if override:
        return os.path.abspath(override)
    return os.path.abspath(os.path.join(str(config.paths().config_dir), FILE_NAME))


def load(force=False):
    """The validated config, or an empty one when no file exists (a copy)."""
    global _CACHE, _CACHE_KEY, _SCRIPT_IDS
    path = config_path()
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        with _CACHE_LOCK:
            _CACHE = {"version": VERSION, "stages": {}}
            _CACHE_KEY = None
            _SCRIPT_IDS = {}
        return copy.deepcopy(_CACHE)
    except OSError as exc:
        raise LocalModelsError("cannot stat local model config %s: %s"
                               % (path, exc))

    key = (path, st.st_mtime_ns, st.st_size, st.st_ino)
    with _CACHE_LOCK:
        if _CACHE is not None and not force and _CACHE_KEY == key:
            return copy.deepcopy(_CACHE)

    _check_config_file(path)
    # O_NOFOLLOW turns "the file became a symlink" into an error at open time;
    # fstat on the opened descriptor then proves we read the file we checked.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LocalModelsError("cannot open local model config %s: %s"
                               % (path, exc))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            raise LocalModelsError("%s changed while it was being read" % path)
        if opened.st_size > MAX_FILE_BYTES:
            raise LocalModelsError("%s is larger than %d bytes"
                                   % (path, MAX_FILE_BYTES))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            try:
                raw = json.load(handle)
            except ValueError as exc:
                raise LocalModelsError("cannot read local model config %s: %s"
                                       % (path, exc))
    finally:
        if fd is not None:
            os.close(fd)

    cfg = _validate(raw, path)
    with _CACHE_LOCK:
        _CACHE = cfg
        _CACHE_KEY = key
    return copy.deepcopy(cfg)


def configured():
    """Whether any stage has a local process config."""
    return bool(load()["stages"])


def stage(role):
    """One validated stage entry (a copy), or None."""
    return copy.deepcopy(load()["stages"].get(role))


def models():
    """The configured embedding models: ``[{"model", "label"}]``."""
    entry = load()["stages"].get("embed") or {}
    return [{"model": item["id"], "label": item["label"]}
            for item in entry.get("models", [])]


def _link_port(link):
    """The explicit port of a link's base_url, or None."""
    base = (link or {}).get("base_url") or ""
    try:
        parsed = urlparse(base)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.hostname not in LOOPBACK_HOSTS:
        return None
    return port


def local_stage(link):
    """The stage entry that owns this link, or None when it is not local."""
    role = (link or {}).get("role")
    if role not in STAGE_ROLES:
        return None
    entry = stage(role)
    if entry is None:
        return None
    port = _link_port(link)
    if port is None:
        return None
    health_port = urlparse(entry["health_url"]).port
    if port != health_port:
        return None
    return entry


def capabilities(link):
    """What the UI may do with this link: ``{"start_stop": bool, "models": [...]}``."""
    try:
        entry = local_stage(link)
    except LocalModelsError:
        return {"start_stop": False, "models": []}
    return {
        "start_stop": entry is not None,
        "models": ([item["id"] for item in entry.get("models", [])]
                   if entry is not None and entry.get("models") else []),
    }


def _probe_url(url, timeout=None):
    """``(ok, data)`` from one /health URL -- never raises."""
    try:
        with httpx.Client(timeout=timeout or HEALTH_TIMEOUT) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            data = {}
        return True, data
    except Exception as exc:  # noqa: BLE001 - a probe failure is data
        return False, {"error": "%s: %s" % (type(exc).__name__, exc)}


def probe(link):
    """Probe the stage's configured health URL (not the link's base + /health).

    A link's base_url may carry a path (e.g. ``/v1`` for an OpenAI-compatible
    embedder); the config names the exact health endpoint, so that is what is
    asked. A non-local link falls back to its own ``base_url + /health``.
    """
    entry = local_stage(link)
    url = entry["health_url"] if entry is not None else (
        ((link or {}).get("base_url") or "").rstrip("/") + "/health")
    if not url:
        return False, {"error": "no health URL"}
    return _probe_url(url)


def status(link):
    """The panel's readout for one stage link: running/model/label/detail."""
    entry = local_stage(link)
    label = ((entry or {}).get("label") or (link or {}).get("label") or "")
    ok, data = probe(link)
    if not ok:
        return {"running": False, "model": "", "label": label,
                "detail": str(data.get("error") or "not reachable")}
    out = {"running": True, "model": "", "label": label, "detail": ""}
    model = data.get("model") or (link or {}).get("model") or ""
    if model:
        out["model"] = str(model)
    for key in ("dim", "threshold", "prompt"):
        value = data.get(key)
        if isinstance(value, (int, float, str)):
            out[key] = value
    return out


def start(link, model=None):
    """Run the stage's ``start`` action and report the resulting status."""
    entry = local_stage(link)
    if entry is None:
        raise NotLocalError(
            "link '%s' is not a local process this service can start; "
            "add a matching local_models.json entry or use the link instead"
            % (link or {}).get("id"))
    role = link.get("role")
    args = []
    if role == "embed":
        chosen = model if model is not None else (link.get("model") or "")
        if chosen:
            if not isinstance(chosen, str) or not MODEL_ID_RE.match(chosen):
                raise NotLocalError("model must be a plain model id")
            configured_ids = [item["id"] for item in entry.get("models", [])]
            if configured_ids and chosen not in configured_ids:
                raise NotLocalError(
                    "unknown embedding model '%s' (configured: %s)"
                    % (chosen, ", ".join(configured_ids)))
            args.append(chosen)
    elif model:
        raise NotLocalError("the rerank stage takes no model argument")

    with _LOCKS[role]:
        # Idempotent: starting a model that is already serving is not an error.
        before = status(link)
        if before.get("running"):
            return before
        _check_script(role, entry)
        _run_script(role, entry, link, "start", args, entry["start_timeout"])
        for _ in range(int(START_WAIT)):
            out = status(link)
            if out.get("running"):
                return out
            time.sleep(1)
        return status(link)


def stop(link):
    """Run the stage's ``stop`` action; fail loudly if the endpoint still answers."""
    entry = local_stage(link)
    if entry is None:
        raise NotLocalError(
            "link '%s' is not a local process this service can stop"
            % (link or {}).get("id"))
    role = link.get("role")
    with _LOCKS[role]:
        _check_script(role, entry)
        _run_script(role, entry, link, "stop", [], entry["stop_timeout"])
        for _ in range(int(STOP_WAIT)):
            if not probe(link)[0]:
                return {"running": False, "link_id": (link or {}).get("id"),
                        "label": entry.get("label") or ""}
            time.sleep(1)
        raise StartError(
            "the %s stop script finished, but the endpoint still answers at %s"
            % (role, entry["health_url"]))


def _check_config_file(path):
    """Ownership/permission proof for the config file before it is trusted."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise LocalModelsError("%s is not a regular file" % path)
    if st.st_uid != os.geteuid():
        raise LocalModelsError(
            "%s is not owned by the service user (uid %d vs %d)"
            % (path, st.st_uid, os.geteuid()))
    if st.st_mode & 0o022:
        raise LocalModelsError(
            "%s is writable by other users (mode %04o); chmod 600 it"
            % (path, stat.S_IMODE(st.st_mode)))
    _check_directory(path, os.path.dirname(path))


def _check_directory(path, directory):
    try:
        st = os.lstat(directory)
    except OSError as exc:
        raise LocalModelsError("cannot stat %s: %s" % (directory, exc))
    if not stat.S_ISDIR(st.st_mode):
        raise LocalModelsError("%s is not a directory" % directory)
    if st.st_mode & 0o022:
        raise LocalModelsError(
            "%s is writable by other users (mode %04o); local model config "
            "and scripts must live in a directory only the owner can write"
            % (directory, stat.S_IMODE(st.st_mode)))


def _check_script(role, entry):
    """Re-validate the serve script immediately before executing it."""
    path = entry["script"]
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise LocalModelsError("%s is not a regular file (symlinks are refused)"
                               % path)
    if st.st_uid != os.geteuid():
        raise LocalModelsError(
            "%s is not owned by the service user (uid %d vs %d)"
            % (path, st.st_uid, os.geteuid()))
    if st.st_mode & 0o022:
        raise LocalModelsError(
            "%s is writable by other users (mode %04o)"
            % (path, stat.S_IMODE(st.st_mode)))
    if not st.st_mode & 0o100:
        raise LocalModelsError("%s is not executable" % path)
    _check_directory(path, os.path.dirname(path))
    with _CACHE_LOCK:
        changed = _SCRIPT_IDS.get(role)
    if changed is not None and (st.st_dev, st.st_ino) != changed:
        raise LocalModelsError(
            "%s changed since the config was loaded; reload the service" % path)


def _run_script(role, entry, link, action, args, timeout):
    script = entry["script"]
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "USER": os.environ.get("USER", ""),
    }
    port = _link_port(link)
    if port is not None:
        env[entry["port_env"]] = str(port)
    try:
        proc = subprocess.run([script, action] + list(args), env=env,
                              capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError:
        raise StartError("the %s script is missing: %s" % (role, script))
    except subprocess.TimeoutExpired:
        raise StartError(
            "the %s script did not finish within %d s" % (role, timeout))
    except OSError as exc:
        raise StartError("cannot run the %s script: %s" % (role, exc))
    if proc.returncode != 0:
        tail = _tail(proc.stderr or proc.stdout or "")
        raise StartError("the %s script failed (exit %d)%s"
                         % (role, proc.returncode,
                            (": " + tail) if tail else ""))


def _tail(text):
    """The last non-empty output line, truncated -- with known secrets removed."""
    lines = [line.strip() for line in str(text or "").splitlines()
             if line.strip()]
    tail = lines[-1] if lines else ""
    token = os.environ.get("PEEKSEED_TOKEN") or ""
    if token:
        tail = tail.replace(token, "<redacted>")
    return tail[-MAX_TAIL:] if tail else ""


def _validate(raw, path):
    if not isinstance(raw, dict):
        raise LocalModelsError("%s must contain a JSON object" % path)
    unknown = [key for key in raw if key not in ("version", "stages")]
    if unknown:
        raise LocalModelsError("%s: unknown top-level field(s): %s"
                               % (path, ", ".join(sorted(unknown))))
    version = raw.get("version")
    if isinstance(version, bool) or version != VERSION:
        raise LocalModelsError("%s: version must be the integer %d"
                               % (path, VERSION))
    stages = raw.get("stages")
    if not isinstance(stages, dict):
        raise LocalModelsError("%s: stages must be an object" % path)

    out = {"version": VERSION, "stages": {}}
    script_ids = {}
    for role, entry in stages.items():
        if role not in STAGE_ROLES:
            raise LocalModelsError(
                "%s: stage '%s' is not locally controllable (allowed: %s)"
                % (path, role, ", ".join(STAGE_ROLES)))
        clean = _validate_stage(role, entry, path)
        out["stages"][role] = clean
        st = os.lstat(clean["script"])
        script_ids[role] = (st.st_dev, st.st_ino)
    global _SCRIPT_IDS
    with _CACHE_LOCK:
        _SCRIPT_IDS = script_ids
    return out


def _validate_stage(role, entry, path):
    where = "%s: stages.%s" % (path, role)
    if not isinstance(entry, dict):
        raise LocalModelsError("%s must be an object" % where)
    unknown = [key for key in entry if key not in STAGE_FIELDS]
    if unknown:
        raise LocalModelsError("%s: unknown field(s): %s"
                               % (where, ", ".join(sorted(unknown))))

    script = entry.get("script")
    if not isinstance(script, str) or not os.path.isabs(script):
        raise LocalModelsError("%s.script must be an absolute path" % where)
    if not os.path.exists(script):
        raise LocalModelsError("%s.script does not exist: %s" % (where, script))
    _check_script_path(role, script, where)

    health_url = entry.get("health_url")
    if not isinstance(health_url, str):
        raise LocalModelsError("%s.health_url must be a string" % where)
    try:
        parsed = urlparse(health_url)
        port = parsed.port
    except ValueError:
        raise LocalModelsError("%s.health_url has an invalid port" % where)
    if parsed.scheme not in ("http", "https"):
        raise LocalModelsError("%s.health_url must be http(s)" % where)
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise LocalModelsError(
            "%s.health_url must point at a loopback host (127.0.0.1, "
            "localhost or ::1)" % where)
    if port is None:
        raise LocalModelsError(
            "%s.health_url must name an explicit port" % where)

    port_env = entry.get("port_env", DEFAULT_PORT_ENV[role])
    if not isinstance(port_env, str) or not PORT_ENV_RE.match(port_env):
        raise LocalModelsError(
            "%s.port_env must match ^[A-Z][A-Z0-9_]{0,39}$" % where)

    label = entry.get("label", DEFAULT_LABELS[role])
    if not isinstance(label, str):
        raise LocalModelsError("%s.label must be a string" % where)
    label = label.strip() or DEFAULT_LABELS[role]
    if len(label) > MAX_LABEL:
        raise LocalModelsError("%s.label is longer than %d characters"
                               % (where, MAX_LABEL))

    models = entry.get("models", [])
    if not isinstance(models, list):
        raise LocalModelsError("%s.models must be a list" % where)
    if role != "embed" and models:
        raise LocalModelsError(
            "%s.models is only meaningful for the embed stage" % where)
    seen = set()
    clean_models = []
    for index, item in enumerate(models):
        at = "%s.models[%d]" % (where, index)
        if not isinstance(item, dict):
            raise LocalModelsError("%s must be an object" % at)
        unknown = [key for key in item if key not in ("id", "label")]
        if unknown:
            raise LocalModelsError("%s: unknown field(s): %s"
                                   % (at, ", ".join(sorted(unknown))))
        model_id = item.get("id")
        if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id):
            raise LocalModelsError(
                "%s.id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$" % at)
        if model_id in seen:
            raise LocalModelsError("%s.id is a duplicate: %s" % (at, model_id))
        seen.add(model_id)
        item_label = item.get("label", model_id)
        if not isinstance(item_label, str):
            raise LocalModelsError("%s.label must be a string" % at)
        item_label = item_label.strip() or model_id
        if len(item_label) > MAX_LABEL:
            raise LocalModelsError("%s.label is longer than %d characters"
                                   % (at, MAX_LABEL))
        clean_models.append({"id": model_id, "label": item_label})

    start_timeout = entry.get("start_timeout", DEFAULT_START_TIMEOUT)
    if (isinstance(start_timeout, bool) or not isinstance(start_timeout, int)
            or not 10 <= start_timeout <= 900):
        raise LocalModelsError(
            "%s.start_timeout must be an integer between 10 and 900" % where)
    stop_timeout = entry.get("stop_timeout", DEFAULT_STOP_TIMEOUT)
    if (isinstance(stop_timeout, bool) or not isinstance(stop_timeout, int)
            or not 5 <= stop_timeout <= 300):
        raise LocalModelsError(
            "%s.stop_timeout must be an integer between 5 and 300" % where)

    clean = {"label": label, "script": script, "port_env": port_env,
             "health_url": health_url, "start_timeout": start_timeout,
             "stop_timeout": stop_timeout}
    if role == "embed":
        clean["models"] = clean_models
    return clean


def _check_script_path(role, script, where):
    st = os.lstat(script)
    if not stat.S_ISREG(st.st_mode):
        raise LocalModelsError(
            "%s.script must be a regular file, not a symlink: %s"
            % (where, script))
    if st.st_uid != os.geteuid():
        raise LocalModelsError(
            "%s.script is not owned by the service user: %s" % (where, script))
    if st.st_mode & 0o022:
        raise LocalModelsError(
            "%s.script is writable by other users (mode %04o): %s"
            % (where, stat.S_IMODE(st.st_mode), script))
    if not st.st_mode & 0o100:
        raise LocalModelsError(
            "%s.script is not executable: %s" % (where, script))
    _check_directory(script, os.path.dirname(script))
