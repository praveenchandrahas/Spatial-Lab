"""Local control server: serves the page and speaks JSON over one WebSocket.

Listens on 127.0.0.1 only. Every request must carry a Host of 127.0.0.1:<port> or
localhost:<port>, and WebSocket and POST requests must also carry a matching Origin, so a web
page from anywhere else cannot drive the amplifier (cross-site WebSocket hijacking, DNS rebinding).
The level ceiling is not settable from here.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import numpy as np
from aiohttp import web, WSMsgType

from .engine import Engine
from .output import OutputError
from .trajectories import (build_presets, delete_recording, list_recordings, load_recording,
                           save_recording, RecordedTrajectory)

# Explicit types: the system mime table may lack .js, and nosniff makes browsers refuse such module scripts.
CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".txt": "text/plain", ".md": "text/plain"}

WEB_DIR = Path(__file__).parent / "web"
MAX_UPLOAD = 200 * 1024 * 1024
MAX_FILE_SECONDS = 300
TICK_S = 0.04


def decode_audio(data: bytes) -> np.ndarray:
    """Any audio ffmpeg can read -> mono float32 at 48 kHz (first five minutes)."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", "pipe:0", "-vn", "-t", str(MAX_FILE_SECONDS),
           "-f", "f32le", "-ac", "1", "-ar", "48000", "pipe:1"]
    try:
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=180)
    except FileNotFoundError:
        raise ValueError("ffmpeg is not installed (sudo apt install ffmpeg)")
    except subprocess.TimeoutExpired:
        raise ValueError("decoding took too long")
    if r.returncode != 0 or not r.stdout:
        raise ValueError("could not decode that file: " + r.stderr.decode(errors="replace")[:200])
    return np.frombuffer(r.stdout, dtype="<f4").copy()


