"""Model links: the user's own endpoints, configured, validated and masked.

peekseed does not ship a model. Every model is a *link* the user adds in the UI:
the embedder that turns text into vectors, an optional reranker, and one or more
answer models. The link store is a pure config module -- it owns the file, the
schema, validation and the enabled-link normalization; it never imports the web
app or talks to the network.

File location comes from the data root (config/links.json). It never stores an
API key: a link names the environment variable that holds one (``api_key_env``,
e.g. ``PEEKSEED_ANSWER_API_KEY``), so keys live only in
``deploy/env/peekseed.env``. The file is written atomically with mode 0600 and
every response goes through mask(): the browser learns the variable name and
whether the key is set, never the key itself.

Schema (version 1):

    {"version": 1, "links": [
        {"id": "my-embed", "role": "embed", "label": "My embedder",
         "protocol": "openai", "base_url": "http://127.0.0.1:11434/v1",
         "api_key_env": "", "model": "nomic-embed-text", "enabled": true,
         "chunk": {"target_chars": 8000, "max_chars": 12000, "image_cap": 3}},
        ...
    ]}

``api_key_env`` is optional; when set it must match
``^PEEKSEED_[A-Z0-9_]+_API_KEY$`` so a UI user cannot point a link at another
secret (for example ``PEEKSEED_TOKEN``) and send it to a URL they control.

An embed link's ``chunk`` sizes pick the peelfeed chunk set its index is built
from (``c<target>-<max>-i<cap>``; links with equal sizes share one set). An embed
link without sizes cannot build an index. Other roles always store ``null``.

Roles and protocols:

    embed   openai  POST {base_url}/embeddings            (OpenAI-compatible)
            custom  POST {base_url}/embed                 (custom text array)
    rerank  custom  POST {base_url}/rerank                (score documents)
    answer  openai  POST {base_url}/chat/completions      (streaming chat)

At most one embed link and one rerank link may be enabled at a time; any number
of answer links may be enabled (the UI shows a per-question picker). Enabling a
stage link disables its siblings, which is why the link just enabled wins.

First run writes an empty config. There are no baked-in default endpoints,
models or keys: a missing or corrupt model must never be mistaken for a working
default.
"""

import copy
import json
import os
import re
import threading
from urllib.parse import urlparse

from . import config, security

