"""One writer per collection: advisory lock files with stale-holder detection.

The index builder is the only writer of a collection's derived data, but two
writers can race in three ways: two HTTP requests starting an index build, a CLI
``reindex`` while the server builds, and a crashed process leaving a lock file
behind. This module gives every build one exclusive ``fcntl`` lock per collection
and records *who* holds it (pid, host, start time, job id) so the API can answer
"busy, held by pid 1234 since 10:02" instead of guessing.

``flock`` is released by the kernel when a process dies, so a dead holder can
never block a new build; the recorded pid is checked as well, which lets a human
tell a stale record from a live one. Locks live under ``.peekseed/jobs/locks/``
next to the job records and are never deleted while locked (unlinking a locked
inode would let a second process lock a different inode for the same name).
"""

import fcntl
import json
import os
import re
import socket
import time
from pathlib import Path

from .pdf import IngestError

__all__ = ["CollectionBusy", "CollectionLock", "collection_lock"]

_LOCK_SUFFIX = ".lock"
_SAFE_NAME = re.compile(r"[^a-z0-9._-]+")


class CollectionBusy(IngestError):
    """Another process is already writing this collection."""


def _safe_collection(collection):
    name = _SAFE_NAME.sub("-", str(collection or "").strip().lower()).strip("-._")
    return name[:96] or "default"


def _pid_alive(pid):
    """True when ``pid`` names a live process on this machine."""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class CollectionLock(object):
    """An exclusive, non-blocking lock for one collection's index writes.

    ``acquire()`` raises :class:`CollectionBusy` when another live process holds
    the lock. The holder record written into the lock file is advisory: the
    kernel's ``flock`` is the actual exclusion, and the record is what lets the
    error message name the holder. A record with a dead pid and a free ``flock``
    is simply overwritten on the next acquire (that is the stale-lock case).
    """

    def __init__(self, jobs_dir, collection):
        self.collection = _safe_collection(collection)
        self.path = Path(jobs_dir) / "locks" / (self.collection + _LOCK_SUFFIX)
        self._fd = None
        self.holder = {}

    # -- lifecycle ---------------------------------------------------------
    def acquire(self, *, job_id=None, timeout=None):
        """Take the lock or raise :class:`CollectionBusy`.

        ``timeout`` is accepted for a future queueing path; 0/None means "fail
        fast", which is what the API's 409 contract needs.
        """
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = self._read_holder(fd)
            os.close(fd)
            self.holder = holder
            raise CollectionBusy(self.busy_message(holder)) from None
        self._fd = fd
        self.holder = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "collection": self.collection,
            "job": str(job_id or ""),
            "started_at": int(time.time()),
        }
        self._write_holder(fd, self.holder)
        return self

    def release(self):
        """Release the lock (idempotent); the file stays as an unlocked record."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            self._write_holder(fd, {
                "released": True,
                "collection": self.collection,
                "pid": os.getpid(),
                "at": int(time.time()),
            })
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            self.holder = {}

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc_info):
        self.release()
        return False

    # -- diagnostics -------------------------------------------------------
    def busy_message(self, holder=None):
        holder = holder or self.holder or {}
        pid = holder.get("pid")
        since = holder.get("started_at")
        parts = ["collection '%s' is already being written" % self.collection]
        if pid:
            parts.append("by pid %s" % pid)
        if holder.get("job"):
            parts.append("(job %s)" % holder["job"])
        if since:
            try:
                parts.append("since %s" % time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(int(since))))
            except (TypeError, ValueError, OSError):
                pass
        if pid and not _pid_alive(pid):
            parts.append("- the recorded holder is gone; retry to clear the "
                         "stale lock")
        return " ".join(parts)

    def probe(self):
        """The current holder record without taking the lock (None when free)."""
        if not self.path.is_file():
            return None
        fd = os.open(str(self.path), os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return self._read_holder(fd) or {"collection": self.collection}
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        finally:
            os.close(fd)

    @staticmethod
    def _read_holder(fd):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 65536)
        except OSError:
            return {}
        try:
            value = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write_holder(fd, holder):
        payload = (json.dumps(holder, sort_keys=True) + "\n").encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.ftruncate(fd, len(payload))
        try:
            os.fsync(fd)
        except OSError:
            pass


def collection_lock(jobs_dir, collection, **kwargs):
    """A convenience context manager: ``with collection_lock(jobs, name): ...``."""
    lock = CollectionLock(jobs_dir, collection)
    lock.acquire(**kwargs)
    return lock