def create_app(engine: Engine, layout, cfg: dict, output) -> web.Application:
    port = int(cfg["port"])
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}
    presets = build_presets(layout.room)
    clients: set[web.WebSocketResponse] = set()
    state = {"pending_rec": None, "out_status": output.status(), "err_sent": False}

    csp = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
           f"connect-src 'self' ws://127.0.0.1:{port} ws://localhost:{port}; "
           "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    @web.middleware
    async def guard(request: web.Request, handler):
        if request.headers.get("Host", "") not in hosts:
            raise web.HTTPForbidden(text="unexpected Host header")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in origins:
            raise web.HTTPForbidden(text="unexpected Origin")
        if (request.path == "/ws" or request.method != "GET") and origin is None:
            raise web.HTTPForbidden(text="Origin required")
        return await handler(request)

    async def add_headers(request, response):
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"

    async def static(request):
        rel = request.match_info["name"]
        f = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in f.parents or not f.is_file() or f.suffix not in CONTENT_TYPES:
            raise web.HTTPNotFound()
        return web.FileResponse(f, headers={"Content-Type": CONTENT_TYPES[f.suffix] + "; charset=utf-8"})

    async def index(request):
        return web.FileResponse(WEB_DIR / "index.html")

    def init_message() -> dict:
        return {
            "t": "init",
            "layout": layout.to_public(),
            "presets": [{"id": t.id, "label": t.label, "period": t.period, "note": t.note} for t in presets.values()],
            "recordings": list_recordings(),
            "cfg": {"ceiling_db": engine.ceiling_db, "fade_in_s": engine.fade_in_s, "hp_hz": engine.hp_hz},
            "backend": output.name, "can_route": output.can_route,
            "out": state["out_status"],
            "snap": engine.snapshot(),
        }

    async def broadcast(obj: dict):
        if not clients:
            return
        s = json.dumps(obj)
        await asyncio.gather(*(ws.send_str(s) for ws in list(clients)), return_exceptions=True)

    async def event(msg: str, level: str = "info", **extra):
        await broadcast({"t": "event", "level": level, "msg": msg, **extra})

    async def do_route(mode: str):
        loop = asyncio.get_running_loop()
        engine.set_duck(True)
        await asyncio.sleep(0.25)               # let the fade-out finish before any link changes
        try:
            info = await loop.run_in_executor(None, output.set_route, mode)
            engine.set_params({"out_mode": mode})
            state["out_status"] = output.status()
            msg = "Routing: " + ("through CamillaDSP" if mode == "through" else "direct to the interface")
            await event(msg, "warn" if info.get("warnings") else "info", warnings=info.get("warnings", []))
        except OutputError as e:
            await event(f"Route not changed: {e}", "error")
        finally:
            engine.set_duck(False)

    async def handle(ws, d: dict):
        t = d.get("t")
        if t == "hb":
            return
        if t == "set" and isinstance(d.get("patch"), dict):
            patch = d["patch"]
            bad = engine.set_params(patch)
            if bad:
                await event("Ignored: " + ", ".join(bad), "warn")
        elif t == "pos" and isinstance(d.get("p"), list) and len(d["p"]) == 3:
            engine.set_user_pos(d["p"])
        elif t == "cmd":
            await command(d)

    async def command(d: dict):
        c = d.get("c")
        loop = asyncio.get_running_loop()
        if c == "unmute":
            engine.unmute()
        elif c == "mute":
            engine.mute("user")
        elif c == "panic":
            engine.mute("panic", panic=True)
        elif c == "route" and d.get("mode") in ("direct", "through"):
            await do_route(d["mode"])
        elif c == "traj":
            obj = presets.get(d.get("id"))
            if obj is None:
                await event("Unknown path", "warn")
                return
            engine.start_motion(obj, float(d.get("speed", 1.0)), bool(d.get("loop", True)))
        elif c == "stop_motion":
            engine.stop_motion()
        elif c == "motion_speed":
            engine.set_motion_speed(float(d.get("speed", 1.0)))
        elif c == "rec_start":
            engine.stop_motion()
            engine.start_record()
            state["pending_rec"] = None
        elif c == "rec_stop":
            pts = engine.stop_record()
            await finish_recording(pts)
        elif c == "rec_save":
            pts = state["pending_rec"]
            if not pts:
                await event("Nothing recorded to save", "warn")
                return
            try:
                name = save_recording(str(d.get("name", "")), pts)
            except ValueError as e:
                await event(str(e), "warn")
                return
            state["pending_rec"] = None
            await broadcast({"t": "recs", "recordings": list_recordings(), "pending": None})
            await event(f"Saved recording '{name}'")
        elif c == "rec_discard":
            state["pending_rec"] = None
            await broadcast({"t": "recs", "recordings": list_recordings(), "pending": None})
        elif c == "replay":
            try:
                obj = load_recording(str(d.get("name", "")))
            except (ValueError, OSError):
                await event("Could not load that recording", "warn")
                return
            engine.start_motion(obj, float(d.get("speed", 1.0)), bool(d.get("loop", False)))
        elif c == "replay_pending":
            pts = state["pending_rec"]
            if pts and len(pts) > 1:
                engine.start_motion(RecordedTrajectory("(unsaved)", pts), float(d.get("speed", 1.0)),
                                    bool(d.get("loop", False)))
        elif c == "rec_delete":
            try:
                delete_recording(str(d.get("name", "")))
            except ValueError:
                return
            await broadcast({"t": "recs", "recordings": list_recordings(), "pending": None})
        elif c == "hp":
            await loop.run_in_executor(None, engine.set_hp, float(d.get("hz", engine.hp_hz)))
            await event(f"Source high-pass set to {engine.hp_hz:.0f} Hz")
        elif c == "reset_tweaks":
            engine.reset_speaker_tweaks()
        else:
            await event("Unknown command", "warn")

    async def finish_recording(pts):
        if pts and len(pts) > 2:
            state["pending_rec"] = pts
            dur = pts[-1][0] - pts[0][0]
            await broadcast({"t": "recs", "recordings": list_recordings(),
                             "pending": {"duration": round(dur, 2)}})
        else:
            await event("Recording was too short", "warn")

    async def ws_handler(request):
        ws = web.WebSocketResponse(max_msg_size=64 * 1024)
        await ws.prepare(request)
        clients.add(ws)
        engine.touch()
        await ws.send_str(json.dumps(init_message()))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    engine.touch()
                    try:
                        d = json.loads(msg.data)
                        if isinstance(d, dict):
                            await handle(ws, d)
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:  # noqa: BLE001 - never let one bad message kill the socket
                        await event(f"Error: {e}", "error")
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            clients.discard(ws)
            if not clients:
                engine.mute("the page was closed")
        return ws

    async def upload(request):
        loop = asyncio.get_running_loop()
        name = (request.query.get("name") or "file")[:80]
        data = await request.read()
        if len(data) < 1000:
            return web.json_response({"ok": False, "error": "file is empty or too small"}, status=400)
        try:
            samples = await loop.run_in_executor(None, decode_audio, data)
            await loop.run_in_executor(None, engine.set_file, samples, name)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        await event(f"Loaded '{name}' ({len(samples) / 48000:.1f} s)")
        return web.json_response({"ok": True, "seconds": round(len(samples) / 48000, 1)})

    async def state_json(request):
        return web.json_response(init_message())

    async def ticker(app):
        async def run():
            loop = asyncio.get_running_loop()
            last_status = 0.0
            while True:
                await asyncio.sleep(TICK_S)
                now = time.monotonic()
                if now - last_status > 2.5 and output.can_route:
                    last_status = now
                    try:
                        state["out_status"] = await loop.run_in_executor(None, output.status)
                    except Exception:  # noqa: BLE001
                        pass
                if engine.rec_done is not None and engine.rec is None:
                    pts, engine.rec_done = engine.rec_done, None
                    await finish_recording(pts)
                if engine.error is not None and not state["err_sent"]:
                    state["err_sent"] = True
                    await event(f"Audio engine stopped: {engine.error}. Restart Spatial-Lab.", "error")
                await broadcast({"t": "tick", "s": engine.snapshot(), "out": state["out_status"]})

        task = asyncio.create_task(run())
        yield
        task.cancel()

    async def on_cleanup(app):
        engine.mute("shutdown", panic=True)
        await asyncio.sleep(0.1)
        engine.stop()

    app = web.Application(middlewares=[guard], client_max_size=MAX_UPLOAD)
    app.on_response_prepare.append(add_headers)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/upload", upload)
    app.router.add_get("/api/state", state_json)
    app.router.add_get("/{name:.+}", static)
    app.cleanup_ctx.append(ticker)
    app.on_cleanup.append(on_cleanup)
    return app
