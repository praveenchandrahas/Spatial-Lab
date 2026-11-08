"""User config: ~/.config/spatial-lab/config.yml. The level ceiling lives here, not in the page."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

DEFAULTS = {
    "ceiling_db": -40.0,   # the loudest the master level can ever be, in dBFS (RMS of the source)
    "fade_in_s": 3.0,      # unmuting ramps up over this long
    "host": "127.0.0.1",   # never anything else: the control page is local only
    "port": 8780,
    "hp_hz": 200.0,        # the source is high-passed here, so the Z906 sub gets almost nothing
    "block": 512,          # frames per audio block (10.7 ms at 48 kHz)
    "backend": "pipewire",
    "pw_latency_ms": 40,
}


def config_path() -> Path:
    env = os.environ.get("SPATIAL_LAB_CONFIG")
    return Path(env).expanduser() if env else Path("~/.config/spatial-lab/config.yml").expanduser()


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{p} must contain a mapping")
        for k in DEFAULTS:
            if k in raw:
                cfg[k] = raw[k]
    cfg["host"] = "127.0.0.1"                       # not configurable
    cfg["ceiling_db"] = max(-90.0, min(0.0, float(cfg["ceiling_db"])))
    cfg["fade_in_s"] = max(0.5, min(20.0, float(cfg["fade_in_s"])))
    cfg["hp_hz"] = max(20.0, min(2000.0, float(cfg["hp_hz"])))
    cfg["block"] = int(cfg["block"])
    return cfg


def save_config(patch: dict) -> Path:
    p = config_path()
    cur = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            cur = yaml.safe_load(fh) or {}
    cur.update(patch)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cur, fh, sort_keys=True)
    os.replace(tmp, p)
    return p
