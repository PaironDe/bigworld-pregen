"""Simulierter Generator für Tests ohne Minecraft.

Schreibt deterministische Regionsdateien, deren Inhalt nur vom Seed und den
Chunk-Koordinaten abhängt. Mit diff=True weicht ein Chunk pro Kachel ab, um den
Determinismus-Vergleich zu testen.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from ..common import anvil, nbt
from ..common.tiles import Tile
from .server import Aborted


class FakeGenerator:
    def __init__(self, world_dir: Path, seed: int = 12345, delay: float = 0.0, diff: bool = False, log=print):
        self.world = world_dir
        self.seed = seed
        self.delay = delay
        self.diff = diff
        self.log = log

    def shutdown(self) -> None:
        pass

    def make_template(self, abort: threading.Event) -> None:
        self.world.mkdir(parents=True, exist_ok=True)
        (self.world / "data" / "minecraft").mkdir(parents=True, exist_ok=True)
        if not (self.world / "level.dat").exists():
            (self.world / "level.dat").write_bytes(nbt.dumps({"Data": {"LevelName": "world"}}))
        (self.world / "data" / "minecraft" / "random_sequences.dat").write_bytes(b"laufzeit")
        if not (self.world / "data" / "minecraft" / "world_gen_settings.dat").exists():
            (self.world / "data" / "minecraft" / "world_gen_settings.dat").write_bytes(
                nbt.dumps({"data": {"seed": nbt.Long(self.seed)}}))

    def _chunk(self, cx: int, cz: int) -> bytes:
        h = hashlib.sha256(f"{self.seed}:{cx}:{cz}".encode()).digest()
        heights = nbt.IntArray([b for b in h[:16]])
        block = "minecraft:stone" if h[0] % 2 else "minecraft:dirt"
        if self.diff and cx % 32 == 7 and cz % 32 == 7:
            block = "minecraft:diamond_ore"
        return nbt.dumps({
            "Status": "minecraft:full", "xPos": cx, "zPos": cz,
            "LastUpdate": nbt.Long(int(time.time())), "InhabitedTime": nbt.Long(0),
            "Heights": heights,
            "sections": [{"Y": 0, "block_states": {"palette": [{"Name": block}]},
                          "biomes": {"palette": ["minecraft:plains"]}}],
        })

    def generate(self, tile: Tile, dim_path: str, abort: threading.Event, progress) -> None:
        region_dir = self.world / dim_path / "region" if dim_path else self.world / "region"
        region_dir.mkdir(parents=True, exist_ok=True)
        regions = tile.regions()
        # wie Chunky: auch Nachbarregionen bekommen ein paar (unfertige) Chunks
        rx_n, rz_n = tile.rx1 + 1, tile.rz0
        anvil.write_region(region_dir / f"r.{rx_n}.{rz_n}.mca",
                           {0: nbt.dumps({"Status": "minecraft:features"})})
        total = len(regions) * 1024
        done = 0
        for rx, rz in regions:
            if abort.is_set():
                raise Aborted()
            chunks = {}
            for i in range(1024):
                cx, cz = rx * 32 + i % 32, rz * 32 + i // 32
                chunks[i] = self._chunk(cx, cz)
            anvil.write_region(region_dir / f"r.{rx}.{rz}.mca", chunks)
            done += 1024
            progress(chunks=done, percent=100.0 * done / total, eta=None, cps=1024 / max(self.delay, 0.001))
            if self.delay:
                time.sleep(self.delay)
