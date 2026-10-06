// The speaker. It never decides what plays: it loads whatever the server says
// is "now", follows the room's pause/volume, reports a heartbeat every second,
// and says when a track ends. That "ended" is the only thing that advances the queue.

// The key arrives once in the #fragment (never sent to the server in a URL), then lives in this browser.
const frag = new URLSearchParams(location.hash.slice(1));
if (frag.get("k")) { localStorage.setItem("rubato.speakerkey", frag.get("k")); history.replaceState(null, "", `${ROOT}/speaker`); }
let key = localStorage.getItem("rubato.speakerkey") || "";

const audio = $("audio");
let ws = null, token = null, state = null;
let loadedQid = null, resumeAt = 0, retried = null;
let unlocked = false, buffering = false, stopped = false;
let ctx = null, gain = null, wakeLock = null;

const coneEl = cone("min(460px, 42vh, 34vw)");
$("cone-slot").prepend(coneEl);
$("tap-ic").innerHTML = icon("play");
const spec = spectrum(56, "Rb · 780.24 nm");
$("prog-slot").append(spec.el);
$("dev-name").value = localStorage.getItem("rubato.device") || "";

// ---- connection
function connect() {
  if (stopped || !key) return;
  ws = new WebSocket(wsUrl("ws/speaker"));
  ws.onopen = () => ws.send(JSON.stringify({ type: "auth", key, device: $("dev-name").value.trim() }));
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "hello") {
      token = m.token;
      loadedQid = null; // the old token is dead: reload the current track with the new one
      banner(null);
      beat();
    } else if (m.type === "state" || m.type === "patch") {
      const s = applyPatch(state, m);
      if (s) apply(s); else send({ type: "resync" });
    }
    else if (m.type === "taken_over") stop("Taken over", `Another device${m.by ? ` (${m.by})` : ""} opened the speaker link and is playing now. Only one speaker plays at a time.`, "Take it back");
    else if (m.type === "disconnected") stop("Disconnected", "The host disconnected this speaker.", "Reconnect");
    else if (m.type === "revoked") stop("Key rotated", "The host rotated the speaker key. Open the new speaker link on this device.", null, true);
  };
  ws.onclose = (e) => {
    ws = null;
    token = null;
    if (stopped) return;
    if (e.code === 4002) return stop("Taken over", "Another device opened the speaker link and is playing now.", "Take it back");
    if (e.code === 4004) return stop("Disconnected", "The host disconnected this speaker.", "Reconnect");
    if (e.code === 4003) { localStorage.removeItem("rubato.speakerkey"); key = ""; return stop("Speaker key not accepted", "Open the speaker link from the host dashboard, or paste the speaker key.", null, true); }
    if (e.code === 4029) return stop("Locked out", "Too many wrong keys from this network. Try again in 10 minutes.", "Retry");
    banner("Offline · reconnecting…");
    speakerPill($("sp-pill"), { state: "offline" });
    setTimeout(connect, 1500);
  };
}

function stop(title, text, button, askKey = false) {
  stopped = true;
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  loadedQid = null;
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  setCone(coneEl, state && state.now, "off");
  speakerPill($("sp-pill"), { state: "offline" });
  $("ov-h").textContent = title;
  $("ov-p").textContent = text;
  $("ov-key").classList.toggle("hidden", !askKey);
  $("ov-btn").classList.toggle("hidden", !button);
  $("ov-btn").textContent = button || "";
  $("overlay").classList.remove("hidden");
  (askKey ? $("ov-key-in") : $("ov-btn")).focus();
}
function resume() {
  stopped = false;
  $("overlay").classList.add("hidden");
  connect();
}
$("ov-btn").addEventListener("click", resume);
$("ov-key").addEventListener("submit", (e) => {
  e.preventDefault();
  const k = $("ov-key-in").value.trim();
  if (!k) return;
  key = k;
  localStorage.setItem("rubato.speakerkey", k);
  resume();
});
function banner(text) {
  $("banner").classList.toggle("hidden", !text);
  $("banner-t").textContent = text || "";
}

// ---- the one tap browsers require before a page may play audio
function unlock() {
  try {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    // Volume goes through a gain node: iOS ignores audio.volume, but not Web Audio.
    gain = ctx.createGain();
    ctx.createMediaElementSource(audio).connect(gain).connect(ctx.destination);
    ctx.resume();
  } catch { ctx = gain = null; }
  unlocked = true;
  audio.play().catch(() => {}); // inside the tap: blesses this <audio> for later play() calls
  requestWakeLock();
  const name = $("dev-name").value.trim();
  localStorage.setItem("rubato.device", name);
  send({ type: "name", device: name });
  if (state) apply(state);
  beat();
}
$("tap").addEventListener("click", unlock);
async function requestWakeLock() {
  try { if ("wakeLock" in navigator && !wakeLock) wakeLock = await navigator.wakeLock.request("screen"); } catch {}
  if (wakeLock) wakeLock.addEventListener("release", () => (wakeLock = null), { once: true });
}
document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && unlocked && requestWakeLock());

// ---- reconcile <audio> with the room
function applyVolume(v) {
  const g = Math.pow(Math.max(0, Math.min(100, v)) / 100, 2); // perceptual curve
  if (gain) gain.gain.setTargetAtTime(g, ctx.currentTime, 0.05);
  else audio.volume = g;
}

