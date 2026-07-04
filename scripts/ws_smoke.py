"""WebSocket smoke test for the room consumer.

Exercises the control-plane contract end-to-end against a running dev server:
join/name flow, host-authority, input validation, file meta, signaling relay,
host migration, and the room capacity cap.

Usage:
    pip install websockets          # test-only dep, not in requirements.txt
    python manage.py runserver      # in another terminal
    python scripts/ws_smoke.py
"""

import asyncio
import json
import os
import sys
import uuid

import websockets

# Point WS_BASE_B at a second worker (e.g. ws://127.0.0.1:8001) to prove the
# Redis-backed store works across processes: client A and client B then talk
# through different servers while sharing one room.
BASE = os.environ.get("WS_BASE", "ws://127.0.0.1:8000") + "/ws/room/"
BASE_B = os.environ.get("WS_BASE_B", os.environ.get("WS_BASE", "ws://127.0.0.1:8000")) + "/ws/room/"

PASS, FAIL = 0, 1
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


async def recv_until(ws, typ, timeout=3.0):
    """Read frames until one of type `typ` arrives; return it (others dropped)."""
    async with asyncio.timeout(timeout):
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == typ:
                return msg


async def expect_silence(ws, typ, timeout=0.8):
    """True if no frame of type `typ` arrives within `timeout`."""
    try:
        async with asyncio.timeout(timeout):
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == typ:
                    return False, msg
    except TimeoutError:
        return True, None


