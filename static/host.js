// Host dashboard: the speaker (volume, cap, key), the queue, the room code,
// who has their hands on it, and engine health. Plays no audio.
const key = localStorage.getItem("rubato.hostkey");
let state = null, stateAt = 0, lastBeatAt = null, shownCode = null;

const H = () => ({ "X-Host-Key": key || "" });
const post = (path, body) => fetch(path, { method: "POST", headers: { ...H(), "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const inviteUrl = () => `${(state && state.public_url) || APP}/?code=${state ? state.code : ""}`;

function connect() {
  const ws = new WebSocket(wsUrl("ws/host"));
  ws.onopen = () => ws.send(JSON.stringify({ type: "auth", key }));
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "state") apply(m);
    else if (m.type === "beat" && state && state.now && m.qid === state.now.qid) {
      lastBeatAt = performance.now();
      setProgress(m.qid, m.position, m.playing, state.now.duration);
    }
  };
  ws.onclose = (e) => {
    if (e.code === 4003) { localStorage.removeItem("rubato.hostkey"); return askForKey("That host key didn't work."); }
    if (e.code === 4029) return askForKey("Too many wrong keys from this network. Try again in 10 minutes.");
    $("hd-pill").replaceChildren(el("span", { class: "dot off" }), el("span", { class: "st", textContent: "Reconnecting…" }));
    setTimeout(connect, 2000);
  };
  setInterval(() => ws.readyState === 1 && ws.send("{}"), 25000);
}

const miniCone = cone("96px");
$("mini-cone").append(miniCone);
const spec = spectrum(40);
$("prog-slot").replaceWith(spec.el);
spec.el.classList.add("prog");
const since = (secs) => (secs == null ? null : secs + (performance.now() - stateAt) / 1000);

function apply(s) {
  state = s;
  stateAt = performance.now();
  const h = s.host, n = s.now, e = h.engine, sp = h.speaker;
  if (sp.beat_ago != null) lastBeatAt = performance.now() - sp.beat_ago * 1000;

  const ck = e.cookies;
  $("relink-banner").classList.toggle("hidden", ck === "valid");
  $("relink-title").textContent = ck === "missing" ? "YouTube not linked." : "YouTube link expired.";
  $("relink-sub").textContent = ck === "missing" ? "Playback is off until you link a YouTube Music account." : "YouTube asked for a sign-in check. Playback may stop until you relink.";

  speakerPill($("hd-pill"), s.speaker, { device: true });
  speakerPill($("sp-pill"), s.speaker);

  // speaker card
  setCone(miniCone, n, s.speaker.state === "offline" || !n ? "off" : s.status === "playing" ? "playing" : "paused");
  $("sp-name").textContent = sp.connected ? sp.device : "No speaker";
  $("sp-agent").textContent = sp.connected ? (sp.device === sp.agent ? "Browser speaker" : sp.agent) : "Open the speaker link on the device that plays.";
  $("disconnect").disabled = !sp.connected;
  $("rotate").disabled = sp.key === "env";
  syncSliders();
  $("vol-v").textContent = s.volume;
  $("cap-v").textContent = s.cap;

  // engine
  const acct = e.account;
  $("e-account").textContent = ck === "missing" ? "Not linked" : !acct ? "Not checked" : !acct.signed_in ? "Signed out" : acct.premium ? "Premium" : "Signed in · no Premium";
  $("e-account").className = `v ${ck === "missing" || (acct && !acct.signed_in) ? "bad" : ""}`;
  $("e-stream").textContent = e.stream ? e.stream.replace(/ · itag \d+/, "") : "–";
  $("e-cache").textContent = `${e.cache_tracks} track${e.cache_tracks === 1 ? "" : "s"} · ${e.cache_mb} MB`;
  const NEXT = { cached: ["Prefetched", ""], resolving: ["Fetching…", ""], failed: ["Failed", "bad"], idle: ["–", ""] };
  const [nt, nc] = NEXT[e.next] || [e.next, ""];
  $("e-next").textContent = nt; $("e-next").className = `v ${nc}`;
  $("radio-btn").textContent = s.radio ? "On" : "Off";
  $("radio-btn").setAttribute("aria-label", s.radio ? "Autoplay radio is on. Turn it off" : "Autoplay radio is off. Turn it on");
  $("e-wrong").textContent = `${e.wrong_codes} / ${e.wrong_code_limit}`;
  $("e-wrong").className = `v ${e.wrong_codes ? "bad" : ""}`;

  // now playing
  if ($("np-art")._qid !== (n && n.qid)) { $("np-art")._qid = n && n.qid; $("np-art").replaceChildren(art(n, "art")); }
  $("np-by").textContent = n ? `Now playing · ${n.auto ? "from radio" : `queued by ${n.by}`}${s.status === "offline" ? " · speaker offline" : s.paused ? " · paused" : ""}` : "Nothing playing";
  $("np-title").textContent = n ? n.title : "Queue something";
  $("np-sub").textContent = n ? [n.artists, n.album].filter(Boolean).join(" · ") : "Search on the right, or let guests do it.";
  spec.setTrack(n ? n.videoId : null);
  setProgress(n && n.qid, s.position, s.playing, n && n.duration);
  $("pause").disabled = $("skip").disabled = !n;
  $("pause").innerHTML = icon(s.paused ? "play" : "pause");
  $("pause").setAttribute("aria-label", s.paused ? "Play" : "Pause");

  // queue
  $("q-sub").textContent = `${s.queue.length} song${s.queue.length === 1 ? "" : "s"} · ${Math.round(s.queue.reduce((a, t) => a + (t.duration || 0), 0) / 60)} min`;
  if (!sorter.dragging) $("queue").replaceChildren(...(s.queue.length ? s.queue.map((t) => qrow(t, e)) : [el("div", { class: "empty", textContent: "Nothing queued. Search on the right to add songs." })]));
  $("radio-block").classList.toggle("hidden", !s.radio);
  const last = s.queue.length ? s.queue[s.queue.length - 1] : n;
  $("radio-div").textContent = last ? `Radio takes over after ${last.title}` : "Radio";
  $("auto").replaceChildren(...s.auto.slice(0, 5).map((t) => autorow(t)));

  // room code, QR
  if (s.code !== shownCode) {
    shownCode = s.code;
    $("codebox").replaceChildren(...[...s.code].map((c) => el("span", { textContent: c })));
    $("join-url").textContent = inviteUrl();
    $("qr").src = `api/qr.svg?code=${s.code}&base=${encodeURIComponent((s.public_url || APP))}`;
    $("pop-guest").href = inviteUrl();
  }
  renderHands();
}

function renderHands() {
  const s = state, h = s.host;
  $("h-count").textContent = `${h.hands.length} connected`;
  $("hands").replaceChildren(...(h.hands.length ? h.hands.map((p) => el("div", { class: "hrow" }, avatar(p.name, p.initials, "lg"),
    el("div", { style: "min-width:0" }, el("div", { class: "n ellip", textContent: p.label }),
      el("div", { class: "l ellip", textContent: p.last ? `${p.last} · ${ago(since(p.ago))}` : p.device || "No moves yet" })),
    el("div", { class: "r" }, el("b", { textContent: p.count }), "moves"))) : [el("div", { class: "empty", textContent: "Nobody here yet. Share the code." })]));
  $("feed").replaceChildren(...(s.feed.length ? s.feed.slice(0, 8).map((f) => el("div", { class: "frow" }, avatar(f.name, f.initials, "sm"),
    el("span", { class: "ellip" }, el("b", { textContent: f.who }), ` ${f.text}`), el("span", { class: "ago", textContent: ago(since(f.ago)) }))) : [el("div", { class: "empty", style: "padding:8px 0", textContent: "Every play, pause, skip and volume change shows up here." })]));
}
setInterval(() => state && renderHands(), 5000);

function qrow(t, engine) {
  const row = el("div", { class: "trow" }, gripBtn(t), art(t),
    el("div", { class: "grow" },
      el("div", { class: "t ellip", textContent: t.title }),
      el("div", { class: "s ellip", textContent: t.artists + (engine.next_qid === t.qid && engine.next === "cached" ? " · cached" : "") })),
    el("span", { class: "who" }, avatar(t.by_name, t.initials), el("span", { class: "ellip", textContent: t.by })),
    el("span", { class: "dur", textContent: fmt(t.duration) }),
    el("button", { class: "iconbtn bare", html: icon("x"), ariaLabel: `Remove ${t.title}`, onclick: () => remove(t) }));
  row.dataset.qid = t.qid;
  return row;
}
const sorter = sortable($("queue"), (qid, index) => post("api/move", { qid, index }));

function autorow(t) {
  return el("div", { class: "trow" }, el("span", { style: "width:22px" }), art(t),
    el("div", { class: "grow" }, el("div", { class: "t ellip", style: "font-weight:500", textContent: t.title }), el("div", { class: "s ellip", textContent: t.artists })),
    el("span", { class: "dur", textContent: fmt(t.duration) }),
    el("button", { class: "iconbtn", html: icon("plus"), ariaLabel: `Add ${t.title} to the queue`, onclick: () => addIds([t.videoId], t.title) }));
}

// ---- actions (the host's show up in the feed as "<name> (host)")
async function remove(t) { if ((await post("api/remove", { qid: t.qid })).ok) toast(`Removed ${t.title}`); }
async function addIds(videoIds, label) {
  const r = await post("api/add", { videoIds });
  if (r.ok) { const { added, place } = await r.json(); toast(added > 1 ? `Added ${added} songs` : place === 0 ? `${label} is playing now` : `${label} is ${ordinal(place)} in line`); }
  else toast(r.status === 409 ? "The queue is full." : "Couldn't add that.");
}
$("pause").addEventListener("click", () => state && post("api/pause", { paused: !state.paused }));
$("skip").innerHTML = icon("skip");
$("skip").addEventListener("click", () => state && state.now && post("api/skip", { qid: state.now.qid }));
$("radio-btn").addEventListener("click", () => state && post("api/host/radio", { on: !state.radio }));

// volume + cap sliders: send while dragging (throttled) and on release
const volBusy = { vol: false, cap: false };
function syncSliders() {
  if (!state) return;
  if (!volBusy.vol) { $("vol").value = state.volume; setRangeFill($("vol")); }
  if (!volBusy.cap) { $("cap").value = state.cap; setRangeFill($("cap")); }
}
function slider(id, send) {
  let t = null, sent = 0;
  const go = () => { sent = performance.now(); send(+$(id).value); };
  $(id).addEventListener("input", () => {
    volBusy[id] = true;
    setRangeFill($(id));
    $(`${id}-v`).textContent = $(id).value;
    clearTimeout(t);
    if (performance.now() - sent > 200) go(); else t = setTimeout(go, 200);
  });
  $(id).addEventListener("change", () => { clearTimeout(t); go(); setTimeout(() => { volBusy[id] = false; syncSliders(); }, 600); });
}
slider("vol", (v) => post("api/volume", { volume: v }));
slider("cap", (v) => post("api/host/cap", { cap: v }));

$("rotate").replaceChildren(ic("key"), "Rotate key");
$("rotate").addEventListener("click", async () => {
  if (!confirm("Rotate the speaker key? The current speaker disconnects and its link stops working. You'll get a new link to open on the speaker.")) return;
  const r = await post("api/host/speaker-key");
  const d = await r.json().catch(() => ({}));
  if (!r.ok) return toast(d.detail || "Couldn't rotate the key.");
  $("key-url").textContent = `${(state && state.public_url) || APP}/speaker#k=${d.key}`;
  $("key-modal").classList.remove("hidden");
});
$("key-copy").replaceChildren(ic("copy"), "Copy");
$("key-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("key-url").textContent); toast("Speaker link copied"); } catch { toast("Select and copy the link"); }
});
$("key-close").innerHTML = icon("x");
$("key-close").addEventListener("click", () => { $("key-modal").classList.add("hidden"); $("key-url").textContent = ""; });
$("disconnect").replaceChildren(ic("unplug"), "Disconnect");
$("disconnect").addEventListener("click", () => post("api/host/disconnect-speaker"));

