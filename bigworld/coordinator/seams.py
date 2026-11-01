"""Nähte zwischen Kacheln in der Staging-Welt vermessen.

Für jede Grenze zwischen zwei fertigen Kacheln wird die Oberflächenhöhe (Heightmap
OCEAN_FLOOR, ohne Wasser) direkt links/rechts der Grenze verglichen und mit dem Sprung
eine Spalte weiter innen verglichen (natürliches Gelände). Ein großer Unterschied
"an der Naht" gegenüber "daneben" ist eine sichtbare Stufe.

Ausgewertet wird pro 16-Block-Abschnitt (Chunkbreite) der Median, damit einzelne Bäume
oder Klippen nicht als Naht zählen.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from ..common import anvil, nbt
from ..common.tiles import REGION_BLOCKS, Tile

HEIGHTMAP = "OCEAN_FLOOR"


def _bits_for(n_longs: int) -> int | None:
    for bits in range(1, 33):
        per = 64 // bits
        if -(-256 // per) == n_longs:
            return bits
    return None


class Surface:
    """Liest Oberflächenhöhen aus den Regionsdateien einer Dimension (mit Cache)."""

    def __init__(self, region_dir: Path):
        self.dir = region_dir
        self.regions: dict[tuple[int, int], anvil.RegionFile | None] = {}
        self.chunks: dict[tuple[int, int], tuple[list[int], int] | None] = {}

    def _chunk(self, cx: int, cz: int):
        key = (cx, cz)
        if key in self.chunks:
            return self.chunks[key]
        rk = (cx >> 5, cz >> 5)
        if rk not in self.regions:
            p = self.dir / f"r.{rk[0]}.{rk[1]}.mca"
            self.regions[rk] = anvil.RegionFile(p) if p.is_file() else None
        reg = self.regions[rk]
        result = None
        if reg is not None:
            data = reg.chunk_bytes(anvil.chunk_index(cx, cz))
            if data:
                hm = nbt.read_root_field(data, "Heightmaps") or {}
                raw = hm.get(HEIGHTMAP) or hm.get("WORLD_SURFACE")
                ypos = nbt.read_root_field(data, "yPos")
                if raw and isinstance(raw, tuple):
                    import struct

                    longs = [x & 0xFFFFFFFFFFFFFFFF for x in struct.unpack(f">{len(raw[1]) // 8}q", raw[1])]
                    bits = _bits_for(len(longs))
                    if bits:
                        per, mask = 64 // bits, (1 << bits) - 1
                        vals = [(longs[i // per] >> ((i % per) * bits)) & mask for i in range(256)]
                        result = (vals, (ypos if isinstance(ypos, int) else -4) * 16)
        self.chunks[key] = result
        return result

    def height(self, x: int, z: int) -> int | None:
        c = self._chunk(x >> 4, z >> 4)
        if c is None:
            return None
        vals, min_y = c
        return vals[(x & 15) + (z & 15) * 16] + min_y


@dataclass
class Segment:
    x: int
    z: int
    y: int
    seam_jump: float
    inner_jump: float

    @property
    def excess(self) -> float:
        return self.seam_jump - self.inner_jump


@dataclass
class SeamResult:
    tile_a: str
    tile_b: str
    worker_a: str
    worker_b: str
    axis: str  # "x" = Grenze bei konstantem x, "z" = bei konstantem z
    at: int
    segments: list[Segment] = field(default_factory=list)

    @property
    def mixed(self) -> bool:
        return self.worker_a != self.worker_b


def measure_seam(surface: Surface, a: Tile, b: Tile, axis: str) -> list[Segment]:
    """a liegt links/oben von b. Liefert einen Eintrag pro 16 Blöcke Nahtlänge."""
    segs = []
    if axis == "x":  # senkrechte Grenze: a.x1 == b.x0
        x_edge = b.x0
        lo, hi = max(a.z0, b.z0), min(a.z1, b.z1)
        for z0 in range(lo, hi, 16):
            seam, inner, ys = [], [], []
            for z in range(z0, min(z0 + 16, hi)):
                h = [surface.height(x_edge + d, z) for d in (-2, -1, 0, 1)]
                if None in h:
                    continue
                seam.append(abs(h[2] - h[1]))
                inner.append((abs(h[1] - h[0]) + abs(h[3] - h[2])) / 2)
                ys.append(max(h))
            if seam:
                segs.append(Segment(x_edge, z0 + 8, max(ys) + 2, statistics.median(seam), statistics.median(inner)))
    else:  # waagrechte Grenze: a.z1 == b.z0
        z_edge = b.z0
        lo, hi = max(a.x0, b.x0), min(a.x1, b.x1)
        for x0 in range(lo, hi, 16):
            seam, inner, ys = [], [], []
            for x in range(x0, min(x0 + 16, hi)):
                h = [surface.height(x, z_edge + d) for d in (-2, -1, 0, 1)]
                if None in h:
                    continue
                seam.append(abs(h[2] - h[1]))
                inner.append((abs(h[1] - h[0]) + abs(h[3] - h[2])) / 2)
                ys.append(max(h))
            if seam:
                segs.append(Segment(x0 + 8, z_edge, max(ys) + 2, statistics.median(seam), statistics.median(inner)))
    return segs


def analyze(world: Path, dim_path: str, tiles: list[tuple[Tile, str]]) -> list[SeamResult]:
    """tiles: fertige Kacheln einer Dimension mit dem Namen des Workers, der sie generiert hat."""
    surface = Surface(world / dim_path / "region")
    by_pos = {(t.tx, t.tz): (t, w) for t, w in tiles}
    results = []
    for (tx, tz), (t, w) in sorted(by_pos.items()):
        for axis, nb in (("x", (tx + 1, tz)), ("z", (tx, tz + 1))):
            if nb not in by_pos:
                continue
            u, wu = by_pos[nb]
            r = SeamResult(t.key, u.key, w, wu, axis, u.x0 if axis == "x" else u.z0)
            r.segments = measure_seam(surface, t, u, axis)
            results.append(r)
    return results


def summarize(results: list[SeamResult], top: int = 15) -> str:
    lines = []
    for label, group in (("Gemischte Nähte (verschiedene Rechner)", [r for r in results if r.mixed]),
                         ("Kontrolle: Nähte zwischen Kacheln desselben Rechners", [r for r in results if not r.mixed])):
        lines.append(f"\n== {label} ==")
        if not group:
            lines.append("  keine")
            continue
        for r in group:
            segs = r.segments
            if not segs:
                lines.append(f"  {r.axis}={r.at}: keine Daten")
                continue
            ex = [s.excess for s in segs]
            bad = sum(1 for e in ex if e >= 3)
            lines.append(
                f"  Grenze {r.axis} = {r.at:6d}  ({r.worker_a} | {r.worker_b}):  "
                f"{len(segs)} Abschnitte, Stufe an der Naht im Mittel {statistics.mean(s.seam_jump for s in segs):.1f} "
                f"gegenüber {statistics.mean(s.inner_jump for s in segs):.1f} daneben, "
                f"{bad} Abschnitte mit >= 3 Blöcken Extra-Stufe, größte {max(ex):.0f}"
            )
    mixed = [s for r in results if r.mixed for s in r.segments]
    if mixed:
        lines.append(f"\n== Die {top} auffälligsten Stellen an gemischten Nähten (zum Ansehen) ==")
        for s in sorted(mixed, key=lambda s: s.excess, reverse=True)[:top]:
            lines.append(f"  /tp {s.x} {s.y} {s.z}    Stufe {s.seam_jump:.0f} Blöcke an der Naht, daneben {s.inner_jump:.0f}")
    return "\n".join(lines)


__all__ = ["analyze", "summarize", "REGION_BLOCKS"]
