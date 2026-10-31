from pathlib import Path


from bigworld.common import anvil, nbt, package
from bigworld.common.compare import compare_packages
from bigworld.common.tiles import Tile, region_range, tiles_for_dimension, tiles_touching_area


def test_region_range():
    assert region_range(2000) == (-4, 3)
    assert region_range(15360) == (-30, 29)
    assert region_range(512) == (-1, 0)


def test_tiles_cover_every_region_exactly_once():
    for radius, t in [(2000, 4), (15360, 4), (15360, 8), (3000, 3)]:
        tiles = tiles_for_dimension("minecraft:overworld", radius, t)
        lo, hi = region_range(radius)
        seen = [r for tile in tiles for r in tile.regions()]
        assert len(seen) == len(set(seen))
        assert set(seen) == {(x, z) for x in range(lo, hi + 1) for z in range(lo, hi + 1)}


def test_tile_counts():
    assert len(tiles_for_dimension("o", 15360, 4)) == 256  # 60 Regionen je Achse -> 14 volle + 2 halbe Kacheln
    assert len(tiles_for_dimension("o", 2000, 4)) == 4


def test_chunky_params_cover_tile():
    tile = Tile("o", 0, 0, 0, 3, 0, 3)
    p = tile.chunky_params()
    assert p["center_x"] == 1024 and p["radius_x"] == 1024 + 32


def test_tiles_touching_area():
    tiles = tiles_for_dimension("o", 2000, 4)
    hit = tiles_touching_area(tiles, -10, -10, 10, 10)
    assert {(t.tx, t.tz) for t in hit} == {(-1, -1), (0, -1), (-1, 0), (0, 0)}
    hit = tiles_touching_area(tiles, 100, 100, 200, 200)
    assert {(t.tx, t.tz) for t in hit} == {(0, 0)}


def test_nbt_roundtrip_and_fast_field():
    root = {"DataVersion": 5000, "Status": "minecraft:full", "sections": [{"Y": 1}], "x": nbt.Long(5)}
    data = nbt.dumps(root)
    assert nbt.parse(data)["Status"] == "minecraft:full"
    assert nbt.read_root_field(data, "Status") == "minecraft:full"
    assert nbt.read_root_field(data, "Missing") is None


def _chunk(status="minecraft:full", extra=0):
    return nbt.dumps({"Status": status, "LastUpdate": nbt.Long(extra), "Heights": nbt.IntArray([1, 2, 3])})


def test_region_check(tmp_path: Path):
    p = tmp_path / "r.0.0.mca"
    anvil.write_region(p, {i: _chunk() for i in range(1024)})
    assert anvil.check_region_complete(p).ok
    anvil.write_region(p, {i: _chunk("minecraft:features" if i == 5 else "minecraft:full") for i in range(1023)})
    res = anvil.check_region_complete(p)
    assert not res.ok and res.missing == [1023] and res.not_full[0][0] == 5


def test_package_validation_and_compare(tmp_path: Path):
    tile = Tile("minecraft:overworld", 0, 0, 0, 0, 0, 0)
    dim = "dimensions/minecraft/overworld"
    for name, extra in [("a", 1), ("b", 2)]:
        world = tmp_path / name / "world"
        (world / dim / "region").mkdir(parents=True)
        anvil.write_region(world / dim / "region" / "r.0.0.mca", {i: _chunk(extra=extra) for i in range(1024)})
        anvil.write_region(world / dim / "region" / "r.1.0.mca", {0: _chunk()})  # Nachbar, darf nicht mit
        files = package.collect_tile_files(world, dim, tile)
        assert [f[0] for f in files] == [f"{dim}/region/r.0.0.mca"]
        package.write_package(tmp_path / f"{name}.tar", {"tile": tile.to_dict()}, files)
        package.validate_package(tmp_path / f"{name}.tar", tile, dim)

    rep = compare_packages(tmp_path / "a.tar", tmp_path / "b.tar")
    assert rep.deterministic and rep.chunks_equal_after_ignore == 1024

    assert not package.member_allowed(f"{dim}/region/r.1.0.mca", tile, dim)
    assert not package.member_allowed(f"{dim}/region/../r.0.0.mca", tile, dim)
    assert package.member_allowed(f"{dim}/region/c.31.31.mcc", tile, dim)
    assert not package.member_allowed(f"{dim}/region/c.32.0.mcc", tile, dim)
    assert not package.member_allowed("level.dat", tile, dim)


def test_compare_detects_difference(tmp_path: Path):
    tile = Tile("o", 0, 0, 0, 0, 0, 0)
    for name, status in [("a", "minecraft:full"), ("b", "minecraft:full")]:
        world = tmp_path / name
        (world / "region").mkdir(parents=True)
        chunks = {i: _chunk(status) for i in range(10)}
        if name == "b":
            chunks[3] = nbt.dumps({"Status": "minecraft:full", "LastUpdate": nbt.Long(0), "Heights": nbt.IntArray([9])})
        anvil.write_region(world / "region" / "r.0.0.mca", chunks)
        package.write_package(tmp_path / f"{name}.tar", {}, package.collect_tile_files(world, "", tile))
    rep = compare_packages(tmp_path / "a.tar", tmp_path / "b.tar")
    assert not rep.deterministic and rep.chunks_different == 1
    assert "Heights" in rep.examples[0]
