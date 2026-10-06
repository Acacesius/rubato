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


def test_drag_is_for_everyone_and_stale_drags_are_refused(client):
    sid = join(client)
    post(client, "/api/add", sid, videoIds=ids(5))   # one plays, four queued
    q = [t["qid"] for t in main.room.queue]
    r = post(client, "/api/move", sid, qid=q[3], index=0, from_index=3)
    assert r.status_code == 200 and r.json()["index"] == 0
    assert [t["qid"] for t in main.room.queue] == [q[3], q[0], q[1], q[2]]
    assert main.room.feed[-1]["kind"] == "reorder"
    # Someone else's drag landed first: q[1] is no longer where this client saw it.
    assert post(client, "/api/move", sid, qid=q[1], index=0, from_index=1).status_code == 409
    assert post(client, "/api/move", sid, qid="gone", index=0, from_index=0).status_code == 409
    assert post(client, "/api/move", qid=q[1], index=0).status_code == 401   # needs a session


def test_concurrent_drags_never_corrupt_the_queue(client):
    import random
    from concurrent.futures import ThreadPoolExecutor
    sid = join(client)
    post(client, "/api/add", headers=H, videoIds=ids(12))
    before = sorted(t["qid"] for t in main.room.queue)

    def drag(i):
        rnd = random.Random(i)
        q = list(main.room.queue)
        frm = rnd.randrange(len(q))
        return post(client, "/api/move", headers=H, qid=q[frm]["qid"], index=rnd.randrange(len(q)), from_index=frm).status_code
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(drag, range(60)))
    assert set(codes) <= {200, 409}
    assert sorted(t["qid"] for t in main.room.queue) == before


def test_seek_clamps_is_attributed_and_ignores_old_speaker_beats(client):
    sid = join(client)
    post(client, "/api/add", sid, videoIds=ids(2))
    room = main.room
    qid = room.now["qid"]
    assert post(client, "/api/seek", sid, qid=qid, position=30).status_code == 409   # still loading
    room.loading = False
    r = post(client, "/api/seek", sid, qid=qid, position=95.5).json()
    assert r == {"ok": True, "position": 95.5} and room.pos == 95.5
    assert room.snapshot()["seek"] == {"id": 1, "pos": 95.5}
    assert post(client, "/api/seek", sid, qid=qid, position=9999).json()["position"] == 200   # track length
    assert post(client, "/api/seek", sid, qid=qid, position=-5).json()["position"] == 0
    assert post(client, "/api/seek", sid, qid="stale", position=10).json() == {"ok": False, "stale": True}
    assert post(client, "/api/seek", qid=qid, position=10).status_code == 401   # needs a session
    assert room.feed[-1]["kind"] == "seek" and room.feed[-1]["text"].startswith("jumped to 0:00")
    post(client, "/api/seek", headers=H, qid=qid, position=120)
    assert room.feed[-1]["who"].endswith("(host)")
    # The speaker hasn't applied seek #4 yet: its beat from the old spot doesn't move the position.
    with client.websocket_connect("/ws/speaker") as sp:
        sp.send_json({"type": "auth", "key": "test-speaker-key"})
        sp.receive_json()
        sp.send_json({"type": "beat", "qid": qid, "pos": 7.0, "paused": False, "seek": room.seek_id - 1})
        sp.send_json({"type": "name", "device": "x"})   # a round trip, so the beat has been handled
        while sp.receive_json().get("type") not in ("state", "patch"):
            pass
        assert abs(room.pos - 120) < 0.01
        sp.send_json({"type": "beat", "qid": qid, "pos": 121.0, "paused": False, "seek": room.seek_id})
        sp.send_json({"type": "name", "device": "y"})
        for _ in range(3):
            if abs(room.pos - 121) < 0.01:
                break
            sp.receive_json()
        assert abs(room.pos - 121) < 0.01


def test_back_restarts_then_goes_to_previous_even_after_skip(client):
    sid = join(client)
    post(client, "/api/add", sid, videoIds=ids(3))
    room = main.room
    first = room.now
    # Nothing before the first song: Back restarts it.
    assert post(client, "/api/back", sid, qid=first["qid"]).json()["did"] == "restarted"
    assert post(client, "/api/skip", sid, qid=first["qid"]).json()["ok"]
    second = room.now
    # Just started (< 3 s): Back goes to the previous song; the one we left returns to the front.
    r = post(client, "/api/back", sid, qid=second["qid"]).json()
    assert r["did"] == "back" and room.now["videoId"] == first["videoId"] and room.now["qid"] != first["qid"]
    assert room.queue[0] is second and room.feed[-1]["text"].startswith("went back to") and room.feed[-1]["who"] == "Sam"
    # Played a while: Back restarts instead, and the speaker is told to jump (seek id).
    room.loading, room.pos, room.pos_at = False, 42.0, time.monotonic()
    sid0 = room.seek_id
    assert post(client, "/api/back", headers=H, qid=room.now["qid"]).json()["did"] == "restarted"
    assert room.position() == 0.0 and room.seek_id == sid0 + 1 and room.now["videoId"] == first["videoId"]
    assert post(client, "/api/back", sid, qid="old").json() == {"ok": False, "stale": True}
    assert post(client, "/api/back", qid=room.now["qid"]).status_code == 401
    assert room.snapshot()["can_back"] is False   # first song again: nothing before it


