// Shared helpers. All text goes through textContent: track titles and names are untrusted.
const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v; // only ever our own ICON/SVG strings
    else if (k === "style") n.style.cssText = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k.startsWith("aria") || k === "title" || k === "role" || k === "tabindex") n.setAttribute(k.replace(/^aria([A-Z])/, (_, c) => `aria-${c.toLowerCase()}`), v);
    else n[k] = v;
  }
  for (const k of kids) if (k != null && k !== false) n.append(k);
  return n;
}

// The app may live at / or under a prefix such as /rubato; each page sets <base> to the app root.
const ROOT = new URL(document.baseURI).pathname.replace(/\/$/, "");
const APP = location.origin + ROOT;   // e.g. https://host:8443/rubato
const wsUrl = (path) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${ROOT}/${path}`;

// replaceChildren() would print a null as "null"; el() skips them, and so does this.
const put = (...kids) => kids.filter((k) => k != null && k !== false);

function fmt(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}
const ago = (s) => (s == null ? "" : s < 5 ? "now" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`);
const ordinal = (n) => `${n}${["th", "st", "nd", "rd"][(n % 100 > 10 && n % 100 < 14) ? 0 : n % 10 < 4 ? n % 10 : 0]}`;

// Stroke icons on a 24px grid. Static strings, safe for innerHTML.
const P = {
  play: '<path d="M7 4.5v15l12-7.5z" fill="currentColor"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  skip: '<path d="M6 5l10 7-10 7z"/><path d="M18 5v14"/>',
  queue: '<path d="M4 6h16M4 12h10M4 18h10"/><path d="M17 14l4 3-4 3z"/>',
  speaker: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  next: '<path d="M4 7h10M4 12h7M4 17h7"/><path d="M16 11l5 3.5-5 3.5z"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  back: '<path d="M15 18l-6-6 6-6"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  radio: '<circle cx="12" cy="12" r="2"/><path d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 16.2a6 6 0 0 0 0-8.4M4.9 4.9a10 10 0 0 0 0 14.2M19.1 19.1a10 10 0 0 0 0-14.2"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.7L20 8.5"/><path d="M20 3.5v5h-5"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  grip: '<circle cx="9" cy="6" r="1" fill="currentColor"/><circle cx="15" cy="6" r="1" fill="currentColor"/><circle cx="9" cy="12" r="1" fill="currentColor"/><circle cx="15" cy="12" r="1" fill="currentColor"/><circle cx="9" cy="18" r="1" fill="currentColor"/><circle cx="15" cy="18" r="1" fill="currentColor"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  unplug: '<path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0z"/><path d="M12 16v5"/><path d="M3 3l18 18"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${P[name]}</svg>`;
const ic = (name) => el("span", { html: icon(name), style: "display:contents" });

// ---- avatars: initials on one of five fixed color pairs, chosen by a stable hash of the name
const PAIRS = [["#3A2428", "#FFB3BE"], ["#2B2A3A", "#C3BFF2"], ["#23332D", "#A9DDBF"], ["#3A3322", "#F2D79B"], ["#2E2430", "#E2B8E8"]];
function hashOf(s) {
  let h = 0;
  for (const c of String(s || "")) h = (h * 31 + c.codePointAt(0)) >>> 0;
  return h;
}
const pairOf = (name) => PAIRS[hashOf((name || "").trim().toLowerCase()) % PAIRS.length];
const initialsOf = (name) => (name || "").trim().split(/\s+/).slice(0, 2).map((w) => [...w][0] || "").join("").toUpperCase() || "?";
function avatar(name, initials, cls = "") {
  const [bg, fg] = pairOf(name);
  return el("span", { class: `avatar ${cls}`, title: name, ariaHidden: "true", textContent: initials || initialsOf(name), style: `background:${bg};color:${fg}` });
}
function avatarStack(names, max = 3) {
  const s = el("div", { class: "stack", ariaLabel: `${names.length} in the room`, role: "img" });
  names.slice(0, max).forEach((n) => s.append(avatar(n)));
  if (names.length > max) s.append(el("span", { class: "avatar", style: "background:var(--raised);color:var(--text)", textContent: `+${names.length - max}` }));
  return s;
}

// Album art, or an art-colored tile with the track's initials when there's none (or it fails to load).
// Tiles for songs use letters and digits only: "Dreams (2004 Remaster)" -> "D2", not "D(".
const titleInitials = (title) => ((title || "").match(/[\p{L}\p{N}]+/gu) || []).slice(0, 2).map((w) => [...w][0]).join("").toUpperCase() || "?";
function artPh(t, cls) {
  const [bg, fg] = pairOf(t ? t.title : "");
  return el("div", { class: `${cls} ph`, style: `background:${bg};color:${fg}`, textContent: t ? titleInitials(t.title) : "" });
}
function art(t, cls = "art") {
  if (!t || !t.thumb) return artPh(t, cls);
  return el("img", { class: cls, src: t.thumb, alt: "", loading: "lazy", onerror: (e) => e.target.replaceWith(artPh(t, cls)) });
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2800);
}

