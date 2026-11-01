"""Live-Merge, Teil-Reset und Stichproben."""

from pathlib import Path

from bigworld.common import anvil, nbt
from bigworld.common.tiles import Tile
from bigworld.coordinator import config, live
from bigworld.coordinator.state import State


def chunk(status="minecraft:full", tag=0) -> bytes:
    return nbt.dumps({"Status": status, "LastUpdate": nbt.Long(tag), "Heights": nbt.IntArray([tag])})


def tag_of(region: anvil.RegionFile, i: int) -> int:
    return nbt.parse(region.chunk_bytes(i))["LastUpdate"]


def make_state(tmp: Path, preset="standard") -> State:
    text = config.example_config(preset)
    text = text.replace('data_dir = "/srv/bigworld"', f'data_dir = "{tmp / "srv"}"')
    p = tmp / "c.toml"
    p.write_text(text)
    return State(config.load(p))


def test_blob_roundtrip_keeps_chunks_and_timestamps(tmp_path):
    p = tmp_path / "r.0.0.mca"
    anvil.write_region(p, {i: chunk(tag=i) for i in (0, 5, 1023)}, timestamp=1234)
    r = anvil.RegionFile(p)
    entries = {i: (r.blob(i), r.timestamp(i)) for i in range(1024) if r.blob(i)}
    out = tmp_path / "copy" / "r.0.0.mca"
    out.parent.mkdir()
    anvil.write_region_blobs(out, entries)
    c = anvil.RegionFile(out)
    assert [i for i in range(1024) if c.has_chunk(i)] == [0, 5, 1023]
    assert tag_of(c, 5) == 5 and c.timestamp(1023) == 1234


def test_new_region_is_copied_completely(tmp_path):
    src, dst = tmp_path / "staging" / "dim", tmp_path / "live" / "dim"
    for storage in ("region", "entities", "poi"):
        (src / storage).mkdir(parents=True)
        anvil.write_region(src / storage / "r.0.0.mca", {i: chunk(tag=7) for i in range(1024)})
    stats = live.MergeStats()
    live.merge_region_set(src, dst, 0, 0, stats)
    assert stats.regions_new == 1 and stats.chunks_added == 1024
    for storage in ("region", "entities", "poi"):
        assert (dst / storage / "r.0.0.mca").read_bytes() == (src / storage / "r.0.0.mca").read_bytes()


def test_existing_full_chunks_are_never_overwritten(tmp_path):
    src, dst = tmp_path / "staging" / "dim", tmp_path / "live" / "dim"
    (src / "region").mkdir(parents=True)
    (src / "entities").mkdir(parents=True)
    (dst / "region").mkdir(parents=True)
    (dst / "entities").mkdir(parents=True)
    # Staging: alle Chunks fertig (Markierung 1)
    anvil.write_region(src / "region" / "r.0.0.mca", {i: chunk(tag=1) for i in range(1024)})
    anvil.write_region(src / "entities" / "r.0.0.mca", {i: chunk(tag=1) for i in range(1024)})
    # Live: Chunk 0 bebaut (fertig, Markierung 99), Chunk 1 unfertig, Rest fehlt
    anvil.write_region(dst / "region" / "r.0.0.mca", {0: chunk(tag=99), 1: chunk("minecraft:features", 50)})
    anvil.write_region(dst / "entities" / "r.0.0.mca", {0: chunk(tag=99)})
    stats = live.MergeStats()
    live.merge_region_set(src, dst, 0, 0, stats)
    r = anvil.RegionFile(dst / "region" / "r.0.0.mca")
    e = anvil.RegionFile(dst / "entities" / "r.0.0.mca")
    assert tag_of(r, 0) == 99 and tag_of(e, 0) == 99     # Bebautes bleibt
    assert tag_of(r, 1) == 1                             # unfertiger Chunk ersetzt
    assert all(r.has_chunk(i) for i in range(1024))
    assert tag_of(e, 500) == 1
    assert stats.chunks_added == 1023 and stats.chunks_kept == 1
    assert not list(dst.rglob("*.bwtmp"))


