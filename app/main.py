"""rubato: one speaker, everyone's hands on it.

One device (/speaker) plays the audio; everyone else is a remote control.

The server owns the queue, the room state and command arbitration. Guests and
the host send intents over HTTP; the server validates them, applies them,
records who did it, and pushes the new state to everyone, the speaker included
(the speaker reconciles its <audio> to that state).

The speaker owns time. It sends a heartbeat about once a second (track,
position, paused, volume, buffering), and the queue only advances when the
speaker reports that a track ended. There is no server-side end timer and no
clock sync: guests interpolate progress locally from the latest heartbeat.
A watchdog marks the speaker offline after BEAT_TIMEOUT seconds without a beat.
"""
import asyncio
import contextlib
import io
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass

import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cookies as cookiejar
from . import setup as setupstate
from .artcache import art
from .delta import Sync, dumps
from .netstats import net
from .media import COOKIES, PROBE_VIDEO_ID, Resolver, _thumb, make_ytmusic, probe_account, song_to_track, to_track

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rubato")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain signed googlevideo URLs

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/app/config")
ENV_PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")


def public_url() -> str:
    """Invite link / QR base: PUBLIC_URL env wins, then the wizard's choice, else blank (= the page's own address)."""
    return ENV_PUBLIC_URL or setupstate.load_state().get("public_url", "")


def env_key(name: str) -> str:
    return os.environ.get(name, "").strip()


HOST_KEY = env_key("HOST_KEY") or setupstate.stored_host_key()            # updated by the setup wizard
SPEAKER_KEY = env_key("SPEAKER_KEY") or setupstate.stored_speaker_key()   # updated by the wizard / Rotate
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
MAX_QUEUE = 100
ADD_LIMIT, ADD_WINDOW = 10, 60.0      # adds per client IP per window (seconds)
ACT_LIMIT, ACT_WINDOW = 120, 60.0     # other controls (pause, volume, reorder...) per client IP per window
FAIL_LIMIT, FAIL_WINDOW = 10, 600.0   # wrong codes/keys per client IP per window
AUTO_TARGET = 10
MAX_FAILS = 3        # consecutive unplayable tracks before giving up
BACK_RESTART = 3.0   # Back restarts the current song once it has played this long, like any player
BEAT_TIMEOUT = 5.0   # seconds without a speaker heartbeat before the speaker counts as offline
FEED_MAX = 20
NAME_MAX = 20
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")
DEBOUNCE = 0.1         # a burst of changes (an album's worth of adds) goes out as one push
PREFETCH_DELAY = 3.0   # fetch the next track once the queue has stopped changing for this long

ytm = make_ytmusic()
resolver = Resolver()
last_probe: dict | None = None  # latest YouTube link check, for the Engine card


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))


def _quiet(task: asyncio.Task):
    """Prefetch failures are retried at play time; don't log them as unhandled."""
    task.add_done_callback(lambda t: t.cancelled() or t.exception())


# ------------------------------------------------------------------ names

_INVISIBLE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")


def clean_name(raw) -> str:
    """Trimmed, single-spaced, no control/bidi characters. Rendered via textContent only."""
    return re.sub(r"\s+", " ", _INVISIBLE.sub("", str(raw or ""))).strip()[:NAME_MAX].strip()


def initials(name: str) -> str:
    """First letters of up to two words, uppercase."""
    return "".join(w[0] for w in name.split()[:2]).upper() or "?"


def host_name() -> str:
    return setupstate.load_state().get("host_name") or "Host"


@dataclass
class Actor:
    key: str    # guest session id, or "host"
    name: str   # the bare name: avatar color + initials
    label: str  # what the feed shows, e.g. "Sam" or "Dana (host)"


@dataclass
class Session:
    sid: str
    name: str


class Speaker:
    """The one device that plays audio. Its token authorises /stream for as long as it holds the lock."""

    def __init__(self, ws: WebSocket, device: str, agent: str):
        self.ws = ws
        self.device = device
        self.agent = agent
        self.token = secrets.token_urlsafe(24)
        self.last_beat: float | None = None  # monotonic
        self.online = False
        self.locked = True       # waiting for the tap that unlocks audio
        self.buffering = False
        self.volume: int | None = None


