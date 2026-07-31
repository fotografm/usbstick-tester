"""Web front end: pushes metric bins over a websocket at a fixed cadence."""

from __future__ import annotations

import asyncio
import json
import os

from aiohttp import WSMsgType, web

from . import device

STATIC = os.path.join(os.path.dirname(__file__), "static")

# Slower than the 100 ms bin because every push carries the amendable tail of
# recent bins, not just the new ones.  Five pushes a second is well inside what
# the chart can usefully redraw.
PUSH_INTERVAL = 0.2


async def index(request):
    return web.FileResponse(os.path.join(STATIC, "index.html"))


async def devices(request):
    protected = device.root_disks()
    found = device.scan()
    for disk in found:
        disk["protected"] = disk["name"] in protected
    return web.json_response({"devices": found})


async def select(request):
    body = await request.json()
    request.app["bench"].select(body.get("device") or None)
    return web.json_response({"ok": True})


async def eject(request):
    request.app["bench"].eject()
    return web.json_response({"ok": True})


async def pin(request):
    body = await request.json()
    request.app["bench"].pin(body.get("phase") or None)
    return web.json_response({"ok": True})


async def websocket(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    metrics = request.app["metrics"]
    bench = request.app["bench"]

    seq = 0
    event_seq = 0
    reader = asyncio.create_task(_drain(ws))
    try:
        while not ws.closed:
            snap = metrics.snapshot(seq, event_seq)
            seq = snap["seq"]
            event_seq = snap["event_seq"]
            snap["bench"] = bench.status()
            await ws.send_str(json.dumps(snap))
            await asyncio.sleep(PUSH_INTERVAL)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        reader.cancel()
    return ws


async def _drain(ws):
    """Consume client frames so close/ping handling stays responsive."""
    async for msg in ws:
        if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
            break


def build_app(metrics, bench):
    app = web.Application()
    app["metrics"] = metrics
    app["bench"] = bench
    app.add_routes(
        [
            web.get("/", index),
            web.get("/ws", websocket),
            web.get("/api/devices", devices),
            web.post("/api/select", select),
            web.post("/api/pin", pin),
            web.post("/api/eject", eject),
            web.static("/static", STATIC),
        ]
    )
    return app
