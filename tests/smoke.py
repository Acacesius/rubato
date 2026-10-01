#!/usr/bin/env python3
"""Smoke test for a rubato image. Starts a throwaway container with an empty
config and the same hardening as docker-compose.yml, checks it, removes it.
Used by CI before publishing; runnable locally:

    python3 tests/smoke.py --image rubato:dev

Checks: healthcheck goes healthy on a read-only root filesystem with no
capabilities; runs as non-root; the setup token is printed and /setup is gated
by it; /host and /speaker redirect to /setup; setup can be completed through
the API; the audio endpoint refuses the room code (401/403); the in-image
selftest (ytmusicapi search + yt-dlp stream resolution) passes.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def sh(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def request(url, method="GET", headers=None, body=None):
    opener = urllib.request.build_opener(NoRedirect)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {**(headers or {}), **({"Content-Type": "application/json"} if data else {})}
    try:
        r = opener.open(urllib.request.Request(url, method=method, headers=hdrs, data=data), timeout=10)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    args = ap.parse_args()
    port, cfg = free_port(), tempfile.mkdtemp(prefix="rubato-smoke-")
    os.chmod(cfg, 0o777)  # the container user must be able to write it, whatever the runner's uid
    name = f"rubato-smoke-{port}"
    failures = []

    def check(label, ok, extra=""):
        print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + extra) if extra else ''}")
        if not ok:
            failures.append(label)

    sh("docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8766", "-v", f"{cfg}:/app/config",
       "--read-only", "--tmpfs", "/tmp:size=256m,mode=1777", "--cap-drop", "ALL",
       "--security-opt", "no-new-privileges:true", "--memory", "1g", "--pids-limit", "256", args.image)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            health = sh("docker", "inspect", "-f", "{{.State.Health.Status}}", name, check=False).stdout.strip()
            if health == "healthy":
                break
            time.sleep(2)
        check("healthcheck reports healthy (read-only rootfs, cap_drop ALL)", health == "healthy", health)
        uid = sh("docker", "exec", name, "id", "-u").stdout.strip()
        check("runs as non-root", uid != "0", f"uid {uid}")
        logs = sh("docker", "logs", name).stdout + sh("docker", "logs", name).stderr
        m = re.search(r"setup token:\s+([A-Za-z0-9_-]{20,})", logs)
        check("setup token printed in logs", bool(m))
        token = m.group(1) if m else ""
        status, headers, _ = request(f"{base}/host")
        check("/host redirects to /setup before setup", status == 303 and headers.get("location") == "setup", str(status))
        status, headers, _ = request(f"{base}/speaker")
        check("/speaker redirects to /setup before setup", status == 303 and headers.get("location") == "setup", str(status))
        check("/setup is served", request(f"{base}/setup")[0] == 200)
        check("setup API rejects missing token", request(f"{base}/api/setup/verify", "POST")[0] == 403)
        check("setup API rejects wrong token", request(f"{base}/api/setup/verify", "POST", {"X-Setup-Token": "wrong"})[0] == 403)
        T = {"X-Setup-Token": token}
        check("setup API accepts the printed token", request(f"{base}/api/setup/verify", "POST", T)[0] == 200)
        check("setup: host name", request(f"{base}/api/setup/host-name", "POST", T, {"name": "Smoke"})[0] == 200)
        s1, _, b1 = request(f"{base}/api/setup/host-key", "POST", T, {})
        s2, _, b2 = request(f"{base}/api/setup/speaker-key", "POST", T, {})
        host_key, speaker_key = json.loads(b1).get("key", ""), json.loads(b2).get("key", "")
        check("setup: host key and speaker key generated, and different", s1 == s2 == 200 and host_key and speaker_key and host_key != speaker_key)
        check("setup: finish", request(f"{base}/api/setup/finish", "POST", T)[0] == 200)
        logs = sh("docker", "logs", name).stdout + sh("docker", "logs", name).stderr
        code = re.findall(r"room code ([A-Z0-9]{5})", logs)[-1]
        check("room code works for the guest API", request(f"{base}/api/join", "POST", body={"code": code, "name": "Smoke guest"})[0] == 200)
        a = request(f"{base}/stream/BSTsnWoslP4?code={code}")[0]
        b = request(f"{base}/stream/BSTsnWoslP4?t={code}")[0]
        c = request(f"{base}/stream/BSTsnWoslP4?t={speaker_key}")[0]
        check("audio refuses the room code (401 as ?code=, 403 as ?t=), and the raw speaker key", a == 401 and b == 403 and c == 403, f"{a} {b} {c}")
        check("host API refuses the speaker key", request(f"{base}/api/new-session", "POST", {"X-Host-Key": speaker_key})[0] == 403)
        st = sh("docker", "exec", name, "python", "-m", "app.selftest", check=False)
        try:
            res = json.loads(st.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            res = {"result": "fail", "detail": st.stderr[-300:]}
        check("selftest: deno + ytmusicapi + yt-dlp", res.get("result") == "pass",
              f"stream={res.get('stream')} search={res.get('search')} {res.get('detail') or ''}".strip())
    finally:
        sh("docker", "rm", "-f", name, check=False)
        sh("docker", "run", "--rm", "-v", f"{cfg}:/c", "--entrypoint", "sh", args.image, "-c", "rm -rf /c/* /c/.[!.]*", check=False)
    print("\nSMOKE", "FAILED: " + ", ".join(failures) if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
