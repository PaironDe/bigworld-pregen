"""Chunkweiser Vergleich zweier Generierungen derselben Kachel (Determinismus-Test).

Verglichen wird, was in der Welt tatsächlich steht, nicht wie es gespeichert ist:

- Blöcke und Biome werden aus Palette + gepackten Indizes dekodiert und Position für
  Position verglichen. Die Reihenfolge der Palette hängt von der Generierungsreihenfolge
  ab und sagt nichts über die Welt aus.
- Listen ohne feste Bedeutung der Reihenfolge (Block-Entities, geplante Ticks,
  Strukturreferenzen, PostProcessing, POI) werden vor dem Vergleich sortiert.
- Zeitstempel (LastUpdate, InhabitedTime, Tick-Zeitpunkte) werden ignoriert.
- Tiere: Minecraft würfelt Werte wie EggLayTime oder Attribut-Basiswerte beim Spawnen mit
  einem nicht reproduzierbaren Zufall aus. Verglichen werden deshalb nur Art und Position.

Das Ergebnis ist nach Kategorien aufgeteilt. Für die Frage "ist die Welt gleich?" zählen
Blöcke, Biome und Strukturen (siehe CRITICAL). Licht, Heightmaps und Tiere werden nur
gemeldet.
"""

from __future__ import annotations

import json
import math
import struct
import tarfile
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import anvil, nbt
from .hashing import safe_relpath

CRITICAL = ("blocks", "biomes", "structures", "block_entities", "missing_region")
REGION_IGNORE = {"LastUpdate", "InhabitedTime"}


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _longs(raw) -> list[int]:
    if isinstance(raw, tuple) and raw[0] == "L":
        b = raw[1]
        return [x & 0xFFFFFFFFFFFFFFFF for x in struct.unpack(f">{len(b) // 8}q", b)]
    return []


def _canon(obj) -> str:
    def conv(o):
        if isinstance(o, tuple) and len(o) == 2 and isinstance(o[1], (bytes, bytearray)):
            return [o[0], o[1].hex()]
        if isinstance(o, dict):
            return {k: conv(v) for k, v in o.items()}
        if isinstance(o, list):
            return [conv(v) for v in o]
        return o

    return json.dumps(conv(obj), sort_keys=True, separators=(",", ":"))


