"""Command line: serve, render, check, set-ceiling, layout."""
from __future__ import annotations

import argparse
import shutil
import sys

from . import __version__
from .config import config_path, load_config, save_config
from .layout import find_layout_path, load_layout


def cmd_serve(a) -> int:
    from aiohttp import web
    from .engine import Engine
    from .output import OutputError, make_output
    from .server import create_app

    cfg = load_config()
    if a.port:
        cfg["port"] = a.port
    if a.backend:
        cfg["backend"] = a.backend
    layout = load_layout(a.layout)
    engine = Engine(layout, cfg)
    out = make_output(cfg["backend"], layout, cfg)
    try:
        out.start()
    except OutputError as e:
        print(f"spatial-lab: cannot start audio output: {e}", file=sys.stderr)
        return 1
    engine.watchdog_s = 4.0
    app = create_app(engine, layout, cfg, out)
    engine.start(out)
    url = f"http://127.0.0.1:{cfg['port']}/"
    print(f"Spatial-Lab {__version__}  layout: {layout.name}  backend: {out.name}")
    print(f"  open {url}   (this machine only)")
    print(f"  starts muted; level ceiling {engine.ceiling_db:.0f} dBFS (change with: spatial-lab set-ceiling DB)")
    if a.open:
        import webbrowser
        webbrowser.open(url)
    web.run_app(app, host="127.0.0.1", port=cfg["port"], print=None, handle_signals=True)
    return 0


def cmd_render(a) -> int:
    from .render import render_path, write_outputs
    from .trajectories import build_presets

    cfg = load_config()
    layout = load_layout(a.layout)
    presets = build_presets(layout.room)
    if a.path not in presets:
        print("paths: " + ", ".join(presets), file=sys.stderr)
        return 2
    params = {"doppler": a.doppler, "dist_exp": a.dist, "air_ex": a.air, "law": a.law,
              "spread_deg": a.spread, "corr": a.corr, "align": a.align}
    res = render_path(layout, cfg, a.path, a.seconds, a.speed, a.master_db, params)
    files = write_outputs(res, a.out, a.path, plot=not a.no_plot)
    print(f"wrote {len(files)} files to {a.out}")
    for f in files[:3]:
        print("  ", f)
    return 0


def cmd_check(a) -> int:
    cfg = load_config()
    path = find_layout_path(a.layout)
    layout = load_layout(a.layout)
    print(f"Spatial-Lab {__version__}")
    print(f"layout      {path}  ({layout.name})")
    print(f"config      {config_path()}  ceiling {cfg['ceiling_db']:.0f} dBFS, fade-in {cfg['fade_in_s']:.1f} s, "
          f"high-pass {cfg['hp_hz']:.0f} Hz, port {cfg['port']}")
    for tool in ("pw-cat", "pw-link", "ffmpeg"):
        print(f"{tool:11s} {shutil.which(tool) or 'NOT FOUND'}")
    if shutil.which("pw-link"):
        from .output import OutputError, pw_links, pw_ports
        try:
            ins, outs = set(pw_ports("i")), pw_ports("o")
            sink = any(p.startswith(layout.sink + ":") for p in ins)
            cd = any(p.startswith(layout.capture_node + ":") for p in ins)
            print(f"USB sink    {'present' if sink else 'MISSING'}  ({layout.sink})")
            print(f"CamillaDSP  {'present' if cd else 'not running'}  ({layout.capture_node})")
            print(f"mpv         {'PLAYING (stop it before using Spatial-Lab)' if any(p.startswith('mpv:') for p in outs) else 'not playing'}")
            if cd:
                links = pw_links()
                ok = any(o.startswith(layout.playback_node + ":") and i.startswith(layout.sink + ":") for o, i in links)
                print(f"CamillaDSP -> interface links  {'ok' if ok else 'MISSING (Through mode will be silent)'}")
        except OutputError as e:
            print(f"PipeWire    {e}")
    print("speakers (port, name, role, azimuth/elevation from the seat, distance):")
    for s in layout.speakers:
        if s.role == "sub":
            print(f"  {s.port}  {s.name:4s} sub   (kept silent)")
        else:
            flag = "  provisional" if s.provisional else ""
            print(f"  {s.port}  {s.name:4s} {s.role:6s} az {s.azimuth_deg:7.1f}  el {s.elevation_deg:5.1f}  "
                  f"{s.distance_m / 0.3048:4.1f} ft{flag}")
    return 0


def cmd_set_ceiling(a) -> int:
    if not -90.0 <= a.db <= 0.0:
        print("ceiling must be between -90 and 0 dBFS", file=sys.stderr)
        return 2
    p = save_config({"ceiling_db": float(a.db)})
    print(f"ceiling set to {a.db:.1f} dBFS in {p}. Restart Spatial-Lab for it to apply.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="spatial-lab", description="Interactive 3D audio test bench for a multichannel room.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the audio engine and the local web page")
    s.add_argument("--backend", choices=("pipewire", "null", "wav"))
    s.add_argument("--port", type=int)
    s.add_argument("--layout")
    s.add_argument("--open", action="store_true", help="open the page in the default browser")
    s.set_defaults(fn=cmd_serve)

    r = sub.add_parser("render", help="render a path offline to WAV files and plots (nothing is played)")
    r.add_argument("--path", default="lap")
    r.add_argument("--seconds", type=float, default=24.0)
    r.add_argument("--speed", type=float, default=1.0)
    r.add_argument("--master-db", type=float, default=-20.0)
    r.add_argument("--out", default="renders")
    r.add_argument("--layout")
    r.add_argument("--law", choices=("vbap", "nearest"), default="vbap")
    r.add_argument("--spread", type=float, default=0.0)
    r.add_argument("--corr", type=float, default=0.0)
    r.add_argument("--dist", type=float, default=0.5, help="distance level exponent (0 = off)")
    r.add_argument("--air", type=float, default=1.0, help="air absorption exaggeration (0 = off)")
    r.add_argument("--doppler", action="store_true")
    r.add_argument("--align", choices=("auto", "on", "off"), default="off")
    r.add_argument("--no-plot", action="store_true")
    r.set_defaults(fn=cmd_render)

    c = sub.add_parser("check", help="show what Spatial-Lab sees (tools, PipeWire ports, layout)")
    c.add_argument("--layout")
    c.set_defaults(fn=cmd_check)

    k = sub.add_parser("set-ceiling", help="set the loudest master level, in dBFS")
    k.add_argument("db", type=float)
    k.set_defaults(fn=cmd_set_ceiling)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
