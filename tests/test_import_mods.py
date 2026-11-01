import json
import zipfile
from pathlib import Path

from bigworld.coordinator import bundle
import sys
sys.path.insert(0, str(Path(__file__).parent))
from test_e2e import _write_config  # noqa: E402


def jar(path: Path, mod_id: str, env: str = "*", depends: dict | None = None, extra: dict | None = None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": mod_id, "version": "1", "environment": env,
                                                  "depends": depends or {}}))
        for k, v in (extra or {}).items():
            z.writestr(k, v)


def test_import_selects_server_mods_and_keeps_helpers(tmp_path):
    cfg = _write_config(tmp_path, 1)
    mods = cfg.bundle_src / "mods"
    mods.mkdir(parents=True)
    jar(mods / "Chunky.jar", "chunky")
    jar(mods / "old.jar", "oldmod")
    src = tmp_path / "pack"
    src.mkdir()
    jar(src / "fabric-api.jar", "fabric-api")
    jar(src / "sodium.jar", "sodium", env="client")
    jar(src / "voxy-1.jar", "voxy")
    jar(src / "lod-server-support-1.jar", "lss")
    jar(src / "ores.jar", "ores", depends={"fabric-api": "*", "missinglib": "*"},
        extra={"data/ores/worldgen/placed_feature/x.json": "{}", "a/B.class": b"net/minecraft/class_123".decode()})
    warnings = bundle.import_mods(cfg, src, log=lambda *_: None)
    names = sorted(p.name for p in mods.glob("*.jar"))
    assert names == ["Chunky.jar", "fabric-api.jar", "ores.jar"]
    assert any("missinglib" in w for w in warnings)
    assert any("Intermediary" in w for w in warnings)
    assert (cfg.bundle_src / "mods.old" / "old.jar").exists()
