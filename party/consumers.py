import math
import uuid

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from party.store import get_store, server_now_ms

MAX_PEERS = 5  # mesh WebRTC is O(n²) connections; the spec caps rooms at ~5
MAX_POSITION_S = 60 * 60 * 24  # sanity bound for playhead positions
HEARTBEAT_HOLDOFF_MS = 2000  # heartbeats yield to fresh user actions this long


class RoomConsumer(AsyncJsonWebsocketConsumer):
    BUFFER_MS = 250  # how far in the future a sync should execute

    @property
    def store(self):
        return get_store()

    async def connect(self):
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        self.group = f"room.{self.room_id}"
        self.peer_id = uuid.uuid4().hex[:8]
        self.joined = False
        self.pending = False

        await self.accept()

        # A locked, occupied room admits nobody directly: the guest waits in a
        # knock state until the host admits (or denies) them. An empty room is
        # never locked-in-practice — its first joiner becomes the host.
        if await self.store.locked_and_occupied(self.room_id):
            self.pending = True
            await self.store.add_pending(self.room_id, self.peer_id, self.channel_name)
            await self.send_json({"type": "waiting"})
            return

        await self._complete_join()

    async def _complete_join(self):
        self.pending = False
        await self.channel_layer.group_add(self.group, self.channel_name)

        res = await self.store.join(
            self.room_id, self.peer_id, self.channel_name, MAX_PEERS
        )
        if res is None:
            await self.channel_layer.group_discard(self.group, self.channel_name)
            await self.send_json({"type": "room_full", "max": MAX_PEERS})
            await self.close(code=4001)
            return
        self.joined = True

        await self.send_json(
            {
                "type": "joined",
                "selfId": self.peer_id,
                "isHost": res.host == self.peer_id,
                "host": res.host,
                "peers": res.peers,  # [{id, name}] so newcomers see names immediately
                "file": res.file,
                "state": res.state,  # so a late joiner can catch up immediately
                "locked": res.locked,
            }
        )
        await self.channel_layer.group_send(
            self.group,
            {"type": "peer.joined", "peerId": self.peer_id, "origin": self.channel_name},
        )

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group, self.channel_name)
        if getattr(self, "pending", False):
            await self.store.remove_pending(self.room_id, self.peer_id)
            return
        if not getattr(self, "joined", False):
            return
        res = await self.store.leave(self.room_id, self.peer_id)
        if res.emptied:
            return
        await self.channel_layer.group_send(
            self.group, {"type": "peer.left", "peerId": self.peer_id}
        )
        # Host migration: the store promoted the next peer and cleared the old
        # host's file meta; the new host re-publishes its own on promotion.
        if res.new_host:
            await self.channel_layer.group_send(
                self.group, {"type": "host.changed", "peerId": res.new_host}
            )
            await self.channel_layer.group_send(
                self.group, {"type": "room.file", "file": None}
            )

    async def receive_json(self, content):
        if not isinstance(content, dict):
            return
        msg_type = content.get("type")

        if msg_type == "ping":
            # Reply immediately for NTP-style offset estimation.
            await self.send_json(
                {"type": "pong", "t0": content.get("t0"), "serverTime": server_now_ms()}
            )
            return

        if getattr(self, "pending", False):
            # A waiting guest may only knock. Re-knocks are fine (the client
            # repeats them so a migrated host still sees the request).
            if msg_type == "knock":
                if not await self.store.locked_and_occupied(self.room_id):
                    # Room got unlocked or emptied while we waited — walk in.
                    await self.store.remove_pending(self.room_id, self.peer_id)
                    await self._complete_join()
                    return
                host = await self.store.get_host(self.room_id)
                channel = (
                    await self.store.get_peer_channel(self.room_id, host)
                    if host else None
                )
                if channel:
                    await self.channel_layer.send(
                        channel,
                        {
                            "type": "room.knock",
                            "peerId": self.peer_id,
                            "name": str(content.get("name", ""))[:40],
                        },
                    )
            return

        if not getattr(self, "joined", False):
            return
        if not await self.store.peer_exists(self.room_id, self.peer_id):
            return  # e.g. room state expired server-side

        if msg_type == "name":
            name = str(content.get("name", ""))[:40]
            if await self.store.set_name(self.room_id, self.peer_id, name):
                await self.channel_layer.group_send(
                    self.group,
                    {"type": "peer.named", "peerId": self.peer_id, "name": name},
                )

        elif msg_type == "control":
            # Shared control: anyone may play/pause/seek; the server sequences
            # everything, so the last action wins. Host heartbeats keep anchors
            # fresh but never stomp fresh actions (store enforces the hold-off).
            try:
                position = float(content.get("position", 0.0))
            except (TypeError, ValueError):
                return
            if not math.isfinite(position):
                return  # NaN/inf would serialize into JSON that JS can't parse
            anchor = server_now_ms()
            state = {
                "playing": bool(content.get("playing")),
                "anchorPosition": min(max(position, 0.0), MAX_POSITION_S),
                "anchorServerTime": anchor,
            }
            accepted = await self.store.set_state(
                self.room_id,
                self.peer_id,
                state,
                heartbeat=bool(content.get("heartbeat")),
                holdoff_ms=HEARTBEAT_HOLDOFF_MS,
                now_ms=anchor,
            )
            if not accepted:
                return
            await self.channel_layer.group_send(
                self.group,
                {
                    "type": "room.sync",
                    "by": self.peer_id,
                    "executeAt": anchor + self.BUFFER_MS,
                    **state,
                },
            )

        elif msg_type == "delete":
            # Only the host may tear the room down for everyone.
            if await self.store.get_host(self.room_id) == self.peer_id:
                await self.channel_layer.group_send(self.group, {"type": "room.closed"})

        elif msg_type == "lock":
            # Host-only: toggle admission control. Unlocking lets every guest
            # who is still waiting at the door walk in.
            if await self.store.get_host(self.room_id) != self.peer_id:
                return
            locked = bool(content.get("locked"))
            await self.store.set_locked(self.room_id, locked)
            await self.channel_layer.group_send(
                self.group, {"type": "room.locked", "locked": locked}
            )
            if not locked:
                for channel in await self.store.pop_all_pending(self.room_id):
                    await self.channel_layer.send(
                        channel, {"type": "pending.result", "allow": True}
                    )

        elif msg_type == "admit":
            # Host-only: resolve a knock (allow=false denies).
            if await self.store.get_host(self.room_id) != self.peer_id:
                return
            to = content.get("peerId")
            if not isinstance(to, str):
                return
            channel = await self.store.pop_pending(self.room_id, to)
            if channel:
                await self.channel_layer.send(
                    channel,
                    {"type": "pending.result", "allow": bool(content.get("allow"))},
                )

        elif msg_type == "kick":
            # Host-only: remove a peer. Their consumer closes itself, which
            # broadcasts the normal peer_left on disconnect.
            to = content.get("peerId")
            if not isinstance(to, str) or to == self.peer_id:
                return
            if await self.store.get_host(self.room_id) != self.peer_id:
                return
            channel = await self.store.get_peer_channel(self.room_id, to)
            if channel:
                await self.channel_layer.send(channel, {"type": "room.kick"})

        elif msg_type == "mute":
            # Host-only: ask a peer's client to mute its mic. Cooperative by
            # nature (media is P2P; the server never touches it) — the target
            # may unmute itself, same as every mainstream call app.
            to = content.get("peerId")
            if not isinstance(to, str) or to == self.peer_id:
                return
            if await self.store.get_host(self.room_id) != self.peer_id:
                return
            channel = await self.store.get_peer_channel(self.room_id, to)
            if channel:
                await self.channel_layer.send(channel, {"type": "room.mute"})

        elif msg_type == "file":
            if await self.store.get_host(self.room_id) != self.peer_id:
                return
            try:
                size = int(content.get("size", 0))
            except (TypeError, ValueError):
                return
            file_hash = content.get("hash")
            meta = {
                "name": str(content.get("name", ""))[:200],
                "size": max(size, 0),
                "hash": str(file_hash)[:64] if file_hash else None,
            }
            await self.store.set_file(self.room_id, meta)
            await self.channel_layer.group_send(
                self.group, {"type": "room.file", "file": meta}
            )

        elif msg_type == "signal":
            # Relay WebRTC signaling verbatim to one target peer.
            to = content.get("to")
            channel = (
                await self.store.get_peer_channel(self.room_id, to)
                if isinstance(to, str)
                else None
            )
            if channel:
                await self.channel_layer.send(
                    channel,
                    {
                        "type": "relay.signal",
                        "from": self.peer_id,
                        "data": content.get("data"),
                    },
                )

    # ---- group/relay handlers ----

    async def peer_joined(self, event):
        if event.get("origin") == self.channel_name:
            return  # don't notify the newcomer about itself
        await self.send_json({"type": "peer_joined", "peerId": event["peerId"]})

    async def peer_left(self, event):
        await self.send_json({"type": "peer_left", "peerId": event["peerId"]})

    async def peer_named(self, event):
        await self.send_json(
            {"type": "peer_named", "peerId": event["peerId"], "name": event["name"]}
        )

    async def host_changed(self, event):
        await self.send_json({"type": "host_changed", "peerId": event["peerId"]})

    async def room_sync(self, event):
        await self.send_json(
            {
                "type": "sync",
                "by": event["by"],
                "playing": event["playing"],
                "anchorPosition": event["anchorPosition"],
                "anchorServerTime": event["anchorServerTime"],
                "executeAt": event["executeAt"],
            }
        )

    async def room_closed(self, event):
        await self.send_json({"type": "room_closed"})

    async def room_file(self, event):
        await self.send_json({"type": "file", "file": event["file"]})

    async def relay_signal(self, event):
        await self.send_json(
            {"type": "signal", "from": event["from"], "data": event["data"]}
        )

    async def room_locked(self, event):
        await self.send_json({"type": "locked", "locked": event["locked"]})

    async def room_knock(self, event):
        await self.send_json(
            {"type": "knock", "peerId": event["peerId"], "name": event["name"]}
        )

    async def room_kick(self, event):
        await self.send_json({"type": "kicked"})
        await self.close(code=4003)

    async def room_mute(self, event):
        await self.send_json({"type": "muted"})

    async def pending_result(self, event):
        if not getattr(self, "pending", False):
            return  # stale admit (e.g. we already disconnected/rejoined)
        if event["allow"]:
            await self._complete_join()
        else:
            self.pending = False
            await self.send_json({"type": "denied"})
            await self.close(code=4002)