async def main():
    room = uuid.uuid4().hex[:10]
    uri = BASE + room + "/"
    uri_b = BASE_B + room + "/"

    # Connect sequentially: B must join *after* A is named for the name test.
    a = await websockets.connect(uri)
    b = None
    try:
        # --- join flow ---
        ja = await recv_until(a, "joined")
        check("first joiner is host", ja["isHost"] and ja["host"] == ja["selfId"])
        await a.send(json.dumps({"type": "name", "name": "alice"}))
        await recv_until(a, "peer_named")

        b = await websockets.connect(uri_b)
        jb = await recv_until(b, "joined")
        check("second joiner is not host", not jb["isHost"])
        names = {p["id"]: p["name"] for p in jb["peers"]}
        check("joined carries existing peers' names", names.get(ja["selfId"]) == "alice",
              str(jb["peers"]))
        await recv_until(a, "peer_joined")

        # --- clock ping ---
        await a.send(json.dumps({"type": "ping", "t0": 12345}))
        pong = await recv_until(a, "pong")
        check("pong echoes t0 with serverTime", pong["t0"] == 12345 and "serverTime" in pong)

        # --- shared control: anyone may act, last action wins ---
        await b.send(json.dumps({"type": "control", "playing": True, "position": 99.0}))
        sync_a = await recv_until(a, "sync")
        await recv_until(b, "sync")  # drain b's own copy
        check("non-host control drives the room",
              sync_a["anchorPosition"] == 99.0 and sync_a["by"] == jb["selfId"],
              json.dumps(sync_a))

        # host heartbeat inside the 2s hold-off after an action is dropped...
        await a.send(json.dumps(
            {"type": "control", "playing": True, "position": 200.0, "heartbeat": True}))
        quiet, leak = await expect_silence(b, "sync")
        check("heartbeat suppressed right after an action", quiet, str(leak))
        # ...but accepted once the hold-off has passed
        await asyncio.sleep(2.1)
        await a.send(json.dumps(
            {"type": "control", "playing": True, "position": 201.0, "heartbeat": True}))
        hb = await recv_until(b, "sync")
        await recv_until(a, "sync")  # drain a's own copy
        check("heartbeat accepted after hold-off", hb["anchorPosition"] == 201.0)
        # only the host is the timekeeper — non-host heartbeats are ignored
        await b.send(json.dumps(
            {"type": "control", "playing": True, "position": 300.0, "heartbeat": True}))
        quiet, leak = await expect_silence(a, "sync")
        check("non-host heartbeat ignored", quiet, str(leak))

        await a.send(json.dumps({"type": "control", "playing": True, "position": 42.5}))
        sync_b = await recv_until(b, "sync")
        await recv_until(a, "sync")  # drain a's own copy
        check("host control broadcast to room",
              sync_b["anchorPosition"] == 42.5 and sync_b["playing"] is True
              and sync_b["executeAt"] == sync_b["anchorServerTime"] + 250,
              json.dumps(sync_b))

        # --- input validation ---
        await a.send(json.dumps({"type": "control", "playing": True, "position": "bogus"}))
        quiet, leak = await expect_silence(b, "sync")
        check("garbage position rejected", quiet, str(leak))
        await a.send('{"type": "control", "playing": true, "position": NaN}')
        quiet, leak = await expect_silence(b, "sync")
        check("NaN position rejected", quiet, str(leak))
        await a.send('[1, 2, 3]')  # non-dict frame must not kill the socket
        await a.send(json.dumps({"type": "ping", "t0": 1}))
        pong = await recv_until(a, "pong")
        check("non-dict frame ignored, socket alive", pong["t0"] == 1)

        # --- file meta ---
        await b.send(json.dumps({"type": "file", "name": "x.mkv", "size": 100}))
        quiet, leak = await expect_silence(a, "file")
        check("non-host file meta ignored", quiet, str(leak))
        await a.send(json.dumps(
            {"type": "file", "name": "movie.mkv", "size": 1234, "hash": "abc123"}))
        fmsg = await recv_until(b, "file")
        check("host file meta broadcast",
              fmsg["file"] == {"name": "movie.mkv", "size": 1234, "hash": "abc123"},
              json.dumps(fmsg))

        # --- signaling relay ---
        await b.send(json.dumps(
            {"type": "signal", "to": ja["selfId"], "data": {"sdp": {"type": "offer"}}}))
        sig = await recv_until(a, "signal")
        check("signal relayed verbatim with sender id",
              sig["from"] == jb["selfId"] and sig["data"] == {"sdp": {"type": "offer"}})
        await b.send(json.dumps({"type": "signal", "to": ["bad"], "data": {}}))
        await b.send(json.dumps({"type": "ping", "t0": 2}))
        pong = await recv_until(b, "pong")
        check("unhashable signal target ignored, socket alive", pong["t0"] == 2)

        # --- host migration on disconnect ---
        await a.close()
        left = await recv_until(b, "peer_left")
        hc = await recv_until(b, "host_changed")
        fclear = await recv_until(b, "file")
        check("host leave -> peer_left + promotion + file meta cleared",
              left["peerId"] == ja["selfId"] and hc["peerId"] == jb["selfId"]
              and fclear["file"] is None)

        # promoted client may now control
        await b.send(json.dumps({"type": "control", "playing": False, "position": 7.0}))
        sync = await recv_until(b, "sync")
        check("promoted host may control", sync["anchorPosition"] == 7.0)
    finally:
        await a.close()
        if b is not None:
            await b.close()

    # --- capacity cap (connections alternate across both bases) ---
    room2 = uuid.uuid4().hex[:10]
    bases2 = [BASE + room2 + "/", BASE_B + room2 + "/"]
    conns = [await websockets.connect(bases2[i % 2]) for i in range(5)]
    for c in conns:
        await recv_until(c, "joined")
    extra = await websockets.connect(bases2[1])
    full = await recv_until(extra, "room_full")
    check("6th joiner rejected with room_full", full.get("max") == 5)
    for c in conns:
        await c.close()
    await extra.close()

    # --- admission control (lock / knock / admit / deny) + kick ---
    room3 = uuid.uuid4().hex[:10]
    uri3 = BASE + room3 + "/"
    uri3_b = BASE_B + room3 + "/"

    h = await websockets.connect(uri3)
    jh = await recv_until(h, "joined")
    check("fresh room reports unlocked", jh.get("locked") is False)

    g1 = await websockets.connect(uri3_b)
    jg1 = await recv_until(g1, "joined")
    await recv_until(h, "peer_joined")

    await g1.send(json.dumps({"type": "lock", "locked": True}))
    quiet, leak = await expect_silence(h, "locked")
    check("non-host lock ignored", quiet, str(leak))

    await h.send(json.dumps({"type": "lock", "locked": True}))
    lk = await recv_until(g1, "locked")
    await recv_until(h, "locked")  # drain host's own copy
    check("host lock broadcast to room", lk["locked"] is True)

    g2 = await websockets.connect(uri3)
    await recv_until(g2, "waiting")
    check("joiner to a locked room is parked at the door", True)
    await g2.send(json.dumps({"type": "knock", "name": "carol"}))
    kn = await recv_until(h, "knock")
    check("knock relayed to host with name", kn["name"] == "carol")

    await g1.send(json.dumps({"type": "admit", "peerId": kn["peerId"], "allow": True}))
    quiet, leak = await expect_silence(g2, "joined")
    check("non-host admit ignored", quiet, str(leak))

    await h.send(json.dumps({"type": "admit", "peerId": kn["peerId"], "allow": True}))
    jg2 = await recv_until(g2, "joined")
    check("admitted guest joins (room still locked)",
          jg2["locked"] is True and not jg2["isHost"])
    await recv_until(h, "peer_joined")
    await recv_until(g1, "peer_joined")

    g3 = await websockets.connect(uri3_b)
    await recv_until(g3, "waiting")
    await g3.send(json.dumps({"type": "knock", "name": "mallory"}))
    kn3 = await recv_until(h, "knock")
    await h.send(json.dumps({"type": "admit", "peerId": kn3["peerId"], "allow": False}))
    d = await recv_until(g3, "denied")
    check("denied guest gets denied", d["type"] == "denied")

    await g1.send(json.dumps({"type": "mute", "peerId": jg2["selfId"]}))
    quiet, leak = await expect_silence(g2, "muted")
    check("non-host mute ignored", quiet, str(leak))

    await h.send(json.dumps({"type": "mute", "peerId": jg2["selfId"]}))
    m = await recv_until(g2, "muted")
    check("host mute delivered to target", m["type"] == "muted")

    await g1.send(json.dumps({"type": "kick", "peerId": jg2["selfId"]}))
    quiet, leak = await expect_silence(g2, "kicked")
    check("non-host kick ignored", quiet, str(leak))

    await h.send(json.dumps({"type": "kick", "peerId": jg2["selfId"]}))
    kicked = await recv_until(g2, "kicked")
    pl = await recv_until(g1, "peer_left")
    check("host kick removes the peer (kicked + peer_left)",
          kicked["type"] == "kicked" and pl["peerId"] == jg2["selfId"])

    g4 = await websockets.connect(uri3)
    await recv_until(g4, "waiting")
    await h.send(json.dumps({"type": "lock", "locked": False}))
    jg4 = await recv_until(g4, "joined")
    check("unlock auto-admits waiting guests", jg4["locked"] is False)

    for c in (h, g1, g2, g3, g4):
        await c.close()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return FAIL if failed else PASS


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