def test_clear_queue_is_host_only_and_keeps_now_and_history(client):
    sid = join(client)
    post(client, "/api/add", sid, videoIds=ids(5))
    room = main.room
    post(client, "/api/skip", sid, qid=room.now["qid"])
    now, history = room.now, list(room.back_stack)
    assert post(client, "/api/host/clear", sid).status_code == 403          # a guest, room code and all
    assert client.post("/api/host/clear", headers={"X-Host-Key": "wrong"}).status_code == 403
    assert len(room.queue) == 3
    assert client.post("/api/host/clear", headers=H).json() == {"ok": True, "removed": 3}
    assert room.queue == [] and room.now is now and list(room.back_stack) == history
    assert room.feed[-1]["kind"] == "clear" and room.feed[-1]["text"] == "cleared the queue (3 songs)"


class FakeYT:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def rate_song(self, vid, rating):
        if self.fail:
            raise RuntimeError("Server returned HTTP 401: Unauthorized")
        self.calls.append((vid, rating))
        return {"actions": []}

    def get_watch_playlist(self, videoId, limit=1):
        return {"tracks": [{"videoId": videoId, "likeStatus": "INDIFFERENT"}]}


def test_like_is_host_only_reports_errors_and_unlikes(client, monkeypatch):
    sid = join(client)
    room = main.room
    post(client, "/api/add", sid, videoIds=ids(1))
    vid = room.now["videoId"]
    yt = FakeYT()
    monkeypatch.setattr(main, "cookie_ytmusic", lambda path=None: yt)
    monkeypatch.setattr(main.resolver, "cookie_health", lambda: "valid")
    # A guest (room code and session) is refused by the server, whatever they send.
    assert post(client, "/api/host/like", sid, videoId=vid, on=True).status_code == 403
    assert yt.calls == []
    r = client.post("/api/host/like", json={"videoId": vid, "on": True}, headers=H)
    assert r.json() == {"ok": True, "liked": True} and yt.calls == [(vid, "LIKE")]
    assert room.snapshot("host")["liked"] is True
    assert "liked" not in room.snapshot()   # guests and the speaker never see it
    assert room.feed[-1]["kind"] == "like"
    client.post("/api/host/like", json={"videoId": vid, "on": False}, headers=H)
    assert yt.calls[-1] == (vid, "INDIFFERENT") and room.liked[vid] is False
    # YouTube says no: the host sees the error and nothing pretends it worked.
    monkeypatch.setattr(main, "cookie_ytmusic", lambda path=None: FakeYT(fail=True))
    r = client.post("/api/host/like", json={"videoId": vid, "on": True}, headers=H)
    assert r.status_code == 502 and "Nothing was changed" in r.json()["detail"] and room.liked[vid] is False


def test_artist_is_cached_per_artist_trimmed_and_fails_quietly(client, monkeypatch):
    main.artist_cache.clear()
    main.artist_misses.clear()
    calls = []
    bio = "Daft Punk were a French electronic music duo. " * 12 + "From Wikipedia (https://en.wikipedia.org/wiki/Daft_Punk)"

    def get_artist(cid):
        calls.append(cid)
        if cid.endswith("bad"):
            raise RuntimeError("nope")
        return {"name": "Daft Punk", "description": bio, "thumbnails": [{"url": "https://lh3.googleusercontent.com/abc=w540-h225-p-l90-rj"}]}
    monkeypatch.setattr(main.ytm, "get_artist", get_artist)
    good, bad = "UC" + "a" * 22, "UC" + "b" * 19 + "bad"
    q = lambda cid, code=None: client.get(f"/api/artist?id={cid}&code={code or main.room.code}")
    a = q(good).json()
    assert a["name"] == "Daft Punk" and a["art"].startswith("art/")
    assert len(a["description"]) <= main.BIO_MAX and a["description"].endswith(".") and "Wikipedia" not in a["description"]
    for _ in range(5):  # a queue full of one band: one lookup
        assert q(good).json() == a
    assert calls == [good]
    assert q(bad).json() == {"id": bad, "name": None, "description": None, "art": None}
    q(bad)
    assert calls == [good, bad]   # the miss is remembered too
    assert q(good, "WRONG").status_code == 403
    assert q("not-an-id").status_code == 400
    assert main.short_bio("One two three four five six", 12) == "One two…"


def test_tracks_carry_artist_id():
    from app.media import to_track
    t = to_track({"videoId": "x" * 11, "title": "T", "artists": [{"name": "A", "id": "UC" + "c" * 22}]})
    assert t["artistId"] == "UC" + "c" * 22