function apply(s) {
  state = s;
  render(s);
  applyVolume(s.volume);
  if (!unlocked || !token) return;
  const n = s.now;
  if (!n || s.status === "unlinked") {
    if (loadedQid) { audio.pause(); audio.removeAttribute("src"); audio.load(); loadedQid = null; }
    return;
  }
  if (s.status === "loading") return; // the server is still pulling it into RAM
  if (n.qid !== loadedQid) {
    loadedQid = n.qid;
    retried = null;
    resumeAt = s.position > 1 ? s.position : 0; // resume where this track last was (reconnect / takeover)
    audio.src = `stream/${n.videoId}?t=${encodeURIComponent(token)}`;
    if (resumeAt) audio.addEventListener("loadedmetadata", () => { audio.currentTime = resumeAt; }, { once: true });
    buffering = !s.paused;
  }
  if (s.paused && !audio.paused) audio.pause();
  else if (!s.paused && audio.paused) audio.play().catch((err) => { if (err.name === "NotAllowedError") lockAgain(); });
}
function lockAgain() {
  unlocked = false;
  render(state);
  beat();
}

// ---- heartbeat: what is actually coming out of the speaker
function send(m) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(m)); }
function beat() {
  send({ type: "beat", qid: loadedQid, pos: audio.currentTime || 0, paused: audio.paused, buffering, locked: !unlocked,
         volume: state ? state.volume : null });
}
// The heartbeat clock runs in a Worker: Chrome cuts timers in a hidden, silent
// tab to about once a minute, which would make an idle speaker look dead.
try {
  const clock = new Worker(URL.createObjectURL(new Blob(["setInterval(() => postMessage(0), 1000);"], { type: "text/javascript" })));
  clock.onmessage = beat;
} catch {
  setInterval(beat, 1000);
}

audio.addEventListener("waiting", () => { buffering = true; beat(); });
audio.addEventListener("stalled", () => { if (!audio.paused) { buffering = true; beat(); } });
for (const ev of ["playing", "pause", "canplaythrough"]) audio.addEventListener(ev, () => { buffering = false; beat(); });
audio.addEventListener("seeked", beat);
audio.addEventListener("ended", () => { if (loadedQid) send({ type: "ended", qid: loadedQid }); });
audio.addEventListener("error", () => {
  if (!loadedQid || !audio.getAttribute("src") || !audio.src.includes("/stream/")) return;
  // One retry first (e.g. a token that changed mid-load); a second failure skips the track.
  if (retried !== loadedQid && token && state && state.now && state.now.qid === loadedQid) {
    retried = loadedQid;
    const at = audio.currentTime || resumeAt;
    audio.src = `stream/${state.now.videoId}?t=${encodeURIComponent(token)}`;
    if (at) audio.addEventListener("loadedmetadata", () => { audio.currentTime = at; }, { once: true });
    if (!state.paused) audio.play().catch(() => {});
    return;
  }
  send({ type: "error", qid: loadedQid, detail: audio.error ? `media error ${audio.error.code}` : "unknown" });
});

// ---- display
function render(s) {
  const n = s.now, sp = { ...s.speaker };
  if (!unlocked) sp.state = ws ? "locked" : "offline";
  speakerPill($("sp-pill"), sp, { device: true });
  $("volcap").replaceChildren("VOL ", el("b", { textContent: s.volume }), " · ", el("span", { class: "cap", textContent: `CAP ${s.cap}` }));
  $("tap").classList.toggle("hidden", unlocked);
  $("name-row").classList.toggle("hidden", unlocked);
  setCone(coneEl, n, !n || !unlocked ? "off" : s.status === "playing" ? "playing" : "paused");
  $("url").textContent = (s.public_url || APP).replace(/^https?:\/\//, "");
  $("codebox").replaceChildren(...[...s.code].map((c) => el("span", { textContent: c })));
  $("people").textContent = s.people.length ? `${s.people.length} on the remote` : "";
  $("now-label").textContent = n ? `Now playing · ${n.auto ? "from radio" : `queued by ${n.by}`}${s.paused ? " · paused" : ""}` : "Nothing playing";
  $("title").textContent = n ? n.title : "Queue something";
  $("sub").textContent = n ? [n.artists, n.album].filter(Boolean).join(" · ") : "Scan in, search, and it plays here.";
  spec.setTrack(n ? n.videoId : null);
  $("hands").replaceChildren(...(s.feed.length ? s.feed.slice(0, 3).map((f) => el("span", { class: "hand" }, avatar(f.name, f.initials, "lg"),
    el("span", { class: "ellip" }, el("b", { textContent: f.who }), ` ${f.text}`))) : [el("span", { class: "muted", style: "font-size:16px", textContent: "Nobody's touched it yet." })]));
  const up = [...s.queue, ...s.auto].slice(0, 3);
  $("upnext").replaceChildren(...(up.length ? up.map((t, i) => el("div", { class: "urow" }, el("span", { class: "ix", textContent: String(i + 1).padStart(2, "0") }),
    el("div", { style: "min-width:0" }, el("div", { class: "t ellip", textContent: t.title }), el("div", { class: "s ellip", textContent: t.artists })),
    t.auto ? el("span", { class: "label", textContent: "Radio" }) : avatar(t.by_name, t.initials, "lg"))) : [el("div", { class: "muted", style: "font-size:16px;padding:10px 0", textContent: "Empty. Grab the aux." })]));
}

// progress: on the speaker the <audio> element is the truth
setInterval(() => {
  if (!state || !state.now) return spec.set(0, 0);
  const local = unlocked && loadedQid === state.now.qid && audio.readyState > 0;
  spec.set(local ? audio.currentTime : state.position, state.now.duration || audio.duration || 0);
}, 250);

if (key) connect();
else stop("Speaker link needed", "Open the speaker link shown on the host dashboard, or paste the speaker key.", null, true);
