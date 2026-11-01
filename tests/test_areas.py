from pathlib import Path

import pytest

from bigworld.coordinator import config
from bigworld.coordinator.state import State, StateError


def make(tmp: Path, radius: int = 0, tile_regions: int = 4) -> State:
    text = config.example_config()
    text = text.replace('data_dir = "/srv/bigworld"', f'data_dir = "{tmp / "srv"}"')
    text = text.replace("radius = 4000", f"radius = {radius}")
    text = text.replace("tile_regions = 4", f"tile_regions = {tile_regions}")
    p = tmp / "c.toml"
    p.write_text(text)
    return State(config.load(p))


def tiles(st: State, dim="minecraft:overworld"):
    return {(r["tx"], r["tz"]): r for r in st.q("SELECT * FROM tiles WHERE dim=? AND in_world=1", dim)}


def test_no_area_means_no_work(tmp_path):
    st = make(tmp_path)
    assert not tiles(st)
    w = st.register_worker("w", {})
    st.set_template("h", "x")
    r = st.acquire(w, "h", "h")
    assert r["wait"] and "Bereich" in r["reason"]


def test_area_tiles_are_clipped_to_touched_regions(tmp_path):
    st = make(tmp_path)
    st.add_area("minecraft:overworld", -1000, -1000, 999, 999)
    t = tiles(st)
    assert set(t) == {(-1, -1), (0, -1), (-1, 0), (0, 0)}
    assert (t[(-1, -1)]["rx0"], t[(-1, -1)]["rx1"]) == (-2, -1)
    assert (t[(0, 0)]["rx0"], t[(0, 0)]["rx1"]) == (0, 1)


def test_areas_processed_in_order_and_front(tmp_path):
    st = make(tmp_path)
    st.set_template("h", "x")
    st.cfg.verify_tiles = []
    st.add_area("minecraft:overworld", 0, 0, 511, 511, "erst")
    st.add_area("minecraft:the_nether", 0, 0, 511, 511, "dann")
    w = st.register_worker("w", {})
    assert st.acquire(w, "h", "h")["tile"]["dim"] == "minecraft:overworld"
    st.add_area("minecraft:the_end", 0, 0, 511, 511, "vorgezogen", front=True)
    w2 = st.register_worker("w2", {})
    assert st.acquire(w2, "h", "h")["tile"]["dim"] == "minecraft:the_end"


def test_done_tile_survives_area_delete_and_regrows(tmp_path):
    st = make(tmp_path)
    aid = st.add_area("minecraft:overworld", 0, 0, 511, 511)["id"]
    st.x("UPDATE tiles SET status='done'")
    st.delete_area(aid)
    t = tiles(st)
    assert t[(0, 0)]["status"] == "done" and t[(0, 0)]["priority"] is None
    # Größerer Bereich über die fertige Kachel: sie wird neu generiert
    st.add_area("minecraft:overworld", 0, 0, 1535, 511)
    t = tiles(st)
    assert t[(0, 0)]["status"] == "pending" and (t[(0, 0)]["rx0"], t[(0, 0)]["rx1"]) == (0, 2)
    # Kleinerer Bereich innerhalb einer fertigen Kachel ändert nichts
    st.x("UPDATE tiles SET status='done'")
    st.add_area("minecraft:overworld", 0, 0, 100, 100)
    assert tiles(st)[(0, 0)]["status"] == "done"


def test_default_areas_once_and_back_after_reset(tmp_path):
    st = make(tmp_path, radius=4000)
    assert len(st.areas()) == 3 and st.areas()[0]["label"] == "Standard ±4000"
    assert len(tiles(st)) == 16  # ±4096 Blöcke = 4 x 4 Kacheln
    st.delete_area(st.areas()[0]["id"])
    st.sync_tiles()
    assert len(st.areas()) == 2  # gelöschter Standardbereich kommt nicht von selbst zurück
    st.add_area("minecraft:overworld", 0, 0, 511, 511, "eigener")
    st.reset_tiles(None)
    assert [a["label"] for a in st.areas()] == ["Standard ±4000"] * 3


def test_area_limits(tmp_path):
    st = make(tmp_path)
    with pytest.raises(StateError):
        st.add_area("minecraft:overworld", 0, 0, 100_000, 10)
    with pytest.raises(StateError):
        st.add_area("minecraft:nope", 0, 0, 10, 10)


def test_preview_counts_only_new(tmp_path):
    st = make(tmp_path)
    st.add_area("minecraft:overworld", 0, 0, 1023, 1023)
    p = st.preview_area("minecraft:overworld", 0, 0, 2047, 1023)
    assert p["regions"] == 8 and p["new_regions"] == 4 and p["new_chunks"] == 4096


def test_init_preset_keeps_tokens_from_broken_file(tmp_path):
    from bigworld.coordinator import cli

    p = tmp_path / "c.toml"
    p.write_text('C=/etc/x\nWT=$(sudo grep ...)\nworker_token = "gyl6NKLzpOzAkiC97UYfFmjHi2NHgKVg"\nadmin_token = "$AT"\n')
    assert cli.main(["--config", str(p), "init", "--preset", "bigworld", "--force"]) == 0
    cfg = config.load(p)
    assert cfg.worker_token == "gyl6NKLzpOzAkiC97UYfFmjHi2NHgKVg"
    assert len(cfg.admin_token) >= 16 and cfg.mixed_gpus_ok
    assert [d.name for d in cfg.dimensions][-2:] == ["spacereloaded:moon", "spacereloaded:mars"]
    assert cfg.verify_tiles == [] and cfg.deploy.backup_dir == ""
    assert list(tmp_path.glob("c.toml.bak-*"))
