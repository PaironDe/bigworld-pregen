import math
from pathlib import Path

from bigworld.common import anvil, nbt, package
from bigworld.common.compare import compare_packages
from bigworld.common.tiles import Tile


def pack(indices, bits):
    per = 64 // bits
    longs = [0] * math.ceil(len(indices) / per)
    for i, v in enumerate(indices):
        longs[i // per] |= v << ((i % per) * bits)
    return nbt.LongArray(longs)


def chunk(blocks, palette_order=None, extra=None, biome="minecraft:plains"):
    """blocks: Liste von 4096 Blocknamen für Abschnitt Y=0."""
    names = palette_order or sorted(set(blocks))
    bits = max(4, math.ceil(math.log2(len(names)))) if len(names) > 1 else 4
    bs = {"palette": [{"Name": n} for n in names]}
    if len(names) > 1:
        bs["data"] = pack([names.index(b) for b in blocks], bits)
    c = {"Status": "minecraft:full", "LastUpdate": nbt.Long(1),
         "sections": [{"Y": 0, "block_states": bs, "biomes": {"palette": [biome]}}]}
    if extra:
        c.update(extra)
    return nbt.dumps(c)


def build(tmp: Path, name: str, chunks: dict, entities: dict | None = None) -> Path:
    world = tmp / name
    (world / "region").mkdir(parents=True)
    anvil.write_region(world / "region" / "r.0.0.mca", chunks)
    if entities:
        (world / "entities").mkdir()
        anvil.write_region(world / "entities" / "r.0.0.mca", entities)
    tar = tmp / f"{name}.tar"
    package.write_package(tar, {}, package.collect_tile_files(world, "", Tile("o", 0, 0, 0, 0, 0, 0)))
    return tar


BLOCKS = ["minecraft:stone"] * 2000 + ["minecraft:dirt"] * 2000 + ["minecraft:tin_ore"] * 96


def test_palette_order_does_not_matter(tmp_path):
    a = build(tmp_path, "a", {0: chunk(BLOCKS, ["minecraft:stone", "minecraft:dirt", "minecraft:tin_ore"])})
    b = build(tmp_path, "b", {0: chunk(BLOCKS, ["minecraft:tin_ore", "minecraft:stone", "minecraft:dirt"])})
    rep = compare_packages(a, b).to_dict()
    assert rep["deterministic"] and rep["blocks_different"] == 0


def test_real_block_difference_with_coordinates(tmp_path):
    other = list(BLOCKS)
    other[5] = "minecraft:diamond_ore"
    a = build(tmp_path, "a", {33: chunk(BLOCKS)})
    b = build(tmp_path, "b", {33: chunk(other)})
    rep = compare_packages(a, b).to_dict()
    assert not rep["deterministic"]
    assert rep["blocks_different"] == 1
    assert rep["block_examples"][0].startswith("/tp 21 0 16")  # Chunk 33 = cx 1, cz 1; Index 5 = x 5
    assert "diamond_ore" in rep["block_examples"][0]


def test_animal_random_values_ignored_but_reported(tmp_path):
    def ent(egg):
        return nbt.dumps({"Entities": [{"id": "minecraft:chicken", "Pos": [1.5, 64.0, 2.5], "EggLayTime": egg,
                                        "UUID": nbt.IntArray([egg, 1, 2, 3])}]})
    a = build(tmp_path, "a", {0: chunk(BLOCKS)}, {0: ent(100)})
    b = build(tmp_path, "b", {0: chunk(BLOCKS)}, {0: ent(9000)})
    rep = compare_packages(a, b).to_dict()
    assert rep["deterministic"] and rep["minor"].get("entities_random") == 1


def test_light_only_is_minor(tmp_path):
    a = build(tmp_path, "a", {0: chunk(BLOCKS, extra={"isLightOn": 1})})
    b = build(tmp_path, "b", {0: chunk(BLOCKS, extra={"isLightOn": 0})})
    rep = compare_packages(a, b).to_dict()
    assert rep["deterministic"] and rep["minor"].get("light") == 1


def test_biome_difference_is_critical(tmp_path):
    a = build(tmp_path, "a", {0: chunk(BLOCKS)})
    b = build(tmp_path, "b", {0: chunk(BLOCKS, biome="minecraft:desert")})
    rep = compare_packages(a, b).to_dict()
    assert not rep["deterministic"] and rep["critical"]["biomes"] == 1
