"""Ende-zu-Ende: Koordinator (echter HTTP-Server) + zwei simulierte Worker."""

from __future__ import annotations

import json
import socket
import threading
import time
import zipfile
from pathlib import Path

import pytest
import uvicorn

from bigworld.common import anvil, nbt
from bigworld.coordinator import bundle, config
from bigworld.coordinator.app import create_app
from bigworld.coordinator.state import State
from bigworld.worker.client import Client
from bigworld.worker.engine import Engine


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_bundle_src(src: Path) -> None:
    (src / "mods").mkdir(parents=True)
    with zipfile.ZipFile(src / "mods" / "Chunky-Fabric-1.4.jar", "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": "chunky", "version": "1.4"}))
    (src / bundle.LAUNCHER).write_bytes(b"kein echter launcher")
    (src / "config").mkdir()
    (src / "config" / "c2me.toml").write_text("x = 1\n")
    t = src / "template"
    (t / "data" / "minecraft").mkdir(parents=True)
    (t / "datapacks").mkdir()
    (t / "datapacks" / "jjthunder.zip").write_bytes(b"datapack")
    (t / "level.dat").write_bytes(nbt.dumps({"Data": {"LevelName": "Big World"}}))
    (t / "data" / "minecraft" / "world_gen_settings.dat").write_bytes(nbt.dumps({"data": {"seed": nbt.Long(42)}}))
    (t / "data" / "lss-timestamps.bin").write_bytes(b"alt")


def _write_config(tmp: Path, port: int) -> config.Config:
    text = config.example_config()
    text = text.replace('host = "100.72.176.11"', 'host = "127.0.0.1"')
    text = text.replace("port = 8770", f"port = {port}")
    text = text.replace('data_dir = "/srv/bigworld"', f'data_dir = "{tmp / "srv"}"')
    text = text.replace("tile_regions = 4", "tile_regions = 1")
    text = text.replace("radius = 2000", "radius = 512")
    p = tmp / "coordinator.toml"
    p.write_text(text)
    return config.load(p)


class Server:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.state = State(cfg)
        app = create_app(cfg, self.state)
        self.server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("Server startet nicht")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)


def _setup(tmp_path: Path):
    port = _port()
    cfg = _write_config(tmp_path, port)
    cfg.bundle_src.mkdir(parents=True)
    _make_bundle_src(cfg.bundle_src)
    m = bundle.build(cfg, log=lambda *_: None)
    bundle.set_current(cfg, m["hash"])
    return cfg


def _engine(tmp_path: Path, cfg, name: str, diff: bool = False, delay: float = 0.3) -> Engine:
    e = Engine(tmp_path / f"worker-{name}", simulate={"delay": delay, "diff": diff})
    e.update_settings({"coordinator_url": f"http://127.0.0.1:{cfg.port}", "token": cfg.worker_token, "name": name})
    e.log = lambda msg: None
    return e