ROLES = ("embed", "rerank", "answer")
STAGE_ROLES = ("embed", "rerank")
PROTOCOLS = {
    "embed": ("openai", "custom"),
    "rerank": ("custom",),
    "answer": ("openai",),
}
DEFAULT_PROTOCOL = {"embed": "openai", "rerank": "custom", "answer": "openai"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
#: The only variable names a link may read its key from (a security rule).
_KEY_ENV_RE = re.compile(r"^PEEKSEED_[A-Z0-9_]+_API_KEY$")
MAX_LABEL = 80
MAX_ID = 40
VERSION = 1
FIELDS = ("id", "role", "label", "protocol", "base_url", "api_key_env", "model",
          "enabled", "chunk")
CHUNK_KEYS = ("target_chars", "max_chars", "image_cap")
#: peelfeed's chunk packer never puts more than 3 images in one chunk.
MAX_CHUNK_IMAGES = 3
MAX_CHUNK_CHARS = 200000


class LinkConfigError(Exception):
    """The config FILE cannot be read, parsed or validated.

    ``detail`` names the file and the broken field, never a key value. The web
    layer reports this as HTTP 500 JSON; hot paths fall back to "no model
    configured", which is a supported state.
    """


class LinkValidationError(ValueError):
    """One submitted link is invalid (HTTP 400 at the API).

    Holds every error string, so the UI can show all of them rather than only
    the first one. ``str(e)`` is the joined detail the API returns.
    """

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# mtime+size(inode) cache: the API resolves links on every request, so an
# unchanged file must cost one os.stat. The inode matters because our own
# writes are atomic (os.replace swaps the file) and may keep the same size.
_CACHE = None
_CACHE_KEY = None
# One write lock per process: FastAPI runs these sync handlers in a threadpool,
# so two tabs can issue read-modify-write cycles at once. Atomic replace stops
# a corrupt file, not a lost update.
_WRITE_LOCK = threading.RLock()


def config_path():
    """The links file for the current data root."""
    return config.paths().links_file


def reset_cache():
    """Drop the file cache (tests point PEEKSEED_HOME elsewhere)."""
    global _CACHE, _CACHE_KEY
    _CACHE = None
    _CACHE_KEY = None


def _signature(path):
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _normalize_base(url):
    return (url or "").strip().rstrip("/")


def _base_error(base):
    """The validation message for one base_url, or None when it is usable.

    The syntax checks are local and precise; the SSRF checks are delegated to
    :func:`peekseed.security.validate_url`, so every path that can create a link
    (API, CLI, a hand-edited links.json) gets the same
    metadata/link-local/non-http(s) guard.
    """
    if not base:
        return "base_url is required"
    if not isinstance(base, str):
        return "base_url must be a string"
    if any(c.isspace() for c in base):
        return "base_url must not contain whitespace"
    try:
        p = urlparse(base)
    except ValueError:
        return "base_url must look like http(s)://host[:port][/path]"
    if p.scheme not in ("http", "https") or not p.netloc or not p.hostname:
        return "base_url must look like http(s)://host[:port][/path]"
    try:
        p.port
    except ValueError:
        return "base_url has an invalid port"
    return security.validate_url(base)


def api_key(link):
    """The API key for ``link``, read from the environment at call time ("" if none)."""
    name = str((link or {}).get("api_key_env") or "")
    if not name or not _KEY_ENV_RE.match(name):
        return ""
    return os.environ.get(name, "")


def _blank_link(role, label=""):
    return {
        "id": "",
        "role": role,
        "label": label,
        "protocol": DEFAULT_PROTOCOL.get(role, ""),
        "base_url": "",
        "api_key_env": "",
        "model": "",
        "enabled": True,
        "chunk": None,
    }


def _chunk_sizes(value, role):
    """Normalize one submitted ``chunk`` value; returns ``(sizes_or_None, errors)``."""
    if value is None:
        return None, []
    if role != "embed":
        return None, ["chunk sizes apply to embed links only"]
    if not isinstance(value, dict) or set(value) != set(CHUNK_KEYS):
        return None, ["chunk must be an object with target_chars, max_chars and "
                      "image_cap (or null)"]
    sizes = {}
    for key in CHUNK_KEYS:
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, int):
            return None, ["chunk.%s must be an integer" % key]
        sizes[key] = number
    if not 0 < sizes["target_chars"] <= sizes["max_chars"] <= MAX_CHUNK_CHARS:
        return None, ["chunk sizes need 0 < target_chars <= max_chars <= %d"
                      % MAX_CHUNK_CHARS]
    if not 1 <= sizes["image_cap"] <= MAX_CHUNK_IMAGES:
        return None, ["chunk.image_cap must be between 1 and %d" % MAX_CHUNK_IMAGES]
    return sizes, []


def chunk_set_name(sizes):
    """The peelfeed chunk set for these sizes (``c8000-12000-i3``), or ""."""
    if not isinstance(sizes, dict):
        return ""
    return "c%d-%d-i%d" % tuple(sizes[key] for key in CHUNK_KEYS)


