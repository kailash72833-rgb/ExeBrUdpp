"""
Durable storage for the bot (accounts, orders / history, devices, token cache, free-plan requests ...).

Why this exists
---------------
Railway / Render start every deploy and every restart with a FRESH disk, so anything that is only
kept in local files (accounts.json, orders.json ...) disappears -> accounts "get deleted" and the
order history is empty again.

How it works
------------
* Every document is stored in MongoDB (same database the users / sessions already live in),
  collection "app_data", one document per name:  {"_id": name, "json": "<json text>", ...}
  (kept as JSON text so odd key names like "my.uid" can never break Mongo).
* Reads come from memory (loaded once from MongoDB at first use).
* Writes go to memory + a local file (fast backup) and are pushed to MongoDB by a background
  thread within about a second.  `flush()` pushes everything immediately (used on shutdown).
* No MongoDB configured / reachable  ->  plain local files exactly like before (a loud warning is printed).
* A document that could not be READ from MongoDB is never written back (so a hiccup can't wipe data).
* If a MongoDB document does not exist yet but a local file does, the file is imported once (migration).
"""
import atexit
import json
import os
import threading
import time
from typing import Any, Dict, Optional

try:
    from pymongo import MongoClient
except ImportError:                                   # pymongo missing -> local files only
    MongoClient = None

COLLECTION = "app_data"
MAX_DOC_BYTES = 15 * 1024 * 1024                      # MongoDB hard limit is 16 MB per document
CONNECT_ATTEMPTS = max(1, int(os.environ.get("PERSIST_CONNECT_ATTEMPTS", "5") or 5))


def _log(msg: str):
    print(f"[STORAGE] {msg}", flush=True)