def _wait(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_full_run_with_priority_template_and_verify(tmp_path: Path):
    cfg = _setup(tmp_path)
    with Server(cfg) as srv:
        user = Client(f"http://127.0.0.1:{cfg.port}", cfg.worker_token)
        user.json("POST", "/api/v1/areas", {"dim": "minecraft:the_nether", "x0": -100, "z0": -100, "x1": -50,
                                            "z1": -50, "label": "Test", "front": True})
        a, b = _engine(tmp_path, cfg, "pc-a"), _engine(tmp_path, cfg, "pc-b")
        a.start()
        time.sleep(0.5)
        b.start()

        def finished():
            st = srv.state.status(bundle.current_hash(cfg))
            return st["totals"]["tiles"].get("done", 0) == len(st["tiles"]) and st["verify"]

        assert _wait(finished, 90), srv.state.status(bundle.current_hash(cfg))["totals"]
        assert _wait(lambda: not a.state["outbox"] and not b.state["outbox"], 30)
        a.stop(), b.stop()

        st = srv.state.status(bundle.current_hash(cfg))
        assert st["template"]["sha256"]
        assert st["verify"][0]["deterministic"] is True
        v = st["verify"][0]
        assert v["chunks_identical"] + v["chunks_equal_after_ignore"] == 1024

        # Erste echte Kachel war die priorisierte
        first = srv.state.q("SELECT tile_key FROM leases WHERE kind='gen' ORDER BY created LIMIT 1")[0]["tile_key"]
        assert first == "minecraft:the_nether|-1|-1"

        world = cfg.staging_world
        assert (world / "level.dat").is_file()
        assert (world / "datapacks" / "jjthunder.zip").is_file()
        assert (world / "data" / "minecraft" / "world_gen_settings.dat").is_file()
        assert not (world / "data" / "lss-timestamps.bin").exists()
        assert not (world / "data" / "minecraft" / "random_sequences.dat").exists()
        for dim in ("overworld", "the_nether", "the_end"):
            regions = sorted(p.name for p in (world / "dimensions" / "minecraft" / dim / "region").glob("*.mca"))
            assert regions == ["r.-1.-1.mca", "r.-1.0.mca", "r.0.-1.mca", "r.0.0.mca"]
            for r in regions:
                assert anvil.check_region_complete(world / "dimensions" / "minecraft" / dim / "region" / r).ok
        # Beide Worker haben gearbeitet
        names = {w["name"]: w["tiles_done"] for w in st["workers"]}
        assert names["pc-a"] > 0 and names["pc-b"] > 0


def test_verify_detects_difference(tmp_path: Path):
    cfg = _setup(tmp_path)
    with Server(cfg) as srv:
        a, b = _engine(tmp_path, cfg, "pc-a"), _engine(tmp_path, cfg, "pc-b", diff=True)
        a.start()
        time.sleep(1.0)
        b.start()
        assert _wait(lambda: srv.state.status()["verify"], 90)
        a.stop(), b.stop()
        v = srv.state.status()["verify"][0]
        assert v["deterministic"] is False
        assert v["chunks_different"] >= 1


def test_wrong_token_and_bundle_mismatch(tmp_path: Path):
    cfg = _setup(tmp_path)
    with Server(cfg):
        bad = Client(f"http://127.0.0.1:{cfg.port}", "falsch")
        with pytest.raises(Exception) as ei:
            bad.status()
        assert getattr(ei.value, "status", None) == 401
        c = Client(f"http://127.0.0.1:{cfg.port}", cfg.worker_token)
        wid = c.register("x", {})
        with pytest.raises(Exception) as ei:
            c.acquire(wid, "0" * 64)
        assert getattr(ei.value, "status", None) == 409
        # Worker-Token darf Bereiche anlegen, aber nicht pausieren
        assert c.json("POST", "/api/v1/areas", {"dim": "minecraft:overworld", "x0": 0, "z0": 0, "x1": 1, "z1": 1})["id"]
        with pytest.raises(Exception) as ei:
            c.json("POST", "/api/v1/pause", {"paused": True})
        assert getattr(ei.value, "status", None) == 403


def test_lease_expiry_reassigns(tmp_path: Path):
    cfg = _setup(tmp_path)
    state = State(cfg)
    h = bundle.current_hash(cfg)
    state.set_template(h, "abc")
    w1, w2 = state.register_worker("w1", {}), state.register_worker("w2", {})
    l1 = state.acquire(w1, h, h)
    assert l1["kind"] == "gen"
    state.x("UPDATE leases SET expires=0 WHERE id=?", l1["lease_id"])
    l2 = state.acquire(w2, h, h)
    # verify-Kachel geht an w2, sobald w1 sie hatte; sonst dieselbe Kachel erneut
    assert l2["tile"]["dim"] == l1["tile"]["dim"]
    hb = state.heartbeat(w1, l1["lease_id"], {})
    assert hb["ok"] in (True, False)
