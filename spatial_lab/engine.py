"""The real-time engine: source -> distance cues -> panning -> per-speaker tweaks -> master.

One thread calls process_block() in a loop. Everything the browser can change goes through
set_params() / the small command methods, which validate and clamp. The level ceiling is fixed
at construction from the config file and cannot be raised from the page.

Safety, in order of the signal path:
  * starts muted; unmuting fades in over `fade_in_s` (raised cosine);
  * master level can never exceed the ceiling; level changes slew at 12 dB/s up;
  * mute fades out in 0.12 s, panic in 0.02 s;
  * if the page stops sending heartbeats the engine mutes itself;
  * the LFE output is always silent;
  * the output is clipped at full scale (a last guard, not a per-channel limiter).
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np

from . import dsp
from .dsp import FS, C_SOUND, db_to_lin, lin_to_db
from .layout import Layout
from .trajectories import RecordedTrajectory, Trajectory
from .vbap import Vbap, energy_vector

ENUMS = {
    "mode": ("object", "manual", "identify"),
    "law": ("vbap", "nearest"),
    "src": ("pink", "file"),
    "align": ("auto", "on", "off"),
    "out_mode": ("direct", "through"),
}
RANGES = {
    "master_db": (-90.0, 0.0),
    "burst_on_ms": (0.0, 5000.0),
    "burst_off_ms": (0.0, 5000.0),
    "spread_deg": (0.0, 60.0),
    "dist_exp": (0.0, 2.0),
    "air_ex": (0.0, 50.0),
    "doppler_scale": (0.0, 20.0),
    "corr": (0.0, 1.0),
}
BOOLS = ("doppler",)
SPK_RANGES = {"trim_db": (-60.0, 0.0), "delay_ms": (0.0, 50.0)}
SPK_BOOLS = ("invert", "mute")
MAX_RECORD_S = 120.0
LEVEL_UP_DB_PER_S = 12.0
LEVEL_DOWN_DB_PER_S = 60.0
MIN_RADIUS = 0.12


def _clamp(v, lo, hi):
    return max(lo, min(hi, float(v)))


class Engine:
    def __init__(self, layout: Layout, cfg: dict):
        self.layout = layout
        self.cfg = cfg
        self.N = int(cfg["block"])
        self.nout = layout.n_out
        self.ceiling_db = float(cfg["ceiling_db"])
        self.fade_in_s = float(cfg["fade_in_s"])
        self.lock = threading.RLock()

        # geometry
        self.by_port = layout.by_port()
        self.panned = layout.panned
        self.pan_ports = [s.port for s in self.panned]
        self.vbap = Vbap(np.array([s.direction for s in self.panned]))
        self.names = [(s.name if s else f"out{i}") for i, s in enumerate(self.by_port)]
        self.name_to_port = {s.name: s.port for s in layout.speakers}
        rmax = max(s.distance_m for s in self.panned)
        self.align_delay_ms = np.zeros(self.nout)
        self.align_gain = np.ones(self.nout)
        for s in self.panned:
            self.align_delay_ms[s.port] = (rmax - s.distance_m) / C_SOUND * 1000.0
            self.align_gain[s.port] = s.distance_m / rmax
        room = layout.room
        self.bounds = (np.array([room["x"][0], room["y"][0], room["z"][0]]),
                       np.array([room["x"][1], room["y"][1], room["z"][1]]))

        # parameters
        spk = {s.name: {"trim_db": 0.0, "delay_ms": 0.0, "invert": False, "mute": False}
               for s in layout.speakers if s.role != "sub"}
        self.p = {
            "mode": "object", "master_db": min(self.ceiling_db, self.ceiling_db - 6.0),
            "src": "pink", "burst_on_ms": 400.0, "burst_off_ms": 300.0,
            "law": "vbap", "spread_deg": 0.0,
            "dist_exp": 0.5, "air_ex": 1.0, "doppler": False, "doppler_scale": 1.0,
            "align": "auto", "out_mode": "direct",
            "corr": 0.0,
            "manual_set": [s.name for s in self.panned[:2]],
            "identify_speaker": None, "solo": None,
            "spk": spk,
        }
        self.muted = True
        self.duck = False
        self.fast_up = False
        self.panic = False
        self.hp_hz = float(cfg["hp_hz"])

        # state
        self.pos_target = np.array([0.0, 1.5, 0.2])
        self.pos = self.pos_target.copy()
        self.last_dir = np.array([0.0, 1.0, 0.0])
        self.r_prev = float(np.linalg.norm(self.pos))
        self.motion: dict | None = None
        self.rec: dict | None = None
        self.rec_done: list | None = None
        self.gate_fade = 0.0
        self.level_db = self.p["master_db"]
        self.amp_prev = 0.0
        self.g_prev = np.zeros(self.nout)
        self.d_prev = np.zeros(self.nout)
        self.t_total = 0.0
        self.clips = 0
        self.last_hb = time.monotonic()
        self.watchdog_s: float | None = None
        self.mute_reason = "startup"
        self.id_current: str | None = None
        self.id_t = 0.0
        self._prev_mode = "object"

        # sources
        self.pink_seed = 1
        self.src_pink = dsp.LoopSource(dsp.pink_loop(20.0, self.hp_hz, self.pink_seed))
        self.file_raw: np.ndarray | None = None
        self.file_name: str | None = None
        self.src_file: dsp.LoopSource | None = None
        self.gate = dsp.BurstGate()
        self.id_gate = dsp.BurstGate()

        # processors
        self.doppler_bank = dsp.DelayBank(1, 0.45, self.N)
        self.spk_delay = dsp.DelayBank(self.nout, 0.12, self.N)
        self.air = dsp.AirAbsorption()
        self.conv = dsp.FFTConvolver(dsp.decorrelator_kernels(self.nout), self.N)

        self.meters = self._empty_meters()
        self.load = 0.0
        self._thread: threading.Thread | None = None
        self._run = False
        self.backend = None

    # ------------------------------------------------------------------ parameters

    def set_params(self, patch: dict) -> list[str]:
        """Validate and apply. Returns the keys that were rejected."""
        bad = []
        with self.lock:
            for k, v in patch.items():
                try:
                    if k in ENUMS:
                        if v not in ENUMS[k]:
                            raise ValueError
                        self.p[k] = v
                    elif k == "master_db":
                        self.p[k] = _clamp(v, RANGES[k][0], self.ceiling_db)
                    elif k in RANGES:
                        self.p[k] = _clamp(v, *RANGES[k])
                    elif k in BOOLS:
                        self.p[k] = bool(v)
                    elif k == "manual_set":
                        names = [n for n in v if n in self.p["spk"]]
                        if not 1 <= len(names) <= self.nout:
                            raise ValueError
                        self.p[k] = list(dict.fromkeys(names))
                    elif k in ("identify_speaker", "solo"):
                        if v is not None and v not in self.p["spk"]:
                            raise ValueError
                        self.p[k] = v
                    elif k == "spk":
                        for name, d in v.items():
                            if name not in self.p["spk"]:
                                raise ValueError
                            for kk, vv in d.items():
                                if kk in SPK_RANGES:
                                    self.p["spk"][name][kk] = _clamp(vv, *SPK_RANGES[kk])
                                elif kk in SPK_BOOLS:
                                    self.p["spk"][name][kk] = bool(vv)
                                else:
                                    bad.append(f"spk.{name}.{kk}")
                    else:
                        raise ValueError
                except (ValueError, TypeError):
                    bad.append(k)
        return bad

    def reset_speaker_tweaks(self):
        with self.lock:
            for d in self.p["spk"].values():
                d.update({"trim_db": 0.0, "delay_ms": 0.0, "invert": False, "mute": False})
            self.p["solo"] = None
            self.p["corr"] = 0.0

    # ------------------------------------------------------------------ commands

    def unmute(self):
        with self.lock:
            self.muted = False
            self.panic = False
            self.fast_up = False
            self.mute_reason = ""

    def mute(self, reason: str = "user", panic: bool = False):
        with self.lock:
            self.muted = True
            self.fast_up = False
            self.panic = panic or self.panic
            self.mute_reason = reason

    def set_duck(self, on: bool):
        """Fade out for a moment (route switches). Coming back is fast (0.4 s), unlike unmuting."""
        with self.lock:
            self.duck = bool(on)
            self.fast_up = (not on) and (not self.muted)

    def touch(self):
        self.last_hb = time.monotonic()

    def open_instantly(self):
        """Offline renders only: skip the fade-in so the first block already has full level."""
        with self.lock:
            self.muted = False
            self.panic = False
            self.gate_fade = 1.0
            self.level_db = min(self.p["master_db"], self.ceiling_db)
            self.amp_prev = db_to_lin(self.level_db)
            self.mute_reason = ""

    def set_user_pos(self, pos):
        p = np.asarray(pos, dtype=float)
        if p.shape != (3,) or not np.all(np.isfinite(p)):
            return
        lo, hi = self.bounds
        with self.lock:
            self.pos_target = np.minimum(np.maximum(p, lo), hi)
            if self.motion is not None and self.motion.get("kind") != "replay_hold":
                self.motion = None

    def start_motion(self, obj, speed: float = 1.0, loop: bool = True):
        with self.lock:
            self.motion = {"obj": obj, "t": 0.0, "speed": _clamp(speed, 0.1, 6.0), "loop": bool(loop),
                           "id": obj.id, "done": False}

    def set_motion_speed(self, speed: float):
        with self.lock:
            if self.motion:
                self.motion["speed"] = _clamp(speed, 0.1, 6.0)

    def stop_motion(self):
        with self.lock:
            self.motion = None

    def start_record(self):
        with self.lock:
            self.rec = {"t": 0.0, "pts": []}
            self.rec_done = None

    def stop_record(self) -> list | None:
        with self.lock:
            pts = self.rec["pts"] if self.rec else None
            self.rec = None
            return pts

    def set_hp(self, hp_hz: float):
        hp = _clamp(hp_hz, 20.0, 2000.0)
        pink = dsp.LoopSource(dsp.pink_loop(20.0, hp, self.pink_seed))
        file_src = None
        if self.file_raw is not None:
            file_src = dsp.LoopSource(dsp.highpass_file(self.file_raw, hp).astype(np.float32))
        with self.lock:
            self.hp_hz = hp
            self.src_pink = pink
            if file_src is not None:
                self.src_file = file_src

    def set_file(self, samples: np.ndarray, name: str):
        raw = np.asarray(samples, dtype=np.float32)
        if raw.ndim != 1 or len(raw) < FS // 2:
            raise ValueError("file is too short (needs at least half a second)")
        src = dsp.LoopSource(dsp.highpass_file(raw, self.hp_hz).astype(np.float32))
        with self.lock:
            self.file_raw = raw
            self.file_name = name
            self.src_file = src

    # ------------------------------------------------------------------ the block

    def _empty_meters(self):
        return {"peak": [-120.0] * self.nout, "rms": [-120.0] * self.nout,
                "gains": [0.0] * self.nout, "rv": None, "re": None}

    def _advance_motion(self, dt: float):
        m = self.motion
        if m is None:
            return
        m["t"] += dt * m["speed"]
        obj = m["obj"]
        finished = (not m["loop"]) and m["t"] >= obj.duration
        p = obj.pos(obj.duration - 1e-6 if finished else m["t"])
        # the orbit is a virtual path around the seat and is allowed to leave the room box
        self.pos_target = p if obj.id == "orbit" else np.clip(p, self.bounds[0], self.bounds[1])
        if finished:
            m["done"] = True
            self.motion = None

    def _object_gains(self, p, pos, r):
        v = pos if np.linalg.norm(pos) >= MIN_RADIUS else self.last_dir * MIN_RADIUS
        if p["law"] == "nearest":
            g = self.vbap.nearest(v)
        else:
            g = self.vbap.gains_spread(v, p["spread_deg"])
        gd = min(1.0, (1.0 / max(r, MIN_RADIUS)) ** p["dist_exp"])
        out = np.zeros(self.nout)
        for g_i, port in zip(g, self.pan_ports):
            out[port] = g_i * gd
        return out

    def process_block(self) -> np.ndarray:
        """Returns (N, nout) float32."""
        t0 = time.perf_counter()
        N = self.N
        dt = N / FS
        with self.lock:
            p = {k: (dict((n, dict(d)) for n, d in v.items()) if k == "spk" else
                     (list(v) if isinstance(v, list) else v)) for k, v in self.p.items()}
            muted, panic, duck = self.muted, self.panic, self.duck
            self._advance_motion(dt)
            if self.rec is not None:
                self.rec["pts"].append([round(self.rec["t"], 4), *[float(x) for x in self.pos_target]])
                self.rec["t"] += dt
                if self.rec["t"] >= MAX_RECORD_S:
                    self.rec_done = self.rec["pts"]
                    self.rec = None
            target = self.pos_target.copy()
            src = self.src_file if (p["src"] == "file" and self.src_file is not None) else self.src_pink

        # position smoothing (30 ms) so a mouse drag does not step
        alpha = 1.0 - math.exp(-dt / 0.03)
        self.pos = self.pos + alpha * (target - self.pos)
        r_now = float(np.linalg.norm(self.pos))
        if r_now >= MIN_RADIUS:
            self.last_dir = self.pos / r_now
        r = max(r_now, MIN_RADIUS)

        # source and burst gate
        s = src.next(N)
        if p["mode"] == "identify" and self._prev_mode != "identify":
            self.id_gate.t = 0
            self.id_t = 0.0
        self._prev_mode = p["mode"]
        if p["mode"] == "identify":
            s = s * self.id_gate.process(N, 1000.0, 600.0)
        else:
            s = s * self.gate.process(N, p["burst_on_ms"], p["burst_off_ms"])

        # target gains per output port
        if p["mode"] == "object":
            g = self._object_gains(p, self.pos, r)
        elif p["mode"] == "manual":
            g = np.zeros(self.nout)
            names = [n for n in p["manual_set"] if n in self.name_to_port]
            for n in names:
                g[self.name_to_port[n]] = 1.0 / math.sqrt(max(len(names), 1))
        else:  # identify: one speaker at a time
            order = [s_.name for s_ in self.panned]
            who = p["identify_speaker"]
            if who is None:
                who = order[int(self.id_t // 1.6) % len(order)]
            self.id_t += dt
            self.id_current = who
            g = np.zeros(self.nout)
            g[self.name_to_port[who]] = 1.0
        base_g = g.copy()

        # per-speaker tweaks
        solo = p["solo"]
        d_user = np.zeros(self.nout)
        for name, d in p["spk"].items():
            port = self.name_to_port[name]
            g[port] *= db_to_lin(d["trim_db"])
            if d["mute"] or (solo is not None and name != solo):
                g[port] = 0.0
            if d["invert"]:
                g[port] = -g[port]
            d_user[port] = d["delay_ms"]
        aligned = p["align"] == "on" or (p["align"] == "auto" and p["out_mode"] == "direct")
        if aligned:
            g = g * self.align_gain
            d_user = d_user + self.align_delay_ms
        g[[i for i, sp in enumerate(self.by_port) if sp is None or sp.role == "sub"]] = 0.0

        # distance cues on the mono source (object mode only)
        if p["mode"] == "object":
            if p["air_ex"] > 0:
                s = self.air.process(s, r, p["air_ex"])
            if p["doppler"]:
                d0 = self.r_prev * p["doppler_scale"] / C_SOUND * FS
                d1 = r * p["doppler_scale"] / C_SOUND * FS
            else:
                d0 = d1 = 0.0
            s = self.doppler_bank.process(s[None, :], d0, d1)[0]
        else:
            self.doppler_bank.process(s[None, :], 0.0, 0.0)
        self.r_prev = r

        # width: per-speaker decorrelation
        dec = self.conv.process(s)
        c = p["corr"]
        x = math.sqrt(1.0 - c) * s[None, :] + math.sqrt(c) * dec

        # gains with a ramp across the block, then per-speaker delay
        ramp = (np.arange(N) + 1.0) / N
        gm = self.g_prev[:, None] + (g - self.g_prev)[:, None] * ramp[None, :]
        y = x * gm
        d_end = np.minimum(d_user, 50.0 + self.align_delay_ms.max()) * FS / 1000.0
        y = self.spk_delay.process(y, self.d_prev, d_end)
        self.g_prev = g
        self.d_prev = d_end

        # master: gate (fade) x level (slew limited)
        tgt_gate = 0.0 if (muted or duck) else 1.0
        up = dt / (0.4 if self.fast_up else self.fade_in_s)
        down = dt / (0.02 if panic else 0.12)
        self.gate_fade += float(np.clip(tgt_gate - self.gate_fade, -down, up))
        if self.gate_fade >= 0.999:
            self.fast_up = False
        gate_amp = math.sin(0.5 * math.pi * self.gate_fade) ** 2
        tgt_level = min(p["master_db"], self.ceiling_db)
        step = float(np.clip(tgt_level - self.level_db, -LEVEL_DOWN_DB_PER_S * dt, LEVEL_UP_DB_PER_S * dt))
        self.level_db += step
        amp = gate_amp * db_to_lin(self.level_db)
        a = self.amp_prev + (amp - self.amp_prev) * ramp
        self.amp_prev = amp
        out = y * a[None, :]
        out[[i for i, sp in enumerate(self.by_port) if sp is None or sp.role == "sub"], :] = 0.0
        over = np.abs(out) > 1.0
        if over.any():
            self.clips += int(over.sum())
            out = np.clip(out, -1.0, 1.0)

        self.t_total += dt
        self._publish(out, g, base_g, r, p)
        self.load = 0.9 * self.load + 0.1 * ((time.perf_counter() - t0) / dt)
        return out.T.astype(np.float32)

    def _publish(self, out, g, base_g, r, p):
        peak = np.max(np.abs(out), axis=1)
        rms = np.sqrt(np.mean(out ** 2, axis=1))
        idx = [s.port for s in self.panned]
        dirs = np.array([s.direction for s in self.panned])
        gv = g[idx]
        rv = re = None
        sabs = np.sum(np.abs(gv))
        if sabs > 1e-6:
            vv = (gv[:, None] * dirs).sum(axis=0)
            if abs(gv.sum()) >= 0.2 * sabs:
                n = np.linalg.norm(vv)
                rv = self._ang(vv / max(gv.sum(), 1e-9)) | {"mag": round(float(min(1.0, n / max(gv.sum(), 1e-9))), 3)}
            re_v = energy_vector(gv, dirs)
            re = self._ang(re_v) | {"mag": round(float(np.linalg.norm(re_v)), 3)}
        self.meters = {
            "peak": [round(lin_to_db(v), 1) for v in peak],
            "rms": [round(lin_to_db(v), 1) for v in rms],
            "gains": [round(float(v), 3) for v in np.abs(g)],
            "signs": [int(np.sign(v)) for v in g],
            "rv": rv, "re": re,
        }

    @staticmethod
    def _ang(v):
        n = float(np.linalg.norm(v))
        if n < 1e-9:
            return {"az": 0.0, "el": 0.0}
        return {"az": round(math.degrees(math.atan2(v[0], v[1])), 1),
                "el": round(math.degrees(math.asin(max(-1.0, min(1.0, v[2] / n)))), 1)}

    # ------------------------------------------------------------------ state for the page

    def snapshot(self) -> dict:
        with self.lock:
            p = {k: v for k, v in self.p.items()}
            pos = [float(v) for v in self.pos]
            r = float(np.linalg.norm(self.pos))
            m = self.motion
            motion = None if m is None else {"id": m["id"], "t": round(m["t"], 2), "speed": m["speed"],
                                             "loop": m["loop"], "duration": round(m["obj"].duration, 2)}
            rec = None if self.rec is None else {"t": round(self.rec["t"], 2)}
            return {
                "params": p, "pos": [round(v, 4) for v in pos], "r": round(r, 3),
                "az": round(math.degrees(math.atan2(pos[0], pos[1])), 1),
                "el": round(math.degrees(math.asin(max(-1, min(1, pos[2] / max(r, 1e-6))))), 1),
                "muted": self.muted, "duck": self.duck, "mute_reason": self.mute_reason,
                "gate": round(self.gate_fade, 3), "level_db": round(self.level_db, 1),
                "ceiling_db": self.ceiling_db, "fade_in_s": self.fade_in_s,
                "hp_hz": self.hp_hz, "file": self.file_name,
                "motion": motion, "recording": rec,
                "meters": self.meters, "load": round(self.load, 3), "clips": self.clips,
                "id_current": self.id_current if p["mode"] == "identify" else None,
            }

    # ------------------------------------------------------------------ running

    def start(self, backend):
        self.backend = backend
        self._run = True
        self._thread = threading.Thread(target=self._loop, name="spatial-lab-engine", daemon=True)
        self._thread.start()

    def _loop(self):
        try:
            while self._run:
                if self.watchdog_s is not None and not self.muted \
                        and time.monotonic() - self.last_hb > self.watchdog_s:
                    self.mute("watchdog: the page stopped responding")
                block = self.process_block()
                self.backend.write(block)
        except Exception as e:  # noqa: BLE001 - report and stop, silence is the safe state
            self.mute("engine stopped: %s" % e, panic=True)
            self.error = e
            self._run = False

    def stop(self):
        self._run = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self.backend:
            self.backend.close()

    error: Exception | None = None

    @property
    def running(self) -> bool:
        return self._run
