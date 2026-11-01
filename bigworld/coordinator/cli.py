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
    import shutil
    import time as _time

    from . import config

    p = Path(args.config or config.DEFAULT_CONFIG_PATH)
    values: dict = {}
    if p.exists():
        if not args.force:
            print(f"{p} existiert bereits, nichts geändert. Neu schreiben mit --force (Tokens bleiben erhalten).")
            return 0
        values = config.existing_values(p.read_text(encoding="utf-8", errors="replace"))
        backup = p.with_name(p.name + f".bak-{_time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(p, backup)
        print(f"Alte Datei gesichert: {backup}")
    if args.worker_token:
        values["worker_token"] = args.worker_token
    if args.admin_token:
        values["admin_token"] = args.admin_token
    text = config.example_config(args.preset, values)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    import tomllib

    tomllib.loads(text)  # Selbstprüfung
    os.replace(tmp, p)
    os.chmod(p, 0o640)
    try:
        import grp

        os.chown(p, 0, grp.getgrnam("bigworld").gr_gid)
    except (KeyError, PermissionError, ImportError, OSError):
        pass
    print(f"Geschrieben: {p} (Vorlage '{args.preset}')")
    for key in ("worker_token", "admin_token"):
        if key in values:
            print(f"  {key}: übernommen")
        else:
            print(f"  {key}: NEU erzeugt -> {config.existing_values(text)[key]}")
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
    if args.bundle_cmd == "mods":
        warnings = bundle.import_mods(cfg, Path(args.source))
        for w in warnings:
            print(f"WARNUNG: {w}")
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
            print(f"ACHTUNG: Mit einem anderen Bundle sind schon Kacheln fertig:")
            for dim, by_hash in sorted(state.done_by_dim().items()):
                old = {k: v for k, v in by_hash.items() if k != h}
                if old:
                    print(f"  {dim:28s} " + ", ".join(f"{n} Kacheln ({k[:12]})" for k, n in old.items()))
            print("Ein neues Bundle mischt sonst zwei verschiedene Generierungen in einer Welt. Entweder:")
            print("  - alles neu:        bigworld-coord reset --all, danach bundle build")
            print("  - nur betroffene:   bigworld-coord reset --dim <dimension> ... für jede Dimension, die die")
            print("                      neuen Mods verändern, danach bundle build --force und")
            print("                      bigworld-coord recheck (Stichprobe für die übrigen Dimensionen)")
            return 3
        previous = bundle.current_hash(cfg)
        bundle.set_current(cfg, h)
        if previous and previous != h:
            state.supersede_all()
            print("Laufende Kacheln des alten Bundles abgebrochen, die Worker synchronisieren neu.")
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
    for i, a in enumerate(st["areas"], 1):
        print(f"  Bereich #{i} {a['label'] or ''} {a['dim']} x {a['x0']}..{a['x1']} z {a['z0']}..{a['z1']}: "
              f"{100 * a['progress']:.0f} %")
    print(f"Chunks:     {t['chunks_done']:,} / {t['chunks_total']:,} ({pct:.1f} %)   {t['cps']:.1f} Chunks/s")
    if t.get("live_tiles"):
        waiting = t["tiles"].get("done", 0) - t["live_tiles"]
        print(f"Live-Welt:  {t['live_tiles']} Kacheln drin, {max(0, waiting)} fertig und warten auf den Live-Merge")
    rechecks = [x for x in st["tiles"] if x["verify"] and x["key"] not in {v["tile"] for v in st["verify"]}]
    for x in rechecks:
        print(f"Stichprobe offen: {x['key']}")
    if t["eta_seconds"]:
        print(f"Restzeit:   ~{t['eta_seconds'] / 3600:.1f} h")
    for w in st["workers"]:
        state = "online " if w["online"] else "offline"
        tile = f"{w['kind']} {w['tile']} {100 * (w['progress'] or 0):.0f}%" if w["tile"] else "-"
        print(f"  {w['name']:20s} {state} {w['gpu'] or '':28s} {tile:40s} {w['cps'] or 0:6.1f} cps  fertig: {w['tiles_done']}")
    for v in st["verify"]:
        print(f"Vergleich {v['tile']}: {'WELT IDENTISCH' if v.get('deterministic') else 'WELT VERSCHIEDEN'} "
              f"({v.get('worker_a')} vs {v.get('worker_b')})")
        if v.get("critical"):
            print(f"  kritisch:   {v['critical']}")
        if v.get("minor"):
            print(f"  unkritisch: {v['minor']}")
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


def cmd_seams(args) -> int:
    from ..common.tiles import Tile
    from . import seams
    from .state import State

    cfg = _cfg(args)
    state = State(cfg)
    names = {r["id"]: r["name"] for r in state.q("SELECT id, name FROM workers")}
    dims = [d for d in cfg.dimensions if not args.dim or d.name.endswith(args.dim)]
    for d in dims:
        rows = state.q("SELECT * FROM tiles WHERE dim=? AND status='done' AND in_world=1", d.name)
        tiles = [(Tile(r["dim"], r["tx"], r["tz"], r["rx0"], r["rx1"], r["rz0"], r["rz1"]),
                  names.get(r["done_by"], r["done_by"] or "?")) for r in rows]
        print(f"\n##### {d.name}: {len(tiles)} fertige Kacheln")
        for t, w in sorted(tiles, key=lambda x: (x[0].tz, x[0].tx)):
            print(f"  Kachel {t.tx:3d},{t.tz:3d}  x {t.x0}..{t.x1 - 1}  z {t.z0}..{t.z1 - 1}  von {w}")
        if len(tiles) < 2:
            continue
        print(seams.summarize(seams.analyze(cfg.staging_world, d.path, tiles), args.top))
    return 0


def cmd_reset(args) -> int:
    import shutil

    from .state import State

    cfg = _cfg(args)
    state = State(cfg)
    if args.all:
        ans = input("Alle Kacheln und Bereiche löschen und die Staging-Welt leeren? (ja/nein) ")
        if ans.strip().lower() != "ja":
            print("Abgebrochen")
            return 1
        n = state.reset_tiles(None)
        staging = cfg.data_dir / "staging"
        if staging.exists():
            shutil.rmtree(staging)
        print(f"{n} Kacheln und alle Bereiche gelöscht, Staging geleert. "
              f"Standardbereiche aus der Config neu angelegt: {len(state.areas())}")
    elif args.dim:
        for d in args.dim:
            try:
                cfg.dimension(d)
            except KeyError:
                print(f"Unbekannte Dimension {d}. Bekannt: {', '.join(x.name for x in cfg.dimensions)}")
                return 2
        ans = input(f"Alle Kacheln von {', '.join(args.dim)} neu generieren und ihre Regionsdateien in der "
                    "Staging-Welt löschen? Bereiche bleiben. (ja/nein) ")
        if ans.strip().lower() != "ja":
            print("Abgebrochen")
            return 1
        n = state.reset_dims(args.dim)
        for d in args.dim:
            dim_dir = cfg.staging_world / cfg.dimension(d).path
            for storage in ("region", "entities", "poi"):
                if (dim_dir / storage).exists():
                    shutil.rmtree(dim_dir / storage)
        print(f"{n} Kacheln in {', '.join(args.dim)} zurückgesetzt, Staging-Dateien gelöscht.")
    else:
        print(f"{state.reset_tiles(args.tiles)} Kacheln zurückgesetzt")
    return 0


def cmd_recheck(args) -> int:
    """Stichprobe je Dimension: fertige Kachel mit dem aktuellen Bundle neu generieren und vergleichen."""
    from . import bundle, merge
    from .state import State

    cfg = _cfg(args)
    state = State(cfg)
    h = bundle.current_hash(cfg)
    if not h:
        print("Kein aktives Bundle")
        return 1
    dims = args.dim or [d for d, by in state.done_by_dim().items() if any(k != h for k in by)]
    if not dims:
        print("Keine Dimension mit Kacheln aus einem älteren Bundle, nichts zu prüfen.")
        return 0
    online = {w["id"] for w in state.status()["workers"] if w["online"]}
    for d in dims:
        rows = state.q("SELECT * FROM tiles WHERE dim=? AND status='done' AND bundle_hash IS NOT ? "
                       "ORDER BY order_key", d, h)
        if not rows:
            print(f"{d}: keine passende fertige Kachel")
            continue
        row = next((r for r in rows if r["done_by"] in online), rows[0])
        n = merge.create_recheck(cfg, state, row["key"], row["done_by"])
        who = state.q("SELECT name FROM workers WHERE id=?", row["done_by"])
        print(f"{d}: Stichprobe {row['key']} ({n} Dateien), wird von {who[0]['name'] if who else row['done_by']} "
              f"neu generiert{'' if row['done_by'] in online else ' (Worker gerade offline)'}")
    print("Ergebnis danach mit `bigworld-coord status` (Zeilen 'Vergleich').")
    return 0


def cmd_pause(args) -> int:
    from .state import State

    State(_cfg(args)).set_setting("paused", "0" if args.cmd == "resume" else "1")
    print("Generierung " + ("fortgesetzt" if args.cmd == "resume" else "pausiert (laufende Kacheln laufen zu Ende)"))
    return 0


def cmd_live_merge(args) -> int:
    from . import live
    from .state import State

    cfg = _cfg(args)
    try:
        live.run(cfg, State(cfg), scheduled=args.scheduled, ignore_mods=args.ignore_mods, dry_run=args.dry_run)
    except live.LiveMergeError as e:
        print(f"FEHLER: {e}")
        return 1
    return 0


def cmd_deploy(args) -> int:
    from . import deploy
    from .state import State

    cfg = _cfg(args)
    try:
        deploy.run(cfg, State(cfg), allow_incomplete=args.allow_incomplete,
                   ignore_mods=args.ignore_mods, sync_mods=args.sync_mods)
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

    ini = sub.add_parser("init", help="Konfiguration anlegen oder mit --force neu schreiben (Tokens bleiben)")
    ini.add_argument("--preset", choices=["standard", "bigworld"], default="standard")
    ini.add_argument("--force", action="store_true", help="vorhandene Datei ersetzen (Sicherung wird angelegt)")
    ini.add_argument("--worker-token")
    ini.add_argument("--admin-token")
    ini.set_defaults(fn=cmd_init)
    sub.add_parser("serve", help="Koordinator starten (systemd)").set_defaults(fn=cmd_serve)

    b = sub.add_parser("bundle", help="Bundle für die Worker vorbereiten und bauen")
    bs = b.add_subparsers(dest="bundle_cmd", required=True)
    pr = bs.add_parser("prepare", help="bundle-src aus vorhandenen Ordnern befüllen (liest nur)")
    pr.add_argument("--world", required=True, help="Welt mit level.dat und datapacks/ (Seed-Quelle)")
    pr.add_argument("--mods-from", action="append", default=[], help="Ordner mit Mod-Jars (mehrfach möglich)")
    pr.add_argument("--config-from", help="config/-Ordner mit Mod-Configs")
    pr.add_argument("--launcher", help="Fabric-Server-Launcher-Jar")
    md = bs.add_parser("mods", help="bundle-src/mods durch eine Modpack-Liste ersetzen (Ordner oder ZIP)")
    md.add_argument("source")
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
    sm = sub.add_parser("seams", help="Nähte zwischen Kacheln in der Staging-Welt vermessen")
    sm.add_argument("--dim", help="nur diese Dimension, z. B. overworld")
    sm.add_argument("--top", type=int, default=15)
    sm.set_defaults(fn=cmd_seams)
    r = sub.add_parser("reset", help="Kacheln zurücksetzen")
    r.add_argument("--all", action="store_true")
    r.add_argument("--dim", action="append", help="nur diese Dimension neu (mehrfach möglich)")
    r.add_argument("tiles", nargs="*")
    r.set_defaults(fn=cmd_reset)
    rc = sub.add_parser("recheck", help="Stichprobe: fertige Kachel mit neuem Bundle neu generieren und vergleichen")
    rc.add_argument("--dim", action="append", help="Dimension (Standard: alle mit Kacheln aus älterem Bundle)")
    rc.set_defaults(fn=cmd_recheck)
    sub.add_parser("pause", help="Generierung pausieren").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="Generierung fortsetzen").set_defaults(fn=cmd_pause)
    d = sub.add_parser("deploy", help="Staging-Welt ins Pterodactyl-Volume einsetzen (mit Bestätigung)")
    d.add_argument("--allow-incomplete", action="store_true")
    d.add_argument("--sync-mods", action="store_true", help="Live-Mods an das Bundle angleichen (alte werden gesichert)")
    d.add_argument("--ignore-mods", action="store_true", help=argparse.SUPPRESS)
    d.set_defaults(fn=cmd_deploy)
    lv = sub.add_parser("live-merge", help="Fertige Kacheln in die Live-Welt einfügen (nur fehlende Chunks)")
    lv.add_argument("--scheduled", action="store_true", help="vom Timer: ohne Rückfrage, nur wenn [live_merge] enabled")
    lv.add_argument("--dry-run", action="store_true", help="nur anzeigen, was eingefügt würde")
    lv.add_argument("--ignore-mods", action="store_true", help=argparse.SUPPRESS)
    lv.set_defaults(fn=cmd_live_merge)
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
