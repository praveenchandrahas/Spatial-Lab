"""Offline rendering: run the same engine without a sound card and keep every block.

Used by `spatial-lab render` (8-channel WAV, one WAV per speaker, plots) and by the tests.
The ceiling is lifted to the requested master level, which is safe because nothing is played.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .dsp import FS
from .engine import Engine
from .layout import Layout
from .trajectories import build_presets


def render_path(layout: Layout, cfg: dict, path_id: str | None, seconds: float, speed: float = 1.0,
                master_db: float = -20.0, params: dict | None = None, pos=None) -> dict:
    cfg = dict(cfg)
    cfg["ceiling_db"] = max(cfg["ceiling_db"], master_db)
    eng = Engine(layout, cfg)
    eng.set_params({"burst_on_ms": 0, "master_db": master_db, **(params or {})})
    if path_id:
        eng.start_motion(build_presets(layout.room)[path_id], speed, True)
    elif pos is not None:
        eng.set_user_pos(pos)
        eng.pos = np.asarray(eng.pos_target, dtype=float).copy()
        eng.r_prev = float(np.linalg.norm(eng.pos))
    eng.open_instantly()
    nblocks = int(round(seconds * FS / eng.N))
    audio, t, posn, gains, re_az, re_el, rv_az = [], [], [], [], [], [], []
    for _ in range(nblocks):
        audio.append(eng.process_block())
        m = eng.meters
        t.append(eng.t_total)
        posn.append(eng.pos.copy())
        gains.append(m["gains"])
        re = m["re"] or {"az": float("nan"), "el": float("nan")}
        rv = m["rv"] or {"az": float("nan")}
        re_az.append(re["az"])
        re_el.append(re["el"])
        rv_az.append(rv["az"])
    return {
        "engine": eng, "audio": np.concatenate(audio), "t": np.array(t), "pos": np.array(posn),
        "gains": np.array(gains), "re_az": np.array(re_az), "re_el": np.array(re_el), "rv_az": np.array(rv_az),
        "names": eng.names,
    }


def write_outputs(res: dict, out_dir: str | Path, label: str, plot: bool = True) -> list[Path]:
    from scipy.io import wavfile
    out = Path(out_dir)
    (out / "speakers").mkdir(parents=True, exist_ok=True)
    files = []
    p = out / f"{label}_8ch.wav"
    wavfile.write(p, FS, res["audio"])
    files.append(p)
    for i, n in enumerate(res["names"]):
        if n.startswith("LFE"):
            continue
        q = out / "speakers" / f"{label}_{i + 1}_{n}.wav"
        wavfile.write(q, FS, res["audio"][:, i].copy())
        files.append(q)
    if plot:
        try:
            files.append(_plot(res, out / f"{label}.png", label))
        except ImportError:
            pass
    return files


def _plot(res: dict, path: Path, label: str) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    eng: Engine = res["engine"]
    t = res["t"]
    names = res["names"]
    pos = res["pos"]
    az = np.degrees(np.arctan2(pos[:, 0], pos[:, 1]))
    el = np.degrees(np.arcsin(np.clip(pos[:, 2] / np.maximum(np.linalg.norm(pos, axis=1), 1e-6), -1, 1)))
    fig, ax = plt.subplots(3, 1, figsize=(11, 10), sharex=True)
    ax[0].plot(t, az, label="object azimuth (intended)", lw=1.6)
    ax[0].plot(t, res["re_az"], "--", label="predicted image azimuth, energy vector", lw=1.2)
    ax[0].plot(t, el, label="object elevation (intended)", lw=1.0, alpha=0.7)
    ax[0].plot(t, res["re_el"], "--", label="predicted image elevation, energy vector", lw=1.0, alpha=0.7)
    ax[0].set_ylabel("degrees")
    ax[0].set_title(f"{label}: where the object is vs where the speaker gains put the image (computed, not measured)")
    ax[0].legend(loc="upper right", fontsize=8, ncol=2)
    ax[0].grid(alpha=0.3)
    for i, n in enumerate(names):
        if n.startswith("LFE"):
            continue
        ax[1].plot(t, res["gains"][:, i], label=n, lw=1.2)
    ax[1].set_ylabel("speaker gain (linear)")
    ax[1].set_title("Gain on each speaker (after distance, trims and alignment)")
    ax[1].legend(loc="upper right", fontsize=8, ncol=4)
    ax[1].grid(alpha=0.3)
    a = res["audio"]
    blk = eng.N
    rms = np.sqrt(np.mean(a.reshape(-1, blk, a.shape[1]) ** 2, axis=1))
    for i, n in enumerate(names):
        if n.startswith("LFE"):
            continue
        ax[2].plot(t, 20 * np.log10(np.maximum(rms[:, i], 1e-6)), label=n, lw=1.0)
    ax[2].set_ylim(-90, None)
    ax[2].set_ylabel("dBFS (block RMS)")
    ax[2].set_xlabel("seconds")
    ax[2].set_title("What is sent to each output")
    ax[2].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
