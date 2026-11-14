"""Deterministic input payloads for data_digest (plan §3 invariant 6, §4).

A blob is a pure function of its size: SHAKE-256 of a fixed seed, so every run and
every strategy sees identical bytes (DECISIONS D-010). Blobs are generated once per
size, written atomically as `<size>.bin` with a `<size>.sha256` sidecar, and reused.
Generation is CPU work, so callers run `ensure()` in a thread, never on the event loop.
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

SEED = b"dwr-blob-v1"


class BlobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, size: int) -> Path:
        return self.root / f"{size}.bin"

    def ensure(self, size: int) -> str:
        """Create the blob for `size` if missing; return its sha256 hex digest."""
        with self._lock:
            data_path, sha_path = self.path(size), self.root / f"{size}.sha256"
            if data_path.is_file() and sha_path.is_file() and data_path.stat().st_size == size:
                return sha_path.read_text(encoding="ascii").strip()
            data = generate(size)
            sha = hashlib.sha256(data).hexdigest()
            _atomic_write(data_path, data)
            _atomic_write(sha_path, sha.encode("ascii"))
            return sha


def generate(size: int) -> bytes:
    return hashlib.shake_256(SEED).digest(size) if size else b""


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
