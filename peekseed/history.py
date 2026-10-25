#!/usr/bin/env python
"""peekseed history: one record of every search mode and every model dialog.

This module owns ALL history persistence for the service. The app and the page
never touch files, indexes or eviction; they call the :class:`HistoryStore`
interface below.

The design goal, restated: *every search the user can see, and every model dialog
they have had about it, is recoverable after a refresh — from one history list,
in the mode it was produced, with its type updated when a model touches it — and
any of those dialogs can be continued without losing the thread.*

A history entry is one user-initiated investigation:

* ``entry.search`` — the retrieval that opened it (``SearchLog``), or ``null``
  for an auto-search question, which has no seed.
* ``entry.runs[]`` — every model run (ask / research / auto) performed on it, in
  order. Each run owns its own nested ``searches`` (the model's own ``search``
  tool calls), its own evidence ledger and its own citation namespace.

``entry.kind`` is **derived**, never supplied: ``"llm"`` once there is at least
one run, else ``"search"``. It flips at run start (before the first model call),
so a failed or interrupted dialog still counts as an LLM search of the entry.

Layout (v2), one entry per file plus a rebuildable index::

    <history dir>/index.json             {"version":2, "updated_at":…, "entries":[meta…]}
    <history dir>/active_runs.json       {runtime_id:…, runs:[{entry_id, run_id, started_at}]}
    <history dir>/entries/<id>.json      one entry

Write order is always entry file (tmp -> os.replace) then index (tmp ->
os.replace), so a crash between the two reconciles cleanly at next boot. A v1
single-file store (``history_v1.json``) migrates once.

Importing this module has NO disk side effects: the store is built by the app at
startup or by an explicit constructor, and tests inject a temp directory.
"""

import json
import os
import re
import threading
import time
import uuid


# v3: meta rows carry a bounded `sources` list (distinct source files from the
# search results) for the UI's per-source history filter. A v2 index is not
# rewritten in place: the version mismatch triggers a rebuild from the entry
# files, which recomputes every meta row with the new field.
SCHEMA_VERSION = 3

# --- defaults (env-overridable) --------------------------------------------
HISTORY_MAX = int(os.environ.get("HISTORY_MAX", "20"))
HISTORY_MAX_BYTES = int(os.environ.get(
    "HISTORY_MAX_BYTES", str(8 << 20)))                      # 8 MiB
HISTORY_ENTRY_MAX_BYTES = int(os.environ.get(
    "HISTORY_ENTRY_MAX_BYTES", str(256 << 10)))              # 256 KiB
HISTORY_MAX_RUNS = int(os.environ.get("HISTORY_MAX_RUNS", "10"))
HISTORY_ANSWER_CHARS = int(os.environ.get("HISTORY_ANSWER_CHARS", "60000"))
HISTORY_REASONING_CHARS = int(os.environ.get("HISTORY_REASONING_CHARS", "20000"))
HISTORY_ROW_TEXT_CHARS = int(os.environ.get("HISTORY_ROW_TEXT_CHARS", "400"))
HISTORY_TOOL_ROWS = int(os.environ.get("HISTORY_TOOL_ROWS", "20"))
HISTORY_LEDGER_ROWS = int(os.environ.get("HISTORY_LEDGER_ROWS", "120"))
HISTORY_SEARCH_LOGS = int(os.environ.get("HISTORY_SEARCH_LOGS", "12"))
HISTORY_SEARCH_ROWS = int(os.environ.get("HISTORY_SEARCH_ROWS", "10"))
HISTORY_CHECKPOINT_SEC = float(os.environ.get("HISTORY_CHECKPOINT_SEC", "15"))
HISTORY_RUN_STALE_SEC = float(os.environ.get("HISTORY_RUN_STALE_SEC", "600"))
# The retired single-file store this module can migrate. It is looked for inside
# the history directory first, then beside it, so a data root that predates the
# per-entry layout can still be adopted without a manual move.
HISTORY_FILE_NAME = "history_v1.json"

# --- thread (conversation context) caps -------------------------------------
THREAD_CONTEXT_TURNS = int(os.environ.get("THREAD_CONTEXT_TURNS", "4"))
THREAD_QUESTION_CHARS = int(os.environ.get("THREAD_QUESTION_CHARS", "500"))
THREAD_ANSWER_CHARS = int(os.environ.get("THREAD_ANSWER_CHARS", "2000"))
THREAD_CONTEXT_CHARS = int(os.environ.get("THREAD_CONTEXT_CHARS", "8000"))
_CITE_RE = re.compile(r"(?<![\w\].])\[(\d{1,3})\]")
_ABBREV = "…"


class HistoryError(Exception):
    """A history failure worth surfacing, with a code, HTTP status and detail.

    Codes: ``history_gone`` (410), ``run_not_found`` (404), ``bad_entry`` (400),
    ``persist_failed`` (500). A persistence failure never raises in the record
    path — it is logged and recorded as ``persist_warning`` — so these are for
    the read/validation path and for the caller that must know.
    """

    def __init__(self, code, status, detail):
        super().__init__(detail)
        self.code = code
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _new_id(prefix, now=None):
    now = now if now is not None else time.time()
    return "%s_%d-%s" % (prefix, int(now), uuid.uuid4().hex[:8])


def _now():
    return time.time()


def _short(text, cap):
    """Cap ``text`` to ``cap`` chars, appending the truncation marker."""
    text = text or ""
    if len(text) <= cap:
        return text, False
    return text[:cap].rstrip() + _ABBREV, True


def _cut_with_note(text, cap):
    """Cap ``text`` and say how much was removed: ``(text, dropped_chars)``.

    Every cut adds a ``[… truncated N chars]`` suffix, so a reader can tell a
    shortened value from a short one.
    """
    text = text or ""
    if len(text) <= cap:
        return text, 0
    head = text[:cap].rstrip()
    dropped = len(text) - len(head)
    return head + " [… truncated %d chars]" % dropped, dropped


def _entry_size(entry):
    """Approximate on-disk size of one entry (compact JSON bytes)."""
    try:
        return len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


# Distinct source files are kept per meta row for the UI's source filter. The
# cap bounds the index; rows are ordered by rank, so the first hits win.
_META_SOURCES_MAX = 12


def _result_sources(results):
    """Distinct non-empty ``source`` values from result rows, rank order."""
    out = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        src = r.get("source")
        if not src:
            continue
        src = str(src)
        if src not in out:
            out.append(src)
            if len(out) >= _META_SOURCES_MAX:
                break
    return out


