"""Chunked xxHash64 helpers used by the offload engine and verifier."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import xxhash

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB: good balance for SD readers and SSDs

ProgressFn = Callable[[int], None]  # called with the number of bytes just processed
CancelFn = Callable[[], bool]  # return True to abort


class Cancelled(Exception):
    pass


def new_hasher() -> "xxhash.xxh64":
    return xxhash.xxh64()


def xxh64_file(
    path: Path | str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    progress: ProgressFn | None = None,
    cancelled: CancelFn | None = None,
) -> str:
    """Hash a file, streaming in chunks. Returns lowercase hex digest."""
    h = new_hasher()
    with open(path, "rb", buffering=0) as f:
        while True:
            if cancelled is not None and cancelled():
                raise Cancelled()
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
            if progress is not None:
                progress(len(chunk))
    return h.hexdigest()


def copy_and_hash(
    src: Path | str,
    dst: Path | str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    progress: ProgressFn | None = None,
    cancelled: CancelFn | None = None,
) -> str:
    """Copy *src* to *dst* while hashing the bytes read from *src*.

    The destination is fsync'd before returning so a later read-back sees data
    that is really on the platter / flash, not just in the OS cache.
    """
    h = new_hasher()
    with open(src, "rb", buffering=0) as fin, open(dst, "wb", buffering=0) as fout:
        while True:
            if cancelled is not None and cancelled():
                raise Cancelled()
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
            fout.write(chunk)
            if progress is not None:
                progress(len(chunk))
        fout.flush()
        os.fsync(fout.fileno())
    return h.hexdigest()


def xxh64_bytes(data: bytes) -> str:
    return xxhash.xxh64(data).hexdigest()