class Room:
    def __init__(self):
        self.code = new_code()
        self.queue: list[dict] = []   # added by people, plays first
        self.auto: list[dict] = []    # radio suggestions, plays when the queue is empty
        self.now: dict | None = None
        self.loading = False          # current track not yet in RAM
        self.paused = False           # what the room asked for
        self.pos = 0.0                # last position the speaker reported for `now`
        self.pos_at = time.monotonic()
        self.playing = False          # the speaker says audio is actually coming out
        self.seek_id = 0              # bumped on every seek; the speaker echoes the one it has applied
        self.seek_pos = 0.0           # where that seek went
        self.volume = 60
        self.cap = 80
        self.radio = True
        self.speaker: Speaker | None = None
        self.hosts: set[WebSocket] = set()
        self.guests: dict[WebSocket, str] = {}       # socket -> session id
        self.sessions: dict[str, Session] = {}
        self.devices: dict[str, str] = {}            # session id -> "iPhone · Safari"
        self.feed: deque[dict] = deque(maxlen=FEED_MAX)
        self.hands: dict[str, dict] = {}             # actor key -> {count, last, at}
        self.history: deque[str] = deque(maxlen=200)  # videoIds, keeps radio from repeating
        self.back_stack: deque[dict] = deque(maxlen=30)  # tracks that played, newest last: what Back goes to
        self.add_log: dict[str, deque] = {}
        self.act_log: dict[str, deque] = {}
        self.lock = asyncio.Lock()
        self.auto_lock = asyncio.Lock()
        self.fails = 0
        self.sync = Sync()
        self._flush: asyncio.Task | None = None
        self._prefetch: asyncio.Task | None = None

    # ---- derived values
    def online(self) -> bool:
        return bool(self.speaker and self.speaker.online)

    def speaker_state(self) -> str:
        """offline / locked / buffering / live: the speaker status pill."""
        if not self.online():
            return "offline"
        if self.speaker.locked:
            return "locked"
        if self.speaker.buffering and not self.paused:
            return "buffering"
        return "live"

    def status(self) -> str:
        if not self.now:
            return "idle"
        if resolver.cookie_health() == "missing":
            return "unlinked"
        if self.loading:
            return "loading"
        if not self.online():
            return "offline"
        if self.speaker.locked:
            return "locked"
        if self.paused:
            return "paused"
        if self.speaker.buffering:
            return "buffering"
        return "playing"

    def position(self) -> float:
        p = self.pos + (time.monotonic() - self.pos_at if self.playing and self.online() else 0.0)
        d = self.now and self.now.get("duration")
        return min(p, d) if d else p

    def next_item(self) -> dict | None:
        return self.queue[0] if self.queue else self.auto[0] if self.auto else None

    @staticmethod
    def public(it: dict | None) -> dict | None:
        return None if it is None else {k: v for k, v in it.items() if not k.startswith("_")}

    def feed_public(self) -> list[dict]:
        # "key" is a session id, which authorises that guest's actions: it never leaves the server.
        # "ts" is server time: clients show how long ago, so the feed doesn't change every second.
        return [{k: v for k, v in e.items() if k not in ("at", "key")} for e in reversed(self.feed)]

    def connected(self) -> list[str]:
        """Session ids of guests with an open socket, deduplicated (one person, several tabs)."""
        return list(dict.fromkeys(self.guests.values()))

    def people(self) -> list[str]:
        return sorted({self.sessions[s].name for s in self.connected() if s in self.sessions}, key=str.lower)

    def hands_rows(self) -> list[dict]:
        rows = []
        keys = (["host"] if self.hosts else []) + self.connected()
        for k in keys:
            name = host_name() if k == "host" else self.sessions[k].name if k in self.sessions else None
            if not name:
                continue
            h = self.hands.get(k, {})
            rows.append({"name": name, "label": f"{name} (host)" if k == "host" else name, "initials": initials(name),
                         "device": "" if k == "host" else self.devices.get(k, ""), "count": h.get("count", 0),
                         "last": h.get("last"), "ts": h.get("ts")})
        return rows

    def cache_stats(self) -> tuple[int, int]:
        n = size = 0
        for task in resolver.media.values():
            if task.done() and not task.cancelled() and not task.exception() and task.result().data is not None:
                n += 1
                size += len(task.result().data)
        return n, size

    def host_panel(self) -> dict:
        nxt = self.next_item()
        cutoff = time.monotonic() - FAIL_WINDOW
        n, size = self.cache_stats()
        sp = self.speaker
        return {
            "name": host_name(),
            "speaker": {"connected": sp is not None, "device": sp.device if sp else None, "agent": sp.agent if sp else None,
                        "beat_ago": round(time.monotonic() - sp.last_beat, 1) if sp and sp.last_beat else None,
                        "reported_volume": sp.volume if sp else None,
                        "key": "env" if env_key("SPEAKER_KEY") else "stored" if SPEAKER_KEY else None},
            "hands": self.hands_rows(),
            "engine": {"stream": resolver.stream_format(self.now["videoId"]) if self.now else None,
                       "next_qid": nxt["qid"] if nxt else None,
                       "next": resolver.prefetch_state(nxt["videoId"]) if nxt else "idle",
                       "cache_tracks": n, "cache_mb": round(size / 1048576, 1),
                       "cookies": resolver.cookie_health(),
                       "account": None if not last_probe else {"name": last_probe.get("name"), "signed_in": last_probe.get("account"),
                                                              "premium": last_probe.get("premium")},
                       "wrong_codes": sum(1 for q in fail_log.values() for t in q if t > cutoff),
                       "wrong_code_limit": FAIL_LIMIT},
        }

    def snapshot(self, role: str = "guest") -> dict:
        """Everything a client shows, except the queue (which travels as edits, see delta.py)."""
        s = {
            "now": self.public(self.now),
            "server_time": round(time.time(), 2),
            "status": self.status(),
            "speaker": {"state": self.speaker_state(), "device": self.speaker.device if self.speaker else None},
            "position": round(self.position(), 2),
            "playing": self.status() == "playing" and self.playing,
            "paused": self.paused,
            "seek": {"id": self.seek_id, "pos": round(self.seek_pos, 2)},  # the speaker jumps when the id changes
            "volume": self.volume,
            "cap": self.cap,
            "auto": [self.public(t) for t in self.auto],
            "can_back": bool(self.back_stack),
            "radio": self.radio,
            "feed": self.feed_public(),
            "people": self.people(),
            "notice": NOTICES.get(resolver.cookie_health()),
            "code": self.code,
            "public_url": public_url(),
        }
        if role == "host":
            s["host"] = self.host_panel()
        return s

    def beat_msg(self) -> dict:
        """The cheap once-a-second update: just enough to move progress bars."""
        return {"type": "beat", "qid": self.now["qid"] if self.now else None, "position": round(self.position(), 2),
                "playing": self.status() == "playing" and self.playing}

    # ---- broadcasting
    @staticmethod
    async def _send_text(ws: WebSocket, text: str):
        try:
            await ws.send_text(text)
        except Exception:
            pass  # disconnect handler cleans up

    async def _send(self, ws: WebSocket, msg: dict):
        text = json.dumps(msg, separators=(",", ":"))
        net.push(msg.get("type", "?"), text, 1)
        await self._send_text(ws, text)

    async def _send_all(self, kind: str, targets: list[tuple[WebSocket, dict]]):
        texts = [(ws, dumps(m)) for ws, m in targets]
        net.push_batch(kind, [t for _, t in texts])
        await asyncio.gather(*(self._send_text(ws, t) for ws, t in texts))

    async def flush(self):
        """Bring every socket up to date: a patch, or the full state if it has none yet (delta.py)."""
        self.sync.set_queue([self.public(t) for t in self.queue])
        guest = self.snapshot()
        targets = [(ws, guest) for ws in list(self.guests)]
        if self.hosts:
            host = self.snapshot("host")
            targets += [(ws, host) for ws in list(self.hosts)]
        if self.speaker:
            targets.append((self.speaker.ws, guest))
        msgs = [(ws, m) for ws, snap in targets if (m := self.sync.message(ws, snap)) is not None]
        for kind in ("state", "patch"):
            await self._send_all(kind, [(ws, m) for ws, m in msgs if m["type"] == kind])

    async def broadcast(self):
        """Push the current state to everyone, DEBOUNCE from now: a burst of changes goes out as one push."""
        if self._flush is None or self._flush.done():
            self._flush = asyncio.create_task(self._flush_later())

    async def _flush_later(self):
        await asyncio.sleep(DEBOUNCE)
        self._flush = None  # changes made while this push is sending schedule the next one
        await self.flush()

    async def broadcast_beat(self):
        msg = self.beat_msg()
        await self._send_all("beat", [(ws, msg) for ws in list(self.guests) + list(self.hosts)])

    def seek_to(self, pos: float):
        """Move the current track to `pos` seconds. Caller holds self.lock. The speaker sees the new
        seek id in the next state and jumps; until its heartbeat echoes that id, its reports from the
        old position are ignored (see speaker_msg), so progress bars don't snap back."""
        self.pos, self.pos_at, self.seek_pos = pos, time.monotonic(), pos
        self.seek_id += 1

    # ---- attribution
    def record(self, actor: Actor, kind: str, text: str):
        """Log an action to the rolling feed. Repeated volume drags by one person collapse into one line."""
        now = time.monotonic()
        last = self.feed[-1] if self.feed else None
        h = self.hands.setdefault(actor.key, {"count": 0})
        if kind == "volume" and last and last["kind"] == "volume" and last["key"] == actor.key and now - last["at"] < 8:
            last.update(text=text, at=now, ts=round(time.time()))
        else:
            self.feed.append({"id": uuid.uuid4().hex[:8], "key": actor.key, "who": actor.label, "name": actor.name,
                              "initials": initials(actor.name), "kind": kind, "text": text, "at": now, "ts": round(time.time())})
            h["count"] += 1
        h["last"], h["at"], h["ts"] = text, now, round(time.time())
        log.info("%s: %s", actor.label, text)

    # ---- queue mechanics
    def item(self, track: dict, actor: Actor | None = None) -> dict:
        it = {**track, "qid": uuid.uuid4().hex[:10], "auto": actor is None,
              "by": actor.label if actor else "Radio", "by_name": actor.name if actor else "Radio",
              "initials": initials(actor.name) if actor else "R"}
        return it

    def prefetch_next(self):
        """Fetch the next track ahead of time, once the queue has settled for PREFETCH_DELAY seconds.
        Someone clearing or reordering the queue song by song starts one download, not one per change."""
        if self._prefetch and not self._prefetch.done():
            self._prefetch.cancel()
        self._prefetch = asyncio.create_task(self._prefetch_later())

    async def _prefetch_later(self):
        await asyncio.sleep(PREFETCH_DELAY)
        nxt = self.next_item()
        if nxt and not self.loading:  # while the current track loads, its _prepare prefetches afterwards
            _quiet(resolver.load(nxt["videoId"], prefetch=True))

    async def advance(self, reason: str):
        """Move to the next track. Caller holds self.lock."""
        prev = self.now
        if prev:
            self.history.append(prev["videoId"])
            self.back_stack.append(prev)
        if self.queue:
            self.now = self.queue.pop(0)
        elif self.auto:
            self.now = self.auto.pop(0)
        elif prev and self.radio:
            await self.refill_auto(prev)
            self.now = self.auto.pop(0) if self.auto else None
        else:
            self.now = None
        self._start()
        log.info("advance (%s): %s", reason, self.now and f"{self.now['artists']} - {self.now['title']}")
        if self.now:
            # Queue ran dry: seed radio from the track that's now playing. If that
            # track is itself a radio pick, keep the existing radio list going.
            if self.radio and not self.queue and (not self.now["auto"] or len(self.auto) < 3):
                if not self.now["auto"]:
                    self.auto.clear()
                asyncio.create_task(self.refill_auto_and_broadcast(self.now))

    def _start(self):
        """self.now just changed: back to 0:00 and get the track into RAM. Caller holds self.lock."""
        self.pos, self.pos_at, self.playing = 0.0, time.monotonic(), False
        self.loading = self.now is not None
        if self.now:
            asyncio.create_task(self._prepare(self.now))

    def go_back(self):
        """Play the previous track again. The current one goes back to the front of the queue (or of
        the radio list, if it was a radio pick), so skipping forward returns to it. Caller holds self.lock."""
        prev, cur = self.back_stack.pop(), self.now
        if cur:
            (self.auto if cur.get("auto") else self.queue).insert(0, cur)
        self.now = {**prev, "qid": uuid.uuid4().hex[:10]}  # a new qid: the speaker reloads it, stale skips miss
        self._start()
        log.info("back: %s - %s", self.now["artists"], self.now["title"])
        self.prefetch_next()

    async def _prepare(self, item: dict):
        """Get the track into RAM before telling the speaker to load it."""
        if resolver.cookie_health() == "missing":
            return  # stays "unlinked"; relinked() picks it up once cookies arrive
        try:
            media, err = await resolver.load(item["videoId"]), None
        except Exception as e:
            media, err = None, e
        async with self.lock:
            if self.now is not item:
                return
            if err:
                await self.failed(f"could not load: {err}")
            else:
                self.fails = 0
                if media.duration:
                    item["duration"] = media.duration
                self.loading = False
                self.prefetch_next()
        await self.broadcast()

    async def failed(self, why: str):
        """The current track can't be played. Caller holds self.lock."""
        self.fails += 1
        log.warning("%s (%d in a row): %s", self.now["videoId"], self.fails, why)
        if self.fails >= MAX_FAILS:
            log.error("giving up after %d failures; check cookies or run ./update.sh", self.fails)
            self.fails, self.now, self.loading = 0, None, False
        else:
            await self.advance("unplayable")

    async def relinked(self):
        """New cookies were saved: start the current song if it was waiting on them."""
        resolver.bot_check = False
        async with self.lock:
            if self.now and self.loading:
                asyncio.create_task(self._prepare(self.now))
        await self.broadcast()

    async def refill_auto(self, seed: dict):
        async with self.auto_lock:
            if not self.radio or len(self.auto) >= AUTO_TARGET:
                return
            try:
                wp = await asyncio.to_thread(ytm.get_watch_playlist, videoId=seed["videoId"], limit=30)
            except Exception as e:
                log.warning("autoplay fetch failed for %s: %s", seed["videoId"], e)
                return
            skip = set(self.history) | {t["videoId"] for t in self.queue + self.auto} | {seed["videoId"]}
            if self.now:
                skip.add(self.now["videoId"])
            for t in filter(None, map(to_track, wp.get("tracks", []))):
                if len(self.auto) >= AUTO_TARGET:
                    break
                search_cache[t["videoId"]] = t  # so "add to queue" from the radio list keeps metadata
                if t["videoId"] not in skip:
                    skip.add(t["videoId"])
                    self.auto.append(self.item(t))
            log.info("autoplay: %d radio tracks seeded from %s", len(self.auto), seed["videoId"])

    async def refill_auto_and_broadcast(self, seed: dict):
        await self.refill_auto(seed)
        self.prefetch_next()
        await self.broadcast()

    async def rotate(self):
        """New session: fresh code, empty room, everyone holding the old code is signed out. The speaker stays."""
        async with self.lock:
            self.code = new_code()
            self.queue.clear()
            self.auto.clear()
            self.now = None
            self.loading = self.paused = self.playing = False
            self.pos = 0.0
            self.history.clear()
            self.back_stack.clear()
            self.feed.clear()
            self.hands.clear()
            self.sessions.clear()
            self.devices.clear()
            old = list(self.guests)
            self.guests.clear()
        for ws in old:
            with contextlib.suppress(Exception):
                await ws.close(code=4001, reason="room code changed")
        log.info("new session, code %s", self.code)
        await self.broadcast()

    # ---- the speaker
    async def drop_speaker(self, code: int, reason: str, msg: dict | None = None):
        """Release the speaker lock. Its stream token dies with it."""
        sp, self.speaker = self.speaker, None
        if not sp:
            return
        self.playing = False
        if msg:
            await self._send(sp.ws, msg)
        with contextlib.suppress(Exception):
            await sp.ws.close(code=code, reason=reason)
        log.info("speaker released (%s); paused at %.1fs", reason, self.pos)
        await self.broadcast()

    async def watchdog(self):
        """No heartbeat for BEAT_TIMEOUT seconds: the speaker is offline and the room shows paused."""
        while True:
            await asyncio.sleep(0.5)
            sp = self.speaker
            if sp and sp.online and sp.last_beat and time.monotonic() - sp.last_beat > BEAT_TIMEOUT:
                sp.online = False
                self.playing = False
                log.warning("speaker %s missed heartbeats for %.0fs: offline", sp.device, BEAT_TIMEOUT)
                await self.broadcast()