$("copy-link").replaceChildren(ic("link"), "Copy invite link");
$("copy-link").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(inviteUrl()); toast("Invite link copied"); }
  catch { prompt("Copy the invite link:", inviteUrl()); }
});
$("new-session").replaceChildren(ic("refresh"), "New session");
$("new-session").addEventListener("click", async () => {
  if (!confirm("Start a new session? This clears the queue and the feed, and changes the room code. Guests will need the new code; the speaker stays connected.")) return;
  await post("api/new-session");
});
$("settings").innerHTML = icon("sliders");
$("settings").addEventListener("click", (e) => {
  e.stopPropagation();
  const open = $("settings-pop").classList.toggle("hidden");
  $("settings").setAttribute("aria-expanded", String(!open));
});
document.addEventListener("click", () => { $("settings-pop").classList.add("hidden"); $("settings").setAttribute("aria-expanded", "false"); });
$("pop-name").replaceChildren(ic("user"), "Change your name");
$("pop-name").addEventListener("click", async () => {
  const name = prompt("Your name, as the room sees it (shown as “name (host)”):", state ? state.host.name : "");
  if (name == null) return;
  const r = await post("api/host/name", { name });
  toast(r.ok ? "Name saved" : "Use 1 to 20 characters.");
});

// ---- Relink YouTube: the same upload + probe flow as setup, authorised by the host key
function openRelink() {
  $("relink-modal").classList.remove("hidden");
  mountLinker($("relink-linker"), { headers: H, onChange: (res) => {
    if (res && res.account) toast(res.premium ? "YouTube linked · Premium" : "YouTube linked");
  } });
  $("relink-close").focus();
}
const closeModals = () => { $("relink-modal").classList.add("hidden"); $("key-modal").classList.add("hidden"); };
$("relink-open").addEventListener("click", openRelink);
$("pop-relink").addEventListener("click", openRelink);
$("relink-close").innerHTML = icon("x");
$("relink-close").addEventListener("click", closeModals);
$("relink-modal").addEventListener("click", (e) => e.target.id === "relink-modal" && closeModals());
document.addEventListener("keydown", (e) => e.key === "Escape" && closeModals());
$("pop-relink").replaceChildren(ic("upload"), "Relink YouTube");
$("pop-guest").replaceChildren(ic("external"), "Open guest view");
$("add-ic").innerHTML = icon("search");

