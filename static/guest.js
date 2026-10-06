// Guest remote. Plays no audio: every control is a request to the server,
// which applies it, records who did it, and tells the speaker.
let code = null, sid = localStorage.getItem("rubato.sid") || "", state = null;
let myName = localStorage.getItem("rubato.name") || "";

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, sid, ...body }) } : {});
  if (r.status === 403) { leave("That room code is no longer valid."); throw new Error("bad code"); }
  if (r.status === 401) { leave("The host started a new session. Join again with the new code."); throw new Error("no session"); }
  if (r.status === 429 && !["api/add", "api/volume"].includes(path)) { leave("Too many wrong codes from this network. Try again in 10 minutes."); throw new Error("locked"); }
  return r;
}

// ---- join: five code boxes (auto-advance, backspace, paste fills all) + name
const boxes = [];
for (let i = 0; i < 5; i++) {
  const b = el("input", { maxLength: 1, inputMode: "text", autocapitalize: "characters", autocomplete: "off", spellcheck: false, ariaLabel: `Room code letter ${i + 1}` });
  b.addEventListener("input", () => {
    const v = b.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (v.length > 1) return fillCode(v, i);
    b.value = v;
    if (v && i < 4) boxes[i + 1].focus();
  });
  b.addEventListener("keydown", (e) => {
    if (e.key === "Backspace" && !b.value && i > 0) { boxes[i - 1].focus(); boxes[i - 1].value = ""; e.preventDefault(); }
    else if (e.key === "ArrowLeft" && i > 0) boxes[i - 1].focus();
    else if (e.key === "ArrowRight" && i < 4) boxes[i + 1].focus();
  });
  b.addEventListener("paste", (e) => { e.preventDefault(); fillCode((e.clipboardData.getData("text") || "").toUpperCase().replace(/[^A-Z0-9]/g, ""), 0); });
  b.addEventListener("focus", () => b.select());
  boxes.push(b);
  $("codeboxes").append(b);
}
function fillCode(v, from = 0) {
  [...v].slice(0, 5 - from).forEach((c, j) => (boxes[from + j].value = c));
  boxes[Math.min(4, from + v.length)].focus();
}
const typedCode = () => boxes.map((b) => b.value).join("");
$("name-input").value = myName;
$("join-arrow").innerHTML = icon("arrow");

// "On the speaker now" teaser. Only shown once we have a code to ask with (QR link or a remembered one).
function teaser(data) {
  const t = $("teaser");
  if (!data) {
    t.replaceChildren(el("div", { class: "art ph", style: "background:var(--raised);color:var(--muted)", html: icon("speaker") }),
      el("div", { class: "grow" }, el("div", { class: "label", textContent: "On the speaker now" }),
        el("div", { class: "s", style: "margin-top:4px", textContent: "Enter the room code to see what's playing." })));
    return;
  }
  const n = data.now;
  t.replaceChildren(...put(n ? art(n) : el("div", { class: "art ph", style: "background:var(--raised);color:var(--muted)", html: icon("speaker") }),
    el("div", { class: "grow" },
      el("div", { class: "label red", textContent: n && data.speaker !== "live" ? "On the speaker · paused" : "On the speaker now" }),
      el("div", { class: "t ellip", textContent: n ? n.title : "Nothing yet. Be the first to queue." }),
      n ? el("div", { class: "s ellip", textContent: [n.artists, n.by && `queued by ${n.by}`].filter(Boolean).join(" · ") } ) : null),
    data.people ? el("span", { class: "mono muted", style: "font-size:12px", textContent: `${data.people} here` }) : null));
}

$("join").addEventListener("submit", (e) => {
  e.preventDefault();
  join(typedCode(), $("name-input").value);
});

