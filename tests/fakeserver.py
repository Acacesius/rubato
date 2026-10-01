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


media.Resolver._download = _download
if os.environ.get("FAKE_NO_ART"):  # screenshots: no real album art, the UI's initials tiles instead
    media._thumb = lambda thumbs: None
media.Resolver.cookie_health = lambda self: "valid"
media.Resolver.stream_format = lambda self, vid: "WAV test tone · itag 0" if vid in self.media else None
media.probe_account = lambda: {"account": True, "name": "Test", "premium": True, "aac256": True, "problem": None, "video": "x"}

if __name__ == "__main__":
    from app import main
    if os.environ.get("FAKE_CODE"):  # screenshots: a fixed, obviously fake room code
        main.room.code = os.environ["FAKE_CODE"]
    uvicorn.run(main.app, host="0.0.0.0", port=8766, log_level="info")
