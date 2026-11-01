import math

from bigworld.common import anvil, nbt
from bigworld.common.tiles import Tile
from bigworld.coordinator import seams


def heightmap(values, bits=9):
    per = 64 // bits
    longs = [0] * math.ceil(256 / per)
    for i, v in enumerate(values):
        longs[i // per] |= v << ((i % per) * bits)
    return nbt.LongArray(longs)


def test_seam_step_detected(tmp_path):
    # Zwei 1x1-Regionen-Kacheln nebeneinander (x 0..511 und 512..1023), rechte Kachel 10 Blöcke höher
    region_dir = tmp_path / "region"
    region_dir.mkdir()
    for rx, h in ((0, 70), (1, 80)):
        chunks = {}
        for i in range(1024):
            chunks[i] = nbt.dumps({"yPos": -4, "Heightmaps": {"OCEAN_FLOOR": heightmap([h + 64] * 256)}})
        anvil.write_region(region_dir / f"r.{rx}.0.mca", chunks)
    a, b = Tile("o", 0, 0, 0, 0, 0, 0), Tile("o", 1, 0, 1, 1, 0, 0)
    res = seams.analyze(tmp_path, "", [(a, "pc"), (b, "server")])
    assert len(res) == 1 and res[0].mixed
    segs = res[0].segments
    assert len(segs) == 32 and all(s.seam_jump == 10 and s.inner_jump == 0 for s in segs)
    assert segs[0].x == 512 and segs[0].y == 82
    text = seams.summarize(res)
    assert "/tp 512 82 8" in text


def test_tall_world_bit_width_ambiguity():
    # 12 Bit (JJThunder-Höhen) und 11 Bit ergeben beide 52 Longs; Dekodierung muss 12 wählen
    import random

    random.seed(3)
    vals = [1500 + (i % 16) + (i // 16) + random.randint(0, 2) for i in range(256)]
    longs = list(heightmap(vals, bits=12))
    assert len(longs) == 52
    assert seams._decode_heightmap([x & 0xFFFFFFFFFFFFFFFF for x in longs]) == vals