def _auto_id(label, role, taken):
    """A fresh id from the LABEL when the UI sends none, uniquified."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", (label or "").strip().lower())
    slug = slug.strip("-._")[:MAX_ID]
    if not slug or not re.match(r"^[a-z0-9]", slug):
        slug = role if role in ROLES else "link"
    base = slug
    n = 2
    while slug in taken:
        suffix = "-%d" % n
        slug = base[:MAX_ID - len(suffix)].strip("-._") + suffix
        n += 1
    return slug


def validate(data, existing=None):
    """Normalize one submitted link; returns ``(link, errors)``.

    ``existing`` is the current link on update: omitted fields keep their value
    and id/role may not change. On create, id and role are required (create()
    generates the id from the label when the UI sends none).

    The returned link always has every schema field, so callers can persist it
    directly; ``errors`` is empty exactly when it is safe to persist.
    """
    if not isinstance(data, dict):
        return {}, ["link fields must be a JSON object"]
    errors = []
    for key in data:
        if key not in FIELDS and key != "api_key":
            errors.append("unknown field '%s'" % key)

    link = copy.deepcopy(existing) if isinstance(existing, dict) else {}

    def given(key, default):
        return data[key] if key in data else link.get(key, default)

    link_id = given("id", None)
    if not isinstance(link_id, str) or not link_id:
        errors.append("id is required")
        link_id = ""
    elif not ID_RE.match(link_id):
        errors.append("id must match ^[a-z0-9][a-z0-9._-]{0,39}$")
    if existing is not None and "id" in data and data["id"] != existing.get("id"):
        errors.append("id is immutable after creation")

    role = given("role", None)
    if role not in ROLES:
        errors.append("role must be one of " + ", ".join(ROLES))
    if existing is not None and "role" in data and data["role"] != existing.get("role"):
        errors.append("role is immutable after creation")
    stage = role in STAGE_ROLES
    answer = role == "answer"

    label = given("label", "")
    if not isinstance(label, str):
        errors.append("label must be a string")
        label = ""
    else:
        label = label.strip()
        if not label:
            errors.append("label is required")
        elif len(label) > MAX_LABEL:
            errors.append("label is longer than %d characters" % MAX_LABEL)

    base_url = given("base_url", "")
    if not isinstance(base_url, str):
        errors.append("base_url must be a string")
        base_url = ""
    base_url = _normalize_base(base_url)
    base_err = _base_error(base_url)
    if base_err:
        errors.append(base_err)

    protocol = given("protocol", None)
    allowed = PROTOCOLS.get(role, ())
    if protocol in (None, ""):
        protocol = DEFAULT_PROTOCOL.get(role, "")
    elif protocol not in allowed:
        errors.append("protocol must be one of " + ", ".join(allowed)
                      + (" for %s links" % role if allowed else ""))
        protocol = DEFAULT_PROTOCOL.get(role, "")

    # API keys are never stored here: a link names the env variable that holds
    # one. The name pattern is a security rule (see _KEY_ENV_RE).
    if str(data.get("api_key") or "").strip():
        errors.append(
            "api_key is no longer stored in links.json; put the key in "
            "deploy/env/peekseed.env and set api_key_env to its variable name")
    api_key_env = given("api_key_env", "")
    if api_key_env is None:
        api_key_env = ""
    if not isinstance(api_key_env, str):
        errors.append("api_key_env must be a string")
        api_key_env = ""
    else:
        api_key_env = api_key_env.strip()
        if api_key_env and not _KEY_ENV_RE.match(api_key_env):
            errors.append("api_key_env must look like PEEKSEED_<NAME>_API_KEY")

    model = given("model", "")
    if model is None:
        model = ""
    if not isinstance(model, str):
        errors.append("model must be a string")
        model = ""
    else:
        model = model.strip()
        # An embedding space is identified by its link and model. Forgetting the
        # model would make two different embedders look interchangeable, so an
        # embed link must name the model it serves.
        if role == "embed" and not model:
            errors.append("model is required for embed links (the model id the "
                          "endpoint serves)")

    enabled = given("enabled", True)
    if not isinstance(enabled, bool):
        errors.append("enabled must be true or false")
        enabled = True

    chunk, chunk_errors = _chunk_sizes(given("chunk", None), role)
    errors.extend(chunk_errors)

    link = {
        "id": link_id,
        "role": role if role in ROLES else "",
        "label": label,
        "protocol": protocol,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "model": model,
        "enabled": enabled,
        "chunk": chunk,
    }
    return link, errors


def _normalize_enabled(cfg, keep_id=None):
    """At most ONE enabled link per stage role (embed, rerank).

    Enabling a link disables its siblings, which is why ``keep_id`` wins: the
    user just pressed "use" on it. A file that lists two enabled stage links by
    hand keeps the first one, so loading the same file twice is deterministic.
    Answer links have no such rule: any number may be enabled.
    """
    for role in STAGE_ROLES:
        enabled = [l for l in cfg["links"]
                   if l.get("role") == role and l.get("enabled")]
        if len(enabled) <= 1:
            continue
        winner = keep_id if any(l["id"] == keep_id for l in enabled) \
            else enabled[0]["id"]
        for link in enabled:
            if link["id"] != winner:
                link["enabled"] = False


def _validated_config(raw, path):
    """Validate a whole file's contents; raises LinkConfigError on any problem."""
    if not isinstance(raw, dict):
        raise LinkConfigError("model links config %s must be a JSON object" % path)
    version = raw.get("version", VERSION)
    if version != VERSION:
        raise LinkConfigError(
            "model links config %s has unsupported version %r (this version of "
            "peekseed reads version %d)" % (path, version, VERSION))
    raw_links = raw.get("links")
    if not isinstance(raw_links, list):
        raise LinkConfigError(
            "model links config %s is missing its 'links' list" % path)
    links_out, errors, seen = [], [], set()
    for i, item in enumerate(raw_links):
        if not isinstance(item, dict):
            errors.append("links[%d] must be a JSON object" % i)
            continue
        link, errs = validate(item)
        link_id = link.get("id")
        if link_id and link_id in seen:
            errs.append("duplicate id '%s'" % link_id)
        if errs:
            errors.extend("links[%d]: %s" % (i, e) for e in errs)
        else:
            links_out.append(link)
            seen.add(link_id)
    if errors:
        raise LinkConfigError("invalid model links config %s: %s"
                              % (path, "; ".join(errors)))
    return {"version": VERSION, "links": links_out}


