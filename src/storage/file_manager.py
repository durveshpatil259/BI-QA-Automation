"""Low-level filesystem helpers used by the storage layer.

Centralises safe JSON read/write, atomic saves, filename sanitisation and
binary asset copying so higher-level repositories stay small and consistent.
All functions raise :class:`StorageError` on failure with a clear message.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from src.core.exceptions import StorageError
from src.core.logger import get_logger

_logger = get_logger()

# Characters not allowed in Windows filenames/folder names.
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_name(name: str, fallback: str = "untitled") -> str:
    """Return a filesystem-safe version of *name* (for folder/file names)."""
    cleaned = _INVALID_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.rstrip(" .")  # Windows disallows trailing space/dot
    if not cleaned or cleaned.upper() in _RESERVED:
        return fallback
    return cleaned[:120]  # keep paths comfortably short


def ensure_dir(path: Path) -> Path:
    """Create *path* (and parents) if missing; return it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"Could not create directory {path}: {exc}") from exc
    return path


def read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON file, raising :class:`StorageError` on failure."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StorageError(f"File not found: {path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise StorageError(f"Could not read JSON {path}: {exc}") from exc


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* as pretty JSON to *path*.

    Writes to a temp file in the same directory then replaces the target, so a
    crash mid-write can never leave a half-written project file.
    """
    path = Path(path)
    ensure_dir(path.parent)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, path)  # atomic on Windows and POSIX
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as exc:
        raise StorageError(f"Could not write JSON {path}: {exc}") from exc


def save_bytes(path: Path, data: bytes) -> None:
    """Persist raw *data* bytes to *path* (used for uploaded assets)."""
    path = Path(path)
    ensure_dir(path.parent)
    try:
        path.write_bytes(data)
    except OSError as exc:
        raise StorageError(f"Could not write file {path}: {exc}") from exc


def list_dir(path: Path, extensions: tuple[str, ...] | None = None) -> list[Path]:
    """List files directly under *path*, optionally filtered by extension."""
    path = Path(path)
    if not path.exists():
        return []
    files = [p for p in path.iterdir() if p.is_file()]
    if extensions:
        exts = tuple(e.lower() for e in extensions)
        files = [p for p in files if p.suffix.lower() in exts]
    return sorted(files, key=lambda p: p.name.lower())


def delete_path(path: Path) -> None:
    """Delete a file or directory tree if it exists."""
    path = Path(path)
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError as exc:
        raise StorageError(f"Could not delete {path}: {exc}") from exc