# Shown to everyone when YouTube auth needs attention.
NOTICES = {
    "missing": "Playback is off until the host links a YouTube Music account.",
    "expired": "The host's YouTube link has expired. Playback may stop until they relink it.",
}
room = Room()
fail_log: dict[str, deque] = {}
search_cache: dict[str, dict] = {}  # videoId -> track, from searches/albums/playlists/radio


async def startup_probe():
    """Fill the Engine card's Account row without waiting for a relink."""
    global last_probe
    if resolver.cookie_health() != "missing":
        try:
            last_probe = await asyncio.to_thread(probe_account)
        except Exception as e:
            log.info("startup link check failed: %s", type(e).__name__)


@contextlib.asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(room.watchdog()), asyncio.create_task(startup_probe())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Serving under a path prefix (e.g. https://host:8443/rubato): the pages work out
# the prefix themselves, so a proxy that strips it (Tailscale serve --set-path,
# Caddy handle_path) needs nothing. For a proxy that passes it through, set
# ROOT_PATH=/rubato and it is stripped here.
ROOT_PATH = os.environ.get("ROOT_PATH", "").strip().rstrip("/")


class StripRootPath:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and ROOT_PATH:
            path = scope["path"]
            if path == ROOT_PATH or path.startswith(ROOT_PATH + "/"):
                path = path[len(ROOT_PATH):] or "/"
                scope = {**scope, "path": path, "raw_path": path.encode()}
        await self.inner(scope, receive, send)


app.add_middleware(StripRootPath)
log.info("room code %s", room.code)


def _config_writable() -> bool:
    try:
        probe = os.path.join(CONFIG_DIR, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        return True
    except OSError:
        return False


if not _config_writable():
    bar = "!" * 72
    print(f"\n{bar}\n  {CONFIG_DIR} is not writable by this container (uid {os.getuid()}).\n"
          "  rubato stores its setup, keys and cookies there. Fix one of:\n"
          f"    sudo chown -R {os.getuid()}:{os.getgid()} ./config\n"
          "    or set RUBATO_UID / RUBATO_GID in .env to the owner of ./config\n"
          f"{bar}\n", flush=True)
    _token = None
else:
    _token = setupstate.setup_token()
if _token:
    bar = "=" * 72
    print(f"\n{bar}\n  rubato is not set up yet.\n\n  Open  /setup  in your browser and enter this one-time setup token:\n\n"
          f"      {_token}\n\n  Only someone with this token can configure this server. It stops\n"
          f"  working as soon as setup is finished.\n{bar}\n", flush=True)
elif resolver.cookie_health() == "missing":  # (expired can only be known after a lookup)
    log.warning("YouTube is not linked: playback is off. Open /host and use Relink YouTube.")


# ------------------------------------------------------------------ helpers

def device_name(ua: str) -> str:
    """"iPhone · Safari" style label."""
    os_ = next((n for k, n in [("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "Mac"),
                               ("Windows", "Windows"), ("CrOS", "ChromeOS"), ("Linux", "Linux")] if k in ua), "Device")
    if os_ == "Android" and "Pixel" in ua:
        os_ = "Pixel"
    br = next((n for k, n in [("Edg/", "Edge"), ("Firefox/", "Firefox"), ("FxiOS", "Firefox"), ("CriOS", "Chrome"),
                              ("Chrome/", "Chrome"), ("Safari/", "Safari")] if k in ua), "Browser")
    return f"{os_} · {br}"


def client_ip(conn: Request | WebSocket) -> str:
    return conn.client.host if conn.client else "?"


def check_secret(given: str | None, real: str | None, ip: str) -> int:
    """0 if ok, else the HTTP status to answer with. Wrong guesses are counted
    per IP; after FAIL_LIMIT in FAIL_WINDOW that IP is locked out, which keeps
    the 5-letter room code from being brute-forced over a public tunnel."""
    now = time.monotonic()
    q = fail_log.setdefault(ip, deque())
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    if len(q) >= FAIL_LIMIT:
        return 429
    if given and real and secrets.compare_digest(given.encode(), real.encode()):
        return 0
    q.append(now)
    if len(fail_log) > 10000:
        for k in [k for k, v in fail_log.items() if not v]:
            del fail_log[k]
    log.warning("wrong code/key from %s (%d/%d)", ip, len(q), FAIL_LIMIT)
    return 403


def require_code(conn: Request, code: str | None):
    status = check_secret((code or "").upper(), room.code, client_ip(conn))
    if status:
        raise HTTPException(status, "too many wrong codes, try again later" if status == 429 else "bad room code")


def require_host(request: Request):
    """Host endpoints: the host key in the X-Host-Key header (never in URLs)."""
    status = check_secret(request.headers.get("x-host-key", ""), HOST_KEY, client_ip(request))
    if status:
        raise HTTPException(status, "too many attempts" if status == 429 else "bad host key")


def limited(log_: dict[str, deque], ip: str, limit: int, window: float) -> bool:
    now = time.monotonic()
    q = log_.setdefault(ip, deque())
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    if len(log_) > 10000:
        for k in [k for k, v in log_.items() if not v]:
            del log_[k]
    return False


class Act(BaseModel):
    """Every room control: a guest sends the room code + their session id; the host sends X-Host-Key instead."""
    code: str = ""
    sid: str = ""


def actor_for(request: Request, body: Act) -> Actor:
    if request.headers.get("x-host-key") is not None:
        require_host(request)
        n = host_name()
        return Actor("host", n, f"{n} (host)")
    require_code(request, body.code)
    s = room.sessions.get(body.sid)
    if not s:
        raise HTTPException(401, "join with your name first")
    return Actor(s.sid, s.name, s.name)


def control(request: Request, body: Act) -> Actor:
    actor = actor_for(request, body)
    if actor.key != "host" and limited(room.act_log, client_ip(request), ACT_LIMIT, ACT_WINDOW):
        raise HTTPException(429, "slow down")
    return actor


# ------------------------------------------------------------------ pages

def page(name: str):
    return FileResponse(os.path.join(STATIC, name), headers={"Cache-Control": "no-cache"})


@app.get("/")
async def guest_page():
    return page("guest.html")


@app.get("/art/{aid}")
async def artwork(aid: str, s: int = 120):
    """Album art and artist images, from the disk cache (see artcache.py). Public pictures: no code needed."""
    got = await art.get(aid, s)
    if not got:
        raise HTTPException(404)
    return FileResponse(got[0], media_type=got[1], headers={"Cache-Control": "public, max-age=604800, immutable"})


@app.get("/healthz")
async def healthz():
    """Container healthcheck: the web server is up. No auth, no state, no secrets."""
    return {"ok": True, "setup_complete": setupstate.is_complete(), "youtube": resolver.cookie_health()}


@app.get("/host")
async def host_page():
    if not setupstate.is_complete():
        return RedirectResponse("setup", status_code=303)
    return page("host.html")


@app.get("/speaker")
async def speaker_page():
    if not setupstate.is_complete():
        return RedirectResponse("setup", status_code=303)
    return page("speaker.html")


@app.get("/setup")
async def setup_page():
    if setupstate.is_complete():
        return RedirectResponse("host", status_code=303)
    return FileResponse(os.path.join(STATIC, "setup.html"), headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ joining

class JoinBody(BaseModel):
    code: str
    name: str
    sid: str = ""   # a returning guest keeps their session (and their history in the feed)


@app.post("/api/join")
async def join(body: JoinBody, request: Request):
    require_code(request, body.code)
    name = clean_name(body.name)
    if not name:
        raise HTTPException(400, "enter your name (1-20 characters)")
    s = room.sessions.get(body.sid)
    if s:
        s.name = name
    else:
        if len(room.sessions) >= 1000:
            raise HTTPException(503, "room is full")
        sid = secrets.token_urlsafe(16)
        s = room.sessions[sid] = Session(sid, name)
    room.devices[s.sid] = device_name(request.headers.get("user-agent", ""))
    await room.broadcast()
    return {"ok": True, "sid": s.sid, "name": s.name}


@app.get("/api/peek")
async def peek(code: str, request: Request):
    """What's on the speaker, for the Join screen's teaser card (after the code checks out)."""
    require_code(request, code)
    n = room.now
    return {"now": n and {k: n.get(k) for k in ("title", "artists", "thumb", "by", "by_name", "initials", "videoId")},
            "speaker": room.speaker_state(), "people": len(room.people())}


# ------------------------------------------------------------------ search

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_BULK = 50  # tracks per "add all"


def remember(tracks: list[dict]):
    for t in tracks:
        search_cache[t["videoId"]] = t
    while len(search_cache) > 3000:
        search_cache.pop(next(iter(search_cache)))


def artist_names(item: dict) -> str:
    return ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name"))


@app.get("/api/search")
async def search(q: str, code: str, request: Request, kind: str = "songs"):
    require_code(request, code)
    q = q.strip()[:300]
    if not q:
        return {"results": []}
    # A pasted YouTube / YouTube Music link goes straight to that playlist or song.
    if m := re.search(r"[?&]list=([A-Za-z0-9_-]+)", q):
        return {"results": [], "open": {"kind": "playlist", "id": m[1]}}
    if m := re.search(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})", q):
        try:
            t = song_to_track(await asyncio.to_thread(ytm.get_song, m[1]))
        except Exception:
            t = None
        if t:
            remember([t])
        return {"results": [{**t, "kind": "song"}] if t else []}
    kind = kind if kind in ("all", "songs", "albums", "playlists") else "songs"
    # "All" = the three filtered searches in parallel; unfiltered results lack artist/album data.
    kinds = ("songs", "albums", "playlists") if kind == "all" else (kind,)
    limits = {"songs": 10, "albums": 6, "playlists": 6} if kind == "all" else {kind: 20}
    try:
        raws = await asyncio.gather(*(asyncio.to_thread(ytm.search, q, filter=k, limit=limits[k]) for k in kinds))
    except Exception as e:
        log.warning("search failed: %s", e)
        raise HTTPException(502, "search failed")
    return {"results": [r for k, raw in zip(kinds, raws) for r in format_results(k, raw)]}


def format_results(kind: str, raw: list[dict]) -> list[dict]:
    if kind == "songs":
        tracks = [t for t in map(to_track, raw) if t]
        remember(tracks)
        return [{**t, "kind": "song"} for t in tracks]
    out = []
    for r in raw:
        rid = r.get("browseId") or r.get("playlistId")
        if not rid:
            continue
        if kind == "albums":
            sub = [artist_names(r), r.get("year"), r.get("type")]
        else:
            sub = [r.get("author"), f"{r['itemCount']} songs" if r.get("itemCount") else None]
        out.append({"kind": kind[:-1], "id": rid, "title": r.get("title") or "Untitled",
                    "subtitle": " · ".join(str(x) for x in sub if x), "thumb": _thumb(r.get("thumbnails"))})
    return out


@app.get("/api/collection")
async def collection(kind: str, id: str, code: str, request: Request):
    """An album or playlist with its tracks, for browsing and "add all"."""
    require_code(request, code)
    if kind not in ("album", "playlist") or not re.fullmatch(r"[A-Za-z0-9_-]{2,80}", id):
        raise HTTPException(400)
    try:
        if kind == "album":
            a = await asyncio.to_thread(ytm.get_album, id)
            title, thumb = a.get("title") or "Album", _thumb(a.get("thumbnails"))
            subtitle = " · ".join(str(x) for x in [artist_names(a), a.get("year")] if x)
            tracks = [to_track(t, album=title, thumb=thumb) for t in a.get("tracks", [])]
        else:
            p = await asyncio.to_thread(ytm.get_playlist, id, limit=200)
            title, thumb = p.get("title") or "Playlist", _thumb(p.get("thumbnails"))
            author = p.get("author")
            author = author.get("name") if isinstance(author, dict) else author
            subtitle = " · ".join(str(x) for x in [author, f"{p['trackCount']} songs" if p.get("trackCount") else None] if x)
            tracks = [to_track(t, thumb=thumb) for t in p.get("tracks", [])]
    except Exception as e:
        log.warning("%s %s failed: %s", kind, id, e)
        raise HTTPException(502, f"could not load {kind}")
    tracks = [t for t in tracks if t]
    remember(tracks)
    thumb = thumb or next((t["thumb"] for t in tracks if t["thumb"]), None)
    return {"kind": kind, "id": id, "title": title, "subtitle": subtitle, "thumb": thumb, "tracks": tracks}


# ------------------------------------------------------------------ room controls (everyone)

class AddBody(Act):
    videoIds: list[str]   # one track, or a whole album/playlist ("add all")
    next: bool = False    # "play next" instead of the end of the queue


class QidBody(Act):
    qid: str


class MoveBody(QidBody):
    index: int                     # where it goes, counted without it
    from_index: int | None = None  # where the client saw it: if it's moved since, the drag is refused


class PauseBody(Act):
    paused: bool


class SeekBody(QidBody):
    position: float


class VolumeBody(Act):
    volume: int


def find(qid: str) -> dict | None:
    return next((t for t in room.queue + room.auto if t["qid"] == qid), None)


@app.post("/api/add")
async def add(body: AddBody, request: Request):
    actor = actor_for(request, body)
    ids = [v for v in body.videoIds if VIDEO_ID.fullmatch(v)][:MAX_BULK]
    if not ids:
        raise HTTPException(400, "bad videoId")
    if actor.key != "host" and limited(room.add_log, client_ip(request), ADD_LIMIT, ADD_WINDOW):
        raise HTTPException(429, "slow down")
    space = MAX_QUEUE - len(room.queue)
    if space <= 0:
        raise HTTPException(409, "queue full")
    tracks = []
    for vid in ids[:space]:
        t = search_cache.get(vid)
        if not t and len(ids) == 1:
            try:
                t = song_to_track(await asyncio.to_thread(ytm.get_song, vid))
            except Exception:
                t = None
        if t:
            tracks.append(t)
    if not tracks:
        raise HTTPException(404, "unknown track")
    async with room.lock:
        items = [room.item(t, actor) for t in tracks]
        if body.next:
            room.queue[0:0] = items
        else:
            room.queue.extend(items)
        added = {t["videoId"] for t in tracks}
        room.auto = [a for a in room.auto if a["videoId"] not in added]  # promoted from the radio list
        room.record(actor, "queue", f"queued {tracks[0]['title']}" if len(tracks) == 1 else f"queued {len(tracks)} songs")
        if room.now is None:
            room.paused = False
            await room.advance("first add")
        else:
            room.prefetch_next()
        # 0 = playing now, 1 = next in line, ...
        place = 0 if room.now is items[0] else room.queue.index(items[0]) + 1
    await room.broadcast()
    return {"ok": True, "added": len(items), "place": place}


@app.post("/api/remove")
async def remove(body: QidBody, request: Request):
    actor = control(request, body)
    async with room.lock:
        t = find(body.qid)
        if not t:
            return {"ok": False}
        (room.queue if t in room.queue else room.auto).remove(t)
        room.record(actor, "remove", f"removed {t['title']}")
        room.prefetch_next()
    await room.broadcast()
    return {"ok": True}


@app.post("/api/move")
async def move(body: MoveBody, request: Request):
    """Drag to reorder: anyone in the room, any song. Every drag runs under the room lock against the
    queue as it is now, so two people dragging at once can't corrupt it: a drag based on a stale view
    (from_index) is refused and the client redraws from the broadcast."""
    actor = control(request, body)
    async with room.lock:
        t = next((t for t in room.queue if t["qid"] == body.qid), None)
        if not t:
            raise HTTPException(409, "That song isn't in the queue any more.")
        old = room.queue.index(t)
        if body.from_index is not None and old != body.from_index:
            raise HTTPException(409, "The queue changed while you were dragging.")
        room.queue.remove(t)
        new = max(0, min(body.index, len(room.queue)))
        room.queue.insert(new, t)
        if new != old:
            room.record(actor, "reorder", f"moved {t['title']} to #{new + 1}")
            room.prefetch_next()
    await room.broadcast()
    return {"ok": True, "index": new}


@app.post("/api/pause")
async def pause(body: PauseBody, request: Request):
    actor = control(request, body)
    async with room.lock:
        if not room.now:
            raise HTTPException(409, "nothing playing")
        if room.paused != body.paused:
            room.paused = body.paused
            room.record(actor, "pause" if body.paused else "play", "paused" if body.paused else "pressed play")
    await room.broadcast()
    return {"ok": True}


def mmss(secs: float) -> str:
    return f"{int(secs) // 60}:{int(secs) % 60:02d}"


@app.post("/api/seek")
async def seek(body: SeekBody, request: Request):
    """Jump to a position in the current track. Same rights as pause and skip (anyone in the room),
    and it goes in the feed. The server sets the position; the speaker and every remote follow."""
    actor = control(request, body)
    if not math.isfinite(body.position):
        raise HTTPException(400, "bad position")
    async with room.lock:
        if not room.now or room.now["qid"] != body.qid:
            return {"ok": False, "stale": True}
        if room.loading:
            raise HTTPException(409, "The song is still loading.")
        dur = room.now.get("duration") or 0
        pos = max(0.0, min(body.position, dur) if dur else body.position)
        room.seek_to(pos)
        room.record(actor, "seek", f"jumped to {mmss(pos)} in {room.now['title']}")
    await room.broadcast()
    return {"ok": True, "position": pos}


@app.post("/api/skip")
async def skip(body: QidBody, request: Request):
    """Skips immediately. The qid guards against two people skipping at once and skipping two songs."""
    actor = control(request, body)
    async with room.lock:
        if not room.now or room.now["qid"] != body.qid:
            return {"ok": False, "stale": True}
        room.record(actor, "skip", f"skipped {room.now['title']}")
        await room.advance("skip")
    await room.broadcast()
    return {"ok": True}


@app.post("/api/back")
async def back(body: QidBody, request: Request):
    """Back, like any player: restart the song once it has played BACK_RESTART seconds (or when
    there's nothing before it), otherwise go to the previous one. Works after a skip: the server
    keeps the play history. Same rights as skip (anyone in the room), and it goes in the feed."""
    actor = control(request, body)
    async with room.lock:
        if room.now and room.now["qid"] != body.qid:
            return {"ok": False, "stale": True}
        if room.now and (room.position() > BACK_RESTART or not room.back_stack):
            if not room.loading:
                room.seek_to(0.0)
            room.record(actor, "back", f"restarted {room.now['title']}")
            did = "restarted"
        elif room.back_stack:
            room.go_back()
            room.record(actor, "back", f"went back to {room.now['title']}")
            did = "back"
        else:
            return {"ok": False}
    await room.broadcast()
    return {"ok": True, "did": did}


@app.post("/api/volume")
async def volume(body: VolumeBody, request: Request):
    """Guests are clamped to the host's cap; the host can go to 100."""
    actor = control(request, body)
    v = max(0, min(body.volume, 100 if actor.key == "host" else room.cap))
    async with room.lock:
        room.volume = v
        room.record(actor, "volume", f"set volume to {v}")
    await room.broadcast()
    return {"ok": True, "volume": v}


# ------------------------------------------------------------------ host only

class CapBody(BaseModel):
    cap: int


class OnBody(BaseModel):
    on: bool


class NameBody(BaseModel):
    name: str


def host_actor() -> Actor:
    n = host_name()
    return Actor("host", n, f"{n} (host)")


@app.get("/api/qr.svg")
async def qr(code: str, request: Request, base: str = ""):
    """QR for the invite link. Gated by the room code, which is on screen next to it anyway.

    `base` is the app address as the dashboard sees it (prefix included); PUBLIC_URL wins.
    Only the base is caller-supplied: the path and code are added here.
    """
    require_code(request, code)
    if not re.fullmatch(r"https?://[^\s?#]{1,200}", base):
        base = f"{request.url.scheme}://{request.headers.get('host', '')}"
    base = (public_url() or base).rstrip("/")
    buf = io.BytesIO()
    segno.make(f"{base}/?code={room.code}", error="m").save(buf, kind="svg", scale=10, border=2, dark="#000", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.post("/api/host/cap")
async def host_cap(body: CapBody, request: Request):
    require_host(request)
    cap = max(0, min(body.cap, 100))
    async with room.lock:
        room.cap = cap
        if room.volume > cap:
            room.volume = cap
        room.record(host_actor(), "volume", f"capped guest volume at {cap}")
    await room.broadcast()
    return {"ok": True, "cap": cap}


@app.post("/api/host/radio")
async def host_radio(body: OnBody, request: Request):
    require_host(request)
    async with room.lock:
        room.radio = body.on
        if not body.on:
            room.auto.clear()
        elif room.now and not room.queue:
            asyncio.create_task(room.refill_auto_and_broadcast(room.now))
    await room.broadcast()
    return {"ok": True}


@app.post("/api/host/clear")
async def host_clear(request: Request):
    """Remove every upcoming song people queued. The current song keeps playing, the play history
    (Back) stays, and so do the radio picks, which play next as with any empty queue. Host key only:
    a guest's request is refused here, not just by a hidden button."""
    require_host(request)
    async with room.lock:
        n = len(room.queue)
        room.queue.clear()
        room.record(host_actor(), "clear", f"cleared the queue ({n} song{'' if n == 1 else 's'})")
        room.prefetch_next()
        if room.radio and room.now and not room.auto:
            asyncio.create_task(room.refill_auto_and_broadcast(room.now))
    await room.broadcast()
    return {"ok": True, "removed": n}


@app.post("/api/host/name")
async def host_set_name(body: NameBody, request: Request):
    require_host(request)
    name = clean_name(body.name)
    if not name:
        raise HTTPException(400, "enter a name (1-20 characters)")
    state = setupstate.load_state()
    state["host_name"] = name
    setupstate.save_state(state)
    await room.broadcast()
    return {"ok": True, "name": name}


@app.post("/api/host/speaker-key")
async def host_rotate_speaker_key(request: Request):
    """New speaker key, shown once. The current speaker is disconnected and its stream token dies."""
    global SPEAKER_KEY
    require_host(request)
    if env_key("SPEAKER_KEY"):
        raise HTTPException(409, "SPEAKER_KEY is set in the environment; change it there.")
    SPEAKER_KEY = setupstate.new_speaker_key()
    log.info("speaker key rotated")
    await room.drop_speaker(4003, "speaker key rotated", {"type": "revoked"})
    return {"ok": True, "key": SPEAKER_KEY}


@app.post("/api/host/disconnect-speaker")
async def host_disconnect_speaker(request: Request):
    require_host(request)
    await room.drop_speaker(4004, "disconnected by the host", {"type": "disconnected"})
    return {"ok": True}


@app.get("/api/host/net")
async def host_net(request: Request):
    """Network totals since start: pushes to clients, fetches from YouTube, peak concurrency and bandwidth."""
    require_host(request)
    return net.snapshot()


@app.post("/api/new-session")
async def new_session(request: Request):
    require_host(request)
    await room.rotate()
    return {"ok": True, "code": room.code}


# ------------------------------------------------------------------ setup wizard + YouTube linking
# Credentials travel in headers (X-Setup-Token / X-Host-Key), never in URLs,
# so they can't end up in access logs or browser history.

def require_setup_token(request: Request):
    if setupstate.is_complete():
        raise HTTPException(410, "Setup is already complete.")
    status = check_secret(request.headers.get("x-setup-token", ""), setupstate.setup_token(), client_ip(request))
    if status:
        raise HTTPException(status, "Too many attempts. Try again later." if status == 429 else "That setup token isn't right. Copy it from the container logs.")


def require_linker(request: Request):
    """Cookie upload/probe: the setup token during setup, the host key afterwards."""
    if request.headers.get("x-host-key") is not None:
        require_host(request)
        return
    require_setup_token(request)


class SetupKeyBody(BaseModel):
    regenerate: bool = False


class SetupUrlBody(BaseModel):
    url: str


@app.post("/api/setup/verify")
async def setup_verify(request: Request):
    require_setup_token(request)
    host = request.headers.get("host", "")
    return {
        "host_key": "env" if env_key("HOST_KEY") else "stored" if setupstate.stored_host_key() else None,
        "speaker_key": "env" if env_key("SPEAKER_KEY") else "stored" if setupstate.stored_speaker_key() else None,
        "host_name": setupstate.load_state().get("host_name", ""),
        "public_url": public_url(),
        "public_url_env": bool(ENV_PUBLIC_URL),
        "suggested_url": f"{request.url.scheme}://{host}" if host else "",
        "cookies": resolver.cookie_health(),
        "probe_video": PROBE_VIDEO_ID,
    }


@app.post("/api/setup/host-name")
async def setup_host_name(body: NameBody, request: Request):
    require_setup_token(request)
    name = clean_name(body.name)
    if not name:
        raise HTTPException(400, "Enter your name (1-20 characters).")
    state = setupstate.load_state()
    state["host_name"] = name
    setupstate.save_state(state)
    return {"name": name}


@app.post("/api/setup/host-key")
async def setup_host_key(body: SetupKeyBody, request: Request):
    """Generate the host key and return it this once. Later calls only say it exists."""
    global HOST_KEY
    require_setup_token(request)
    if env_key("HOST_KEY"):
        return {"source": "env"}
    if setupstate.stored_host_key() and not body.regenerate:
        return {"source": "stored"}
    HOST_KEY = setupstate.new_host_key()
    log.info("setup: host key generated and saved")
    return {"source": "generated", "key": HOST_KEY}


@app.post("/api/setup/speaker-key")
async def setup_speaker_key(body: SetupKeyBody, request: Request):
    """Same as the host key: generated here, returned once."""
    global SPEAKER_KEY
    require_setup_token(request)
    if env_key("SPEAKER_KEY"):
        return {"source": "env"}
    if setupstate.stored_speaker_key() and not body.regenerate:
        return {"source": "stored"}
    SPEAKER_KEY = setupstate.new_speaker_key()
    log.info("setup: speaker key generated and saved")
    return {"source": "generated", "key": SPEAKER_KEY}


@app.post("/api/setup/public-url")
async def setup_public_url(body: SetupUrlBody, request: Request):
    require_setup_token(request)
    try:
        url = setupstate.check_public_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    state = setupstate.load_state()
    state["public_url"] = url
    setupstate.save_state(state)
    return {"public_url": url, "overridden_by_env": bool(ENV_PUBLIC_URL)}


@app.post("/api/setup/finish")
async def setup_finish(request: Request):
    require_setup_token(request)
    if not HOST_KEY:
        raise HTTPException(409, "Create the host key first.")
    if not SPEAKER_KEY:
        raise HTTPException(409, "Create the speaker key first.")
    setupstate.finish()
    log.info("setup: complete; the setup token is now invalid")
    await room.broadcast()
    return {"ok": True, "redirect": "host"}


@app.post("/api/youtube/cookies")
async def youtube_cookies(request: Request):
    """Upload cookies.txt as the raw request body (max 256 KB). Returns counts only."""
    require_linker(request)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > cookiejar.MAX_UPLOAD:
            break
    try:
        counts = await asyncio.to_thread(cookiejar.ingest, bytes(data), COOKIES)
    except cookiejar.Rejected as e:
        log.info("cookie upload rejected (%d bytes)", len(data))
        raise HTTPException(413 if len(data) > cookiejar.MAX_UPLOAD else 400, str(e))
    log.info("cookies saved: %d YouTube cookies, %d from other sites dropped", counts["youtube_cookies"], counts["dropped_other_sites"])
    await room.relinked()
    return {"ok": True, **counts}


@app.post("/api/youtube/probe")
async def youtube_probe(request: Request):
    """Check the saved cookies against YouTube: account, Premium, 256k AAC."""
    global last_probe
    require_linker(request)
    res = await asyncio.to_thread(probe_account)
    last_probe = res
    resolver.bot_check = res["problem"] in ("bot-check", "signed-out")  # -> "expired"
    await room.broadcast()
    return res


# ------------------------------------------------------------------ audio (the speaker only)

@app.get("/stream/{video_id}")
async def stream(video_id: str, request: Request, t: str = ""):
    """Serve the current track with HTTP Range support, from RAM when possible.

    Only the active speaker can fetch audio: `t` is the random token its
    websocket was given, which dies on takeover, disconnect or key rotation.
    The room code does not work here. Proxying is required because googlevideo
    URLs are signed for the server's IP and expire.
    """
    if not t:
        raise HTTPException(401, "speaker token required")
    status = check_secret(t, room.speaker.token if room.speaker else None, client_ip(request))
    if status:
        raise HTTPException(status, "too many attempts" if status == 429 else "not the active speaker")
    if resolver.cookie_health() == "missing":
        raise HTTPException(409, "YouTube is not linked")
    if not room.now or room.now["videoId"] != video_id:
        raise HTTPException(409, "not the current track")
    try:
        m = await resolver.load(video_id)
    except Exception as e:
        log.warning("load failed for %s: %s", video_id, e)
        raise HTTPException(502, "could not load stream")
    size = m.size
    start, end, code = 0, size - 1, 200
    rng = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", "").strip())
    if rng and (rng[1] or rng[2]):
        if rng[1]:
            start = int(rng[1])
            end = min(int(rng[2]), size - 1) if rng[2] else size - 1
        else:  # suffix range: last N bytes
            start = max(0, size - int(rng[2]))
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        code = 206
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
    if code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if m.data is not None:
        return Response(m.data[start:end + 1], status_code=code, headers=headers, media_type=m.mime)
    return StreamingResponse(resolver.iter_range(video_id, start, end), status_code=code,
                             headers=headers, media_type=m.mime)


# ------------------------------------------------------------------ websockets
# Every socket authenticates with its first message ({"type":"auth", ...}), so
# room codes and keys never appear in URLs or access logs.

async def first_message(ws: WebSocket) -> dict:
    try:
        msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        return msg if isinstance(msg, dict) and msg.get("type") == "auth" else {}
    except (asyncio.TimeoutError, ValueError):
        return {}


async def reject(ws: WebSocket, status: int, reason: str):
    with contextlib.suppress(Exception):
        await ws.close(code=4029 if status == 429 else 4003, reason=reason)


async def client_msg(ws: WebSocket):
    """Guests and hosts only send keepalives, or {"type":"resync"} after missing a patch."""
    try:
        resync = json.loads(await ws.receive_text()).get("type") == "resync"
    except (ValueError, AttributeError):
        return
    if resync:
        room.sync.forget(ws)
        await room.broadcast()


@app.websocket("/ws")
async def guest_ws(ws: WebSocket):
    """Guests: receive state and beats. They act over HTTP."""
    await ws.accept()
    msg = await first_message(ws)
    status = check_secret(str(msg.get("code", "")).upper(), room.code, client_ip(ws))
    if status:
        return await reject(ws, status, "bad room code")
    sid = str(msg.get("sid", ""))
    if sid not in room.sessions:
        with contextlib.suppress(Exception):
            await ws.close(code=4401, reason="join with your name first")
        return
    room.guests[ws] = sid
    await room.broadcast()  # people changed
    try:
        while True:
            await client_msg(ws)  # keepalives, and resync requests
    except WebSocketDisconnect:
        pass
    finally:
        room.guests.pop(ws, None)
        room.sync.forget(ws)
        await room.broadcast()


@app.websocket("/ws/host")
async def host_ws(ws: WebSocket):
    """Host dashboards (any number): state plus the host panel."""
    await ws.accept()
    msg = await first_message(ws)
    status = check_secret(str(msg.get("key", "")), HOST_KEY, client_ip(ws))
    if status:
        return await reject(ws, status, "bad host key")
    room.hosts.add(ws)
    await room.broadcast()
    try:
        while True:
            await client_msg(ws)
    except WebSocketDisconnect:
        pass
    finally:
        room.hosts.discard(ws)
        room.sync.forget(ws)
        await room.broadcast()


@app.websocket("/ws/speaker")
async def speaker_ws(ws: WebSocket):
    """The speaker. The newest device with the key takes over; the old one is told and dropped.

    In:  {"type":"beat", qid, pos, paused, volume, buffering, locked, seek}  ~1/s  (seek: last seek id applied)
         {"type":"ended", qid} / {"type":"error", qid, detail} / {"type":"name", device}
    Out: {"type":"hello", token}, state snapshots, {"type":"taken_over"}.
    """
    await ws.accept()
    msg = await first_message(ws)
    status = check_secret(str(msg.get("key", "")), SPEAKER_KEY, client_ip(ws))
    if status:
        return await reject(ws, status, "bad speaker key")
    device = clean_name(msg.get("device")) or device_name(ws.headers.get("user-agent", ""))
    if room.speaker:
        log.info("speaker %s taken over by %s", room.speaker.device, device)
        await room.drop_speaker(4002, "taken over by another device", {"type": "taken_over", "by": device})
    sp = room.speaker = Speaker(ws, device, device_name(ws.headers.get("user-agent", "")))
    sp.last_beat = time.monotonic()
    log.info("speaker connected: %s (%s)", sp.device, sp.agent)
    await room._send(ws, {"type": "hello", "token": sp.token})
    await room.broadcast()
    try:
        while True:
            try:
                m = json.loads(await ws.receive_text())
            except ValueError:
                continue
            if room.speaker is not sp:
                break
            await speaker_msg(sp, m)
    except WebSocketDisconnect:
        pass
    finally:
        room.sync.forget(ws)
        if room.speaker is sp:
            room.speaker = None
            room.playing = False
            log.info("speaker %s disconnected; paused at %.1fs", sp.device, room.pos)
            await room.broadcast()


async def speaker_msg(sp: Speaker, m: dict):
    kind, qid = m.get("type"), m.get("qid")
    if kind == "beat":
        was = (sp.online, sp.locked, sp.buffering, room.playing)
        sp.last_beat = time.monotonic()
        sp.online = True
        sp.locked = bool(m.get("locked"))
        sp.buffering = bool(m.get("buffering"))
        with contextlib.suppress(TypeError, ValueError):
            sp.volume = int(m.get("volume"))
        # A beat from before the speaker applied the latest seek still carries the old position.
        caught_up = "seek" not in m or m.get("seek") == room.seek_id
        if room.now and qid == room.now["qid"] and caught_up:
            with contextlib.suppress(TypeError, ValueError):
                room.pos = max(0.0, float(m.get("pos")))
                room.pos_at = sp.last_beat
            room.playing = not m.get("paused") and not sp.buffering and not sp.locked
        elif room.now and qid == room.now["qid"]:
            pass  # mid-seek: keep the position the server set
        else:
            room.playing = False
        if was != (sp.online, sp.locked, sp.buffering, room.playing):
            await room.broadcast()
        else:
            await room.broadcast_beat()
    elif kind == "name":
        sp.device = clean_name(m.get("device")) or sp.device
        await room.broadcast()
    elif kind == "resync":
        room.sync.forget(sp.ws)
        await room.broadcast()
    elif kind in ("ended", "error"):
        async with room.lock:
            if not room.now or room.now["qid"] != qid:
                return  # stale: we've already moved past that track
            if kind == "ended":
                room.fails = 0
                await room.advance("ended")
            else:
                await room.failed(f"speaker could not play it ({str(m.get('detail'))[:80]})")
        await room.broadcast()
