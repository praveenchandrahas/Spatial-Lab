import json
import subprocess

from spatial_lab import output


def _dump(order):
    """pw-dump of a stream whose port.id i carries the channel named order[i] (ports listed shuffled)."""
    objs = [{"id": 90, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": output.STREAM_NODE}}}]
    for i, ch in reversed(list(enumerate(order))):
        objs.append({"id": 100 + i, "type": "PipeWire:Interface:Port",
                     "info": {"direction": "output", "props": {"node.id": 90, "port.id": i, "port.name": "output_" + ch}}})
    objs.append({"id": 200, "type": "PipeWire:Interface:Port",   # another node's port must be ignored
                 "info": {"direction": "output", "props": {"node.id": 7, "port.id": 0, "port.name": "output_FL"}}})
    return json.dumps(objs)


def _fake(monkeypatch, text, rc=0):
    monkeypatch.setattr(output, "_run", lambda cmd, timeout=3.0: subprocess.CompletedProcess(cmd, rc, text, ""))


def test_ports_follow_port_id_not_name(monkeypatch):
    # pw-cat labelled channel 2 'FC' (WAV order) although the engine's channel 2 is the front-left height
    wav_order = ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"]
    _fake(monkeypatch, _dump(wav_order))
    got = output.stream_ports(output.STREAM_NODE)
    assert got == ["spatial-lab:output_" + c for c in wav_order]


def test_ports_none_without_pwdump(monkeypatch):
    _fake(monkeypatch, "", rc=1)
    assert output.stream_ports(output.STREAM_NODE) is None
    _fake(monkeypatch, "[]")
    assert output.stream_ports(output.STREAM_NODE) is None
