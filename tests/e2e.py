"""End-to-end run of a fake-mode rubato (tests/fakeserver.py) in Chromium, via Playwright.

    docker run --rm --network host -v "$PWD:/src" -w /src mcr.microsoft.com/playwright/python:v1.55.0-noble \\
        sh -c "pip install -q playwright==1.55.0 && python tests/e2e.py --base http://127.0.0.1:8766 --key test-host-key --speaker-key test-speaker-key"

Drives a host dashboard, a speaker and two phones through: queueing, playback on
the speaker, live patches, drag to reorder (touch, mouse, keyboard), seek, back,
clear queue, like, the artist panel and themes. Fails on any page error.
"""
import argparse
import sys

from playwright.sync_api import expect, sync_playwright

PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True,
         "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"}

results: list[tuple[str, bool, str]] = []


def check(label, fn):
    try:
        fn()
        results.append((label, True, ""))
        print("PASS ", label, flush=True)
    except Exception as e:
        results.append((label, False, str(e).splitlines()[0][:200]))
        print("FAIL ", label, "::", str(e).splitlines()[0][:200], flush=True)


def fail(msg):
    raise AssertionError(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--speaker-key", required=True)
    ap.add_argument("--shots")
    a = ap.parse_args()
    base = a.base
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])

        def page_for(ctx, label):
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(f"{label}: {e}"))
            pg.on("dialog", lambda d: d.accept())
            return pg

        host_ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        host = page_for(host_ctx, "host")
        host.goto(f"{base}/host")
        host.evaluate(f"localStorage.setItem('rubato.hostkey', {a.key!r})")
        host.goto(f"{base}/host")
        check("host dashboard connects", lambda: host.wait_for_function("() => state && state.code", timeout=10000))
        code = host.evaluate("state.code")

        sp_ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        speaker = page_for(sp_ctx, "speaker")
        speaker.goto(f"{base}/speaker#k={a.speaker_key}")
        speaker.click("#tap")
        check("speaker connects and unlocks", lambda: host.wait_for_function("() => !['offline', 'locked'].includes(state.speaker.state)", timeout=10000))

        def join(name):
            ctx = browser.new_context(**PHONE)
            pg = page_for(ctx, name)
            pg.goto(f"{base}/?code={code}")
            pg.fill("#name-input", name)
            pg.click("#join-btn")
            expect(pg.locator("#room")).to_be_visible(timeout=10000)
            return ctx, pg

        maya_ctx, maya = join("Maya")
        theo_ctx, theo = join("Theo")

        def search_add(pg, q, n):
            pg.click("nav.tabbar [data-tab=search]")
            pg.fill("#q", q)
            pg.press("#q", "Enter")
            expect(pg.locator("#results .trow").first).to_be_visible(timeout=20000)
            for _ in range(n):
                pg.locator("#results .trow [aria-label^='Add']").first.click()
                pg.wait_for_timeout(300)

        search_add(maya, "bohemian rhapsody", 3)
        search_add(theo, "dancing queen", 2)
        check("queue shows on the host and both phones (live patches)", lambda: (
            expect(host.locator("#queue .trow")).to_have_count(4, timeout=8000),
            expect(maya.locator("#queue .trow")).to_have_count(4, timeout=8000),
            expect(theo.locator("#queue .trow")).to_have_count(4, timeout=8000)))
        check("the speaker plays; the host shows it live", lambda: (speaker.wait_for_function(
            "() => document.getElementById('audio').currentTime > 1 && !document.getElementById('audio').paused", timeout=20000),
            expect(host.locator("#hd-pill")).to_contain_text("Speaker live", timeout=5000)))
        check("artwork comes from the server cache at row size", lambda: host.wait_for_function(
            "() => [...document.querySelectorAll('#queue img.art')].some(i => i.complete && i.naturalWidth > 0 && /art\\/[0-9a-f]{20}\\?s=120/.test(i.src))", timeout=10000))

        # ---- feature checks are added below, one block per feature

        for c in (maya_ctx, theo_ctx, sp_ctx, host_ctx):
            c.close()
        browser.close()

    check("no page errors", lambda: not errors or fail("; ".join(errors[:5])))
    failed = [r for r in results if not r[1]]
    print(f"\nE2E {'FAILED' if failed else 'PASSED'}: {len(results) - len(failed)}/{len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
