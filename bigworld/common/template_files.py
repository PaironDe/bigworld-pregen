"""Welche Dateien zur Vorlage-Welt gehören (Worker packt, Koordinator prüft)."""

from __future__ import annotations

from pathlib import Path

from .hashing import safe_relpath

# Laufzeitdaten, die eine neue Welt selbst anlegt und die nicht in die Vorlage gehören
RUNTIME_DATA = {
    "lss-timestamps.bin",
    "minecraft/random_sequences.dat",
    "minecraft/weather.dat",
    "minecraft/wandering_trader.dat",
    "minecraft/world_clocks.dat",
    "minecraft/scheduled_events.dat",
    "minecraft/stopwatches.dat",
    "minecraft/custom_boss_events.dat",
    "minecraft/scoreboard.dat",
    "minecraft/raids.dat",
    "minecraft/chunks.dat",
}
REQUIRED = ["level.dat", "data/minecraft/world_gen_settings.dat"]


def member_allowed(rel: str) -> bool:
    if not safe_relpath(rel):
        return False
    if rel == "level.dat":
        return True
    if rel.startswith("datapacks/"):
        return True
    if rel.startswith("data/"):
        rest = rel[len("data/"):]
        return rest not in RUNTIME_DATA and not rest.endswith(".dat_old") and not rest.endswith(".tmp")
    return False


def collect(world: Path) -> list[tuple[str, Path]]:
    out = []
    for p in sorted(world.rglob("*")):
        if p.is_file():
            rel = p.relative_to(world).as_posix()
            if member_allowed(rel):
                out.append((rel, p))
    return out
