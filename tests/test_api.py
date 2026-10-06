"""Server behaviour through the real FastAPI app: patches, reorder, seek, back, clear, like, artist.

No YouTube: there are no cookies, so tracks never load, and radio is off.
"""
import os
import tempfile
import time

os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp())
os.environ["HOST_KEY"] = "test-host-key"
os.environ["SPEAKER_KEY"] = "test-speaker-key"

import pytest
from fastapi.testclient import TestClient

from app import main

H = {"X-Host-Key": "test-host-key"}


@pytest.fixture()
def client():
    main.room.__init__()
    main.room.radio = False
    main.fail_log.clear()
    for i in range(40):
        vid = f"vid{i:08d}"
        main.search_cache[vid] = {"videoId": vid, "title": f"Song {i}", "artists": "X", "album": None, "duration": 200, "thumb": None}
    with TestClient(main.app) as c:
        yield c


def join(c, name="Sam") -> str:
    return c.post("/api/join", json={"code": main.room.code, "name": name}).json()["sid"]


def post(c, path, sid="", headers=None, **body):
    return c.post(path, json={"code": main.room.code, "sid": sid, **body}, headers=headers or {})


def ids(n, start=0):
    return [f"vid{i:08d}" for i in range(start, start + n)]


def test_socket_gets_full_state_then_queue_patches(client):
    sid = join(client)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "code": main.room.code, "sid": sid})
        first = ws.receive_json()
        assert first["type"] == "state" and first["queue"] == [] and "server_time" in first
        post(client, "/api/add", headers=H, videoIds=ids(3))   # one plays, two queued
        m = ws.receive_json()
        while m["type"] != "patch" or "q" not in m:
            m = ws.receive_json()
        assert m["q"]["base"] == first["qver"] and len(m["q"]["put"]) == 2 and all(isinstance(p[1], dict) for p in m["q"]["put"])
        q = [t["qid"] for t in main.room.queue]
        post(client, "/api/move", headers=H, qid=q[1], index=0)
        m = ws.receive_json()
        while "q" not in m:
            m = ws.receive_json()
        assert len(m["q"]["put"]) == 1 and isinstance(m["q"]["put"][0][1], str) and m["q"]["rm"] == []   # a move is one bare qid
        ws.send_json({"type": "resync"})
        m = ws.receive_json()
        while m["type"] != "state":
            m = ws.receive_json()
        assert [t["qid"] for t in m["queue"]] == [q[1], q[0]]


def test_feed_uses_timestamps_not_ago(client):
    sid = join(client)
    post(client, "/api/add", sid, videoIds=ids(1))
    f = main.room.snapshot()["feed"][0]
    assert "ago" not in f and "key" not in f and abs(f["ts"] - time.time()) < 5