def test_merge_tile_and_cleanup(tmp_path):
    staging, world = tmp_path / "staging", tmp_path / "world"
    d = staging / "dimensions/minecraft/overworld/region"
    d.mkdir(parents=True)
    for rx in (0, 1):
        anvil.write_region(d / f"r.{rx}.0.mca", {i: chunk() for i in range(1024)})
    tile = Tile("minecraft:overworld", 0, 0, 0, 1, 0, 0)
    stats = live.MergeStats()
    live.merge_tile(staging, world, "dimensions/minecraft/overworld", tile, stats)
    assert stats.regions_new == 2
    live.remove_tile_from_staging(staging, "dimensions/minecraft/overworld", tile)
    assert not list(d.glob("*.mca"))
    assert len(list((world / "dimensions/minecraft/overworld/region").glob("*.mca"))) == 2


def test_reset_dims_keeps_other_dimensions_and_areas(tmp_path):
    st = make_state(tmp_path)
    keys = {r["dim"]: r["key"] for r in st.q("SELECT key, dim FROM tiles")}
    st.x("UPDATE tiles SET status='done', bundle_hash='old', done_by='w1', live_at=1")
    n_areas = len(st.areas())
    n = st.reset_dims(["minecraft:overworld"])
    assert n > 0 and len(st.areas()) == n_areas
    rows = {r["key"]: r for r in st.q("SELECT * FROM tiles")}
    assert rows[keys["minecraft:overworld"]]["status"] == "pending"
    assert rows[keys["minecraft:overworld"]]["live_at"] is None
    assert rows[keys["minecraft:the_nether"]]["status"] == "done"
    nether = sum(1 for r in rows.values() if r["dim"] == "minecraft:the_nether")
    assert st.done_by_dim()["minecraft:the_nether"] == {"old": nether}
    assert "minecraft:overworld" not in st.done_by_dim()


def test_recheck_only_goes_to_original_worker(tmp_path):
    st = make_state(tmp_path)
    w1 = st.register_worker("server", {})
    w2 = st.register_worker("pc", {})
    st.set_template("h2", "x")
    key = st.q("SELECT key FROM tiles WHERE dim='minecraft:the_nether' LIMIT 1")[0]["key"]
    st.x("UPDATE tiles SET status='done', bundle_hash='old', done_by=? WHERE dim='minecraft:the_nether'", w1)
    st.add_recheck(key, w1)
    st.x("INSERT INTO verify_copies(tile_key, lease_id, worker_id, path, created) VALUES(?,?,?,?,?)",
         key, "staging", "staging", "/x", 0)
    r2 = st.acquire(w2, "h2", "h2")
    assert r2.get("kind") != "verify"
    r1 = st.acquire(w1, "h2", "h2")
    assert r1["kind"] == "verify" and r1["tile"]["dim"] == "minecraft:the_nether"