// search-to-add dropdown
$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("add-q").value.trim(), box = $("add-results");
  if (!q || !state) return;
  box.classList.remove("hidden");
  box.replaceChildren(el("div", { class: "empty", textContent: "Searching…" }));
  const r = await fetch(`api/search?q=${encodeURIComponent(q)}&kind=all&code=${state.code}`);
  if (!r.ok) return box.replaceChildren(el("div", { class: "empty", textContent: "Search failed." }));
  const d = await r.json();
  if (d.open) return addCollection("playlist", d.open.id, "linked playlist");
  box.replaceChildren(...(d.results.length ? d.results.map((x) => el("div", { class: "trow" },
    x.kind === "song" ? art(x) : x.thumb ? el("img", { class: "art", src: x.thumb, alt: "" }) : artPh(x, "art"),
    el("div", { class: "grow" }, el("div", { class: "t ellip", textContent: x.title }),
      el("div", { class: "s ellip", textContent: x.kind === "song" ? [x.artists, x.duration && fmt(x.duration)].filter(Boolean).join(" · ") : `${x.kind === "album" ? "Album" : "Playlist"} · ${x.subtitle}` })),
    el("div", { class: "acts" }, x.kind === "song"
      ? el("button", { class: "iconbtn", html: icon("plus"), ariaLabel: `Add ${x.title}`, onclick: (ev) => { ev.currentTarget.replaceWith(el("span", { class: "iconbtn done", html: icon("check") })); addIds([x.videoId], x.title); } })
      : el("button", { class: "btn", textContent: "Add all", onclick: () => addCollection(x.kind, x.id, x.title) })))) : [el("div", { class: "empty", textContent: "No results." })]));
});
async function addCollection(kind, id, title) {
  const r = await fetch(`api/collection?kind=${kind}&id=${encodeURIComponent(id)}&code=${state.code}`);
  if (!r.ok) return toast(`Couldn't open that ${kind}.`);
  const c = await r.json();
  await addIds(c.tracks.map((t) => t.videoId).slice(0, 50), c.title || title);
}
document.addEventListener("click", (e) => { if (!e.target.closest(".addbox")) $("add-results").classList.add("hidden"); });
$("add-q").addEventListener("focus", () => $("add-results").children.length && $("add-results").classList.remove("hidden"));

// progress + last beat
setInterval(() => {
  if (!state) return;
  spec.set(state.now ? curPos() : 0, state.now ? state.now.duration || 0 : 0);
  const sp = state.host.speaker;
  $("sp-beat").textContent = !sp.connected ? "" : lastBeatAt == null ? "waiting for first beat" : `last beat ${Math.max(0, (performance.now() - lastBeatAt) / 1000).toFixed(0)} s ago`;
}, 250);

function askForKey(msg = "") {
  document.querySelector(".host main").classList.add("hidden");
  $("keygate").classList.remove("hidden");
  $("keygate-err").textContent = msg;
  $("keygate-in").focus();
}
$("keygate").addEventListener("submit", (e) => {
  e.preventDefault();
  const k = $("keygate-in").value.trim();
  if (!k) return;
  localStorage.setItem("rubato.hostkey", k);
  location.replace("host");
});

if (key) connect();
else askForKey();
