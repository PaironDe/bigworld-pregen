"""Aufteilung der Welt in Kacheln, deren Grenzen exakt auf Regionsgrenzen liegen.

Eine Region ist 512 x 512 Blöcke (32 x 32 Chunks) groß und liegt in genau einer
Datei r.<rx>.<rz>.mca. Eine Kachel besteht aus T x T Regionen. Weil Kachelgrenzen
auf Regionsgrenzen liegen, gehört jede Regionsdatei genau einer Kachel. Dadurch
können Kacheln verschiedener PCs ohne Zusammenführen einzelner Chunks
kombiniert werden: Dateien werden nur nebeneinandergelegt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

REGION_BLOCKS = 512
CHUNKS_PER_REGION = 1024

# Rand, um den Chunky über die Kachel hinaus generiert. Die zusätzlichen Chunks
# liegen in Nachbarregionen und werden verworfen. Sie stellen sicher, dass jeder
# Chunk am Kachelrand vollständig ("full") generiert ist, egal wie Chunky die
# Grenze des Rechtecks genau auslegt.
CHUNKY_MARGIN_BLOCKS = 32


def region_range(radius_blocks: int) -> tuple[int, int]:
    """Kleinster Regionsbereich (inklusive), der das Quadrat [-r, r) abdeckt."""
    if radius_blocks <= 0:
        raise ValueError("Radius muss größer als 0 sein")
    n = math.ceil(radius_blocks / REGION_BLOCKS)
    return -n, n - 1


@dataclass(frozen=True)
class Tile:
    dim: str  # z. B. "minecraft:overworld"
    tx: int
    tz: int
    rx0: int  # Regionsbereich, inklusive
    rx1: int
    rz0: int
    rz1: int

    @property
    def key(self) -> str:
        return f"{self.dim}|{self.tx}|{self.tz}"

    @property
    def x0(self) -> int:
        return self.rx0 * REGION_BLOCKS

    @property
    def x1(self) -> int:  # exklusiv
        return (self.rx1 + 1) * REGION_BLOCKS

    @property
    def z0(self) -> int:
        return self.rz0 * REGION_BLOCKS

    @property
    def z1(self) -> int:  # exklusiv
        return (self.rz1 + 1) * REGION_BLOCKS

    def regions(self) -> list[tuple[int, int]]:
        return [
            (rx, rz)
            for rz in range(self.rz0, self.rz1 + 1)
            for rx in range(self.rx0, self.rx1 + 1)
        ]

    def contains_region(self, rx: int, rz: int) -> bool:
        return self.rx0 <= rx <= self.rx1 and self.rz0 <= rz <= self.rz1

    @property
    def chunk_count(self) -> int:
        return len(self.regions()) * CHUNKS_PER_REGION

    def chunky_params(self, margin: int = CHUNKY_MARGIN_BLOCKS) -> dict:
        """Mittelpunkt und Radien für 'chunky shape rectangle'."""
        cx = (self.x0 + self.x1) // 2
        cz = (self.z0 + self.z1) // 2
        rx = (self.x1 - self.x0) // 2 + margin
        rz = (self.z1 - self.z0) // 2 + margin
        return {"center_x": cx, "center_z": cz, "radius_x": rx, "radius_z": rz}

    def distance_from_origin(self) -> float:
        cx = (self.x0 + self.x1) / 2
        cz = (self.z0 + self.z1) / 2
        return math.hypot(cx, cz)

    def to_dict(self) -> dict:
        return {
            "dim": self.dim,
            "tx": self.tx,
            "tz": self.tz,
            "rx0": self.rx0,
            "rx1": self.rx1,
            "rz0": self.rz0,
            "rz1": self.rz1,
            "x0": self.x0,
            "x1": self.x1,
            "z0": self.z0,
            "z1": self.z1,
            "chunks": self.chunk_count,
            "chunky": self.chunky_params(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Tile":
        return cls(d["dim"], d["tx"], d["tz"], d["rx0"], d["rx1"], d["rz0"], d["rz1"])


def tiles_for_dimension(dim: str, radius_blocks: int, tile_regions: int) -> list[Tile]:
    """Alle Kacheln einer Dimension; Randkacheln werden auf den Weltbereich gekürzt."""
    if tile_regions < 1:
        raise ValueError("tile_regions muss mindestens 1 sein")
    rlo, rhi = region_range(radius_blocks)
    tlo, thi = rlo // tile_regions, rhi // tile_regions
    tiles = []
    for tz in range(tlo, thi + 1):
        for tx in range(tlo, thi + 1):
            rx0 = max(tx * tile_regions, rlo)
            rx1 = min(tx * tile_regions + tile_regions - 1, rhi)
            rz0 = max(tz * tile_regions, rlo)
            rz1 = min(tz * tile_regions + tile_regions - 1, rhi)
            if rx0 > rx1 or rz0 > rz1:
                continue
            tiles.append(Tile(dim, tx, tz, rx0, rx1, rz0, rz1))
    return tiles


def tiles_touching_area(tiles: list[Tile], x0: int, z0: int, x1: int, z1: int) -> list[Tile]:
    """Kacheln, die das Blockrechteck (inklusive Grenzen) berühren."""
    ax0, ax1 = min(x0, x1), max(x0, x1)
    az0, az1 = min(z0, z1), max(z0, z1)
    return [
        t
        for t in tiles
        if t.x0 <= ax1 and ax0 < t.x1 and t.z0 <= az1 and az0 < t.z1
    ]
