"""Kommandozeile des Koordinators: bigworld-coord <befehl>."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .. import __version__


def _cfg(args):
    from . import config

    return config.load(args.config)


def cmd_init(args) -> int:
    from . import config

    p = Path(args.config or config.DEFAULT_CONFIG_PATH)
    if p.exists():
        print(f"{p} existiert bereits, nichts geändert.")
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(config.example_config())
    os.chmod(p, 0o640)
    print(f"Beispielkonfiguration mit neuen Tokens geschrieben: {p}")
    print("Bitte prüfen (Radius, Pfade) und danach `bigworld-coord bundle prepare ...` ausführen.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = _cfg(args)
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info", access_log=False)
    return 0


def cmd_bundle(args) -> int:
    from . import bundle
    from .state import State

    cfg = _cfg(args)
    if args.bundle_cmd == "prepare":
        warnings = bundle.prepare(
            cfg,
            world=Path(args.world),
            mods_from=[Path(p) for p in args.mods_from],
            config_from=Path(args.config_from) if args.config_from else None,
            launcher=Path(args.launcher) if args.launcher else None,
        )
        for w in warnings:
            print(f"WARNUNG: {w}")
        print(f"bundle-src liegt in {cfg.bundle_src}. Danach: bigworld-coord bundle build")
        return 0
    if args.bundle_cmd == "fetch-modrinth":
        bundle.fetch_modrinth(cfg, args.slug, args.mc)
        return 0
    if args.bundle_cmd == "fetch-launcher":
        loader = args.loader
        if not loader and args.server_root:
            loader = bundle.detect_loader_version(Path(args.server_root))
            print(f"Erkannter Fabric-Loader: {loader}")
        if not loader:
            print("Loader-Version unbekannt, bitte --loader angeben")
            return 2
        bundle.fetch_launcher(cfg, args.mc, loader)
        return 0
    if args.bundle_cmd == "build":
        manifest = bundle.build(cfg)
        h = manifest["hash"]
        state = State(cfg)
        other = state.done_bundle_hashes() - {h, None}
        if other and not args.force:
            print(f"ACHTUNG: {len(other)} anderes Bundle hat schon fertige Kacheln ({', '.join(x[:12] for x in other)}).")
            print("Ein neues Bundle mischt sonst zwei verschiedene Generierungen in einer Welt.")
            print("Entweder `bigworld-coord reset --all` (alles neu) oder mit --force bewusst aktivieren.")
            return 3
        bundle.set_current(cfg, h)
        print(f"Aktives Bundle: {h}")
        for m in manifest["mods"]:
            print(f"  {m['file']:60s} {m['id']} {m['version']}")
        return 0
    if args.bundle_cmd == "show":
        h = bundle.current_hash(cfg)
        if not h:
            print("Kein aktives Bundle")
            return 1
        print(json.dumps(bundle.load_manifest(cfg, h), indent=2))
        return 0
    return 2


def cmd_status(args) -> int:
    from . import bundle
    from .state import State

    cfg = _cfg(args)
    st = State(cfg).status(bundle.current_hash(cfg))
    t = st["totals"]
    pct = 100 * t["chunks_done"] / t["chunks_total"] if t["chunks_total"] else 0
    print(f"Bundle:     {(bundle.current_hash(cfg) or '-')[:12]}   pausiert: {st['paused']}")
    print(f"Vorlage:    {'vorhanden' if st['template']['sha256'] else 'fehlt'}"
          f"{' (läuft auf ' + st['template']['running_on'] + ')' if st['template']['running_on'] else ''}")
    print(f"Kacheln:    {t['tiles']}")
    print(f"Chunks:     {t['chunks_done']:,} / {t['chunks_total']:,} ({pct:.1f} %)   {t['cps']:.1f} Chunks/s")
    if t["eta_seconds"]:
        print(f"Restzeit:   ~{t['eta_seconds'] / 3600:.1f} h")
    for w in st["workers"]:
        state = "online " if w["online"] else "offline"
        tile = f"{w['kind']} {w['tile']} {100 * (w['progress'] or 0):.0f}%" if w["tile"] else "-"
        print(f"  {w['name']:20s} {state} {w['gpu'] or '':28s} {tile:40s} {w['cps'] or 0:6.1f} cps  fertig: {w['tiles_done']}")
    for v in st["verify"]:
        print(f"Vergleich {v['tile']}: {'IDENTISCH' if v.get('deterministic') else 'UNTERSCHIEDE'} "
              f"({v.get('worker_a')} vs {v.get('worker_b')})")
    for w in st["warnings"]:
        print(f"WARNUNG: {w}")
    return 0


def cmd_compare(args) -> int:
    from . import merge
    from .state import State

    cfg = _cfg(args)
    rep = merge.run_compare(cfg, State(cfg), args.tile)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0 if rep.get("deterministic") else 1


def cmd_reset(args) -> int:
    import shutil

    from .state import State

    cfg = _cfg(args)
    state = State(cfg)
    if args.all:
        ans = input("Alle Kacheln zurücksetzen und die Staging-Welt löschen? (ja/nein) ")
        if ans.strip().lower() != "ja":
            print("Abgebrochen")
            return 1
        n = state.reset_tiles(None)
        staging = cfg.data_dir / "staging"
        if staging.exists():
            shutil.rmtree(staging)
        print(f"{n} Kacheln zurückgesetzt, Staging gelöscht")
    else:
        print(f"{state.reset_tiles(args.tiles)} Kacheln zurückgesetzt")
    return 0


def cmd_deploy(args) -> int:
    from . import deploy
    from .state import State

    cfg = _cfg(args)
    try:
        deploy.run(cfg, State(cfg), allow_incomplete=args.allow_incomplete)
    except deploy.DeployError as e:
        print(f"FEHLER: {e}")
        return 1
    return 0


def cmd_seed(args) -> int:
    from ..common import nbt
    import gzip

    data = Path(args.file).read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    root = nbt.parse(data)

    def find(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() == "seed" and isinstance(v, int):
                    print(f"{path}/{k} = {v}")
                find(v, f"{path}/{k}")

    find(root)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bigworld-coord", description=f"Big-World-Koordinator {__version__}")
    p.add_argument("--config", help="Pfad zur Konfiguration (Standard /etc/bigworld/coordinator.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Beispielkonfiguration mit Tokens anlegen").set_defaults(fn=cmd_init)
    sub.add_parser("serve", help="Koordinator starten (systemd)").set_defaults(fn=cmd_serve)

    b = sub.add_parser("bundle", help="Bundle für die Worker vorbereiten und bauen")
    bs = b.add_subparsers(dest="bundle_cmd", required=True)
    pr = bs.add_parser("prepare", help="bundle-src aus vorhandenen Ordnern befüllen (liest nur)")
    pr.add_argument("--world", required=True, help="Welt mit level.dat und datapacks/ (Seed-Quelle)")
    pr.add_argument("--mods-from", action="append", default=[], help="Ordner mit Mod-Jars (mehrfach möglich)")
    pr.add_argument("--config-from", help="config/-Ordner mit Mod-Configs")
    pr.add_argument("--launcher", help="Fabric-Server-Launcher-Jar")
    fm = bs.add_parser("fetch-modrinth", help="Mod von Modrinth nach bundle-src/mods laden")
    fm.add_argument("slug")
    fm.add_argument("--mc", required=True)
    fl = bs.add_parser("fetch-launcher", help="Fabric-Server-Launcher laden")
    fl.add_argument("--mc", required=True)
    fl.add_argument("--loader")
    fl.add_argument("--server-root", help="Server-Ordner, um die Loader-Version zu erkennen")
    bb = bs.add_parser("build", help="Bundle bauen und aktivieren")
    bb.add_argument("--force", action="store_true")
    bs.add_parser("show", help="Aktives Bundle anzeigen")
    b.set_defaults(fn=cmd_bundle)

    sub.add_parser("status", help="Fortschritt anzeigen").set_defaults(fn=cmd_status)
    c = sub.add_parser("compare", help="Determinismus-Vergleich einer Kachel neu ausführen")
    c.add_argument("tile", help='z. B. "minecraft:overworld|0|0"')
    c.set_defaults(fn=cmd_compare)
    r = sub.add_parser("reset", help="Kacheln zurücksetzen")
    r.add_argument("--all", action="store_true")
    r.add_argument("tiles", nargs="*")
    r.set_defaults(fn=cmd_reset)
    d = sub.add_parser("deploy", help="Staging-Welt ins Pterodactyl-Volume einsetzen (mit Bestätigung)")
    d.add_argument("--allow-incomplete", action="store_true")
    d.set_defaults(fn=cmd_deploy)
    s = sub.add_parser("seed", help="Seed aus level.dat oder world_gen_settings.dat lesen")
    s.add_argument("file")
    s.set_defaults(fn=cmd_seed)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    finally:
        if args.cmd not in ("init", "seed"):
            _fix_data_owner(args)


def _fix_data_owner(args) -> None:
    """Mit sudo ausgeführte Befehle dürfen keine root-eigenen Dateien im Datenordner hinterlassen,
    sonst kann der Dienst (Benutzer bigworld) sie nicht mehr schreiben."""
    if os.name != "posix" or os.geteuid() != 0:
        return
    try:
        cfg = _cfg(args)
    except Exception:  # noqa: BLE001
        return
    root = cfg.data_dir
    if not root.is_dir():
        return
    st = root.stat()
    if st.st_uid == 0:
        return
    for dirpath, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = os.path.join(dirpath, name)
            try:
                if os.lstat(path).st_uid == 0:
                    os.lchown(path, st.st_uid, st.st_gid)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