async function join(c, name, quiet = false) {
  c = c.trim().toUpperCase();
  name = name.trim().replace(/\s+/g, " ");
  if (c.length !== 5) { $("join-err").textContent = "Enter all 5 letters of the code."; return; }
  if (!name || name.length > 20) { $("join-err").textContent = "Enter your name (up to 20 characters)."; $("name-input").focus(); return; }
  const r = await fetch("api/join", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: c, name, sid }) });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    if (!quiet) $("join-err").textContent = r.status === 429 ? "Too many wrong codes. Try again in 10 minutes." : r.status === 403 ? "No room with that code." : d.detail || "Couldn't join.";
    return;
  }
  const d = await r.json();
  code = c; sid = d.sid; myName = d.name;
  localStorage.setItem("rubato.code", code);
  localStorage.setItem("rubato.sid", sid);
  localStorage.setItem("rubato.name", myName);
  history.replaceState(null, "", `${ROOT}/`);
  $("join").classList.add("hidden");
  $("room").classList.remove("hidden");
  connect();
}

function leave(msg) {
  code = null;
  localStorage.removeItem("rubato.code");
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  $("room").classList.add("hidden");
  $("join").classList.remove("hidden");
  $("join-err").textContent = msg || "";
  teaser(null);
}

// ---- live state
let ws = null;
function connect() {
  ws = new WebSocket(wsUrl("ws"));
  ws.onopen = () => ws.send(JSON.stringify({ type: "auth", code, sid }));
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "state" || m.type === "patch") {
      const s = applyPatch(state, m);
      if (s) render(s); else ws.send('{"type":"resync"}');
    }
    else if (m.type === "beat" && state && state.now && m.qid === state.now.qid) setProgress(m.qid, m.position, m.playing, state.now.duration);
  };
  ws.onclose = (e) => {
    ws = null;
    if (e.code === 4001) return leave("The host started a new session. Get the new code from the speaker.");
    if (e.code === 4401) return leave("Join again with your name.");
    if (e.code === 4029) return leave("Too many wrong codes. Try again in 10 minutes.");
    if (e.code === 4003) return leave("That room code is no longer valid.");
    $("sp-pill").replaceChildren(el("span", { class: "dot off" }), el("span", { class: "st", textContent: "Reconnecting…" }));
    setTimeout(() => code && connect(), 2000);
  };
}
setInterval(() => ws && ws.readyState === 1 && ws.send("{}"), 25000); // keep proxies from idling us out

