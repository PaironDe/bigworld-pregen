from __future__ import annotations

import hashlib
import os
from pathlib import Path

BUF = 1024 * 1024


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(BUF)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_relpath(rel: str) -> bool:
    """Nur relative Pfade ohne '..', ohne Laufwerksbuchstaben, mit '/' getrennt."""
    if not rel or rel.startswith("/") or "\\" in rel or ":" in rel:
        return False
    parts = rel.split("/")
    return all(p not in ("", ".", "..") for p in parts)


def join_safe(base: Path, rel: str) -> Path:
    if not safe_relpath(rel):
        raise ValueError(f"Unsicherer Pfad: {rel!r}")
    target = (base / rel).resolve()
    if base.resolve() not in target.parents and target != base.resolve():
        raise ValueError(f"Pfad außerhalb des Zielordners: {rel!r}")
    return target
