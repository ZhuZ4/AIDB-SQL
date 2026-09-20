"""Pin business SQL and official evaluation to the same SQLite runtime.

The workstation's SQLite 3.51.0 uses a repeated coroutine scan for frozen
question 701 (>180 seconds). SQLite 3.40.1 materializes the same subquery and
executes gold twice in <1 second. No SQL, database, or scoring rule is changed.
The official DLL is loaded in worker processes only; the system installation and
the supervisor's bookkeeping database are untouched.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
from pathlib import Path
import sys
from urllib.request import urlopen
import zipfile

VERSION = "3.40.1"
DOWNLOAD_URL = "https://www.sqlite.org/2022/sqlite-dll-win64-x64-3400100.zip"
ARCHIVE_SHA256 = "3f9d170be88166ed87a5faa4b795a6a9ca1896aec3b7f597e53a8bb2c7d8fb8d"
DLL_SHA256 = "1845e2dac344a601460ad5fa15c2ca1cd046ba157fbb64a151094b139c8584bc"
DLL_PATH = Path(__file__).resolve().parents[1] / ".local-services/evaluator-runtime/sqlite-3.40.1/sqlite3.dll"
_DLL_HANDLE = None


def runtime_metadata() -> dict[str, str]:
    """Validate disk artifact without importing SQLite or loading the DLL."""
    if not DLL_PATH.is_file():
        raise RuntimeError("Pinned SQLite runtime missing; run python -m experiments.sqlite_runtime --install")
    actual = hashlib.sha256(DLL_PATH.read_bytes()).hexdigest()
    if actual != DLL_SHA256:
        raise RuntimeError("Pinned SQLite DLL hash mismatch; refusing an untracked SQL runtime")
    return {"version": VERSION, "dll_path": str(DLL_PATH.resolve()), "dll_sha256": actual,
            "download_url": DOWNLOAD_URL, "archive_sha256": ARCHIVE_SHA256}


def install_runtime() -> dict[str, str]:
    """Download only the pinned official archive and verify before installation."""
    if DLL_PATH.exists():
        return runtime_metadata()
    archive = urlopen(DOWNLOAD_URL, timeout=60).read()
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("Official SQLite archive hash mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        dll = contents.read("sqlite3.dll")
    if hashlib.sha256(dll).hexdigest() != DLL_SHA256:
        raise RuntimeError("Official SQLite DLL hash mismatch")
    DLL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DLL_PATH.with_suffix(".dll.tmp")
    temporary.write_bytes(dll)
    temporary.replace(DLL_PATH)
    metadata = runtime_metadata()
    (DLL_PATH.parent / "provenance.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def bootstrap_sqlite_runtime() -> dict[str, str]:
    """Call before importing agent/SQLite; fail closed if another runtime loaded."""
    global _DLL_HANDLE
    metadata = runtime_metadata()
    if sys.platform != "win32":
        raise RuntimeError("This frozen experiment runtime requires Windows x64")
    existing = sys.modules.get("sqlite3") or sys.modules.get("_sqlite3")
    if existing is not None and _DLL_HANDLE is None:
        raise RuntimeError(f"SQLite was imported before bootstrap (version {getattr(existing, 'sqlite_version', 'unknown')}); use a fresh worker process")
    if _DLL_HANDLE is None:
        _DLL_HANDLE = ctypes.WinDLL(str(DLL_PATH.resolve()))
    import sqlite3
    if sqlite3.sqlite_version != VERSION:
        raise RuntimeError(f"Failed to pin SQLite runtime: wanted {VERSION}, loaded {sqlite3.sqlite_version}")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.install:
        install_runtime()
    metadata = bootstrap_sqlite_runtime() if args.check else runtime_metadata()
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