class Persist:
    def __init__(self):
        self._lock = threading.RLock()        # guards the dicts below
        self._io_lock = threading.Lock()      # one MongoDB drain at a time
        self._text: Dict[str, Optional[str]] = {}     # document name -> JSON text (truth while MongoDB is active)
        self._pending: Dict[str, str] = {}            # name -> JSON text not yet in MongoDB
        self._due: Dict[str, float] = {}              # name -> earliest time to write it
        self._interval: Dict[str, float] = {}         # name -> minimum seconds between writes
        self._last_write: Dict[str, float] = {}
        self._blocked: set = set()                    # names that must never be written (read failed)
        self._col = None
        self.mode = "unset"                           # "mongo" | "local" | "degraded"
        self._ready = False
        self._wake = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._err_logged = 0.0
        atexit.register(self._atexit)

    # ------------------------------------------------------------------ connection
    def _ensure(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            url = (os.environ.get("MONGODB_CONNECTION_STRING_2") or os.environ.get("MONGODB_CONNECTION_STRING", "")).strip()
            if not url:
                self.mode = "local"
                _log("!! MongoDB is NOT configured (set MONGODB_CONNECTION_STRING). Accounts / orders are kept in local "
                     "files only and will be LOST on every restart or redeploy.")
            elif MongoClient is None:
                self.mode = "local"
                _log("!! pymongo is not installed - using local files only (data is lost on redeploy).")
            else:
                client = None
                for attempt in range(1, CONNECT_ATTEMPTS + 1):
                    try:
                        client = MongoClient(url, serverSelectionTimeoutMS=5000)
                        client.admin.command("ping")
                        break
                    except Exception as exc:
                        _log(f"MongoDB connect attempt {attempt}/{CONNECT_ATTEMPTS} failed: {exc}")
                        client = None
                        if attempt < CONNECT_ATTEMPTS:
                            time.sleep(3)
                if client is None:
                    self.mode = "degraded"
                    _log("!! MongoDB is not reachable - running on local files only for THIS run. "
                         "Nothing will be written to MongoDB until the next restart.")
                else:
                    try:
                        try:
                            database = client.get_default_database()
                        except Exception:
                            database = client["exe_level_up"]
                        self._col = database[COLLECTION]
                        self.mode = "mongo"
                        _log("MongoDB connected - accounts, orders/history, devices and token cache are saved there.")
                    except Exception as exc:
                        self._col = None
                        self.mode = "degraded"
                        _log(f"!! MongoDB collection error: {exc} - using local files for this run.")
            self._ready = True

    @property
    def active(self) -> bool:
        self._ensure()
        return self._col is not None

    # ------------------------------------------------------------------ local file helpers
    @staticmethod
    def _read_file(path: str) -> Optional[str]:
        try:
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
            if not text:
                return None
            json.loads(text)                           # must be valid JSON
            return text
        except Exception:
            return None

    @staticmethod
    def _write_file(path: str, text: str):
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except Exception as exc:
            _log(f"local save failed {os.path.basename(path)}: {exc}")
            try:
                os.remove(tmp)
            except Exception:
                pass

    # ------------------------------------------------------------------ MongoDB helpers
    def _read_mongo(self, name: str):
        """(text or None, ok).  ok=False when MongoDB could not be read at all."""
        for attempt in range(3):
            try:
                doc = self._col.find_one({"_id": name})
                if doc and isinstance(doc.get("json"), str):
                    return doc["json"], True
                return None, True
            except Exception as exc:
                _log(f"read {name} failed (try {attempt + 1}/3): {exc}")
                time.sleep(1)
        return None, False

    def _start_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._worker_loop, name="persist-writer", daemon=True)
            self._worker.start()

    def _worker_loop(self):
        while True:
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self._drain(force=False)
            except Exception as exc:                  # never let the writer die
                _log(f"writer error: {exc}")

    def _queue(self, name: str, text: str):
        with self._lock:
            self._pending[name] = text
            iv = self._interval.get(name, 0.0)
            self._due[name] = max(time.time(), self._last_write.get(name, 0.0) + iv)
        self._start_worker()
        self._wake.set()

    def _drain(self, force: bool = False) -> bool:
        """Write due documents to MongoDB. True when nothing is left waiting."""
        if self._col is None:
            return True
        with self._io_lock:
            with self._lock:
                now = time.time()
                batch = [(n, t) for n, t in self._pending.items() if force or self._due.get(n, 0.0) <= now]
            for name, text in batch:
                size = len(text.encode("utf-8"))
                if size > MAX_DOC_BYTES:
                    _log(f"!! {name} is {size // 1024} KB - too large for one MongoDB document, kept on local disk only")
                    with self._lock:
                        self._pending.pop(name, None)
                    continue
                try:
                    self._col.replace_one({"_id": name},
                                          {"_id": name, "json": text, "bytes": size, "updated_at": int(time.time())},
                                          upsert=True)
                except Exception as exc:
                    with self._lock:
                        self._due[name] = time.time() + 5.0        # retry soon
                    if time.time() - self._err_logged > 30:
                        self._err_logged = time.time()
                        _log(f"MongoDB write failed for {name} (will retry): {exc}")
                    continue
                with self._lock:
                    if self._pending.get(name) == text:
                        self._pending.pop(name, None)
                    self._last_write[name] = time.time()
            with self._lock:
                return not self._pending

    def flush(self, timeout: float = 10.0) -> bool:
        """Push everything to MongoDB now (used on shutdown and after important changes)."""
        if self._col is None:
            return True
        end = time.time() + max(0.5, timeout)
        while True:
            if self._drain(force=True):
                return True
            if time.time() >= end:
                _log("flush timed out - some data is still waiting for MongoDB")
                return False
            time.sleep(0.5)

    def _atexit(self):
        try:
            self.flush(timeout=5.0)
        except Exception:
            pass

    # ------------------------------------------------------------------ public API
    def _get_text(self, name: str, path: str) -> Optional[str]:
        with self._lock:
            if name in self._text:
                return self._text[name]
        self._ensure()
        text: Optional[str] = None
        file_text = self._read_file(path)
        if self._col is not None:
            mongo_text, ok = self._read_mongo(name)
            if not ok:
                self._blocked.add(name)                # could not read -> never overwrite the stored copy
                text = file_text
                _log(f"!! {name}: MongoDB unreadable, using the local copy and NOT writing it back")
            elif mongo_text is not None:
                text = mongo_text
            elif file_text is not None:
                text = file_text                       # first run with MongoDB: import the local file once
                _log(f"{name}: imported the existing local file into MongoDB")
                with self._lock:
                    self._text[name] = text
                self._queue(name, text)
        else:
            text = file_text
        with self._lock:
            self._text.setdefault(name, text)
            return self._text[name]

    def load(self, name: str, path: str, default: Any) -> Any:
        """Current value of a document (a fresh copy every call)."""
        self._ensure()
        if self._col is None:                          # local mode: behave exactly like the old file code
            text = self._read_file(path)
        else:
            text = self._get_text(name, path)
        if text is None:
            return json.loads(json.dumps(default))
        try:
            return json.loads(text)
        except Exception:
            return json.loads(json.dumps(default))

    def save(self, name: str, path: str, data: Any = None, text: Optional[str] = None, min_interval: float = 0.0):
        """Store a document. Give either `data` (any JSON-able object) or ready-made JSON `text`."""
        self._ensure()
        if text is None:
            text = json.dumps(data, default=str, separators=(",", ":"))
        if self._col is None:
            self._write_file(path, text)
            return
        self._get_text(name, path)                     # make sure we never overwrite data we have not read yet
        with self._lock:
            self._interval[name] = min_interval
            if self._text.get(name) == text and name not in self._pending:
                return                                 # nothing changed
            self._text[name] = text
        self._write_file(path, text)                   # local mirror (backup / fallback)
        if name not in self._blocked:
            self._queue(name, text)

    def describe(self) -> str:
        self._ensure()
        if self._col is None:
            return f"storage mode: {self.mode} (local files only - data is lost on redeploy)"
        try:
            n = self._col.count_documents({})
        except Exception:
            n = -1
        return f"storage mode: MongoDB ({n} documents)"


store = Persist()