def _decode(container: dict | None, count: int, min_bits: int):
    """Palettierter Container -> (Palette als kanonische Strings, Indizes je Position) oder None."""
    if not isinstance(container, dict):
        return None
    names = [_canon(p) for p in (container.get("palette") or [])]
    if not names:
        return None
    if len(names) == 1 or "data" not in container:
        return names, [0] * count
    bits = max(min_bits, math.ceil(math.log2(len(names))))
    per_long = 64 // bits
    mask = (1 << bits) - 1
    try:
        import numpy as np

        arr = np.frombuffer(container["data"][1], dtype=">u8").astype(np.uint64)
        idx = np.arange(count)
        vals = (arr[idx // per_long] >> ((idx % per_long) * bits).astype(np.uint64)) & np.uint64(mask)
        return names, vals.astype(np.int64)
    except ImportError:
        longs = _longs(container["data"])
        return names, [(longs[i // per_long] >> ((i % per_long) * bits)) & mask for i in range(count)]


def _diff_positions(da, db) -> list[int] | None:
    """Positionen, an denen sich zwei dekodierte Container unterscheiden."""
    (na, va), (nb, vb) = da, db
    universe = {n: i for i, n in enumerate(dict.fromkeys(na + nb + ["?"]))}
    ma = [universe[n] for n in na]
    mb = [universe[n] for n in nb]
    try:
        import numpy as np

        la = np.array(ma + [universe["?"]] * 64)[np.asarray(va)]
        lb = np.array(mb + [universe["?"]] * 64)[np.asarray(vb)]
        return [int(i) for i in np.nonzero(la != lb)[0]]
    except ImportError:
        def get(m, v):
            return m[v] if v < len(m) else universe["?"]
        return [i for i, (x, y) in enumerate(zip(va, vb)) if get(ma, x) != get(mb, y)]


def _name_at(decoded, i) -> str:
    names, vals = decoded
    v = int(vals[i])
    return names[v] if v < len(names) else "?"


def _sections_by_y(chunk: dict) -> dict[int, dict]:
    secs = chunk.get("sections") or chunk.get("Sections") or []
    return {s.get("Y", 0): s for s in secs if isinstance(s, dict)}


def _sorted_canon(items) -> list[str]:
    return sorted(_canon(i) for i in (items or []))


# ---------------------------------------------------------------------------
# Vergleich einzelner Chunks
# ---------------------------------------------------------------------------


@dataclass
class ChunkDiff:
    categories: set[str] = field(default_factory=set)
    blocks: int = 0
    biomes: int = 0
    first_block: tuple | None = None  # (x, y, z, a, b)
    detail: dict[str, str] = field(default_factory=dict)


def _note(d: ChunkDiff, cat: str, text: str) -> None:
    d.categories.add(cat)
    d.detail.setdefault(cat, text)


def compare_region_chunk(a: dict, b: dict, cx: int, cz: int) -> ChunkDiff:
    d = ChunkDiff()
    a = {k: v for k, v in a.items() if k not in REGION_IGNORE}
    b = {k: v for k, v in b.items() if k not in REGION_IGNORE}
    seen = {"sections", "Sections", "block_entities", "block_ticks", "fluid_ticks", "PostProcessing",
            "structures", "Heightmaps", "isLightOn"}

    sa, sb = _sections_by_y(a), _sections_by_y(b)
    for y in sorted(set(sa) | set(sb)):
        xa, xb = sa.get(y, {}), sb.get(y, {})
        if xa.get("block_states") != xb.get("block_states"):
            da = _decode(xa.get("block_states"), 4096, 4)
            db = _decode(xb.get("block_states"), 4096, 4)
            if da is None or db is None:
                if da is not None or db is not None:
                    d.blocks += 4096
                    _note(d, "blocks", f"Abschnitt Y={y} nur auf einer Seite vorhanden")
            else:
                pos = _diff_positions(da, db)
                if pos:
                    d.blocks += len(pos)
                    d.categories.add("blocks")
                    if d.first_block is None:
                        i = pos[0]
                        d.first_block = (cx * 16 + i % 16, y * 16 + i // 256, cz * 16 + (i // 16) % 16,
                                         _name_at(da, i), _name_at(db, i))
                    d.detail.setdefault("blocks", f"Abschnitt Y={y}: {len(pos)} Blöcke verschieden")
        if xa.get("biomes") != xb.get("biomes"):
            da = _decode(xa.get("biomes"), 64, 1)
            db = _decode(xb.get("biomes"), 64, 1)
            n = len(_diff_positions(da, db)) if da and db else (64 if (da or db) else 0)
            if n:
                d.biomes += n
                _note(d, "biomes", f"Abschnitt Y={y}: {n} von 64 Biom-Einträgen verschieden")
        for k in ("SkyLight", "BlockLight"):
            if xa.get(k) != xb.get(k):
                _note(d, "light", f"Abschnitt Y={y}: {k}")
        other = (set(xa) | set(xb)) - {"block_states", "biomes", "SkyLight", "BlockLight", "Y"}
        for k in sorted(other):
            if xa.get(k) != xb.get(k):
                _note(d, "other", f"Abschnitt Y={y}: {k}")

    if _sorted_canon(a.get("block_entities")) != _sorted_canon(b.get("block_entities")):
        _note(d, "block_entities", "Block-Entities verschieden (z. B. Truhen, Spawner)")

    for k in ("block_ticks", "fluid_ticks"):
        ta = sorted(_canon({x: e.get(x) for x in ("i", "x", "y", "z")}) for e in a.get(k) or [])
        tb = sorted(_canon({x: e.get(x) for x in ("i", "x", "y", "z")}) for e in b.get(k) or [])
        if ta != tb:
            _note(d, "ticks", k)

    pa = [sorted(s) if isinstance(s, list) else s for s in a.get("PostProcessing") or []]
    pb = [sorted(s) if isinstance(s, list) else s for s in b.get("PostProcessing") or []]
    if pa != pb:
        _note(d, "ticks", "PostProcessing")

    sta, stb = a.get("structures") or {}, b.get("structures") or {}
    if _canon(sta.get("starts")) != _canon(stb.get("starts")):
        _note(d, "structures", "Strukturstarts verschieden")
    ra = {k: sorted(_longs(v)) for k, v in (sta.get("References") or {}).items()}
    rb = {k: sorted(_longs(v)) for k, v in (stb.get("References") or {}).items()}
    if ra != rb:
        _note(d, "structures", "Strukturreferenzen verschieden")

    if a.get("Heightmaps") != b.get("Heightmaps"):
        _note(d, "heightmaps", "Heightmaps")
    if a.get("isLightOn") != b.get("isLightOn"):
        _note(d, "light", "isLightOn")

    for k in sorted((set(a) | set(b)) - seen):
        if _canon(a.get(k)) != _canon(b.get(k)):
            _note(d, "other", k)
    return d


def _entity_key(e: dict) -> str:
    pos = e.get("Pos") or []
    return _canon({"id": e.get("id"), "Pos": [round(p, 2) for p in pos if isinstance(p, float)]})


def compare_entity_chunk(a: dict, b: dict) -> ChunkDiff:
    d = ChunkDiff()
    ea = sorted(_entity_key(e) for e in a.get("Entities") or [])
    eb = sorted(_entity_key(e) for e in b.get("Entities") or [])
    if ea != eb:
        _note(d, "entities_spawn", f"{len(ea)} gegen {len(eb)} Tiere bzw. andere Arten/Positionen")
    else:
        _note(d, "entities_random", "nur zufällige Tierwerte (EggLayTime, Attribute usw.)")
    return d


def compare_poi_chunk(a: dict, b: dict) -> ChunkDiff:
    d = ChunkDiff()

    def norm(c):
        out = {}
        for y, s in (c.get("Sections") or {}).items():
            out[y] = sorted(_canon({k: v for k, v in r.items() if k != "free_tickets"}) for r in s.get("Records") or [])
        return out

    if norm(a) != norm(b):
        _note(d, "poi", "Points of Interest verschieden (z. B. Betten, Arbeitsblöcke)")
    return d


# ---------------------------------------------------------------------------
# Bericht
# ---------------------------------------------------------------------------


@dataclass
class CompareReport:
    files_compared: int = 0
    chunks_identical: int = 0
    chunks_equal_after_ignore: int = 0
    chunks_different: int = 0
    chunks_missing: int = 0
    blocks_different: int = 0
    biome_entries_different: int = 0
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)
    categories: Counter = field(default_factory=Counter)
    missing_by_storage: Counter = field(default_factory=Counter)
    diff_by_storage: Counter = field(default_factory=Counter)
    examples: dict[str, list[str]] = field(default_factory=dict)
    block_examples: list[str] = field(default_factory=list)

    def add_example(self, cat: str, text: str, limit: int = 5) -> None:
        lst = self.examples.setdefault(cat, [])
        if len(lst) < limit:
            lst.append(text)

    @property
    def deterministic(self) -> bool:
        """Welt gleich: keine Unterschiede bei Blöcken, Biomen, Strukturen, Block-Entities."""
        if any(f.split("/")[-2] == "region" for f in self.only_in_a + self.only_in_b):
            return False
        return not any(self.categories.get(c) for c in CRITICAL)

    def to_dict(self) -> dict:
        minor = {k: v for k, v in self.categories.items() if k not in CRITICAL}
        return {
            "deterministic": self.deterministic,
            "files_compared": self.files_compared,
            "chunks_identical": self.chunks_identical,
            "chunks_equal_after_ignore": self.chunks_equal_after_ignore,
            "chunks_different": self.chunks_different,
            "chunks_missing": self.chunks_missing,
            "blocks_different": self.blocks_different,
            "biome_entries_different": self.biome_entries_different,
            "critical": {k: self.categories.get(k, 0) for k in CRITICAL if self.categories.get(k)},
            "minor": minor,
            "missing_by_storage": dict(self.missing_by_storage),
            "diff_by_storage": dict(self.diff_by_storage),
            "only_in_a": self.only_in_a,
            "only_in_b": self.only_in_b,
            "block_examples": self.block_examples,
            "examples": [f"{cat}: {t}" for cat in sorted(self.examples, key=lambda c: (c not in CRITICAL, c))
                         for t in self.examples[cat]],
        }


def _region_files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in root.rglob("r.*.mca") if p.is_file()}


def compare_trees(a_root: Path, b_root: Path) -> CompareReport:
    rep = CompareReport()
    a_files, b_files = _region_files(a_root), _region_files(b_root)
    rep.only_in_a = sorted(set(a_files) - set(b_files))
    rep.only_in_b = sorted(set(b_files) - set(a_files))
    for rel in sorted(set(a_files) & set(b_files)):
        storage = rel.split("/")[-2]
        ra, rb = anvil.RegionFile(a_files[rel]), anvil.RegionFile(b_files[rel])
        rep.files_compared += 1
        for index in range(1024):
            ca, cb = ra.chunk_bytes(index), rb.chunk_bytes(index)
            if ca is None and cb is None:
                continue
            cx, cz = ra.rx * 32 + index % 32, ra.rz * 32 + index // 32
            where = f"{rel} Chunk {index} (Block x {cx * 16}, z {cz * 16})"
            if ca is None or cb is None:
                rep.chunks_missing += 1
                rep.missing_by_storage[storage] += 1
                if storage == "region":
                    rep.categories["missing_region"] += 1
                rep.add_example(f"fehlt_{storage}", f"{where}: nur auf {'A' if cb is None else 'B'}")
                continue
            if ca == cb:
                rep.chunks_identical += 1
                continue
            ta, tb = nbt.parse(ca), nbt.parse(cb)
            if storage == "region":
                d = compare_region_chunk(ta, tb, cx, cz)
            elif storage == "entities":
                d = compare_entity_chunk(ta, tb)
            elif storage == "poi":
                d = compare_poi_chunk(ta, tb)
            else:
                d = ChunkDiff()
                if ta != tb:
                    _note(d, "other", storage)
            if not d.categories:
                rep.chunks_equal_after_ignore += 1
                continue
            if d.categories == {"entities_random"}:
                rep.chunks_equal_after_ignore += 1
                rep.categories["entities_random"] += 1
                continue
            rep.chunks_different += 1
            rep.diff_by_storage[storage] += 1
            rep.blocks_different += d.blocks
            rep.biome_entries_different += d.biomes
            for cat in d.categories:
                rep.categories[cat] += 1
                rep.add_example(cat, f"{where}: {d.detail.get(cat, '')}")
            if d.first_block and len(rep.block_examples) < 15:
                x, y, z, p, q = d.first_block
                rep.block_examples.append(f"/tp {x} {y} {z}  ->  {_short(p)}  gegen  {_short(q)}")
    return rep


def _short(canon: str) -> str:
    try:
        o = json.loads(canon)
        if isinstance(o, dict):
            props = o.get("Properties") or {}
            return o.get("Name", "?") + (("[" + ",".join(f"{k}={v}" for k, v in sorted(props.items())) + "]") if props else "")
        return str(o)
    except (ValueError, TypeError):
        return canon[:80]


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