// ---- views
function show(tab) {
  document.querySelectorAll("[data-view]").forEach((v) => v.classList.toggle("hidden", v.dataset.view !== tab));
  document.querySelectorAll("#room > header").forEach((h) => h.classList.toggle("hidden", h.dataset.for !== tab));
  document.querySelectorAll(".tabbar button").forEach((b) => (b.dataset.tab === tab ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current")));
  if (tab === "search" && !$("q").value) $("q").focus();
}
document.querySelectorAll(".tabbar button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
document.querySelectorAll("[data-ic]").forEach((n) => (n.innerHTML = icon(n.dataset.ic)));

const coneEl = cone("220px");
$("cone-slot").append(coneEl);
const spec = spectrum(32);
$("prog-slot").append(spec.el);
const dialer = dial();
$("dial-slot").append(dialer.el);
$("skip").innerHTML = icon("skip");

const queuedIds = () => new Set([...(state ? state.queue : []), ...(state && state.now ? [state.now] : [])].map((t) => t.videoId));
const coneMode = (s) => (s.speaker.state === "offline" || !s.now ? "off" : s.status === "playing" ? "playing" : "paused");
const byLine = (t) => (t.auto ? "From radio" : `Queued by ${t.by}`);

function render(s) {
  const prev = state;
  state = s;
  setServerTime(s.server_time);
  $("notice").textContent = s.notice || "";
  $("notice").classList.toggle("hidden", !s.notice);
  speakerPill($("sp-pill"), s.speaker);

  // remote
  const n = s.now;
  setCone(coneEl, n, coneMode(s));
  $("by").textContent = n ? (s.status === "offline" ? "Speaker offline · paused" : s.status === "loading" ? "Loading…" : byLine(n)) : "Nothing playing";
  $("np-title").textContent = n ? n.title : "Nothing playing";
  $("np-sub").textContent = n ? [n.artists, n.album].filter(Boolean).join(" · ") : "Search for a song to get the room going";
  spec.setTrack(n ? n.videoId : null);
  setProgress(n && n.qid, s.position, s.playing, n && n.duration);
  $("pause").disabled = !n;
  $("pause").innerHTML = icon(s.paused ? "play" : "pause");
  $("pause").setAttribute("aria-label", s.paused ? "Play" : "Pause");
  $("skip").disabled = !n;
  $("back").disabled = !n && !s.can_back;
  syncVolume();
  $("vol-v").textContent = `${s.volume} / ${s.cap}`;
  dialer.set(s.volume, s.cap);
  const f = s.feed[0];
  $("last-hand").replaceChildren(...(f ? [el("span", { class: "hand" }, avatar(f.name, f.initials, "sm"), el("span", { class: "ellip" }, el("b", { textContent: f.who }), ` ${f.text}`))] : []));

  // queue
  $("q-count").textContent = `${s.queue.length} queued`;
  $("q-now").classList.toggle("hidden", !n);
  if (n) $("q-now").replaceChildren(...put(art(n), el("div", { class: "grow" },
    el("div", { class: "label red", textContent: s.paused ? "Paused" : "Now playing" }),
    el("div", { class: "t ellip", textContent: n.title }), el("div", { class: "s ellip", textContent: n.artists })),
    n.auto ? el("span", { class: "label", textContent: "Radio" }) : avatar(n.by_name, n.initials)));
  $("q-head").textContent = s.queue.length ? `Up next · ${s.queue.length}` : "Up next";
  // Lists rebuild only when they changed (patches keep an unchanged queue identical).
  if (!sorter.dragging && (!prev || prev.queue !== s.queue || prev.radio !== s.radio)) renderQueue(s);
  $("radio-block").classList.toggle("hidden", !s.radio || !s.auto.length);
  const last = s.queue.length ? s.queue[s.queue.length - 1] : n;
  $("radio-div").textContent = last ? `Radio takes over after ${last.title}` : "Radio";
  if (!prev || prev.auto !== s.auto) $("auto").replaceChildren(...s.auto.slice(0, 5).map((t) => trow(t, [addBtn(t)])));

  refreshQueuedMarks();
}

function trow(t, acts, lead = null) {
  return el("div", { class: "trow" }, lead, art(t),
    el("div", { class: "grow" }, el("div", { class: "t ellip", textContent: t.title }),
      el("div", { class: "s ellip", textContent: [t.artists, t.duration && fmt(t.duration)].filter(Boolean).join(" · ") })),
    el("div", { class: "acts" }, ...acts));
}
function qrow(t) {
  const row = trow(t, [avatar(t.by_name, t.initials), el("button", { class: "iconbtn bare", html: icon("x"), ariaLabel: `Remove ${t.title}`, onclick: () => removeTrack(t) })], gripBtn(t));
  row.dataset.qid = t.qid;
  return row;
}
function renderQueue(s) {
  $("queue").replaceChildren(...(s.queue.length ? s.queue.map(qrow) : [el("div", { class: "empty", textContent: s.radio ? "Nothing queued. The radio will pick." : "Nothing queued. Search for something." })]));
  sorter.restore();
}
// Drag to reorder (anyone, any song). The server checks it against the queue as it is now: if someone
// else moved things first it refuses, and the list redraws from the broadcast either way.
async function moveTrack(qid, index, from) {
  const r = await api("api/move", { qid, index, from_index: from });
  if (!r.ok) toast((await r.json().catch(() => ({}))).detail || "Couldn't move that.");
}
const sorter = sortable($("queue"), { onMove: moveTrack, onIdle: () => state && renderQueue(state) });

function addBtn(t, opts = {}) {
  return el("button", { class: "iconbtn", html: icon("plus"), ariaLabel: `Add ${t.title} to the queue`, onclick: (e) => addTracks([t.videoId], { btn: e.currentTarget, label: t.title, ...opts }) });
}
const doneBtn = (t) => el("button", { class: "iconbtn done", html: icon("check"), ariaLabel: `${t.title} is queued`, disabled: true, style: "opacity:1" });

// Search / collection rows show a red check for anything already queued.
function refreshQueuedMarks() {
  const ids = queuedIds();
  document.querySelectorAll("[data-vid]").forEach((row) => {
    const acts = row.querySelector(".acts"), want = ids.has(row.dataset.vid), has = !!acts.querySelector(".done");
    if (want && !has) acts.replaceChildren(doneBtn(row._track));
    else if (!want && has) acts.replaceChildren(addBtn(row._track));
  });
}
function songRow(t) {
  const row = trow(t, [queuedIds().has(t.videoId) ? doneBtn(t) : addBtn(t)]);
  row.dataset.vid = t.videoId;
  row._track = t;
  return row;
}

// progress, between heartbeats
// Seek: drag or click the spectral bar, or arrow keys on it. Same rights as pause and skip; the
// server sets the position, the speaker jumps there, and the feed says who.
const tickProgress = seekableSpectrum(spec, (path, body) => api(path, body));
setInterval(tickProgress, 250);

// ---- room controls
$("pause").addEventListener("click", () => state && state.now && api("api/pause", { paused: !state.paused }));
$("skip").addEventListener("click", () => state && state.now && api("api/skip", { qid: state.now.qid }));
// Back: restarts the song after a few seconds, otherwise the previous one (the server keeps the history).
$("back").innerHTML = icon("prev");
$("back").addEventListener("click", async () => {
  if (!state || (!state.now && !state.can_back)) return;
  const r = await api("api/back", { qid: state.now ? state.now.qid : "" });
  if (r.ok && (await r.json()).did === "restarted") toast("Restarted");
});
setInterval(() => $("back").setAttribute("aria-label", state && state.now && curPos() > 3 ? "Restart song" : "Previous song"), 1000);

// Volume: the range is the control (max = the host's cap); the dial just shows it.
let volBusy = false, volTimer = null, volSent = 0;
function syncVolume() {
  if (volBusy || !state) return; // don't yank the slider out from under a finger
  $("vol").max = state.cap;
  $("vol").value = Math.min(state.volume, state.cap);
  setRangeFill($("vol"));
}
function sendVolume() {
  volSent = performance.now();
  api("api/volume", { volume: +$("vol").value }).catch(() => {});
}
$("vol").addEventListener("input", () => {
  volBusy = true;
  setRangeFill($("vol"));
  dialer.set(+$("vol").value, state ? state.cap : 100);
  clearTimeout(volTimer);
  if (performance.now() - volSent > 200) sendVolume(); else volTimer = setTimeout(sendVolume, 200);
});
$("vol").addEventListener("change", () => { clearTimeout(volTimer); sendVolume(); setTimeout(() => { volBusy = false; syncVolume(); }, 600); });

async function addTracks(videoIds, { btn = null, label = "" } = {}) {
  if (btn) btn.disabled = true;
  const r = await api("api/add", { videoIds });
  if (r.ok) {
    const { added, place } = await r.json();
    if (btn) btn.replaceWith(doneBtn({ title: label }));
    toast(added > 1 ? `Added ${added} songs` : place === 0 ? `${label} is playing now` : `${label} is ${ordinal(place)} in line`);
    return true;
  }
  if (btn) btn.disabled = false;
  toast(r.status === 429 ? "Slow down a little. Try again in a minute." : r.status === 409 ? "The queue is full." : "Couldn't add that one.");
  return false;
}
async function removeTrack(t) {
  const r = await api("api/remove", { qid: t.qid });
  if (r.ok) toast(`Removed ${t.title}`);
}

// ---- search: all / songs / albums / playlists, or a pasted link
let kind = "all";
$("s-ic").innerHTML = icon("search");
$("q-clear").innerHTML = icon("x");
$("q").addEventListener("input", () => $("q-clear").classList.toggle("hidden", !$("q").value));
$("q-clear").addEventListener("click", () => { $("q").value = ""; $("q-clear").classList.add("hidden"); $("results").replaceChildren(); showCollection(false); $("q").focus(); });
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
  kind = c.dataset.kind;
  document.querySelectorAll(".chip").forEach((x) => x.setAttribute("aria-pressed", String(x === c)));
  if ($("q").value.trim()) runSearch();
}));
$("search-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });

const info = (text) => el("div", { class: "empty", textContent: text });

async function runSearch() {
  const q = $("q").value.trim();
  if (!q) return;
  $("q").blur(); // drop the phone keyboard so results are visible
  showCollection(false);
  $("results").replaceChildren(info("Searching…"));
  const r = await api(`api/search?q=${encodeURIComponent(q)}&kind=${kind}&code=${encodeURIComponent(code)}`);
  if (!r.ok) return $("results").replaceChildren(info("Search failed. Try again."));
  const data = await r.json();
  if (data.open) return openCollection(data.open.kind, data.open.id);
  if (!data.results.length) return $("results").replaceChildren(info("No results."));
  const songs = data.results.filter((x) => x.kind === "song");
  const albums = data.results.filter((x) => x.kind === "album");
  const lists = data.results.filter((x) => x.kind === "playlist");
  const out = [];
  if (songs.length) out.push(el("div", { class: "label sechead", textContent: "Songs" }), el("div", { class: "rows" }, ...songs.slice(0, kind === "all" ? 6 : 20).map(songRow)));
  for (const [label, items] of [["Albums", albums], ["Playlists", lists]]) {
    if (!items.length) continue;
    out.push(el("div", { class: "label sechead", textContent: label }), el("div", { class: "cards" }, ...items.slice(0, kind === "all" ? 4 : 20).map((a) =>
      el("button", { class: "acard", onclick: () => openCollection(a.kind, a.id) },
        a.thumb ? el("img", { class: "cover", src: artUrl(a.thumb, 240), alt: "", loading: "lazy" }) : artPh(a, "cover"),
        el("div", { class: "t ellip", textContent: a.title }), el("div", { class: "s ellip", textContent: a.subtitle })))));
  }
  $("results").replaceChildren(...out);
}

function showCollection(on) {
  $("s-coll-view").classList.toggle("hidden", !on);
  $("s-results-view").classList.toggle("hidden", on);
}
$("coll-back").replaceChildren(ic("back"), "Back");
$("coll-back").addEventListener("click", () => showCollection(false));

async function openCollection(k, id) {
  show("search");
  showCollection(true);
  $("coll-title").textContent = "Loading…";
  $("coll-sub").textContent = "";
  $("coll-art").removeAttribute("src");
  $("coll-add").classList.add("hidden");
  $("coll-tracks").replaceChildren();
  const r = await api(`api/collection?kind=${k}&id=${encodeURIComponent(id)}&code=${encodeURIComponent(code)}`);
  if (!r.ok) { $("coll-title").textContent = `Couldn't open that ${k}.`; return; }
  const c = await r.json();
  $("coll-title").textContent = c.title;
  $("coll-sub").textContent = c.subtitle;
  if (c.thumb) $("coll-art").src = artUrl(c.thumb, 240);
  const ids = c.tracks.map((t) => t.videoId).slice(0, 50);
  const addAll = $("coll-add");
  addAll.classList.toggle("hidden", !ids.length);
  addAll.disabled = false;
  addAll.replaceChildren(ic("plus"), c.tracks.length > 50 ? "Add first 50 songs" : ids.length === 1 ? "Add 1 song" : `Add all ${ids.length} songs`);
  addAll.onclick = async () => { if (await addTracks(ids)) { addAll.disabled = true; addAll.replaceChildren(ic("check"), "Added to the queue"); } };
  $("coll-tracks").replaceChildren(...(c.tracks.length ? c.tracks.map(songRow) : [info("This one's empty.")]));
}

// ---- boot: ?code= from the QR link wins, then a remembered code
const initial = (new URLSearchParams(location.search).get("code") || localStorage.getItem("rubato.code") || "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 5);
teaser(null);
if (initial.length === 5) {
  fillCode(initial);
  fetch(`api/peek?code=${encodeURIComponent(initial)}`).then((r) => (r.ok ? r.json() : null)).then((d) => d && teaser(d)).catch(() => {});
}
if (initial.length === 5 && myName && sid) join(initial, myName, true); // returning guest: straight back in
else (initial.length === 5 ? $("name-input") : boxes[0]).focus();