def _write(cfg, path):
    """Atomic 0600 write: temp file in the same directory, then os.replace."""
    config.write_json_atomic(path, cfg, mode=0o600)


def _save(cfg):
    """Persist ``cfg`` and make it the cache (a read right after sees it)."""
    global _CACHE, _CACHE_KEY
    path = config_path()
    try:
        _write(cfg, path)
    except OSError as e:
        raise LinkConfigError("cannot write model links config %s: %s"
                              % (path, e))
    _CACHE = cfg
    _CACHE_KEY = _signature(path)


def load(force=False):
    """The whole config, cached until the file's mtime/size/inode changes.

    A missing file is written once as an empty config; a corrupt file raises
    LinkConfigError instead of crashing the service (callers fall back to "no
    model configured", which is a supported state).
    """
    global _CACHE, _CACHE_KEY
    path = config_path()
    if _CACHE is not None and not force:
        try:
            key = _signature(path)
        except OSError:
            key = None
        if key is not None and key == _CACHE_KEY:
            return _CACHE

    if not os.path.isfile(path):
        config.ensure()
        cfg = {"version": VERSION, "links": []}
        _normalize_enabled(cfg)
        _write(cfg, path)
    else:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            raise LinkConfigError(
                "cannot read model links config %s: %s" % (path, e))
        cfg = _validated_config(raw, path)
        _normalize_enabled(cfg)
    _CACHE = cfg
    _CACHE_KEY = _signature(path)
    return _CACHE


def _find(cfg, link_id):
    return next((l for l in cfg["links"] if l.get("id") == link_id), None)


def links(role=None):
    """Canonical link dicts in insertion order (a copy: callers cannot mutate cache)."""
    cfg = load()
    return [copy.deepcopy(l) for l in cfg["links"]
            if role is None or l.get("role") == role]


def get(link_id):
    cfg = load()
    link = _find(cfg, link_id)
    return copy.deepcopy(link) if link is not None else None