// ---- speaker status pill: Speaker live (red glow) / Buffering (amber) / Speaker offline (grey)
const SPEAKER_TEXT = { live: ["live", "Speaker live"], buffering: ["buf", "Buffering"], locked: ["buf", "Waiting for a tap"], offline: ["off", "Speaker offline"] };
function speakerPill(node, sp, { device = false } = {}) {
  const [cls, text] = SPEAKER_TEXT[sp.state] || SPEAKER_TEXT.offline;
  node.className = `pill spill ${cls}`;
  node.replaceChildren(...put(el("span", { class: `dot ${cls}` }), el("span", { class: "st", textContent: text }),
    device && sp.device && sp.state !== "offline" ? el("span", { class: "dev", textContent: `· ${sp.device}` }) : null));
}

// ---- progress, straight from the speaker's heartbeat. Between beats we
// extrapolate from when the message arrived, on this device's clock only.
const prog = { qid: null, pos: 0, at: 0, playing: false, dur: 0 };
function setProgress(qid, pos, playing, dur) {
  Object.assign(prog, { qid, pos, playing, dur: dur || 0, at: performance.now() });
}
function curPos() {
  const p = prog.playing ? prog.pos + (performance.now() - prog.at) / 1000 : prog.pos;
  return prog.dur ? Math.min(p, prog.dur) : p;
}

// ---- signature: the speaker cone (rings + pulse + art as the dust cap)
function cone(size, extraClass = "") {
  const c = el("div", { class: `cone off ${extraClass}`, style: `--size:${size}`, role: "img", ariaLabel: "Speaker" });
  c.append(...["r1", "r2", "r3", "r4", "r5"].map((r) => el("div", { class: `ring ${r}` })), el("div", { class: "ring pulse" }), el("div", { class: "cap" }));
  c._key = undefined;
  return c;
}
// mode: "playing" (pulse runs) | "paused" (pulse frozen) | "off" (no pulse, dimmed)
function setCone(c, t, mode) {
  c.classList.toggle("playing", mode === "playing");
  c.classList.toggle("off", mode === "off");
  c.setAttribute("aria-label", t ? `Speaker: ${t.title}${mode === "playing" ? ", playing" : mode === "paused" ? ", paused" : ", offline"}` : "Speaker: nothing playing");
  const key = t ? t.qid : null;
  if (c._key === key) return;
  c._key = key;
  const cap = c.querySelector(".cap");
  const fresh = !t ? el("div", { class: "cap ph", style: "background:var(--raised)" }) : t.thumb
    ? el("div", { class: "cap" }, el("img", { src: t.thumb.replace(/=w\d+-h\d+/, "=w544-h544"), alt: "", onerror: () => fresh.replaceWith(capPh(t)) }))
    : capPh(t);
  cap.replaceWith(fresh);
}
function capPh(t) {
  const [bg, fg] = pairOf(t.title);
  return el("div", { class: "cap ph", style: `background:${bg};color:${fg}`, textContent: titleInitials(t.title) });
}

