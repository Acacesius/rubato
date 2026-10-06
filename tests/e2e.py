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

        # Drag to reorder: anyone, any song. Theo by touch (real touch events), the host by mouse and
        # keyboard. The server decides; every client redraws from the broadcast.
        theo.click("nav.tabbar [data-tab=queue]")
        theo.wait_for_timeout(600)
        rows_t = theo.locator("#queue .trow")
        moved = rows_t.last.locator(".t").inner_text()
        g, top = rows_t.last.locator(".grip").bounding_box(), rows_t.first.bounding_box()
        cdp_t = theo_ctx.new_cdp_session(theo)
        x, y = g["x"] + g["width"] / 2, g["y"] + g["height"] / 2
        cdp_t.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for k in range(1, 11):
            cdp_t.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y + (top["y"] - 10 - y) * k / 10}]})
            theo.wait_for_timeout(30)
        cdp_t.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        check("guest touch-drags the last song to the top; the host follows", lambda: (
            expect(rows_t.first.locator(".t")).to_have_text(moved, timeout=5000),
            expect(host.locator("#queue .trow").first.locator(".t")).to_have_text(moved, timeout=5000)))
        rows = host.locator("#queue .trow")
        last = rows.last.locator(".t").inner_text()
        g, first_box = rows.last.locator(".grip").bounding_box(), rows.first.bounding_box()
        host.mouse.move(g["x"] + g["width"] / 2, g["y"] + g["height"] / 2)
        host.mouse.down()
        for k in range(1, 13):
            host.mouse.move(g["x"] + g["width"] / 2, g["y"] + (first_box["y"] + 5 - g["y"]) * k / 12)
            host.wait_for_timeout(25)
        host.mouse.up()
        check("host mouse-drags the last song to the top; phones follow", lambda: (
            expect(rows.first.locator(".t")).to_have_text(last, timeout=5000),
            expect(rows_t.first.locator(".t")).to_have_text(last, timeout=5000)))
        second = rows.nth(1).locator(".t").inner_text()
        rows.nth(1).locator(".grip").focus()
        host.keyboard.press("ArrowUp")
        check("keyboard: ArrowUp on a grip moves it up and keeps focus", lambda: (
            expect(rows.first.locator(".t")).to_have_text(second, timeout=5000),
            host.wait_for_function("t => document.activeElement.classList.contains('grip') && document.activeElement.closest('.trow').querySelector('.t').textContent === t", arg=second, timeout=3000)))
        check("hands on it: the drags are attributed", lambda: expect(host.locator("#feed")).to_contain_text("Theo moved", timeout=5000))

        # Seek: Maya scrubs the spectral bar by touch; nothing is sent until she lets go, then one seek
        # moves the room, the speaker jumps there, and the feed says who. The host steps with arrow keys.
        maya.click("nav.tabbar [data-tab=speaker]")
        maya.wait_for_function("() => state.status === 'playing' && state.now.duration", timeout=20000)
        seeks = []
        maya.on("request", lambda r: "/api/seek" in r.url and seeks.append(r.url))
        bar = maya.locator("#prog-slot .spectrum").bounding_box()
        cy = bar["y"] + bar["height"] / 2
        cdp_m = maya_ctx.new_cdp_session(maya)
        cdp_m.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": bar["x"] + bar["width"] * 0.1, "y": cy}]})
        for k in range(1, 9):
            cdp_m.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": bar["x"] + bar["width"] * (0.1 + 0.6 * k / 8), "y": cy}]})
            maya.wait_for_timeout(40)
        sent_while_dragging = len(seeks)
        preview = maya.evaluate("() => document.querySelector('#prog-slot .spec-times').firstChild.textContent")
        cdp_m.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        check("seek by touch: target time shows while dragging, nothing sent until release, then one seek", lambda: (
            sent_while_dragging == 0 or fail(f"{sent_while_dragging} sent mid-drag"),
            preview not in ("0:00", "0:01", "0:02") or fail(f"preview showed {preview}"),
            maya.wait_for_timeout(800), len(seeks) == 1 or fail(f"{len(seeks)} seeks")))
        check("seek: the speaker jumps there (~70%)", lambda: speaker.wait_for_function(
            "() => { const a = document.getElementById('audio'); return a.currentTime > a.duration * 0.6 && a.currentTime < a.duration * 0.85; }", timeout=5000))
        check("seek: the host's bar follows", lambda: host.wait_for_function(
            "() => curPos() > state.now.duration * 0.6", timeout=5000))
        check("seek: in the feed", lambda: expect(host.locator("#feed")).to_contain_text("Maya jumped to", timeout=5000))
        p0 = speaker.evaluate("() => document.getElementById('audio').currentTime")
        host.locator(".np-ctl .spectrum").focus()
        for _ in range(2):
            host.keyboard.press("ArrowLeft")
        check("seek by keyboard: two ArrowLefts = one jump back ~10 s", lambda: speaker.wait_for_function(
            "p0 => { const t = document.getElementById('audio').currentTime; return t < p0 - 6 && t > p0 - 13; }", arg=p0, timeout=5000))

        # Back: after a skip it goes to the previous song and the skipped-to one returns to the top of
        # the queue; once a song has played a few seconds, Back restarts it. Guests have it too.
        before = host.locator("#np-title").inner_text()
        host.click("#skip")
        expect(host.locator("#np-title")).not_to_have_text(before, timeout=5000)
        skipped_to = host.locator("#np-title").inner_text()
        maya.click("#back")
        check("back (guest, right after a skip) plays the previous song; the speaker follows", lambda: (
            expect(host.locator("#np-title")).to_have_text(before, timeout=5000),
            expect(host.locator("#queue .trow").first.locator(".t")).to_have_text(skipped_to, timeout=5000),
            expect(speaker.locator("#title")).to_have_text(before, timeout=5000)))
        speaker.wait_for_function("() => document.getElementById('audio').currentTime > 4", timeout=15000)
        maya.wait_for_timeout(1200)
        maya.click("#back")
        check("back after a few seconds restarts the song on the speaker", lambda: (
            speaker.wait_for_function("() => document.getElementById('audio').currentTime < 3", timeout=5000),
            expect(host.locator("#np-title")).to_have_text(before)))
        check("back: in the feed", lambda: (expect(host.locator("#feed")).to_contain_text("Maya went back to", timeout=5000),
                                            expect(host.locator("#feed")).to_contain_text("Maya restarted", timeout=5000)))

        # Clear queue: host only (a guest's request is refused by the server), asks first, keeps the song.
        status = maya.evaluate("""async () => (await fetch('api/host/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: state.code, sid: localStorage.getItem('rubato.sid') }) })).status""")
        check("clear: a guest's request is refused (403)", lambda: status == 403 or fail(f"got {status}"))
        playing = host.locator("#np-title").inner_text()
        asked = []
        host.once("dialog", lambda d: asked.append(d.message))
        host.click("#clear-queue")
        check("clear: asks first, empties the queue everywhere, the song keeps playing", lambda: (
            asked or fail("no confirmation"),
            expect(host.locator("#queue .trow")).to_have_count(0, timeout=5000),
            expect(theo.locator("#queue .trow")).to_have_count(0, timeout=5000),
            expect(host.locator("#np-title")).to_have_text(playing),
            speaker.wait_for_function("() => !document.getElementById('audio').paused", timeout=3000)))
        check("clear: in the feed", lambda: expect(host.locator("#feed")).to_contain_text("cleared the queue", timeout=5000))

        for c in (maya_ctx, theo_ctx, sp_ctx, host_ctx):
            c.close()
        browser.close()

    check("no page errors", lambda: not errors or fail("; ".join(errors[:5])))
    failed = [r for r in results if not r[1]]
    print(f"\nE2E {'FAILED' if failed else 'PASSED'}: {len(results) - len(failed)}/{len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
