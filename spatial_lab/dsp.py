"""DSP building blocks: noise source, fractional delay, FFT decorrelator, air absorption."""
from __future__ import annotations

import math

import numpy as np
from scipy import signal

FS = 48000
C_SOUND = 343.0   # m/s


def db_to_lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def lin_to_db(x: float, floor: float = -120.0) -> float:
    return floor if x <= 1e-6 else max(floor, 20.0 * math.log10(x))


# --------------------------------------------------------------------------- sources

def _butter_mag(f: np.ndarray, fc: float, order: int, highpass: bool) -> np.ndarray:
    f = np.maximum(f, 1e-9)
    r = (fc / f) if highpass else (f / fc)
    return 1.0 / np.sqrt(1.0 + r ** (2 * order))


def pink_loop(seconds: float, hp_hz: float, seed: int = 1, fs: int = FS, lp_hz: float = 18000.0) -> np.ndarray:
    """A periodic pink-noise buffer (1/f power), high-passed, normalised to unit RMS.

    Built in the frequency domain with random phase, so it loops with no seam."""
    n = int(round(seconds * fs))
    rng = np.random.default_rng(seed)
    nf = n // 2 + 1
    f = np.arange(nf) * fs / n
    mag = np.zeros(nf)
    mag[1:] = 1.0 / np.sqrt(f[1:])
    mag *= _butter_mag(f, hp_hz, 4, True) * _butter_mag(f, lp_hz, 8, False)
    mag[0] = 0.0
    ph = rng.uniform(0, 2 * np.pi, nf)
    ph[0] = 0.0
    if n % 2 == 0:
        ph[-1] = 0.0
    x = np.fft.irfft(mag * np.exp(1j * ph), n)
    return (x / np.sqrt(np.mean(x ** 2))).astype(np.float64)


def highpass_file(x: np.ndarray, hp_hz: float, fs: int = FS) -> np.ndarray:
    """Zero-phase 4th-order high-pass for a decoded file, then unit RMS."""
    if hp_hz > 0:
        sos = signal.butter(2, hp_hz, btype="highpass", fs=fs, output="sos")
        x = signal.sosfiltfilt(sos, x)
    rms = float(np.sqrt(np.mean(x ** 2)))
    return (x / rms if rms > 1e-9 else x).astype(np.float64)


class LoopSource:
    """Plays a buffer in a loop."""

    def __init__(self, buf: np.ndarray):
        self.buf = np.ascontiguousarray(buf)         # float32 is fine for long files
        self.pos = 0

    def next(self, n: int) -> np.ndarray:
        L = len(self.buf)
        if self.pos + n <= L:
            out = self.buf[self.pos:self.pos + n]
            self.pos = (self.pos + n) % L
            return out.astype(np.float64)
        idx = (self.pos + np.arange(n)) % L
        self.pos = (self.pos + n) % L
        return self.buf[idx].astype(np.float64)


class BurstGate:
    """On/off envelope with raised-cosine edges. on_ms == 0 means always on."""

    def __init__(self, fs: int = FS, ramp_ms: float = 12.0):
        self.fs = fs
        self.ramp = max(1, int(ramp_ms * fs / 1000))
        self.t = 0               # samples into the cycle
        self.level = 1.0

    def process(self, n: int, on_ms: float, off_ms: float) -> np.ndarray:
        if on_ms <= 0:
            self.t = 0
            return np.ones(n)
        on = max(int(on_ms * self.fs / 1000), 2 * self.ramp)
        off = int(max(off_ms, 0) * self.fs / 1000)
        cyc = on + off
        t = (self.t + np.arange(n)) % cyc
        env = np.ones(n)
        up = t < self.ramp
        env[up] = 0.5 - 0.5 * np.cos(np.pi * t[up] / self.ramp)
        dn = (t >= on - self.ramp) & (t < on)
        env[dn] = 0.5 + 0.5 * np.cos(np.pi * (t[dn] - (on - self.ramp)) / self.ramp)
        env[t >= on] = 0.0
        self.t = int((self.t + n) % cyc)
        return env


# --------------------------------------------------------------------------- fractional delay

