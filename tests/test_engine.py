import time

import numpy as np

from spatial_lab.engine import Engine
from spatial_lab.render import render_path


def run(eng, blocks):
    return np.concatenate([eng.process_block() for _ in range(blocks)])


def idx(eng, name):
    return eng.name_to_port[name]


def steady(eng, blocks=40):
    eng.open_instantly()
    run(eng, 30)  # let level slew and gains settle
    return run(eng, blocks)


def rms_db(x):
    return 20 * np.log10(max(float(np.sqrt(np.mean(x ** 2))), 1e-9))


def test_starts_muted_and_silent(engine):
    assert engine.muted
    assert np.max(np.abs(run(engine, 10))) == 0.0


def test_ceiling_clamps_master(engine):
    engine.set_params({"master_db": -10.0})
    assert engine.p["master_db"] == -40.0
    engine.set_params({"master_db": -200.0})
    assert engine.p["master_db"] == -90.0


def test_ceiling_not_exceeded_in_signal(layout, cfg):
    cfg = dict(cfg, ceiling_db=-30.0)
    e = Engine(layout, cfg)
    e.set_params({"master_db": 0.0, "burst_on_ms": 0, "corr": 0.0})
    e.set_user_pos(list(layout.speakers[0].position * 0.9))
    y = steady(e)
    # Total RMS over all outputs stays at or below the ceiling (pink noise peaks sit ~13 dB above RMS).
    assert rms_db(y) + 10 * np.log10(y.shape[1]) <= -30.0 + 0.5


def test_unmute_fades_in(engine):
    engine.set_params({"burst_on_ms": 0})
    engine.unmute()
    first = rms_db(run(engine, 4))
    later = rms_db(run(engine, 400))
    assert first < later - 10


def test_lfe_always_silent(engine, layout):
    engine.set_params({"burst_on_ms": 0, "mode": "manual", "manual_set": [s.name for s in layout.panned]})
    y = steady(engine)
    lfe = [s.port for s in layout.speakers if s.role == "sub"][0]
    assert np.max(np.abs(y[:, lfe])) == 0.0


def test_panic_silences_quickly(engine):
    engine.set_params({"burst_on_ms": 0})
    y = steady(engine)
    assert np.max(np.abs(y)) > 0
    engine.mute("panic", panic=True)
    run(engine, 4)                                    # 0.02 s fade is shorter than 2 blocks
    assert np.max(np.abs(run(engine, 4))) == 0.0


def test_object_at_speaker_goes_to_that_speaker(engine, layout):
    fl = next(s for s in layout.speakers if s.name == "FL")
    engine.set_params({"burst_on_ms": 0, "align": "off"})
    engine.set_user_pos(list(fl.direction * 2.0))
    y = steady(engine)
    levels = {s.name: rms_db(y[:, s.port]) for s in layout.panned}
    assert levels["FL"] > max(v for k, v in levels.items() if k != "FL") + 25


def test_invert_flips_sign(layout, cfg):
    def render(inv):
        e = Engine(layout, cfg)
        e.set_params({"burst_on_ms": 0, "mode": "manual", "manual_set": ["FL"], "align": "off",
                      "spk": {"FL": {"invert": inv}}})
        return steady(e, 10)[:, e.name_to_port["FL"]]
    a, b = render(False), render(True)
    assert np.allclose(a, -b, atol=1e-6)


def test_speaker_delay_shifts_signal(layout, cfg):
    def render(delay):
        e = Engine(layout, cfg)
        e.set_params({"burst_on_ms": 0, "mode": "manual", "manual_set": ["FL"], "align": "off",
                      "spk": {"FL": {"delay_ms": delay}}})
        e.open_instantly()
        return np.concatenate([e.process_block() for _ in range(60)])[:, e.name_to_port["FL"]]
    a, b = render(0.0), render(2.0)       # 96 samples
    n = len(a) - 400
    best = max(range(80, 112), key=lambda lag: float(np.dot(a[:n], b[lag:lag + n])))
    assert 94 <= best <= 98


def test_decorrelation_lowers_correlation_between_pair(layout, cfg):
    def corr(c):
        e = Engine(layout, cfg)
        e.set_params({"burst_on_ms": 0, "mode": "manual", "manual_set": ["FL", "FR"], "align": "off", "corr": c})
        y = steady(e, 100)
        return abs(np.corrcoef(y[:, e.name_to_port["FL"]], y[:, e.name_to_port["FR"]])[0, 1])
    assert corr(0.0) > 0.98
    assert corr(1.0) < 0.3


def test_alignment_orders_arrival_by_distance(engine, layout):
    far = max(layout.panned, key=lambda s: s.distance_m)
    near = min(layout.panned, key=lambda s: s.distance_m)
    assert engine.align_delay_ms[far.port] == 0.0
    assert engine.align_delay_ms[near.port] > 0.0
    assert engine.align_gain[near.port] < engine.align_gain[far.port] == 1.0


def test_identify_cycles_through_speakers(engine, layout):
    engine.set_params({"mode": "identify", "burst_on_ms": 0})
    engine.open_instantly()
    seen = set()
    for _ in range(4000):
        y = engine.process_block()
        loud = np.where(np.max(np.abs(y), axis=0) > 1e-5)[0]
        if len(loud) == 1:
            seen.add(int(loud[0]))
        if len(seen) == len(layout.panned):
            break
    assert seen == {s.port for s in layout.panned}


def test_distance_level_falls_with_distance(layout, cfg):
    def lvl(r):
        e = Engine(layout, cfg)
        e.set_params({"burst_on_ms": 0, "align": "off", "dist_exp": 1.0, "air_ex": 0.0})
        e.set_user_pos([0.0, r, 0.1])
        return rms_db(steady(e))
    assert lvl(1.0) - lvl(2.0) > 4.5


def test_watchdog_mutes_when_page_goes_silent(layout, cfg):
    class Sink:
        def write(self, b):
            time.sleep(0.001)

        def close(self):
            pass
    e = Engine(layout, cfg)
    e.watchdog_s = 0.15
    e.unmute()
    e.touch()
    e.start(Sink())
    time.sleep(0.5)
    e.stop()
    assert e.muted
    assert "watchdog" in e.mute_reason


def test_offline_render_shapes_and_lfe_silent(layout, cfg):
    res = render_path(layout, cfg, "lr_front", 2.0, 1.0, -40.0, {})
    a = res["audio"]
    assert a.shape[1] == layout.n_out and a.shape[0] > 40000
    assert np.isfinite(a).all() and np.max(np.abs(a)) <= 1.0
    lfe = [s.port for s in layout.speakers if s.role == "sub"][0]
    assert np.max(np.abs(a[:, lfe])) == 0.0
