"""Low-level IO helpers for Power BI packages.

Power BI text parts use mixed encodings (``Report/Layout`` is UTF-16-LE;
``DataModelSchema`` often UTF-16; TMDL is UTF-8). These helpers decode bytes
robustly and provide a small, uniform view over a ZIP package's entries.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field

# Fallback order once BOM detection is inconclusive. UTF-8 is tried before
# UTF-16 on purpose: UTF-8 bytes of even length decode as UTF-16 *without*
# erroring (yielding mojibake), so UTF-16 must never be attempted first for
# BOM-less content. Power BI's UTF-16 parts (Layout / DataModelSchema) always
# carry a BOM, which is detected explicitly below.
_FALLBACK_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be", "latin-1")


def decode_text(data: bytes) -> str:
    """Decode *data*, honouring the byte-order mark or NUL-byte pattern.

    Power BI parts are inconsistent: some UTF-16 parts carry a BOM, but others
    (e.g. ``Report/Layout``) are UTF-16-LE with **no** BOM. Because NUL bytes are
    valid UTF-8, BOM-less UTF-16 would silently decode as UTF-8 garbage — so we
    also sniff for the tell-tale NUL-byte distribution before falling back.
    """
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8")
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le")
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be")

    # BOM-less UTF-16 detection: ASCII-heavy UTF-16 text is ~50% NUL bytes,
    # whereas UTF-8/JSON has essentially none.
    sample = data[:4096]
    if sample and sample.count(0) > len(sample) * 0.25:
        even_nul = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
        odd_nul = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
        enc = "utf-16-be" if even_nul > odd_nul else "utf-16-le"
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass

    for enc in _FALLBACK_ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def parse_json_bytes(data: bytes):
    """Decode + parse JSON bytes, tolerating a leading BOM/whitespace."""
    text = decode_text(data).lstrip("﻿ \r\n\t")
    return json.loads(text)


def parse_json_string(text: str):
    """Parse a JSON string that may itself be empty or whitespace."""
    text = (text or "").strip()
    if not text:
        return None
    return json.loads(text)


@dataclass
class PbiPackage:
    """An opened Power BI ZIP package with case-insensitive entry lookup."""

    names: list[str] = field(default_factory=list)
    _zip: zipfile.ZipFile | None = None

    @classmethod
    def open(cls, path) -> "PbiPackage":
        zf = zipfile.ZipFile(path, "r")
        return cls(names=zf.namelist(), _zip=zf)

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()

    def __enter__(self) -> "PbiPackage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- lookup helpers ---------------------------------------------------
    def _norm(self, name: str) -> str:
        return name.replace("\\", "/").lower()

    def has(self, name: str) -> bool:
        target = self._norm(name)
        return any(self._norm(n) == target for n in self.names)

    def find_exact(self, name: str) -> str | None:
        target = self._norm(name)
        for n in self.names:
            if self._norm(n) == target:
                return n
        return None

    def find_endswith(self, suffix: str) -> list[str]:
        s = self._norm(suffix)
        return [n for n in self.names if self._norm(n).endswith(s)]

    def find_in_dir(self, contains: str) -> list[str]:
        c = self._norm(contains)
        return [n for n in self.names if c in self._norm(n)]

    def read_bytes(self, name: str) -> bytes:
        assert self._zip is not None
        real = self.find_exact(name) or name
        return self._zip.read(real)

    def read_text(self, name: str) -> str:
        return decode_text(self.read_bytes(name))

    def read_json(self, name: str):
        return parse_json_bytes(self.read_bytes(name))
