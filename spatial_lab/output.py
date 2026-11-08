"""Audio output backends.

* PipeWireOut: pw-cat plays raw 8-channel float32 from a pipe. The stream is created with
  autoconnect off and linked by hand with pw-link, which is how Roomtone's tone-test.sh does it:
    direct  -> the USB interface sink directly (no CamillaDSP, no bass EQ, no delays/trims)
    through -> camilladsp-capture (the real system, as mpv would use it)
* WavOut: writes the blocks to an 8-channel WAV (offline renders and tests).
* NullOut: discards audio, paced in real time (development without PipeWire).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time

import numpy as np

from .dsp import FS
from .layout import Layout

KNOWN_POSITIONS = {"FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR", "FLC", "FRC", "RC", "BL", "BR", "TFL", "TFR"}
STREAM_NODE = "spatial-lab"


class OutputError(RuntimeError):
    pass


class NullOut:
    name = "null"
    can_route = False

    def __init__(self, layout: Layout, cfg: dict, realtime: bool = True):
        self.layout = layout
        self.realtime = realtime
        self._next = None
        self.route = "direct"

    def start(self):
        self._next = time.monotonic()

    def write(self, block: np.ndarray):
        if self.realtime:
            self._next += block.shape[0] / FS
            delay = self._next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                self._next = time.monotonic()

    def set_route(self, mode: str) -> dict:
        self.route = mode
        return {"route": mode, "warnings": ["null backend: nothing is played"]}

    def status(self) -> dict:
        return {"backend": "null", "route": self.route, "alive": True, "warnings": []}

    def close(self):
        pass


class WavOut:
    name = "wav"
    can_route = False

    def __init__(self, layout: Layout, cfg: dict, path: str):
        self.layout = layout
        self.path = path
        self.blocks: list[np.ndarray] = []
        self.route = "direct"

    def start(self):
        pass

    def write(self, block: np.ndarray):
        self.blocks.append(block.copy())

    def set_route(self, mode: str) -> dict:
        self.route = mode
        return {"route": mode, "warnings": []}

    def status(self) -> dict:
        return {"backend": "wav", "route": self.route, "alive": True, "warnings": []}

    def data(self) -> np.ndarray:
        return np.concatenate(self.blocks) if self.blocks else np.zeros((0, self.layout.n_out), np.float32)

    def close(self):
        if self.blocks:
            from scipy.io import wavfile
            wavfile.write(self.path, FS, self.data().astype(np.float32))


def _run(cmd: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def pw_ports(direction: str) -> list[str]:
    """direction 'o' (output ports) or 'i' (input ports): 'node:port' names."""
    r = _run(["pw-link", f"-{direction}"])
    if r.returncode != 0:
        raise OutputError(f"pw-link -{direction} failed: {r.stderr.strip()}")
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def pw_links() -> list[tuple[str, str]]:
    """(output port, input port) for every link."""
    r = _run(["pw-link", "-l"])
    if r.returncode != 0:
        raise OutputError(f"pw-link -l failed: {r.stderr.strip()}")
    links, cur = [], None
    for ln in r.stdout.splitlines():
        if not ln.strip():
            continue
        if not ln[0].isspace():
            cur = ln.strip()
        elif cur is not None:
            m = re.match(r"\s*\|->\s*(.+)$", ln)
            if m:
                links.append((cur, m.group(1).strip()))
    return links


class PipeWireOut:
    name = "pipewire"
    can_route = True

    def __init__(self, layout: Layout, cfg: dict):
        self.layout = layout
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.route: str | None = None
        self.pn = layout.port_names

    # ---- naming
    def _out(self, i: int) -> str:
        return f"{STREAM_NODE}:output_{self._pos(i)}"

    def _pos(self, i: int) -> str:
        names = self.pn
        return names[i] if all(n in KNOWN_POSITIONS for n in names) else f"AUX{i}"

    def _direct_target(self, i: int) -> str:
        return f"{self.layout.sink}:playback_{self.pn[i]}"

    def _cdsp_target(self, i: int) -> str:
        return f"{self.layout.capture_node}:input_{i + 1}"

    # ---- lifecycle
    def start(self):
        for tool in ("pw-cat", "pw-link"):
            if shutil.which(tool) is None:
                raise OutputError(f"{tool} not found. Install pipewire-bin (sudo apt install pipewire-bin).")
        n = self.layout.n_out
        cmap = ",".join(self._pos(i) for i in range(n))
        props = "{ node.name = %s node.autoconnect = false media.name = Spatial-Lab }" % STREAM_NODE
        cmd = ["pw-cat", "--playback", "--raw", "--rate", str(FS), "--channels", str(n), "--format", "f32",
               "--channel-map", cmap, "--latency", f"{int(self.cfg.get('pw_latency_ms', 40))}ms",
               "-P", props, "-"]
        self._err = tempfile.TemporaryFile()           # kept so a failure can be explained
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=self._err, bufsize=0)
        want = {self._out(i) for i in range(n)}
        t0 = time.monotonic()
        # the stream only shows up in the graph once data flows: feed silence while waiting
        silence = np.zeros((self.cfg["block"], n), dtype=np.float32).tobytes()
        while time.monotonic() - t0 < 6.0:
            if self.proc.poll() is not None:
                self._err.seek(0)
                raise OutputError("pw-cat exited: " + self._err.read().decode(errors="replace").strip())
            try:
                self.proc.stdin.write(silence)
            except BrokenPipeError:
                raise OutputError("pw-cat closed its input")
            if want <= set(pw_ports("o")):
                break
            time.sleep(0.05)
        else:
            raise OutputError("the Spatial-Lab stream did not appear in the PipeWire graph")
        self._unlink_all()
        self.set_route("direct")

    def write(self, block: np.ndarray):
        try:
            self.proc.stdin.write(block.tobytes())
        except (BrokenPipeError, ValueError, AttributeError) as e:
            raise OutputError("pw-cat is gone: %s" % e)

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                self.proc.kill()
            self.proc = None

    # ---- routing
    def _unlink_all(self):
        for out, inp in pw_links():
            if out.startswith(STREAM_NODE + ":"):
                _run(["pw-link", "-d", out, inp])

    def set_route(self, mode: str) -> dict:
        if mode not in ("direct", "through"):
            raise OutputError("route must be direct or through")
        n = self.layout.n_out
        inputs = set(pw_ports("i"))
        target = self._direct_target if mode == "direct" else self._cdsp_target
        missing = [target(i) for i in range(n) if target(i) not in inputs]
        if missing:
            what = "the USB interface" if mode == "direct" else "CamillaDSP (is camilladsp.service running?)"
            raise OutputError(f"cannot route to {what}; missing ports, first: {missing[0]}")
        self._unlink_all()
        for i in range(n):
            r = _run(["pw-link", self._out(i), target(i)])
            if r.returncode != 0 and "exists" not in r.stderr.lower():
                raise OutputError(f"pw-link {self._out(i)} -> {target(i)}: {r.stderr.strip()}")
        self.route = mode
        return {"route": mode, "warnings": self._warnings(mode)}

    def _warnings(self, mode: str) -> list[str]:
        w = []
        try:
            outs = pw_ports("o")
            if any(p.startswith("mpv:") for p in outs):
                w.append("mpv is playing; stop it, or both will play together")
            if mode == "through":
                links = pw_links()
                cd = self.layout.playback_node + ":"
                if not any(o.startswith(cd) and i.startswith(self.layout.sink + ":") for o, i in links):
                    w.append("CamillaDSP's output is not linked to the interface: expect silence")
        except OutputError as e:
            w.append(str(e))
        return w

    def status(self) -> dict:
        st = {"backend": "pipewire", "route": self.route,
              "alive": self.proc is not None and self.proc.poll() is None, "warnings": []}
        try:
            ins, outs = set(pw_ports("i")), pw_ports("o")
            st["sink_present"] = any(p.startswith(self.layout.sink + ":") for p in ins)
            st["cdsp_present"] = any(p.startswith(self.layout.capture_node + ":") for p in ins)
            st["mpv_present"] = any(p.startswith("mpv:") for p in outs)
            if self.route:
                st["warnings"] = self._warnings(self.route)
        except Exception as e:  # noqa: BLE001
            st["warnings"] = [str(e)]
        return st


def make_output(kind: str, layout: Layout, cfg: dict, wav_path: str | None = None):
    if kind == "pipewire":
        return PipeWireOut(layout, cfg)
    if kind == "null":
        return NullOut(layout, cfg)
    if kind == "wav":
        return WavOut(layout, cfg, wav_path or "spatial-lab.wav")
    raise OutputError(f"unknown backend {kind}")