class DelayBank:
    """Per-channel delay lines with cubic (4-point Lagrange) interpolation.

    The read position inside a block moves linearly from d_start to d_end (in samples), which
    is also how Doppler is made. A constant base delay of 2 samples keeps the 4-point read
    behind the write head, so every channel gets the same 0.04 ms of latency."""

    BASE = 2.0

    def __init__(self, channels: int, max_delay_s: float, block: int, fs: int = FS):
        self.ch = channels
        self.fs = fs
        need = int(max_delay_s * fs) + block + 16
        self.L = 1 << int(math.ceil(math.log2(need)))
        self.buf = np.zeros((channels, self.L))
        self.w = 0
        self.max = max_delay_s * fs

    def process(self, x: np.ndarray, d_start, d_end) -> np.ndarray:
        """x: (ch, n); delays in samples, scalars or (ch,) arrays."""
        ch, n = x.shape
        d0 = np.broadcast_to(np.asarray(d_start, dtype=float), (ch,))
        d1 = np.broadcast_to(np.asarray(d_end, dtype=float), (ch,))
        d0 = np.clip(d0, 0.0, self.max) + self.BASE
        d1 = np.clip(d1, 0.0, self.max) + self.BASE
        widx = (self.w + np.arange(n)) % self.L
        self.buf[:, widx] = x
        ramp = (np.arange(n) + 1.0) / n
        d = d0[:, None] + (d1 - d0)[:, None] * ramp[None, :]          # (ch, n)
        pos = (self.w + np.arange(n))[None, :] - d
        i = np.floor(pos).astype(np.int64)
        mu = pos - i
        rows = np.arange(ch)[:, None]
        L = self.L
        s_m1 = self.buf[rows, (i - 1) % L]
        s_0 = self.buf[rows, i % L]
        s_1 = self.buf[rows, (i + 1) % L]
        s_2 = self.buf[rows, (i + 2) % L]
        c_m1 = -mu * (mu - 1.0) * (mu - 2.0) / 6.0
        c_0 = (mu + 1.0) * (mu - 1.0) * (mu - 2.0) / 2.0
        c_1 = -(mu + 1.0) * mu * (mu - 2.0) / 2.0
        c_2 = (mu + 1.0) * mu * (mu - 1.0) / 6.0
        out = c_m1 * s_m1 + c_0 * s_0 + c_1 * s_1 + c_2 * s_2
        self.w = (self.w + n) % self.L
        return out


# --------------------------------------------------------------------------- decorrelation

def decorrelator_kernels(channels: int, taps: int = 2048, tau_ms: float = 12.0, seed: int = 7,
                         fs: int = FS) -> np.ndarray:
    """Random-phase, near-all-pass FIR filters with a short decay, unit energy each."""
    rng = np.random.default_rng(seed)
    nf = taps // 2 + 1
    k = np.zeros((channels, taps))
    t = np.arange(taps)
    win = np.exp(-t / (tau_ms * 1e-3 * fs))
    for c in range(channels):
        ph = rng.uniform(0, 2 * np.pi, nf)
        ph[0] = 0.0
        ph[-1] = 0.0
        h = np.fft.irfft(np.exp(1j * ph), taps) * win
        k[c] = h / np.sqrt(np.sum(h ** 2))
    return k


class FFTConvolver:
    """Overlap-save block convolution of one signal with several kernels (one per channel)."""

    def __init__(self, kernels: np.ndarray, block: int):
        self.ch, self.M = kernels.shape
        self.N = block
        need = self.M - 1 + block
        self.F = 1 << int(math.ceil(math.log2(need)))
        self.H = np.fft.rfft(kernels, self.F, axis=1)
        self.hist = np.zeros(self.M - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        """x: (N,) mono -> (ch, N)"""
        xx = np.concatenate([self.hist, x])
        X = np.fft.rfft(xx, self.F)
        y = np.fft.irfft(X[None, :] * self.H, self.F, axis=1)
        self.hist = xx[-(self.M - 1):]
        return y[:, self.M - 1:self.M - 1 + self.N]


# --------------------------------------------------------------------------- air absorption

ABSORB_DB_PER_M_10K = 0.13     # approximate, at 10 kHz, 20 C, 50 % RH (ISO 9613-1 order of magnitude)


class AirAbsorption:
    """One-pole low-pass whose loss at 10 kHz equals (dB/m x metres x exaggeration)."""

    def __init__(self, fs: int = FS):
        self.fs = fs
        self.zi = np.zeros(1)

    @staticmethod
    def cutoff(distance_m: float, exaggeration: float) -> float:
        a_db = ABSORB_DB_PER_M_10K * max(distance_m, 0.0) * max(exaggeration, 0.0)
        if a_db < 0.02:
            return 24000.0
        r = 10.0 ** (a_db / 10.0) - 1.0
        return min(24000.0, 10000.0 / math.sqrt(r))

    def process(self, x: np.ndarray, distance_m: float, exaggeration: float) -> np.ndarray:
        fc = self.cutoff(distance_m, exaggeration)
        if fc >= 23999:
            self.zi[:] = 0.0
            return x
        p = math.exp(-2.0 * math.pi * fc / self.fs)
        y, self.zi = signal.lfilter([1.0 - p], [1.0, -p], x, zi=self.zi)
        return y
