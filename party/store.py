"""Room state storage.

Two interchangeable backends behind one small async interface:

- MemoryRoomStore — in-process dict. Zero dependencies, perfect for dev and any
  single-process deployment (state dies with the process).
- RedisRoomStore — shared state in Redis so multiple ASGI workers / containers
  can serve the same rooms. Join/leave run as Lua scripts so the capacity
  check, host election and host migration stay atomic across workers.

The backend is chosen once from settings.REDIS_URL (empty -> memory), matching
how the Channels layer is selected in config/settings.py.
"""

import json
import time
from dataclasses import dataclass

from django.conf import settings


def server_now_ms():
    return int(time.time() * 1000)


@dataclass
class JoinResult:
    host: str
    peers: list  # existing peers (without self): [{"id": ..., "name": ...}]
    file: dict | None
    state: dict | None
    locked: bool = False


@dataclass
class LeaveResult:
    emptied: bool
    new_host: str | None


class MemoryRoomStore:
    def __init__(self):
        self.rooms = {}

    def _room(self, room_id):
        return self.rooms.get(room_id)

    async def join(self, room_id, peer_id, channel, max_peers):
        # No awaits between the capacity check and registration, so two
        # concurrent joins on one event loop can't both slip past the cap.
        room = self.rooms.setdefault(
            room_id,
            {"peers": {}, "host": None, "file": None, "state": None,
             "last_action_ms": 0, "locked": False, "pending": {}},
        )
        if len(room["peers"]) >= max_peers:
            return None
        existing = [{"id": pid, "name": p["name"]} for pid, p in room["peers"].items()]
        if room["host"] is None:
            room["host"] = peer_id
        room["peers"][peer_id] = {"channel": channel, "name": ""}
        return JoinResult(room["host"], existing, room["file"], room["state"], room["locked"])

    async def leave(self, room_id, peer_id):
        room = self._room(room_id)
        if not room or peer_id not in room["peers"]:
            return LeaveResult(True, None)
        room["peers"].pop(peer_id)
        if not room["peers"]:
            self.rooms.pop(room_id, None)
            return LeaveResult(True, None)
        new_host = None
        if room["host"] == peer_id:
            # Host migration: promote the next remaining peer. The old host's
            # file meta no longer describes the reference copy, so clear it —
            # the new host re-publishes its own on promotion.
            new_host = next(iter(room["peers"]))
            room["host"] = new_host
            room["file"] = None
        return LeaveResult(False, new_host)

    async def peer_exists(self, room_id, peer_id):
        room = self._room(room_id)
        return bool(room and peer_id in room["peers"])

    async def set_name(self, room_id, peer_id, name):
        room = self._room(room_id)
        if not room or peer_id not in room["peers"]:
            return False
        room["peers"][peer_id]["name"] = name
        return True

    async def set_state(self, room_id, peer_id, state, heartbeat, holdoff_ms, now_ms):
        room = self._room(room_id)
        if not room:
            return False
        if heartbeat:
            if room["host"] != peer_id:
                return False  # only the host is the timekeeper
            if now_ms - room["last_action_ms"] < holdoff_ms:
                return False  # heartbeats yield to fresh user actions
        else:
            room["last_action_ms"] = now_ms
        room["state"] = state
        return True

    async def get_host(self, room_id):
        room = self._room(room_id)
        return room["host"] if room else None

    async def set_file(self, room_id, meta):
        room = self._room(room_id)
        if room:
            room["file"] = meta

    async def get_peer_channel(self, room_id, peer_id):
        room = self._room(room_id)
        peer = room["peers"].get(peer_id) if room else None
        return peer["channel"] if peer else None

    # ---- lock / knock (admission control) ----

    async def set_locked(self, room_id, locked):
        room = self._room(room_id)
        if room:
            room["locked"] = bool(locked)

    async def locked_and_occupied(self, room_id):
        room = self._room(room_id)
        return bool(room and room["peers"] and room["locked"])

    async def add_pending(self, room_id, peer_id, channel):
        room = self._room(room_id)
        if room:
            room["pending"][peer_id] = channel

    async def pop_pending(self, room_id, peer_id):
        room = self._room(room_id)
        return room["pending"].pop(peer_id, None) if room else None

    async def remove_pending(self, room_id, peer_id):
        await self.pop_pending(room_id, peer_id)

    async def pop_all_pending(self, room_id):
        room = self._room(room_id)
        if not room:
            return []
        channels = list(room["pending"].values())
        room["pending"].clear()
        return channels


