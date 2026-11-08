import numpy as np

from spatial_lab import dsp
from spatial_lab.vbap import Vbap, energy_vector


def test_vbap_exact_at_speakers_and_power_one(layout):
    dirs = np.array([s.direction for s in layout.panned])
    v = Vbap(dirs)
    for i, d in enumerate(dirs):
        g = v.gains(d)
        assert abs(g[i] - 1.0) < 1e-6
        assert abs(np.sum(g ** 2) - 1.0) < 1e-6
    rng = np.random.default_rng(1)
    for _ in range(200):
        d = rng.normal(size=3)
        d /= np.linalg.norm(d)
        g = v.gains(d)
        assert abs(np.sum(g ** 2) - 1.0) < 1e-6
        assert np.count_nonzero(g > 1e-9) <= 3
        assert (g >= -1e-9).all()


def test_vbap_image_direction_matches_intent_in_front(layout):
    dirs = np.array([s.direction for s in layout.panned])
    v = Vbap(dirs)
    for az in range(-30, 31, 5):
        a = np.radians(az)
        d = np.array([np.sin(a), np.cos(a), 0.05])
        d /= np.linalg.norm(d)
        e = energy_vector(v.gains(d), dirs)
        got = np.degrees(np.arctan2(e[0], e[1]))
        assert abs(got - az) < 6.0, (az, got)


def test_nearest_is_single_speaker(layout):
    dirs = np.array([s.direction for s in layout.panned])
    g = Vbap(dirs).nearest(dirs[0] + 0.01)
    assert np.count_nonzero(g) == 1


def test_pink_loop_spectrum_slope_and_highpass():
    x = dsp.pink_loop(10.0, 200.0)
    X = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / dsp.FS)
    def band(lo, hi):
        return X[(f >= lo) & (f < hi)].sum()
    assert band(20, 100) < 0.02 * band(400, 20000)
    # pink: equal energy per octave, within 3 dB
    o1, o2 = band(1000, 2000), band(4000, 8000)
    assert abs(10 * np.log10(o1 / o2)) < 3.0


def test_delay_bank_fractional_delay():
    bank = dsp.DelayBank(1, 0.1, 512)
    x = np.zeros(2048)
    x[100] = 1.0
    y = []
    for i in range(0, 2048, 512):
        y.append(bank.process(x[None, i:i + 512], 10.0, 10.0)[0])
    y = np.concatenate(y)
    assert abs(int(np.argmax(np.abs(y))) - (100 + 10)) <= 3


def test_decorrelator_kernels_unit_energy_and_low_correlation():
    k = dsp.decorrelator_kernels(2)
    assert np.allclose(np.sum(k ** 2, axis=1), 1.0, atol=0.05)
    x = np.random.default_rng(0).normal(size=48000)
    a = np.convolve(x, k[0])[:48000]
    b = np.convolve(x, k[1])[:48000]
    assert abs(np.corrcoef(a, b)[0, 1]) < 0.15
