"""Vector base amplitude panning (VBAP, Pulkki 1997) over the speaker hull.

The speakers are points on a unit sphere (directions from the listener). Their convex hull
is a set of triangles; a source direction p falls in one triangle and its gains are
g = p . inverse(L), where the rows of L are that triangle's speaker vectors. Gains are then
normalised so that sum(g^2) = 1 (constant power).

There is no speaker below the horizon, so an imaginary speaker is added straight down to
close the hull. Gain that lands on it is dropped and the rest renormalised, which means a
source below the bed layer collapses onto the bed layer without getting quieter.
"""
from __future__ import annotations

import numpy as np

_EPS = 1e-9


def _unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


class Vbap:
    def __init__(self, directions: np.ndarray):
        """directions: (N, 3) unit vectors of the real speakers, in a fixed order."""
        self.dirs = _unit(np.asarray(directions, dtype=float))
        self.n = len(self.dirs)
        nadir = np.array([[0.0, 0.0, -1.0]])
        self.pts = np.vstack([self.dirs, nadir])          # imaginary speaker = index n
        self.tris, self.inv = self._build()

    def _build(self):
        pts = self.pts
        m = len(pts)
        faces = []
        for i in range(m):
            for j in range(i + 1, m):
                for k in range(j + 1, m):
                    a, b, c = pts[i], pts[j], pts[k]
                    nrm = np.cross(b - a, c - a)
                    ln = np.linalg.norm(nrm)
                    if ln < 1e-12:
                        continue
                    nrm = nrm / ln
                    d = pts @ nrm - a @ nrm
                    # supporting plane: every point on one side (points on the sphere, so
                    # a facet is exactly a triple with the rest all on one side)
                    if np.all(d <= 1e-9) or np.all(d >= -1e-9):
                        faces.append((i, j, k))
        faces = self._prune(faces)
        tris, inv = [], []
        for f in faces:
            L = pts[list(f)]
            if abs(np.linalg.det(L)) < 1e-9:
                continue
            tris.append(f)
            inv.append(np.linalg.inv(L))
        if not tris:
            raise ValueError("speaker directions do not enclose a volume")
        return tris, np.array(inv)

    def _prune(self, faces):
        """Cocircular layouts give overlapping facets; keep a non-overlapping set."""
        want = 2 * len(self.pts) - 4
        if len(faces) <= want:
            return faces
        kept = []
        for f in faces:
            c = _unit(self.pts[list(f)].sum(axis=0))
            clash = False
            for g in kept:
                L = self.pts[list(g)]
                if abs(np.linalg.det(L)) < 1e-9:
                    continue
                if np.all(c @ np.linalg.inv(L) >= -1e-6):
                    clash = True
                    break
            if not clash:
                kept.append(f)
        return kept

    def gains(self, direction) -> np.ndarray:
        """Constant-power gains (length n) for one source direction."""
        p = _unit(np.asarray(direction, dtype=float))
        g_all = np.einsum("j,tjk->tk", p, self.inv)        # (T, 3)
        best = None
        best_min = -1e9
        for t in range(len(self.tris)):
            mn = g_all[t].min()
            if mn > best_min:
                best_min, best = mn, t
        g = np.zeros(self.n + 1)
        g_t = np.clip(g_all[best], 0.0, None)
        for idx, v in zip(self.tris[best], g_t):
            g[idx] += v
        g = g[: self.n]                                    # drop the imaginary speaker
        s = np.sqrt(np.sum(g * g))
        if s < 1e-6:                                       # straight down: nearest speaker
            out = np.zeros(self.n)
            out[int(np.argmax(self.dirs @ p))] = 1.0
            return out
        return g / s

    def gains_spread(self, direction, spread_deg: float, taps: int = 6) -> np.ndarray:
        """Source size: power-average of gains over a small cone around the direction."""
        if spread_deg <= 0.01:
            return self.gains(direction)
        p = _unit(np.asarray(direction, dtype=float))
        ref = np.array([0.0, 0.0, 1.0]) if abs(p[2]) < 0.95 else np.array([1.0, 0.0, 0.0])
        u = _unit(np.cross(p, ref))
        v = np.cross(p, u)
        ang = np.radians(spread_deg)
        acc = self.gains(p) ** 2
        for k in range(taps):
            a = 2 * np.pi * k / taps
            q = np.cos(ang) * p + np.sin(ang) * (np.cos(a) * u + np.sin(a) * v)
            acc += self.gains(q) ** 2
        g = np.sqrt(acc / (taps + 1))
        return g / np.sqrt(np.sum(g * g))

    def nearest(self, direction) -> np.ndarray:
        p = _unit(np.asarray(direction, dtype=float))
        out = np.zeros(self.n)
        out[int(np.argmax(self.dirs @ p))] = 1.0
        return out


def energy_vector(gains: np.ndarray, dirs: np.ndarray) -> np.ndarray:
    """Gerzon energy vector rE = sum(g^2 u) / sum(g^2): where the image is expected to appear."""
    g2 = np.asarray(gains) ** 2
    s = g2.sum()
    return (g2[:, None] * dirs).sum(axis=0) / s if s > 0 else np.zeros(3)