class RedisRoomStore:
    TTL = 24 * 3600  # orphaned rooms evaporate (e.g. a worker died mid-session)

    # KEYS[1]=room hash, KEYS[2]=peers hash
    # ARGV[1]=peer_id, ARGV[2]=peer json, ARGV[3]=max peers, ARGV[4]=ttl
    JOIN_LUA = """
    if redis.call('HLEN', KEYS[2]) >= tonumber(ARGV[3]) then return {0} end
    local existing = redis.call('HGETALL', KEYS[2])
    redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
    redis.call('HSETNX', KEYS[1], 'host', ARGV[1])
    local host = redis.call('HGET', KEYS[1], 'host')
    local file = redis.call('HGET', KEYS[1], 'file') or ''
    local state = redis.call('HGET', KEYS[1], 'state') or ''
    local locked = redis.call('HGET', KEYS[1], 'locked') or ''
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    redis.call('EXPIRE', KEYS[2], ARGV[4])
    return {1, host, cjson.encode(existing), file, state, locked}
    """

    # KEYS[1]=room hash, KEYS[2]=peers hash, KEYS[3]=knock hash; ARGV[1]=peer_id
    LEAVE_LUA = """
    redis.call('HDEL', KEYS[2], ARGV[1])
    if redis.call('HLEN', KEYS[2]) == 0 then
      redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
      return {1, ''}
    end
    local host = redis.call('HGET', KEYS[1], 'host')
    if host == ARGV[1] then
      local ks = redis.call('HKEYS', KEYS[2])
      redis.call('HSET', KEYS[1], 'host', ks[1])
      redis.call('HDEL', KEYS[1], 'file')
      return {0, ks[1]}
    end
    return {0, ''}
    """

    def __init__(self, url):
        import redis.asyncio as aioredis

        self._r = aioredis.from_url(url, decode_responses=True)
        self._join = self._r.register_script(self.JOIN_LUA)
        self._leave = self._r.register_script(self.LEAVE_LUA)

    @staticmethod
    def _room_key(room_id):
        return f"wp:room:{room_id}"

    @staticmethod
    def _peers_key(room_id):
        return f"wp:room:{room_id}:peers"

    @staticmethod
    def _knock_key(room_id):
        return f"wp:room:{room_id}:knock"

    async def join(self, room_id, peer_id, channel, max_peers):
        res = await self._join(
            keys=[self._room_key(room_id), self._peers_key(room_id)],
            args=[peer_id, json.dumps({"channel": channel, "name": ""}), max_peers, self.TTL],
        )
        if res[0] == 0:
            return None
        _, host, existing_json, file_raw, state_raw, locked_raw = res
        parsed = json.loads(existing_json)
        # cjson encodes an empty table as {} and a flat HGETALL as [k1,v1,...]
        pairs = list(parsed.items()) if isinstance(parsed, dict) \
            else list(zip(parsed[::2], parsed[1::2]))
        peers = [{"id": pid, "name": json.loads(raw)["name"]} for pid, raw in pairs]
        return JoinResult(
            host,
            peers,
            json.loads(file_raw) if file_raw else None,
            json.loads(state_raw) if state_raw else None,
            locked_raw == "1",
        )

    async def leave(self, room_id, peer_id):
        emptied, new_host = await self._leave(
            keys=[self._room_key(room_id), self._peers_key(room_id), self._knock_key(room_id)],
            args=[peer_id],
        )
        return LeaveResult(bool(emptied), new_host or None)

    async def peer_exists(self, room_id, peer_id):
        return bool(await self._r.hexists(self._peers_key(room_id), peer_id))

    async def set_name(self, room_id, peer_id, name):
        # Only the owning connection writes its peer entry, so the
        # read-modify-write below has a single writer and needs no lock.
        raw = await self._r.hget(self._peers_key(room_id), peer_id)
        if not raw:
            return False
        entry = json.loads(raw)
        entry["name"] = name
        await self._r.hset(self._peers_key(room_id), peer_id, json.dumps(entry))
        return True

    async def set_state(self, room_id, peer_id, state, heartbeat, holdoff_ms, now_ms):
        room_key = self._room_key(room_id)
        if heartbeat:
            if await self._r.hget(room_key, "host") != peer_id:
                return False  # only the host is the timekeeper
            last = int(await self._r.hget(room_key, "last_action_ms") or 0)
            if now_ms - last < holdoff_ms:
                return False  # heartbeats yield to fresh user actions
        else:
            await self._r.hset(room_key, "last_action_ms", now_ms)
        await self._r.hset(room_key, "state", json.dumps(state))
        await self._r.expire(room_key, self.TTL)
        await self._r.expire(self._peers_key(room_id), self.TTL)
        return True

    async def get_host(self, room_id):
        return await self._r.hget(self._room_key(room_id), "host")

    async def set_file(self, room_id, meta):
        await self._r.hset(self._room_key(room_id), "file", json.dumps(meta))

    async def get_peer_channel(self, room_id, peer_id):
        raw = await self._r.hget(self._peers_key(room_id), peer_id)
        return json.loads(raw)["channel"] if raw else None

    # ---- lock / knock (admission control) ----

    async def set_locked(self, room_id, locked):
        await self._r.hset(self._room_key(room_id), "locked", "1" if locked else "0")
        await self._r.expire(self._room_key(room_id), self.TTL)

    async def locked_and_occupied(self, room_id):
        if await self._r.hget(self._room_key(room_id), "locked") != "1":
            return False
        return await self._r.hlen(self._peers_key(room_id)) > 0

    async def add_pending(self, room_id, peer_id, channel):
        await self._r.hset(self._knock_key(room_id), peer_id, channel)
        await self._r.expire(self._knock_key(room_id), 3600)

    async def pop_pending(self, room_id, peer_id):
        channel = await self._r.hget(self._knock_key(room_id), peer_id)
        if channel:
            await self._r.hdel(self._knock_key(room_id), peer_id)
        return channel

    async def remove_pending(self, room_id, peer_id):
        await self._r.hdel(self._knock_key(room_id), peer_id)

    async def pop_all_pending(self, room_id):
        pending = await self._r.hgetall(self._knock_key(room_id))
        if pending:
            await self._r.delete(self._knock_key(room_id))
        return list(pending.values())


_store = None


def get_store():
    global _store
    if _store is None:
        _store = RedisRoomStore(settings.REDIS_URL) if settings.REDIS_URL else MemoryRoomStore()
    return _store
