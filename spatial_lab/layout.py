"""Speaker layout: which speakers exist, which interface port each is on, and where they are.

Coordinates everywhere in Spatial-Lab: x right, y front (towards the screen), z up,
metres, origin at the listening position (ear height).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

FT = 0.3048
ROLES = ("bed", "height", "sub")


class LayoutError(ValueError):
    pass


@dataclass
class Speaker:
    name: str
    port: int
    role: str
    direction: np.ndarray | None = None      # unit vector, None for the sub
    distance_m: float | None = None
    provisional: bool = False

    @property
    def position(self) -> np.ndarray | None:
        if self.direction is None or self.distance_m is None:
            return None
        return self.direction * self.distance_m

    @property
    def azimuth_deg(self) -> float | None:
        if self.direction is None:
            return None
        return math.degrees(math.atan2(self.direction[0], self.direction[1]))

    @property
    def elevation_deg(self) -> float | None:
        if self.direction is None:
            return None
        return math.degrees(math.asin(max(-1.0, min(1.0, self.direction[2]))))


@dataclass
class Layout:
    name: str
    sink: str
    port_names: list[str]
    capture_node: str
    playback_node: str
    speakers: list[Speaker]
    room: dict = field(default_factory=dict)
    source_path: str = ""

    @property
    def n_out(self) -> int:
        return len(self.port_names)

    @property
    def panned(self) -> list[Speaker]:
        """Speakers that take part in panning (everything except the sub)."""
        return [s for s in self.speakers if s.role != "sub"]

    def by_port(self) -> list[Speaker | None]:
        out: list[Speaker | None] = [None] * self.n_out
        for s in self.speakers:
            out[s.port] = s
        return out

    def by_name(self, name: str) -> Speaker:
        for s in self.speakers:
            if s.name == name:
                return s
        raise KeyError(name)

    def to_public(self) -> dict:
        """What the browser needs: speakers with positions, the room box, port names."""
        sp = []
        for s in self.speakers:
            p = s.position
            sp.append({
                "name": s.name, "port": s.port, "role": s.role,
                "port_name": self.port_names[s.port],
                "pos": None if p is None else [round(float(v), 4) for v in p],
                "distance_m": s.distance_m, "provisional": s.provisional,
                "az": None if s.azimuth_deg is None else round(s.azimuth_deg, 1),
                "el": None if s.elevation_deg is None else round(s.elevation_deg, 1),
            })
        return {"name": self.name, "speakers": sp, "port_names": self.port_names,
                "room": self.room, "sink": self.sink}


def _room(raw: dict) -> dict:
    w = float(raw.get("width_ft", 10)) * FT
    d = float(raw.get("depth_ft", 10)) * FT
    h = float(raw.get("height_ft", 9)) * FT
    back = float(raw.get("seat_from_back_wall_ft", 1.0)) * FT
    ear = float(raw.get("ear_height_ft", 2.0)) * FT
    return {
        "x": [-w / 2, w / 2],
        "y": [-back, d - back],
        "z": [-ear, h - ear],
        "width_m": w, "depth_m": d, "height_m": h, "ear_m": ear,
    }


def parse_layout(raw: dict, source: str = "") -> Layout:
    if not isinstance(raw, dict):
        raise LayoutError("layout file must be a mapping")
    out = raw.get("output") or {}
    port_names = list(out.get("port_names") or [])
    if not port_names:
        raise LayoutError("output.port_names is required")
    cd = raw.get("camilladsp") or {}
    speakers: list[Speaker] = []
    seen_n, seen_p = set(), set()
    for i, c in enumerate(raw.get("speakers") or []):
        where = f"speakers[{i}]"
        name = str(c.get("name", "")).strip()
        if not name or len(name) > 8:
            raise LayoutError(f"{where}.name must be 1-8 characters")
        if name in seen_n:
            raise LayoutError(f"{where}: duplicate name {name}")
        role = c.get("role", "bed")
        if role not in ROLES:
            raise LayoutError(f"{where}.role must be one of {ROLES}")
        port = c.get("port")
        if not isinstance(port, int) or not 0 <= port < len(port_names):
            raise LayoutError(f"{where}.port must be an integer 0..{len(port_names) - 1}")
        if port in seen_p:
            raise LayoutError(f"{where}: port {port} is used twice")
        seen_n.add(name)
        seen_p.add(port)
        direction = dist = None
        if role != "sub":
            d = np.asarray(c.get("dir"), dtype=float)
            if d.shape != (3,) or not np.all(np.isfinite(d)) or np.linalg.norm(d) < 1e-6:
                raise LayoutError(f"{where}.dir must be three numbers")
            direction = d / np.linalg.norm(d)
            dist = float(c.get("distance_ft", 9)) * FT
            if not 0.2 < dist < 20:
                raise LayoutError(f"{where}.distance_ft is out of range")
        speakers.append(Speaker(name, port, role, direction, dist, bool(c.get("provisional", False))))
    if sum(1 for s in speakers if s.role != "sub") < 3:
        raise LayoutError("need at least three speakers that can be panned")
    if sum(1 for s in speakers if s.role == "sub") > 1:
        raise LayoutError("at most one sub")
    return Layout(
        name=str(raw.get("name", "layout")),
        sink=str(out.get("sink", "")),
        port_names=port_names,
        capture_node=str(cd.get("capture_node", "camilladsp-capture")),
        playback_node=str(cd.get("playback_node", "camilladsp-playback")),
        speakers=speakers,
        room=_room(raw.get("room") or {}),
        source_path=source,
    )


def default_layout_path() -> Path:
    return Path(__file__).parent / "layouts" / "author-5.1.2.yml"


def find_layout_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("SPATIAL_LAB_LAYOUT")
    if env:
        return Path(env).expanduser()
    user = Path("~/.config/spatial-lab/layout.yml").expanduser()
    if user.exists():
        return user
    return default_layout_path()


def load_layout(path: str | Path | None = None) -> Layout:
    p = find_layout_path(str(path) if path else None)
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return parse_layout(raw, str(p))