def _recompute_kind(entry):
    """``kind`` is derived from ``runs``; never stored from a caller."""
    entry["kind"] = "llm" if entry.get("runs") else "search"
    return entry["kind"]


def _cite_inert(text):
    """Make ``[3]`` citation markers inert so old numbers cannot alias the
    current run's ledger, which restarts at 1. Bracketed subscripts inside a
    word are left alone by the leading guard (the same one the citation audit
    uses)."""
    return _CITE_RE.sub(lambda m: "(source %s)" % m.group(1), text or "")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class HistoryStore:
    """One history store: per-entry JSON files plus a rebuildable index.

    The store is the ONLY write path for history. It enforces the module
    invariants (kind derived; prior runs never mutated; atomic small writes;
    running entries pinned from eviction; corrupt entry quarantined but a
    too-new schema refused) and it is thread-safe with one re-entrant lock
    guarding the in-memory index, LRU/eviction, clear and the index write.

    ``root`` is the history directory and is required; ``directory`` is accepted
    as an explicit keyword alias. ``history_file`` overrides where the retired
    single-file store is read from.
    """

    def __init__(self, root=None, *, directory=None, max_entries=None,
                 max_bytes=None, entry_max_bytes=None, max_runs=None,
                 runtime_id=None, log=None, history_file=None):
        target = directory or root
        if not target:
            raise ValueError("HistoryStore needs the history directory")
        self.directory = target
        self.log = log or (lambda msg: print("[history] %s" % msg, flush=True))
        self.max_entries = max_entries if max_entries is not None else HISTORY_MAX
        self.max_bytes = max_bytes if max_bytes is not None else HISTORY_MAX_BYTES
        self.entry_max_bytes = (entry_max_bytes if entry_max_bytes is not None
                                else HISTORY_ENTRY_MAX_BYTES)
        self.max_runs = max_runs if max_runs is not None else HISTORY_MAX_RUNS
        self.runtime_id = runtime_id or ("h_%d_%s" % (os.getpid(), uuid.uuid4().hex[:8]))
        self.history_file = history_file
        self._lock = threading.RLock()
        # in-memory index: ordered list of small meta rows, most-recently-first
        self._index = []            # list[dict] of meta rows (see _meta_row)
        self._pinned = {}           # entry_id -> lock/sentinel while being written
        self._active = {}           # run_id -> {entry_id, started_at}
        self._entries_dir = os.path.join(self.directory, "entries")
        self._index_path = os.path.join(self.directory, "index.json")
        self._active_path = os.path.join(self.directory, "active_runs.json")
        os.makedirs(self._entries_dir, exist_ok=True)
        self._migrate_v1()
        self._load_or_rebuild_index()
        self._reconcile()
        self._repair_interrupted()
        self._evict()

    # --- boot ---------------------------------------------------------------
    def _v1_source(self):
        """Where a retired single-file store would be, if the caller did not say."""
        if self.history_file:
            return self.history_file
        inside = os.path.join(self.directory, HISTORY_FILE_NAME)
        if os.path.exists(inside):
            return inside
        beside = os.path.join(os.path.dirname(os.path.abspath(self.directory)),
                              HISTORY_FILE_NAME)
        if os.path.exists(beside):
            return beside
        return inside

    def _migrate_v1(self):
        """Migrate the single-file v1 store once, if it looks like v1."""
        if os.path.exists(self._index_path):
            # v2 index present: the v1 file, if any, is a stale migration source.
            return
        v1 = self._v1_source()
        if not os.path.exists(v1):
            return
        try:
            with open(v1, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            self.log("v1 history corrupt (%s); quarantining %s" % (e, v1))
            try:
                os.rename(v1, v1 + ".corrupt-%d" % int(_now()))
            except OSError:
                pass
            return
        entries = data.get("entries", []) if isinstance(data, dict) else []
        migrated = 0
        for e in entries:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            entry = self._v1_to_v2(e)
            self._write_entry(entry)
            self._index.append(self._meta_row(entry))
            migrated += 1
        # The rename is unconditional: even an empty v1 file is retired so boot
        # does not retry it forever.
        self._write_index()
        try:
            os.rename(v1, v1 + ".v1.bak")
        except OSError:
            pass
        self.log("migrated %d v1 history entr%s to v2" % (
            migrated, "y" if migrated == 1 else "ies"))

    def _v1_to_v2(self, e):
        """Project one v1 entry into a v2 entry. ids are preserved (idempotent)."""
        now = _now()
        eid = e["id"]
        created = e.get("created_at") or now
        results = e.get("results") or []
        search = {
            "id": "%s:s0" % eid,
            "via": "manual",
            "created_at": created,
            "query": e.get("query"),
            "dataset": e.get("dataset"),
            "model": e.get("model"),
            "model_label": e.get("model_label"),
            "topk": len(results),
            "pipeline": "full",
            "stages": {"embed": True, "retrieve": True,
                       "rerank": bool(e.get("reranked"))},
            "timings": e.get("timings"),
            "reranked": e.get("reranked", False),
            "results": results,
            "note": "",
            "error": None,
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "id": eid,
            "kind": "search",
            "origin": "manual_search",
            "created_at": created,
            "updated_at": created,
            "last_viewed_at": e.get("last_viewed_at") or created,
            "dataset": e.get("dataset"),
            "query": e.get("query"),
            "model": e.get("model"),
            "model_label": e.get("model_label"),
            "search": search,
            "runs": [],
            "runs_collapsed": 0,
            "adopted_from": None,
        }

    def _load_or_rebuild_index(self):
        """Load a valid v2 index; otherwise scan entry files and rebuild it."""
        loaded = False
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version", 0) > SCHEMA_VERSION:
                raise HistoryError(
                    "bad_entry", 500,
                    "history index was written by a newer peekseed "
                    "(version %d); refusing to touch it. Upgrade the service."
                    % data.get("version"))
            if data.get("version") == SCHEMA_VERSION:
                self._index = [
                    m for m in data.get("entries", []) if isinstance(m, dict)
                ]
                loaded = True
        except HistoryError:
            raise
        except (OSError, ValueError):
            self._index = []
        if not loaded:
            self._rebuild_index_from_files()

    def _rebuild_index_from_files(self):
        """Scan entries/*.json (bounded) and rebuild the index cache."""
        self._index = []
        for name in sorted(os.listdir(self._entries_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self._entries_dir, name)
            entry = self._read_file(path)
            if entry is None:
                continue
            self._index.append(self._meta_row(entry))
        self.log("rebuilt history index from %d entry file(s)" % len(self._index))

    def _read_file(self, path):
        """Read one entry file; quarantine a corrupt one (loudly, never silently)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            try:
                os.rename(path, path + ".corrupt-%d" % int(_now()))
            except OSError:
                pass
            self.log("quarantined corrupt entry file %s" % path)
            return None

    def _reconcile(self):
        """Adopt orphan entry files and drop index rows without a file."""
        known = {}
        for name in os.listdir(self._entries_dir):
            if name.endswith(".json"):
                known[name] = os.path.join(self._entries_dir, name)
        index_ids = {m.get("id") for m in self._index}
        for m in list(self._index):
            fname = "%s.json" % m.get("id")
            if fname not in known:
                self._index.remove(m)
        # adopt orphans
        for fname, path in known.items():
            eid = fname[:-5]
            if eid in index_ids:
                continue
            entry = self._read_file(path)
            if entry is None:
                continue
            self._index.append(self._meta_row(entry))
        self._sort_index()

    def _sort_index(self):
        self._index.sort(
            key=lambda m: (m.get("last_viewed_at") or 0.0, m.get("created_at") or 0.0),
            reverse=True)

    # --- active runs (crash marker) ----------------------------------------
    def _write_active(self):
        try:
            tmp = self._active_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"runtime_id": self.runtime_id,
                           "runs": [{"entry_id": r["entry_id"],
                                     "run_id": r["run_id"],
                                     "started_at": r["started_at"]}
                                    for r in self._active.values()]},
                          f, ensure_ascii=False)
            os.replace(tmp, self._active_path)
        except OSError as e:
            self._warn("could not write active_runs: %s" % e)

    def _load_active(self):
        self._active = {}
        try:
            with open(self._active_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for r in data.get("runs", []):
                self._active[r.get("run_id")] = {
                    "entry_id": r.get("entry_id"), "run_id": r.get("run_id"),
                    "started_at": r.get("started_at", 0.0),
                    "runtime_id": data.get("runtime_id"),
                }
        except (OSError, ValueError):
            self._active = {}

    def _repair_interrupted(self):
        """Mark runs still ``running`` under a foreign (or stale) runtime as
        ``interrupted``. Bounded by the number of active runs, not the store."""
        self._load_active()
        now = _now()
        to_remove = []
        for run_id, info in self._active.items():
            entry = self._get_entry(info["entry_id"])
            if entry is None:
                to_remove.append(run_id)
                continue
            run = _find_run(entry, run_id)
            if run is None:
                to_remove.append(run_id)
                continue
            foreign = info.get("runtime_id") != self.runtime_id
            stale = not foreign and (now - (info.get("started_at") or 0) >
                                     HISTORY_RUN_STALE_SEC)
            if run.get("status") == "running" and (foreign or stale):
                run["status"] = "interrupted"
                run["finished_at"] = now
                run["interrupted_reason"] = "service restart"
                self._write_entry(entry)
                self._replace_meta(entry)
                to_remove.append(run_id)
        for run_id in to_remove:
            self._active.pop(run_id, None)
        self._write_active()
        self._repair_unmarked_running()

    def _repair_unmarked_running(self):
        """Fallback repair when ``active_runs.json`` is missing/corrupt.

        Boot cannot have a live run in this process, so any entry still carrying
        a ``running`` run (its index mirror, or a rebuilt row) is stale by
        definition: mark it ``interrupted``. Bounded by the number of index rows.
        """
        now = _now()
        for meta in list(self._index):
            eid = meta.get("id")
            active = meta.get("active_run") or {}
            if not active.get("id") and meta.get("last_status") != "running":
                continue
            entry = self._get_entry(eid)
            if entry is None:
                continue
            run = _find_run(entry, active.get("id")) if active.get("id") else None
            if run is None:
                run = next((r for r in reversed(entry.get("runs") or [])
                            if r.get("status") == "running"), None)
            if run is None or run.get("status") != "running":
                continue
            run["status"] = "interrupted"
            run["finished_at"] = now
            run["interrupted_reason"] = "service restart"
            self._write_entry(entry)
            self._replace_meta(entry)
            self.log("interrupted run %s (active_runs marker was missing)"
                     % run.get("id"))

    # --- meta row -----------------------------------------------------------
    def _meta_row(self, entry):
        m = {
            "id": entry.get("id"),
            "kind": entry.get("kind") or "search",
            "origin": entry.get("origin") or "manual_search",
            "created_at": entry.get("created_at"),
            "updated_at": entry.get("updated_at"),
            "last_viewed_at": entry.get("last_viewed_at"),
            "query": entry.get("query"),
            "dataset": entry.get("dataset"),
            "model_label": entry.get("model_label"),
            "num_results": len((entry.get("search") or {}).get("results") or []),
            "num_runs": len(entry.get("runs") or []),
            "runs_collapsed": entry.get("runs_collapsed") or 0,
            "bytes": _entry_size(entry),
        }
        runs = entry.get("runs") or []
        if runs:
            last = runs[-1]
            q = last.get("question") or ""
            m["last_mode"] = last.get("mode")
            m["last_question"] = q
            m["last_status"] = last.get("status")
            # cached active run mirror for the list
            if last.get("status") == "running":
                m["active_run"] = {"id": last.get("id"),
                                   "runtime_id": self.runtime_id,
                                   "started_at": last.get("started_at")}
        # filterable questions (bounded)
        m["questions"] = [r.get("question") for r in runs if r.get("question")][:10]
        # filterable source files (bounded): distinct, rank order
        m["sources"] = _result_sources((entry.get("search") or {}).get("results"))
        # store the search.id alias in meta for convenience
        m["history_id"] = entry.get("id")
        return m

    def _replace_meta(self, entry):
        """Update (or insert) the meta row for ``entry`` in the index.

        Always under the store lock: a recorder checkpoint runs on the worker
        thread, and an unguarded list rewrite could resurrect a row ``clear`` or
        ``_evict`` just dropped.
        """
        with self._lock:
            eid = entry.get("id")
            row = self._meta_row(entry)
            for i, m in enumerate(self._index):
                if m.get("id") == eid:
                    self._index[i] = row
                    return
            self._index.append(row)

    # --- persistence --------------------------------------------------------
    def _warn(self, msg):
        self.log("persist_warning: %s" % msg)

    def _write_entry(self, entry):
        """Write one entry file atomically (tmp -> fsync -> os.replace)."""
        path = os.path.join(self._entries_dir, "%s.json" % entry["id"])
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            self._warn("could not write entry %s: %s" % (entry["id"], e))
            raise

    def _write_index(self):
        """Write the index atomically (under the store lock)."""
        with self._lock:
            tmp = self._index_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"version": SCHEMA_VERSION, "updated_at": _now(),
                               "entries": self._index}, f, ensure_ascii=False)
                os.replace(tmp, self._index_path)
            except OSError as e:
                self._warn("could not write index: %s" % e)

    def _get_entry(self, eid):
        """Read one entry from disk (authoritative), or None if missing/corrupt."""
        path = os.path.join(self._entries_dir, "%s.json" % eid)
        if not os.path.exists(path):
            return None
        entry = self._read_file(path)
        if entry is None:
            # A corrupt entry must not leave a ghost row in the list: drop it
            # from the index outright (the file stays quarantined on disk).
            self._drop_meta(eid)
            self._write_index()
            return None
        _recompute_kind(entry)
        return entry

    # --- pinning ------------------------------------------------------------
    def _pin(self, eid):
        """Pin ``eid`` from eviction until unpinned. Returns a token."""
        tok = object()
        with self._lock:
            self._pinned[eid] = tok
        return tok

    def _unpin(self, eid, tok):
        with self._lock:
            if self._pinned.get(eid) is tok:
                self._pinned.pop(eid, None)

    def _is_pinned(self, eid):
        return eid in self._pinned

    # --- eviction -----------------------------------------------------------
    def _evict(self):
        """Enforce count + byte caps; never evict a pinned (running/writing) entry."""
        with self._lock:
            # count first
            if len(self._index) > self.max_entries:
                self._index.sort(
                    key=lambda m: (m.get("last_viewed_at") or 0.0,
                                   m.get("created_at") or 0.0), reverse=True)
                evict = []
                for m in reversed(self._index):
                    if len(self._index) - len(evict) <= self.max_entries:
                        break
                    eid = m.get("id")
                    if self._is_pinned(eid):
                        continue
                    evict.append(m)
                for m in evict:
                    self._drop_meta(m.get("id"))
                    self._drop_file(m.get("id"))
            # bytes backstop
            total = sum(m.get("bytes") or 0 for m in self._index)
            if total > self.max_bytes:
                self._index.sort(
                    key=lambda m: (m.get("last_viewed_at") or 0.0,
                                   m.get("created_at") or 0.0), reverse=True)
                for m in reversed(self._index):
                    if total <= self.max_bytes:
                        break
                    eid = m.get("id")
                    if self._is_pinned(eid):
                        continue
                    total -= m.get("bytes") or 0
                    self._drop_meta(eid)
                    self._drop_file(eid)
            self._write_index()

    def _drop_meta(self, eid):
        with self._lock:
            self._index = [m for m in self._index if m.get("id") != eid]

    def _drop_file(self, eid):
        path = os.path.join(self._entries_dir, "%s.json" % eid)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            self._warn("could not drop entry %s: %s" % (eid, e))

    # --- public API ---------------------------------------------------------
    def record_search(self, search):
        """Create one search entry from an ``/api/search`` result dict. Returns its id."""
        search = search or {}
        now = _now()
        eid = _new_id("e", now)
        entry = {
            "schema_version": SCHEMA_VERSION,
            "id": eid,
            "kind": "search",
            "origin": "manual_search",
            "created_at": now,
            "updated_at": now,
            "last_viewed_at": now,
            "dataset": search.get("dataset"),
            "query": search.get("query"),
            "model": search.get("model"),
            "model_label": search.get("model_label"),
            "search": _search_log_from_result(eid, search, now),
            "runs": [],
            "runs_collapsed": 0,
            "adopted_from": None,
        }
        with self._lock:
            tok = self._pin(eid)
            try:
                self._write_entry(entry)
                self._replace_meta(entry)
                self._evict()
            except OSError as e:
                # A persistence failure must never fail the search: the results
                # still stream, they just are not saved.
                self._warn("record_search could not persist: %s" % e)
                eid = None
            finally:
                self._unpin(entry["id"], tok)
        return eid

    def begin_run(self, history_id, *, mode, question, model, dataset, seed=None,
                  thread=None, adopted_from=None):
        """Attach to the entry (promoting it) or adopt/create a new one.

        Returns a :class:`RunRecorder`. Persists ``status:"running"`` before
        returning **when it can**; a persistence failure never raises — it
        returns a recorder with ``persisted=False`` and the caller keeps
        streaming. The caller must call ``recorder.finish()``.
        """
        now = _now()
        entry = None
        # An adopt keeps a provenance link to the id the caller asked for, even
        # though that id is no longer in the store.
        adopted_from = adopted_from or history_id
        origin = "auto" if (seed or {}).get("auto") else "manual_search"
        with self._lock:
            if history_id:
                entry = self._get_entry(history_id)
            if entry is None and (seed or {}).get("auto"):
                # new auto entry (search:null); nothing to adopt as a search seed
                adopted_from = None
                eid = _new_id("e", now)
                entry = {
                    "schema_version": SCHEMA_VERSION,
                    "id": eid,
                    "kind": "search",
                    "origin": origin,
                    "created_at": now,
                    "updated_at": now,
                    "last_viewed_at": now,
                    "dataset": dataset,
                    "query": question,
                    "model": model,
                    "model_label": None,
                    "search": None,
                    "runs": [],
                    "runs_collapsed": 0,
                    "adopted_from": None,
                }
            elif entry is None:
                # adopt: a request carried results but the id is evicted/unknown;
                # ``adopted_from`` keeps the stale id it was asking about.
                eid = _new_id("e", now)
                entry = self._make_adopted(seed, dataset, question, model, eid,
                                           now, origin, adopted_from)
            # append a run
            run = _new_run(entry, mode, question, model, dataset, seed, thread,
                           now, self.max_runs)
            entry["runs"] = (entry.get("runs") or []) + [run]
            entry["updated_at"] = now
            entry["last_viewed_at"] = now
            _recompute_kind(entry)
            self._maybe_collapse_runs(entry)
            run_id = run["id"]
            rec = RunRecorder(self, entry, run, persisted=False)
            self._active[run_id] = {"entry_id": entry["id"], "run_id": run_id,
                                    "started_at": now}
            self._write_active()
            # Pin the entry from eviction for the whole run; released in finish().
            rec._pin_token = self._pin(entry["id"])
            try:
                self._write_entry(entry)
                self._replace_meta(entry)
                self._evict()
                rec.persisted = True
            except OSError as e:
                self._warn("begin_run could not persist: %s" % e)
                rec.persisted = False
                rec.persist_warning = str(e)
        return rec

    def _make_adopted(self, seed, dataset, question, model, eid, now,
                      origin, adopted_from):
        search = None
        if seed and seed.get("results"):
            search = _search_log_from_result(eid, dict(
                dataset=dataset, query=question, model=model,
                results=seed.get("results")), now, via="adopted")
        return {
            "schema_version": SCHEMA_VERSION,
            "id": eid,
            "kind": "llm",
            "origin": origin,
            "created_at": now,
            "updated_at": now,
            "last_viewed_at": now,
            "dataset": dataset,
            "query": question,
            "model": model,
            "model_label": None,
            "search": search,
            "runs": [],
            "runs_collapsed": 0,
            "adopted_from": adopted_from,
        }

    def _maybe_collapse_runs(self, entry):
        """Collapse the oldest runs beyond ``max_runs`` into retained summaries.

        A collapsed run keeps a read-only summary (question, mode, status,
        timestamps, answer head) and is counted in ``runs_collapsed``. It is
        never fully deleted while the entry lives, and an earlier run is never
        mutated after it was written.
        """
        runs = entry.get("runs") or []
        if len(runs) <= self.max_runs:
            return
        excess = len(runs) - self.max_runs
        collapsed = []
        kept = []
        for i, r in enumerate(runs):
            if i < excess:
                collapsed.append(r)
            else:
                kept.append(r)
        entry["runs"] = kept
        coll = entry.setdefault("collapsed_runs", [])
        for r in collapsed:
            ans = r.get("answer") or ""
            head = ans[:400] + (_ABBREV if len(ans) > 400 else "")
            coll.append({
                "id": r.get("id"), "seq": r.get("seq"), "mode": r.get("mode"),
                "status": r.get("status"), "question": r.get("question"),
                "started_at": r.get("started_at"), "finished_at": r.get("finished_at"),
                "answer": head, "trimmed": True,
            })
        entry["runs_collapsed"] = (entry.get("runs_collapsed") or 0) + len(collapsed)

    def get(self, history_id, *, view=True):
        """The full entry, or None. view=True bumps recency (LRU bump)."""
        with self._lock:
            entry = self._get_entry(history_id)
            if entry is None:
                return None
            entry = _derive_thread_reconstructable(entry)
            if view:
                entry["last_viewed_at"] = _now()
                self._write_entry(entry)
                self._replace_meta(entry)
                self._evict()
            return entry

    def list_meta(self):
        """Small rows, most-recently-viewed first. Never opens an entry file."""
        with self._lock:
            rows = [dict(m) for m in self._index]
            rows.sort(
                key=lambda m: (m.get("last_viewed_at") or 0.0,
                               m.get("created_at") or 0.0), reverse=True)
            return rows

    def clear(self):
        """Delete unpinned entries. Returns {ok, cleared, kept_running}."""
        with self._lock:
            kept = 0
            cleared = 0
            for m in list(self._index):
                eid = m.get("id")
                if self._is_pinned(eid):
                    kept += 1
                    continue
                self._drop_meta(eid)
                self._drop_file(eid)
                cleared += 1
            self._write_index()
            return {"ok": True, "cleared": cleared, "kept_running": kept}

    def export_v1(self, path):
        """Rewrite search-only entries in the retired single-file shape."""
        rows = []
        for m in self._index:
            entry = self._get_entry(m.get("id"))
            if entry is None or not entry.get("search"):
                continue
            search = entry.get("search") or {}
            rows.append({
                "id": entry["id"],
                "created_at": entry["created_at"],
                "last_viewed_at": entry["last_viewed_at"],
                "dataset": entry.get("dataset"),
                "query": entry.get("query"), "model": entry.get("model"),
                "model_label": entry.get("model_label"),
                "timings": search.get("timings"),
                "reranked": search.get("reranked", False),
                "results": search.get("results") or [],
            })
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": rows}, f, ensure_ascii=False)
        os.replace(tmp, path)
        return len(rows)


# ---------------------------------------------------------------------------
# SearchLog construction
# ---------------------------------------------------------------------------
def _search_log_from_result(eid, result, now, via="manual"):
    """Build a SearchLog from an ``/api/search`` result dict (the manual entry)."""
    timings = result.get("timings") or {}
    stages = {
        "embed": timings.get("embed_sec") is not None,
        "retrieve": timings.get("retrieve_sec") is not None,
        "rerank": bool(result.get("reranked")),
    }
    results = result.get("results") or []
    return {
        "id": "%s:s0" % eid,
        "via": via,
        "created_at": now,
        "query": result.get("query"),
        "dataset": result.get("dataset"),
        "model": result.get("model"),
        "model_label": result.get("model_label"),
        "topk": len(results),
        "pipeline": "full",
        "stages": stages,
        "timings": timings,
        "reranked": bool(result.get("reranked")),
        "results": results,
        "note": "",
        "error": None,
    }


# ---------------------------------------------------------------------------
# Run construction
# ---------------------------------------------------------------------------
def _new_run(entry, mode, question, model, dataset, seed, thread, now,
             max_runs):
    run_id = _new_id("r", now)
    run = {
        "id": run_id,
        # A question number counts ALL questions ever asked on this entry, not
        # just the ones still stored: once older runs collapse to summaries,
        # ``runs_collapsed`` carries the count they no longer contribute. This is
        # what keeps ``seq``/``turn`` monotonic and question chips unique.
        "seq": (entry.get("runs_collapsed") or 0) + len(entry.get("runs") or []) + 1,
        "mode": mode,
        "status": "running",
        "started_at": now,
        "finished_at": None,
        "question": question,
        "model": model,
        "dataset": dataset,
        "seed": _project_seed(seed),
        "thread": _project_thread(thread or {}, run_id, entry, mode),
        "answer": "",
        "reasoning": "",
        "answer_truncated": False,
        "reasoning_truncated": False,
        "trace": [],
        "searches": [],
        "ledger": [],
        "cited": [],
        "unknown": [],
        "usage": None,
        "timings": {"first_token_sec": None, "total_sec": None,
                    "steps": 0, "tool_calls": 0},
        "finish_reason": None,
        "truncated": False,
        "forced": False,
        "repeated": False,
        "client_disconnected": False,
        "trimmed": False,
        "error": None,
    }
    return run


def _project_seed(seed):
    """Project a seed dict to reference fields only (no excerpt bodies)."""
    seed = seed or {}
    excerpts = []
    for e in seed.get("excerpts") or []:
        chunk = e.get("chunk") or e.get("chunk_file") or e.get("id")
        row = {k: e[k] for k in (
            "n", "label", "section", "numbering", "page_start", "page_end",
            "pdf_url", "source", "kind", "dataset") if k in e}
        if chunk:
            row["chunk"] = chunk
        excerpts.append(row)
    return {
        "refs": seed.get("refs") or [],
        "excerpts": excerpts,
        "auto": bool(seed.get("auto")),
    }


def _project_thread(thread, run_id, entry, mode):
    """Project thread metadata into the stored shape (adds default empty fields).

    ``reply_to`` is ALWAYS the previous run in storage order, even when that run
    failed or was interrupted: the conversation still followed it, and only
    successful runs contribute context (``context_runs`` says which). ``turn``
    counts every question ever asked on the entry, including runs that have since
    collapsed to summaries.
    """
    prior = (entry.get("runs") or [])
    prev_id = prior[-1]["id"] if prior else None
    return {
        "turn": ((entry.get("runs_collapsed") or 0) + len(prior) + 1),
        "reply_to": prev_id,
        "context_runs": thread.get("context_runs") or [],
        "context_turns": thread.get("context_turns") or [],
        "context_chars": thread.get("context_chars") or 0,
        "context_dropped": thread.get("context_dropped") or 0,
        "context_digest": thread.get("context_digest"),
        "caps": thread.get("caps") or {},
    }


def _derive_thread_reconstructable(entry):
    """Derive per-run ``thread.reconstructable`` on read; never mutate storage."""
    for run in entry.get("runs") or []:
        run.setdefault("thread", {})
        ctx = run.get("thread") or {}
        ok = True
        for rid in ctx.get("context_runs") or []:
            ref = _find_run(entry, rid)
            if ref is None or ref.get("trimmed"):
                ok = False
                break
        ctx["reconstructable"] = ok
    return entry


def _find_run(entry, run_id):
    for r in entry.get("runs") or []:
        if r.get("id") == run_id:
            return r
    return None


# ---------------------------------------------------------------------------
# The run recorder
# ---------------------------------------------------------------------------
class RunRecorder:
    """One run's in-memory accumulation; the only write path for a dialog.

    The stream recorder tees events *before* the SSE queue; persistence is owned
    by the worker thread, not the response generator, because the worker keeps
    running when the client disconnects. ``finish()`` is idempotent.
    """

    def __init__(self, store, entry, run, persisted):
        self.store = store
        self.entry = entry
        self.run = run
        self.persisted = persisted
        self.persist_warning = None
        self._lock = threading.Lock()
        self._finished = False
        self._last_checkpoint = _now()
        self._dirty = False
        self._ledger_n = 0
        self._pin_token = None
        self.run_id = run["id"]
        self.entry_id = entry["id"]

    # --- observing ----------------------------------------------------------
    def observe(self, event):
        """Tee one SSE event into the run's accumulated buffers.

        A recorder bug is logged and swallowed: history must never break or
        truncate the answer stream.
        """
        try:
            self._observe(event)
        except Exception as e:
            self.store.log("persist_warning: observe failed: %s" % e)

    def _observe(self, event):
        if event is None or self._finished:
            return
        typ = event.get("type")
        with self._lock:
            if typ == "reasoning":
                self._append_reasoning(event.get("text") or "")
            elif typ == "text":
                self._append_answer(event.get("text") or "")
            elif typ in ("step", "tool_call", "tool_result", "narration",
                         "note", "text_retract"):
                self._observe_step(event)
            elif typ == "done":
                self._observe_done(event)
            elif typ == "error":
                self._observe_error(event)
            self._dirty = True
            # Step-done and tool-result are natural checkpoints; the timer covers
            # a long step in between. Checkpoints are small.
            if ((typ == "tool_result")
                    or (typ == "step" and event.get("status") == "done")):
                self._checkpoint_locked(force=True)
            elif _now() - self._last_checkpoint >= HISTORY_CHECKPOINT_SEC:
                self._checkpoint_locked(force=True)

    def _append_answer(self, piece):
        self.run["answer"] = (self.run.get("answer") or "") + (piece or "")
        if len(self.run["answer"]) > HISTORY_ANSWER_CHARS:
            self.run["answer"] = self.run["answer"][:HISTORY_ANSWER_CHARS]
            self.run["answer_truncated"] = True

    def _append_reasoning(self, piece):
        self.run["reasoning"] = (self.run.get("reasoning") or "") + (piece or "")
        if len(self.run["reasoning"]) > HISTORY_REASONING_CHARS:
            self.run["reasoning"] = self.run["reasoning"][:HISTORY_REASONING_CHARS]
            self.run["reasoning_truncated"] = True

    def _observe_step(self, event):
        typ = event.get("type")
        trace = self.run.setdefault("trace", [])
        if typ == "step":
            n = event.get("n")
            status = event.get("status")
            step = next((s for s in trace if s.get("n") == n), None)
            if step is None:
                step = {"n": n, "status": status, "sec": event.get("sec"),
                        "tool_calls": 0, "added": 0, "narration": [],
                        "notes": [], "calls": [], "usage": None,
                        "finish_reason": None}
                trace.append(step)
            else:
                step["status"] = status
                step["sec"] = event.get("sec")
                if event.get("tool_calls") is not None:
                    step["tool_calls"] = event.get("tool_calls")
                if event.get("added") is not None:
                    step["added"] = event.get("added")
                step["usage"] = event.get("usage") or step.get("usage")
                step["finish_reason"] = (event.get("finish_reason")
                                         or step.get("finish_reason"))
        elif typ == "tool_call":
            n = event.get("step")
            step = next((s for s in trace if s.get("n") == n), None)
            if step is None:
                step = {"n": n, "status": "running", "sec": None,
                        "tool_calls": 0, "added": 0, "narration": [],
                        "notes": [], "calls": [], "usage": None,
                        "finish_reason": None}
                trace.append(step)
            call = {"call_id": event.get("call"), "name": event.get("name"),
                    "args": event.get("args"), "arg_error": event.get("arg_error"),
                    "sec": None, "added": 0, "note": "", "error": "",
                    "rows": []}
            step.setdefault("calls", []).append(call)
            self.run["timings"]["tool_calls"] = self.run["timings"].get(
                "tool_calls", 0) + 1
        elif typ == "tool_result":
            n = event.get("step")
            step = next((s for s in trace if s.get("n") == n), None)
            if step is None:
                return
            call = next((c for c in step.get("calls", [])
                         if c.get("call_id") == event.get("call")), None)
            if call is None:
                call = {"call_id": event.get("call"), "name": event.get("name"),
                        "args": event.get("args"), "arg_error": None,
                        "sec": None, "added": 0, "note": "", "error": "",
                        "rows": []}
                step.setdefault("calls", []).append(call)
            call["sec"] = event.get("sec")
            call["error"] = event.get("error") or ""
            call["note"] = event.get("note") or ""
            call["added"] = event.get("added") or 0
            rows = event.get("rows") or []
            call["rows"] = [{k: r.get(k) for k in
                             ("n", "chunk", "dataset", "source", "page_start") if k in r}
                            for r in rows][:HISTORY_TOOL_ROWS]
            self._add_ledger_rows(rows)
            self.run["timings"]["tool_calls"] = self.run["timings"].get(
                "tool_calls", 0)
        elif typ == "narration":
            # narration lives in the step that produced it
            n = next((s["n"] for s in reversed(trace)), None)
            self._append_narration(n, event.get("text") or "")
        elif typ == "note":
            self._append_note(event.get("text") or "")
        elif typ == "text_retract":
            # The page clears the answer when the model narrates and then decides
            # to call a tool; the stored answer must be the same one the reader saw.
            # The retracted text is already in the step's narration (the session
            # emits a narration event with it after this marker).
            self.run["answer"] = ""

    def _append_narration(self, n, text):
        trace = self.run.setdefault("trace", [])
        step = next((s for s in trace if s.get("n") == n), None)
        if step is None:
            step = {"n": n, "status": "running", "narration": [], "notes": [],
                    "calls": []}
            trace.append(step)
        step.setdefault("narration", []).append(text)

    def _append_note(self, text):
        trace = self.run.setdefault("trace", [])
        if trace:
            trace[-1].setdefault("notes", []).append(text)

    def _add_ledger_rows(self, rows):
        """Project ledger rows (drop observation text) and cap the list."""
        ledger = self.run.setdefault("ledger", [])
        for r in rows or []:
            self._ledger_n += 1
            projected = {k: r.get(k) for k in
                         _LEDGER_ROW_KEYS if k in r}
            ledger.append(projected)
        if len(ledger) > HISTORY_LEDGER_ROWS:
            del ledger[:len(ledger) - HISTORY_LEDGER_ROWS]

    def _observe_done(self, event):
        self.run["answer"] = event.get("answer") or self.run.get("answer") or ""
        self.run["finish_reason"] = event.get("finish_reason")
        self.run["truncated"] = event.get("truncated", False)
        self.run["forced"] = event.get("forced", False)
        self.run["repeated"] = event.get("repeated", False)
        self.run["usage"] = event.get("usage_total") or event.get("usage")
        self.run["timings"]["first_token_sec"] = event.get("first_token_sec")
        self.run["timings"]["total_sec"] = event.get("total_sec")
        self.run["timings"]["steps"] = event.get("steps", self.run["timings"].get("steps"))
        self.run["timings"]["tool_calls"] = event.get("tool_calls", self.run["timings"].get("tool_calls"))
        timings = event.get("timings")
        if isinstance(timings, dict):
            for key in ("total_sec", "steps", "tool_calls"):
                if timings.get(key) is not None:
                    self.run["timings"][key] = timings[key]
        self.run["cited"] = event.get("cited") or []
        self.run["unknown"] = event.get("unknown") or []
        # project ledger from done.ledger (drop observation text)
        if event.get("ledger") is not None:
            self.run["ledger"] = []
            self._ledger_n = 0
            for r in event.get("ledger"):
                self._ledger_n += 1
                self.run["ledger"].append({k: r.get(k) for k in
                                           _LEDGER_ROW_KEYS if k in r})
            if len(self.run["ledger"]) > HISTORY_LEDGER_ROWS:
                del self.run["ledger"][:len(self.run["ledger"]) - HISTORY_LEDGER_ROWS]

    def _observe_error(self, event):
        self.run["error"] = {"status": event.get("status"),
                             "detail": event.get("detail")}
        # a failed call may have produced a partial answer; keep it

    def add_search(self, log):
        """Add a nested SearchLog (the model's own ``search`` call).

        The tool seam knows query/dataset/pipeline/rows; the recorder supplies
        the SearchLog identity fields so a restored nested log is self-describing.
        """
        if self._finished:
            return
        with self._lock:
            logs = self.run.setdefault("searches", [])
            log = dict(log)
            log.setdefault("id", "%s:s%d" % (self.run_id, len(logs)))
            log.setdefault("via", "tool")
            log.setdefault("created_at", _now())
            log.setdefault("model", self.run.get("model"))
            log.setdefault("model_label", None)
            log = _trim_search_log(log)
            logs.append(log)
            if len(logs) > HISTORY_SEARCH_LOGS:
                del logs[:len(logs) - HISTORY_SEARCH_LOGS]
            self._dirty = True

    def note_client_disconnected(self):
        if self._finished:
            return
        with self._lock:
            self.run["client_disconnected"] = True
            self._dirty = True

    # --- checkpoint / finish ------------------------------------------------
    def checkpoint(self):
        try:
            with self._lock:
                self._checkpoint_locked(force=True)
        except Exception as e:
            self.store.log("persist_warning: checkpoint failed: %s" % e)

    def _checkpoint_locked(self, force=False):
        if not force and not self._dirty:
            return
        if not self.persisted:
            return
        entry = self.entry
        self._trim_entry_if_needed()
        self.store._write_entry(entry)
        self.store._replace_meta(entry)
        self._dirty = False
        self._last_checkpoint = _now()

    def finish(self, *, status, error=None):
        """Finalize the run. Idempotent, and never raises (worker's ``finally``).

        A recorder bug is logged as ``persist_warning``; it must never skip the
        event-queue sentinel or turn a completed answer into a failed stream.
        """
        with self._lock:
            if self._finished:
                return
            self._finished = True
            try:
                self.run["status"] = status
                if error is not None:
                    self.run["error"] = error
                self.run["finished_at"] = _now()
                self.run.setdefault("answer_truncated",
                                    len(self.run.get("answer") or "") > HISTORY_ANSWER_CHARS)
                self.run.setdefault("reasoning_truncated",
                                    len(self.run.get("reasoning") or "") > HISTORY_REASONING_CHARS)
                self._trim_entry_if_needed()
                try:
                    self.store._write_entry(self.entry)
                    self.store._replace_meta(self.entry)
                    self.store._evict()
                    self.persisted = True
                except OSError as e:
                    self.persist_warning = str(e)
            except Exception as e:
                self.persist_warning = "finish failed: %s" % e
                self.store.log("persist_warning: finish failed: %s" % e)
            finally:
                try:
                    self.store._active.pop(self.run_id, None)
                    self.store._write_active()
                except Exception as e:
                    self.store.log("persist_warning: active-marker cleanup "
                                   "failed: %s" % e)
                if getattr(self, "_pin_token", None) is not None:
                    self.store._unpin(self.entry_id, self._pin_token)

    def _trim_entry_if_needed(self):
        """Trim one entry to its byte cap, in the documented order; mark it.

        Order: reasoning -> narration -> tool row text -> tool rows -> nested
        search rows -> oldest run transcripts (collapse) -> answer head+tail.
        Every cut carries a ``[… truncated N chars]`` suffix, and the entry gets
        a structured ``entry.truncation`` marker so the page/live-checks can say
        what was shortened rather than silently showing less evidence.
        """
        entry = self.entry
        original = _entry_size(entry)
        if original <= self.store.entry_max_bytes:
            return
        self.run["trimmed"] = True
        fields = []
        dropped = 0

        def cut(field, text, cap):
            nonlocal dropped
            new, gone = _cut_with_note(text, cap)
            if gone:
                dropped += gone
                if field not in fields:
                    fields.append(field)
            return new

        # 1. reasoning
        reasoning = self.run.get("reasoning") or ""
        if len(reasoning) > HISTORY_REASONING_CHARS:
            self.run["reasoning"] = cut("reasoning", reasoning,
                                        HISTORY_REASONING_CHARS)
            self.run["reasoning_truncated"] = True
        # 2. narration, per step (the model's own words before a tool call)
        for step in self.run.get("trace", []):
            step["narration"] = [cut("narration", t, HISTORY_ROW_TEXT_CHARS)
                                 for t in (step.get("narration") or [])]
        # 3./4. tool rows: stored rows are references, and the tool's observation
        # text is deliberately not stored; keep the row list bounded if a
        # recorder from an older checkpoint still has more.
        for step in self.run.get("trace", []):
            for call in step.get("calls", []):
                rows = call.get("rows") or []
                if len(rows) > HISTORY_TOOL_ROWS:
                    call["rows"] = rows[:HISTORY_TOOL_ROWS]
                    fields.append("tool_rows")
        # 5. nested search logs: bound rows and row text
        searches = self.run.get("searches") or []
        if searches:
            self.run["searches"] = [_trim_search_log(log) for log in searches]
        # 6. collapse the oldest run transcripts while the entry is still too big
        while (len(entry.get("runs") or []) > 1
               and _entry_size(entry) > self.store.entry_max_bytes):
            oldest = entry["runs"].pop(0)
            answer = oldest.get("answer") or ""
            head = answer[:400] + (_ABBREV if len(answer) > 400 else "")
            entry.setdefault("collapsed_runs", []).append({
                "id": oldest.get("id"), "seq": oldest.get("seq"),
                "mode": oldest.get("mode"), "status": oldest.get("status"),
                "question": oldest.get("question"),
                "started_at": oldest.get("started_at"),
                "finished_at": oldest.get("finished_at"),
                "answer": head, "trimmed": True,
            })
            entry["runs_collapsed"] = (entry.get("runs_collapsed") or 0) + 1
            fields.append("runs")
        # 7. answer head+tail, only for this (current) run
        if _entry_size(entry) > self.store.entry_max_bytes:
            answer = self.run.get("answer") or ""
            if len(answer) > 8192:
                head = answer[:4000]
                tail = answer[-4000:]
                removed = len(answer) - len(head) - len(tail)
                self.run["answer"] = (head + " [… truncated %d chars] " % removed
                                      + tail)
                dropped += removed
                self.run["answer_truncated"] = True
                fields.append("answer")
        entry["truncation"] = {
            "reason": "entry_max_bytes",
            "fields": fields,
            "dropped_chars": dropped,
            "original_bytes": original,
            "at": _now(),
        }


# The row fields a stored ledger keeps. A tool row is projected into this shape
# so the page can render it with the same row renderer as a manual search;
# observation text is never stored twice.
_LEDGER_ROW_KEYS = (
    "n", "label", "section", "numbering", "chapter", "page_start", "page_end",
    "chunk", "source", "kind", "dataset", "pdf_url", "rank", "score",
)

# The row fields a nested SearchLog keeps. ``chunk`` is the canonical handle;
# ``snippet`` is capped and reference fields only are copied.
_SEARCH_ROW_KEYS = (
    "rank", "score", "label", "dataset", "source",
    "section", "numbering", "chapter", "page_start", "page_end",
    "chunk", "snippet", "pdf_url", "highlight", "parent", "path", "pdf_y",
)


def _trim_search_log(log):
    """Bound a nested SearchLog's rows and project them to the row schema."""
    log = dict(log)
    rows = log.get("results") or []
    out = []
    for r in rows[:HISTORY_SEARCH_ROWS]:
        row = {k: r.get(k) for k in _SEARCH_ROW_KEYS if r.get(k) is not None}
        if not row.get("chunk"):
            row["chunk"] = r.get("chunk") or r.get("chunk_file")
        text = r.get("snippet") or r.get("text") or ""
        row["snippet"] = _cut_with_note(text, HISTORY_ROW_TEXT_CHARS)[0]
        out.append(row)
    log["results"] = out
    # ``topk`` is the requested k, not the number of rows that survived caps.
    log["topk"] = log.get("topk") or len(out)
    return log
