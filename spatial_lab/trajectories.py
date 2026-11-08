"""Preset paths for the object, and recorded paths. Positions are metres from the seat (x right, y front, z up)."""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np


def tri(u: float) -> float:
    """0 -> 1 -> 0 over u in [0, 1)."""
    return 1.0 - abs(2.0 * (u % 1.0) - 1.0)


def smooth(x: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * x)


@dataclass
class Trajectory:
    id: str
    label: str
    period: float                       # seconds for one cycle at speed 1
    fn: Callable[[float], tuple]        # u in [0,1) -> (x, y, z)
    note: str = ""

    @property
    def duration(self) -> float:
        return self.period

    def pos(self, t: float) -> np.ndarray:
        return np.array(self.fn((t / self.period) % 1.0), dtype=float)


class RecordedTrajectory:
    def __init__(self, id: str, pts: list):
        a = np.asarray(pts, dtype=float)
        if a.ndim != 2 or a.shape[1] != 4 or len(a) < 2:
            raise ValueError("a recording needs at least two [t, x, y, z] points")
        self.id = id
        self.label = id
        self.t = a[:, 0] - a[0, 0]
        self.xyz = a[:, 1:]
        self.period = float(max(self.t[-1], 1e-3))
        self.note = "recorded"

    @property
    def duration(self) -> float:
        return self.period

    def pos(self, t: float) -> np.ndarray:
        t = max(t, 0.0)
        if t >= self.period:
            t = t % self.period
        return np.array([np.interp(t, self.t, self.xyz[:, k]) for k in range(3)])


def build_presets(room: dict) -> dict[str, Trajectory]:
    x0, x1 = room["x"]
    y0, y1 = room["y"]
    z0, z1 = room["z"]
    m = 0.3
    cx = 0.0
    cy = 0.5 * (y0 + y1) + 0.15
    rx = 0.5 * (x1 - x0) - m
    ry = 0.5 * (y1 - y0) - m
    ztop = z1 - 0.2
    zlow = z0 + 0.25
    front = y1 - 0.4

    lap_y0, lap_y1 = 0.7, y1 - 0.35       # never closer than 0.7 m to the seat

    def lap(u):
        a = 2 * math.pi * u
        return (cx + rx * math.sin(a), 0.5 * (lap_y0 + lap_y1) + 0.5 * (lap_y1 - lap_y0) * math.cos(a), 0.2)

    def orbit(u):
        a = 2 * math.pi * u
        return (0.8 * math.sin(a), 0.8 * math.cos(a), 0.1)

    def front_back(u):
        return (0.0, front - (front - 0.25) * tri(u), 0.15)

    def lr_front(u):
        return (x0 + m + (x1 - x0 - 2 * m) * tri(u), front, 0.2)

    def lr_mid(u):
        return (x0 + m + (x1 - x0 - 2 * m) * tri(u), 1.0, 0.1)

    def floor_ceiling(u):
        return (0.0, 1.1, zlow + (ztop - zlow) * tri(u))

    def height_arc(u):
        k = tri(u)                                     # 0 left -> 1 right via the top
        a = math.pi * k
        return (-(rx) * math.cos(a), 1.0, 0.0 + (ztop - 0.0) * math.sin(a))

    def spiral(u):
        a = 2 * math.pi * 3 * u
        return (0.9 * math.sin(a), 0.9 + 0.8 * math.cos(a), zlow + (ztop - zlow) * tri(u))

    def figure8(u):
        a = 2 * math.pi * u
        return (min(rx, 1.1) * math.sin(a), 1.1 + min(ry, 0.9) * math.sin(a) * math.cos(a) * 2.0, 0.2)

    P = [
        Trajectory("lap", "Lap of the room, ear height", 24, lap, "all the way round, passing close to the surrounds"),
        Trajectory("orbit", "Orbit the seat (virtual, 0.8 m)", 20, orbit, "constant distance; passes through the back wall"),
        Trajectory("front_back", "Screen to seat, down the middle", 16, front_back, "distance cue: level, air loss, Doppler"),
        Trajectory("lr_front", "Left to right across the front", 12, lr_front, "FL, C, FR phantom images"),
        Trajectory("lr_mid", "Left to right, mid-room", 12, lr_mid, "between the front pair and the surrounds"),
        Trajectory("floor_ceiling", "Floor to ceiling, mid-room", 14, floor_ceiling, "bed to heights; level only, no spectral height cue"),
        Trajectory("height_arc", "Over the top, left wall to right wall", 14, height_arc, "the two heights are both ahead of the seat"),
        Trajectory("spiral", "Rising spiral", 30, spiral, "azimuth and height together"),
        Trajectory("figure8", "Figure of eight, ear height", 28, figure8, "crossing the centre line twice"),
    ]
    return {t.id: t for t in P}


# --------------------------------------------------------------------------- recordings on disk

_NAME = re.compile(r"^[A-Za-z0-9_\- ]{1,40}$")


def recordings_dir() -> Path:
    base = os.environ.get("SPATIAL_LAB_DATA")
    p = Path(base).expanduser() if base else Path("~/.local/share/spatial-lab").expanduser()
    return p / "recordings"


def list_recordings() -> list[dict]:
    d = recordings_dir()
    out = []
    if d.exists():
        for f in sorted(d.glob("*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    j = json.load(fh)
                out.append({"name": f.stem, "duration": round(float(j["pts"][-1][0] - j["pts"][0][0]), 2)})
            except Exception:
                continue
    return out


def save_recording(name: str, pts: list) -> str:
    name = name.strip()
    if not _NAME.match(name):
        raise ValueError("name: 1-40 letters, digits, space, - or _")
    d = recordings_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "pts": [[round(float(v), 4) for v in p] for p in pts]}, fh)
    os.replace(tmp, d / f"{name}.json")
    return name


def load_recording(name: str) -> RecordedTrajectory:
    if not _NAME.match(name):
        raise ValueError("bad recording name")
    with open(recordings_dir() / f"{name}.json", "r", encoding="utf-8") as fh:
        j = json.load(fh)
    return RecordedTrajectory(name, j["pts"])


def delete_recording(name: str) -> None:
    if not _NAME.match(name):
        raise ValueError("bad recording name")
    f = recordings_dir() / f"{name}.json"
    if f.exists():
        f.unlink()
