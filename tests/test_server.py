import asyncio
import io
import json
import shutil
import socket
import wave

import aiohttp
import numpy as np
import pytest
from aiohttp.test_utils import TestServer

from spatial_lab.engine import Engine
from spatial_lab.output import NullOut
from spatial_lab.server import create_app


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Harness:
    def __init__(self, layout, cfg):
        self.port = free_port()
        self.cfg = dict(cfg, port=self.port)
        self.engine = Engine(layout, self.cfg)
        self.out = NullOut(layout, self.cfg)
        self.app = create_app(self.engine, layout, self.cfg, self.out)
        self.server = TestServer(self.app, host="127.0.0.1", port=self.port)
        self.base = f"http://127.0.0.1:{self.port}"
        self.hdr = {"Origin": self.base}

    async def __aenter__(self):
        self.out.start()
        self.engine.watchdog_s = None
        self.engine.start(self.out)
        await self.server.start_server()
        self.http = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *a):
        self.engine._run = False
        await self.http.close()
        await self.server.close()

    async def ws(self, headers="default"):
        h = self.hdr if headers == "default" else headers
        return await self.http.ws_connect(f"ws://127.0.0.1:{self.port}/ws", headers=h)

    async def until(self, ws, kind, n=50):
        for _ in range(n):
            m = await ws.receive_json(timeout=3)
            if m.get("t") == kind:
                return m
        raise AssertionError("no " + kind)


def arun(coro):
    return asyncio.run(coro)


def test_index_static_and_mime(layout, cfg):
    async def go():
        async with Harness(layout, cfg) as h:
            r = await h.http.get(h.base + "/")
            assert r.status == 200 and "Spatial-Lab" in await r.text()
            assert "script-src" not in r.headers["Content-Security-Policy"]
            assert "default-src 'self'" in r.headers["Content-Security-Policy"]
            r = await h.http.get(h.base + "/app.js")
            assert r.headers["Content-Type"].startswith("text/javascript")
            r = await h.http.get(h.base + "/vendor/three.module.js")
            assert r.status == 200
            for bad in ("/../server.py", "/%2e%2e/server.py", "/vendor/../../server.py", "/nothing.js"):
                r = await h.http.get(h.base + bad)
                assert r.status in (400, 403, 404), bad
    arun(go())


def test_host_and_origin_checks(layout, cfg):
    async def go():
        async with Harness(layout, cfg) as h:
            r = await h.http.get(h.base + "/", headers={"Host": "evil.example"})
            assert r.status == 403
            r = await h.http.get(h.base + "/", headers={"Origin": "http://evil.example"})
            assert r.status == 403
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await h.ws(headers={"Origin": "http://evil.example"})
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await h.ws(headers={})
            r = await h.http.post(h.base + "/upload", data=b"x" * 2000)           # no Origin
            assert r.status == 403
            r = await h.http.post(h.base + "/upload", data=b"x" * 2000, headers={"Origin": "http://evil.example"})
            assert r.status == 403
    arun(go())


def test_init_message_and_unmute_mute_panic(layout, cfg):
    async def go():
        async with Harness(layout, cfg) as h:
            ws = await h.ws()
            m = await h.until(ws, "init")
            assert m["snap"]["muted"] and m["cfg"]["ceiling_db"] == -40.0
            assert len(m["presets"]) >= 8 and len(m["layout"]["speakers"]) == 8
            await ws.send_json({"t": "cmd", "c": "unmute"})
            await asyncio.sleep(0.2)
            assert not h.engine.muted
            await ws.send_json({"t": "cmd", "c": "panic"})
            await asyncio.sleep(0.2)
            assert h.engine.muted
            await ws.close()
    arun(go())


def test_ceiling_cannot_be_raised_from_page(layout, cfg):
    async def go():
        async with Harness(layout, cfg) as h:
            ws = await h.ws()
            await h.until(ws, "init")
            await ws.send_json({"t": "set", "patch": {"ceiling_db": 0.0, "master_db": 0.0, "bogus": 1}})
            ev = await h.until(ws, "event")
            assert "ceiling_db" in ev["msg"] and "bogus" in ev["msg"]
            assert h.engine.ceiling_db == -40.0 and h.engine.p["master_db"] == -40.0
            await ws.close()
    arun(go())