def active(role):
    """The enabled link of a stage role, or None ("off" is a supported state)."""
    if role not in STAGE_ROLES:
        return None
    for link in links(role):
        if link.get("enabled"):
            return link
    return None


def answer_links(enabled_only=True):
    """Answer links in insertion order -- any number may be enabled."""
    return [l for l in links("answer")
            if l.get("enabled") or not enabled_only]


def default_answer_link():
    """The answer link the page starts on: the first enabled one, else None."""
    found = answer_links()
    return found[0] if found else None


def embed_space_key(link):
    """The directory/identity key for one embedding space.

    An index is only comparable inside the vector space that produced it, so the
    key combines the link id and the model id the link names. A change to either
    is a different space; the UI then shows the dataset as needing an index in
    that space instead of silently querying vectors with the wrong width.
    """
    def slug(value):
        value = re.sub(r"[^a-z0-9._-]+", "-", (value or "").strip().lower())
        return value.strip("-._")[:48] or "default"
    return "%s__%s" % (slug(link.get("id")), slug(link.get("model")))


def mask(link):
    """The public view of one link: NEVER the key, plus what the UI may do."""
    role = link.get("role")
    return {
        "id": link.get("id"),
        "role": role,
        "label": link.get("label"),
        "protocol": link.get("protocol"),
        "base_url": link.get("base_url"),
        "api_key_env": link.get("api_key_env") or "",
        "api_key_set": bool(api_key(link)),
        "model": link.get("model"),
        "enabled": link.get("enabled"),
        "chunk": copy.deepcopy(link.get("chunk")),
        "chunk_set": chunk_set_name(link.get("chunk")),
        "capabilities": {
            "models": role == "answer",
        },
    }


def create(data):
    """Add one link. The id is generated from its label when the UI sends none."""
    if not isinstance(data, dict):
        raise LinkValidationError(["link fields must be a JSON object"])
    with _WRITE_LOCK:
        cfg = load()
        taken = {l["id"] for l in cfg["links"]}
        payload = dict(data)
        if not payload.get("id"):
            payload["id"] = _auto_id(str(payload.get("label") or ""),
                                     str(payload.get("role") or ""), taken)
        link, errors = validate(payload)
        if not errors and link["id"] in taken:
            errors.append("a link with id '%s' already exists" % link["id"])
        if errors:
            raise LinkValidationError(errors)
        new = copy.deepcopy(cfg)
        new["links"].append(link)
        _normalize_enabled(new, keep_id=link["id"] if link["enabled"] else None)
        _save(new)
        return link


def update(link_id, data):
    """Edit one link: omitted fields keep their value."""
    with _WRITE_LOCK:
        cfg = load()
        existing = _find(cfg, link_id)
        if existing is None:
            raise KeyError(link_id)
        if not isinstance(data, dict):
            raise LinkValidationError(["link fields must be a JSON object"])
        link, errors = validate(data, existing=existing)
        if errors:
            raise LinkValidationError(errors)
        new = copy.deepcopy(cfg)
        new["links"][new["links"].index(_find(new, link_id))] = link
        _normalize_enabled(new, keep_id=link["id"] if link["enabled"] else None)
        _save(new)
        return link


def delete(link_id):
    """Remove one link; False when it does not exist."""
    with _WRITE_LOCK:
        cfg = load()
        if _find(cfg, link_id) is None:
            return False
        new = copy.deepcopy(cfg)
        new["links"] = [l for l in new["links"] if l["id"] != link_id]
        _save(new)
        return True


def set_enabled(link_id, enabled):
    """Use/not-use one link, applying the per-role normalization for stage links."""
    if not isinstance(enabled, bool):
        raise LinkValidationError(["enabled must be true or false"])
    with _WRITE_LOCK:
        cfg = load()
        if _find(cfg, link_id) is None:
            raise KeyError(link_id)
        new = copy.deepcopy(cfg)
        link = _find(new, link_id)
        link["enabled"] = enabled
        _normalize_enabled(new, keep_id=link_id if enabled else None)
        _save(new)
        return link
