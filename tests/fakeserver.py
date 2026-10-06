"""Run rubato with synthetic audio, for end-to-end tests without a linked YouTube account.

Search, albums and radio still use the real ytmusicapi; only the audio is
replaced: each track becomes a short sine tone (pitch derived from its videoId),
so tests can hear "ended" quickly. Not part of the image (.dockerignore drops
tests/); mount it in:

    docker run --rm -p 127.0.0.1:18766:8766 -v "$PWD/tests:/app/tests:ro" -v "$CFG:/app/config" \
        -e FAKE_SECONDS=20 rubato:dev python -m tests.fakeserver

FAKE_NO_ART=1 drops album art (for screenshots); FAKE_CODE=XXXXX pins the room code.
"""
import asyncio
import io
import math
import os
import struct
import time
import wave
import zlib

import uvicorn

from app import media

SECONDS = float(os.environ.get("FAKE_SECONDS", "20"))
RATE = 22050


def tone(video_id: str) -> bytes:
    freq = 220 * 2 ** ((zlib.crc32(video_id.encode()) % 24) / 12)  # two octaves above A3
    n = int(SECONDS * RATE)
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / 2000, (n - i) / 2000)
        frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / RATE)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


async def _download(self, video_id: str):
    await asyncio.sleep(0.3)  # a little "fetch" time, so loading states show
    data = await asyncio.to_thread(tone, video_id)
    return media.Media(data, len(data), "audio/wav", SECONDS)


class FakeAccount:
    """Stands in for ytmusicapi signed in as the host (the Like button)."""
    liked: dict = {}

    def rate_song(self, video_id, rating="INDIFFERENT"):
        self.liked[video_id] = rating
        return {"actions": []}

    def get_watch_playlist(self, videoId, limit=1):
        return {"tracks": [{"videoId": videoId, "likeStatus": self.liked.get(videoId, "INDIFFERENT")}]}


def fake_upstream(size: int) -> int:
    """FAKE_UPSTREAM=1 (tests/netbench.py): keep the real download path (prefetch, RAM cache, network
    accounting) and fake only YouTube: yt-dlp "resolves" in a second to a local server that serves
    `size` bytes per track (a tone, padded) with Range support. Returns the port."""
    import http.server
    import socketserver
    import threading

    import functools
    body_of = functools.lru_cache(8)(lambda vid: tone(vid).ljust(size, b"\0"))

    class Upstream(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, like googlevideo

        def do_GET(self):
            body = body_of(self.path.split("?")[0].strip("/"))
            start, end = 0, size - 1
            if (r := self.headers.get("Range", "")).startswith("bytes="):
                a, b = r[6:].split("-")
                start, end = int(a or 0), min(int(b or size - 1), size - 1)
            self.send_response(206 if r else 200)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self.wfile.write(body[start:end + 1])

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def extract(video_id):
        time.sleep(1.0)  # yt-dlp takes a few seconds per lookup in real life
        return {"url": f"http://127.0.0.1:{port}/{video_id}?clen={size}&expire={int(time.time()) + 3600}&dur={SECONDS}",
                "ext": "wav", "format_id": "0", "acodec": "pcm", "abr": 352}

    media._extract = extract
    return port


if os.environ.get("FAKE_UPSTREAM"):
    fake_upstream(int(os.environ.get("FAKE_BYTES", str(7 * 1024 * 1024))))
else:
    media.Resolver._download = _download
if os.environ.get("FAKE_NO_ART"):  # screenshots: no real album art, the UI's initials tiles instead
    media._thumb = lambda thumbs: None
media.Resolver.cookie_health = lambda self: "valid"
media.Resolver.stream_format = lambda self, vid: "WAV test tone · itag 0" if vid in self.media else None
media.probe_account = lambda: {"account": True, "name": "Test", "premium": True, "aac256": True, "problem": None, "video": "x"}

if __name__ == "__main__":
    from app import main
    main.cookie_ytmusic = lambda path=None: FakeAccount()
    if os.environ.get("FAKE_CODE"):  # screenshots: a fixed, obviously fake room code
        main.room.code = os.environ["FAKE_CODE"]
    uvicorn.run(main.app, host="0.0.0.0", port=8766, log_level="info")