def test_closing_last_page_mutes(layout, cfg):
    async def go():
        async with Harness(layout, cfg) as h:
            ws = await h.ws()
            await h.until(ws, "init")
            await ws.send_json({"t": "cmd", "c": "unmute"})
            await asyncio.sleep(0.1)
            assert not h.engine.muted
            await ws.close()
            await asyncio.sleep(0.3)
            assert h.engine.muted
    arun(go())


def test_position_and_motion_commands(layout, cfg):
    async def go():
        async with Harness(layout, cfg) as h:
            ws = await h.ws()
            await h.until(ws, "init")
            await ws.send_json({"t": "pos", "p": [1.0, 2.0, 0.5]})
            await ws.send_json({"t": "pos", "p": [999, 999, 999]})                 # clamped into the room
            await asyncio.sleep(0.1)
            hi = h.engine.bounds[1]
            assert np.all(h.engine.pos_target <= hi + 1e-9)
            await ws.send_json({"t": "pos", "p": [float("nan"), 0, 0]})            # ignored
            await ws.send_json({"t": "cmd", "c": "traj", "id": "orbit", "speed": 2.0})
            await asyncio.sleep(0.1)
            assert h.engine.motion is not None and h.engine.motion["speed"] == 2.0
            await ws.send_json({"t": "cmd", "c": "stop_motion"})
            await asyncio.sleep(0.1)
            assert h.engine.motion is None
            await ws.close()
    arun(go())


def test_record_save_replay_delete(layout, cfg):
    async def go():
        async with Harness(layout, cfg) as h:
            ws = await h.ws()
            await h.until(ws, "init")
            await ws.send_json({"t": "cmd", "c": "rec_start"})
            for i in range(30):
                await ws.send_json({"t": "pos", "p": [0.1 * i - 1.0, 1.5, 0.2]})
                await asyncio.sleep(0.03)
            await ws.send_json({"t": "cmd", "c": "rec_stop"})
            m = await h.until(ws, "recs")
            assert m["pending"]
            await ws.send_json({"t": "cmd", "c": "rec_save", "name": "../evil"})
            await ws.send_json({"t": "cmd", "c": "rec_save", "name": "sweep one"})
            m = await h.until(ws, "recs")
            assert [r["name"] for r in m["recordings"]] == ["sweep one"]
            await ws.send_json({"t": "cmd", "c": "replay", "name": "sweep one", "speed": 1.0})
            await asyncio.sleep(0.2)
            assert h.engine.motion is not None
            await ws.send_json({"t": "cmd", "c": "rec_delete", "name": "sweep one"})
            m = await h.until(ws, "recs")
            assert m["recordings"] == []
            await ws.close()
    arun(go())


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_upload_wav_loads_file_source(layout, cfg):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        t = np.arange(44100 * 2) / 44100
        w.writeframes((np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2").tobytes())

    async def go():
        async with Harness(layout, cfg) as h:
            r = await h.http.post(h.base + "/upload?name=tone.wav", data=buf.getvalue(), headers=h.hdr)
            j = await r.json()
            assert r.status == 200 and j["ok"] and abs(j["seconds"] - 2.0) < 0.2
            assert h.engine.file_name == "tone.wav"
            r = await h.http.post(h.base + "/upload?name=junk", data=b"not audio at all" * 200, headers=h.hdr)
            assert r.status == 400
    arun(go())


def test_route_command_on_backend_without_routing(layout, cfg):
    async def go():
        async with Harness(layout, cfg) as h:
            ws = await h.ws()
            await h.until(ws, "init")
            await ws.send_json({"t": "cmd", "c": "route", "mode": "through"})
            ev = await h.until(ws, "event")
            assert "Routing" in ev["msg"] or "Route" in ev["msg"]
            await asyncio.sleep(0.1)
            assert not h.engine.duck
            await ws.close()
    arun(go())