def test_config_bigworld_has_ad_astra_and_live_merge(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(config.example_config("bigworld"))
    cfg = config.load(p)
    names = {d.name for d in cfg.dimensions}
    assert {"ad_astra:moon", "ad_astra:mars", "ad_astra:venus", "ad_astra:mercury", "ad_astra:glacio"} <= names
    assert cfg.live_merge.enabled and cfg.live_merge.announce_minutes == 5
    p.write_text(config.example_config("standard"))
    assert not config.load(p).live_merge.enabled


def test_world_seed_and_marker(tmp_path):
    import gzip

    w = tmp_path / "world"
    (w / "data/minecraft").mkdir(parents=True)
    (w / "data/minecraft/world_gen_settings.dat").write_bytes(
        gzip.compress(nbt.dumps({"data": {"seed": nbt.Long(2661563763838815197)}})))
    assert live.world_seed(w) == 2661563763838815197
    live.write_marker(w, "abc")
    assert live.read_marker(w)["seed"] == 2661563763838815197


def test_recheck_compares_staging_against_new_copy(tmp_path):
    from bigworld.common import package
    from bigworld.coordinator import merge

    st = make_state(tmp_path)
    cfg = st.cfg
    w1 = st.register_worker("server", {})
    row = st.q("SELECT * FROM tiles WHERE dim='minecraft:the_nether' ORDER BY order_key LIMIT 1")[0]
    tile = st.tile(row["key"])
    st.x("UPDATE tiles SET status='done', bundle_hash='old', done_by=? WHERE key=?", w1, tile.key)
    dim_path = cfg.dimension(tile.dim).path
    reg = cfg.staging_world / dim_path / "region"
    reg.mkdir(parents=True)
    for rx, rz in tile.regions():
        anvil.write_region(reg / f"r.{rx}.{rz}.mca", {i: chunk() for i in range(4)})
    merge.create_recheck(cfg, st, tile.key, w1)
    # Der Worker liefert dieselbe Kachel noch einmal
    st.set_template("h2", "x")
    lease = st.acquire(w1, "h2", "h2")
    assert lease["kind"] == "verify"
    tar = tmp_path / "new.tar"
    package.write_package(tar, {"tile": tile.to_dict()}, package.collect_tile_files(cfg.staging_world, dim_path, tile))
    lrow = st.lease(lease["lease_id"])
    merge.keep_verify_copy(cfg, st, lrow, tar)
    import time
    for _ in range(50):
        res = st.q("SELECT result FROM verify_results WHERE tile_key=?", tile.key)
        if res:
            break
        time.sleep(0.1)
    import json
    rep = json.loads(res[0]["result"])
    assert rep["deterministic"] and rep["worker_a"].startswith("Bestand")


def test_unreadable_live_chunk_is_kept(tmp_path):
    import struct
    src, dst = tmp_path / "staging" / "dim", tmp_path / "live" / "dim"
    (src / "region").mkdir(parents=True)
    (dst / "region").mkdir(parents=True)
    anvil.write_region(src / "region" / "r.0.0.mca", {i: chunk(tag=1) for i in range(2)})
    live_file = dst / "region" / "r.0.0.mca"
    anvil.write_region(live_file, {0: chunk(tag=99)})
    data = bytearray(live_file.read_bytes())
    data[2 * 4096 + 4] = anvil.COMP_LZ4  # wie ein LZ4-komprimierter Chunk
    live_file.write_bytes(bytes(data))
    live.merge_region_set(src, dst, 0, 0, live.MergeStats())
    r = anvil.RegionFile(live_file)
    assert r.blob(0)[4] == anvil.COMP_LZ4   # unangetastet
    assert tag_of(r, 1) == 1                # fehlender Chunk ergänzt


def test_merge_waits_while_deploy_holds_staging(tmp_path):
    import threading
    import time as _t
    from bigworld.coordinator import merge

    st = make_state(tmp_path)
    cfg = st.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    done = threading.Event()
    with merge.staging_locked(cfg, logf=lambda *_: None):
        t = threading.Thread(target=lambda: (merge._enter_merge(cfg, "x").unlink(), done.set()))
        t.start()
        _t.sleep(0.5)
        assert not done.is_set()          # wartet, solange der Deploy die Sperre hält
    t.join(10)
    assert done.is_set()


def test_worker_pause_only_affects_that_worker(tmp_path):
    st = make_state(tmp_path)
    a = st.register_worker("server", {})
    b = st.register_worker("pc", {})
    st.set_template("h", "x")
    st.set_worker_paused("server", True)
    ra = st.acquire(a, "h", "h")
    assert ra["wait"] and "pausiert" in ra["reason"]
    assert st.acquire(b, "h", "h").get("kind") == "gen"
    st.set_worker_paused("server", False)
    assert st.acquire(a, "h", "h").get("kind") == "gen"
    assert any(w["paused"] is False for w in st.status()["workers"])


def test_button_request_bypasses_nightly_switch_but_not_safety(tmp_path, monkeypatch):
    import pytest
    st = make_state(tmp_path)          # Vorlage "standard": live_merge.enabled = false
    cfg = st.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    assert live.run(cfg, st, scheduled=True, log=lambda *_: None) == 0     # ohne Knopf: nichts
    assert live.read_status(cfg)["state"] == "fertig"
    live.request_merge(cfg, 0, "test")
    assert live.read_status(cfg)["requested"]
    monkeypatch.setattr(live.os, "geteuid", lambda: 1000)
    with pytest.raises(live.LiveMergeError, match="sudo"):
        live.run(cfg, st, scheduled=True, log=lambda *_: None)             # mit Knopf: weiter bis zur Prüfung
    s = live.read_status(cfg)
    assert not s["requested"] and s["state"] == "fehler"