// ---- signature: the spectral progress bar. Each song gets its own "emission
// spectrum": line positions come from a PRNG seeded with the track's videoId.
function rng(seed) {
  let a = hashOf(seed) || 1;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
function spectrum(heightPx, midLabel = "") {
  const box = el("div", { class: "spectrum", style: `--h:${heightPx}px`, role: "progressbar", ariaLabel: "Song progress", ariaValuemin: "0", ariaValuemax: "100" });
  const elapsed = el("div", { class: "el" }), head = el("div", { class: "head" });
  const times = el("div", { class: "spec-times" }, el("span", { textContent: "0:00" }), el("span", { class: "mid", textContent: midLabel }), el("span", { textContent: "0:00" }));
  const wrap = el("div", { class: "prog" }, box, times);
  let lines = [], seed = null;
  box.append(elapsed, head);
  function setTrack(id) {
    if (id === seed) return;
    seed = id;
    lines.forEach((l) => l.el.remove());
    lines = [];
    if (!id) return;
    const r = rng(id), n = 56 + Math.floor(r() * 20);
    for (let i = 0; i < n; i++) {
      const x = 0.6 + r() * 98.8, strong = r() < 0.18, h = strong ? 0.7 + r() * 0.3 : 0.2 + r() * 0.5, top = (1 - h) / 2;
      const l = el("i", { class: strong ? "b" : "", style: `left:${x}%;top:${top * 100}%;bottom:${top * 100}%` });
      lines.push({ x, el: l });
      box.insertBefore(l, head);
    }
  }
  function set(pos, dur) {
    const f = dur ? Math.max(0, Math.min(1, pos / dur)) : 0, pct = f * 100;
    elapsed.style.width = `${pct}%`;
    head.style.left = `${pct}%`;
    for (const l of lines) l.el.classList.toggle("on", l.x <= pct);
    box.setAttribute("aria-valuenow", String(Math.round(pct)));
    times.firstChild.textContent = fmt(pos);
    times.lastChild.textContent = fmt(dur);
  }
  return { el: wrap, setTrack, set };
}

// ---- signature: the volume dial (display only; the range input beneath it is the control)
function dial() {
  const ns = "http://www.w3.org/2000/svg", R = 54, C = 64;
  const pt = (deg, r = R) => { const a = (deg * Math.PI) / 180; return [C + r * Math.cos(a), C + r * Math.sin(a)]; };
  const arc = (from, to) => { const [x1, y1] = pt(from), [x2, y2] = pt(to); return `M${x1.toFixed(2)} ${y1.toFixed(2)} A${R} ${R} 0 ${to - from > 180 ? 1 : 0} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`; };
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 128 128");
  svg.setAttribute("aria-hidden", "true");
  const mk = (attrs) => { const p = document.createElementNS(ns, "path"); for (const [k, v] of Object.entries(attrs)) p.setAttribute(k, v); svg.append(p); return p; };
  // 270° sweep from bottom-left (135°) clockwise to bottom-right (405°)
  mk({ d: arc(135, 405), fill: "none", stroke: "#2A2022", "stroke-width": 8, "stroke-linecap": "round" });
  const val = mk({ d: arc(135, 405), fill: "none", stroke: "#F2506A", "stroke-width": 8, "stroke-linecap": "round", pathLength: 100, "stroke-dasharray": "0 100" });
  const tick = mk({ fill: "none", stroke: "#F4B544", "stroke-width": 3, "stroke-linecap": "round" });
  const num = el("b", { textContent: "0" }), capl = el("span", { textContent: "CAP 80" });
  const box = el("div", { class: "dial" }, svg, el("div", { class: "num" }, num, capl));
  return {
    el: box,
    set(v, cap) {
      val.setAttribute("stroke-dasharray", v > 0 ? `${v} 100` : "0 100");
      val.style.opacity = v > 0 ? 1 : 0;
      const deg = 135 + 270 * (cap / 100), [x1, y1] = pt(deg, R - 9), [x2, y2] = pt(deg, R + 9);
      tick.setAttribute("d", `M${x1.toFixed(2)} ${y1.toFixed(2)} L${x2.toFixed(2)} ${y2.toFixed(2)}`);
      num.textContent = v;
      capl.textContent = `CAP ${cap}`;
    },
  };
}

function setRangeFill(input) {
  const max = +input.max || 100;
  input.style.setProperty("--fill", `${(+input.value / max) * 100}%`);
}

// ---- drag-to-reorder for queue rows: pointer drag on the grip, or arrow keys on it.
// Rows carry data-qid; onMove(qid, newIndex) is called with the final index.
function sortable(list, onMove) {
  const ctl = { dragging: false };
  const rows = () => [...list.querySelectorAll(":scope > [data-qid]")];
  const clear = () => rows().forEach((r) => r.classList.remove("drop-above", "drop-below", "dragging"));
  list.addEventListener("pointerdown", (e) => {
    const grip = e.target.closest(".grip");
    if (!grip || e.button > 0) return;
    const row = grip.closest("[data-qid]"), all = rows(), from = all.indexOf(row);
    if (from < 0) return;
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    ctl.dragging = true;
    row.classList.add("dragging");
    let target = from;
    const move = (ev) => {
      clear();
      row.classList.add("dragging");
      const rs = rows();
      target = rs.findIndex((r) => { const b = r.getBoundingClientRect(); return ev.clientY < b.top + b.height / 2; });
      if (target < 0) target = rs.length;
      if (target === from || target === from + 1) return;
      if (target < rs.length) rs[target].classList.add("drop-above"); else rs[rs.length - 1].classList.add("drop-below");
    };
    const up = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
      grip.removeEventListener("pointercancel", up);
      clear();
      ctl.dragging = false;
      const to = target > from ? target - 1 : target;
      if (to !== from) onMove(row.dataset.qid, to);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
    grip.addEventListener("pointercancel", up);
  });
  list.addEventListener("keydown", (e) => {
    const grip = e.target.closest(".grip");
    if (!grip || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
    e.preventDefault();
    const row = grip.closest("[data-qid]"), from = rows().indexOf(row), to = from + (e.key === "ArrowUp" ? -1 : 1);
    if (to >= 0 && to < rows().length) onMove(row.dataset.qid, to);
  });
  return ctl;
}
const gripBtn = (t) => el("button", { class: "grip", html: icon("grip"), ariaLabel: `Reorder ${t.title}. Drag, or use the arrow keys.` });
