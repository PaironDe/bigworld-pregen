"""Chunkweiser Vergleich zweier Generierungen derselben Kachel (Determinismus-Test).

Verglichen werden die entpackten NBT-Daten, nicht die Dateien selbst, weil
Regionsdateien Zeitstempel, Sektor-Reihenfolge und Kompression je nach
Schreibreihenfolge unterschiedlich ablegen. Felder, die legitim abweichen,
werden ignoriert:
- region/:   LastUpdate, InhabitedTime (Zeitstempel/Spielzeit)
- entities/: UUID (zufällig vergeben, auch bei identischer Generierung)
"""

from __future__ import annotations

import tempfile
import tarfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import anvil, nbt
from .hashing import safe_relpath

IGNORE = {
    "region": {"LastUpdate", "InhabitedTime"},
    "entities": {"UUID"},
    "poi": set(),
}


def _strip(obj, ignore: set[str]):
    if isinstance(obj, dict):
        return {k: _strip(v, ignore) for k, v in obj.items() if k not in ignore}
    if isinstance(obj, list):
        return [_strip(v, ignore) for v in obj]
    return obj


def first_difference(a, b, path: str = "") -> str | None:
    if type(a) is not type(b):
        return f"{path or '/'}: Typ {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}/{k}: nur auf {'B' if k not in a else 'A'} vorhanden"
            d = first_difference(a[k], b[k], f"{path}/{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: Listenlänge {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if a != b:
        return f"{path or '/'}: Wert unterschiedlich"
    return None


@dataclass
class CompareReport:
    files_compared: int = 0
    chunks_identical: int = 0
    chunks_equal_after_ignore: int = 0
    chunks_different: int = 0
    chunks_missing: int = 0
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    diff_by_storage: Counter = field(default_factory=Counter)

    @property
    def deterministic(self) -> bool:
        return (
            self.chunks_different == 0
            and self.chunks_missing == 0
            and not self.only_in_a
            and not self.only_in_b
        )

    def to_dict(self) -> dict:
        return {
            "deterministic": self.deterministic,
            "files_compared": self.files_compared,
            "chunks_identical": self.chunks_identical,
            "chunks_equal_after_ignore": self.chunks_equal_after_ignore,
            "chunks_different": self.chunks_different,
            "chunks_missing": self.chunks_missing,
            "only_in_a": self.only_in_a,
            "only_in_b": self.only_in_b,
            "diff_by_storage": dict(self.diff_by_storage),
            "examples": self.examples,
        }


def _region_files(root: Path) -> dict[str, Path]:
    return {
        p.relative_to(root).as_posix(): p
        for p in root.rglob("r.*.mca")
        if p.is_file()
    }


def compare_trees(a_root: Path, b_root: Path, max_examples: int = 20) -> CompareReport:
    rep = CompareReport()
    a_files, b_files = _region_files(a_root), _region_files(b_root)
    rep.only_in_a = sorted(set(a_files) - set(b_files))
    rep.only_in_b = sorted(set(b_files) - set(a_files))
    for rel in sorted(set(a_files) & set(b_files)):
        storage = rel.split("/")[-2]
        ignore = IGNORE.get(storage, set())
        ra, rb = anvil.RegionFile(a_files[rel]), anvil.RegionFile(b_files[rel])
        rep.files_compared += 1
        for index in range(1024):
            ca, cb = ra.chunk_bytes(index), rb.chunk_bytes(index)
            if ca is None and cb is None:
                continue
            if ca is None or cb is None:
                rep.chunks_missing += 1
                if len(rep.examples) < max_examples:
                    rep.examples.append(f"{rel} Chunk {index}: nur auf {'A' if cb is None else 'B'}")
                continue
            if ca == cb:
                rep.chunks_identical += 1
                continue
            ta, tb = _strip(nbt.parse(ca), ignore), _strip(nbt.parse(cb), ignore)
            if ta == tb:
                rep.chunks_equal_after_ignore += 1
                continue
            rep.chunks_different += 1
            rep.diff_by_storage[storage] += 1
            if len(rep.examples) < max_examples:
                rep.examples.append(f"{rel} Chunk {index}: {first_difference(ta, tb)}")
    return rep


def _extract(tar_path: Path, dest: Path) -> None:
    with tarfile.open(tar_path, "r") as tar:
        for m in tar.getmembers():
            if not m.isfile() or not safe_relpath(m.name):
                continue
            target = dest / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                continue
            with open(target, "wb") as out:
                while True:
                    b = src.read(1024 * 1024)
                    if not b:
                        break
                    out.write(b)


def compare_packages(tar_a: Path, tar_b: Path) -> CompareReport:
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "a", Path(tmp) / "b"
        _extract(tar_a, a)
        _extract(tar_b, b)
        return compare_trees(a, b)
