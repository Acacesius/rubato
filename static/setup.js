// First-run setup. The setup token lives in sessionStorage only (this tab),
// and is sent as a header; it never goes in a URL.
let token = sessionStorage.getItem("rubato.setup") || "";
let info = null;        // from /api/setup/verify
let newKey = null;      // host key, shown once right after generating
let newSpeaker = null;  // speaker key, shown once (as the speaker link)
let linked = false;

const auth = () => ({ "X-Setup-Token": token });
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { ...auth(), ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (r.status === 410) { location.replace("host"); throw new Error("done"); }
  return { ok: r.ok, status: r.status, data };
}

// ---- gate
$("gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  token = $("token").value.trim();
  if (!token) return;
  const { ok, data } = await post("api/setup/verify");
  if (!ok) { $("gate-err").textContent = data.detail || "That token didn't work."; return; }
  sessionStorage.setItem("rubato.setup", token);
  start(data);
});

function start(data) {
  info = data;
  $("gate").classList.add("hidden");
  $("wizard").classList.remove("hidden");
  $("host-name").value = info.host_name || "";
  renderKeyStep();
  renderSpeakerStep();
  $("url").value = info.public_url || APP; // what this browser sees, prefix included
  $("url-note").textContent = info.public_url_env ? "PUBLIC_URL is set in the environment and takes precedence over this value." : `Detected from this page: ${APP}`;
  mountLinker($("linker"), { headers: auth, onChange: (res) => {
    linked = !!(res && res.account);
    $("yt-next").disabled = !linked;
  } });
  go(1);
}

function go(n) {
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== String(n)));
  document.querySelectorAll("#steps li").forEach((li) => {
    const s = +li.dataset.step;
    li.className = s < n ? "done" : s === n ? "current" : "";
    if (s === n) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  });
  window.scrollTo({ top: 0 });
  if (n === 5) $("done-lead").textContent = linked
    ? "YouTube is linked. Open the speaker link on the device that plays, then share the room code from the dashboard."
    : "YouTube isn't linked yet, so playback is off. Use “Relink YouTube” on the dashboard when you're ready.";
}

// One "shown once" secret: key text, copy button, a must-tick checkbox, then Continue.
function showOnce(area, text, label, onNext) {
  const saved = el("input", { type: "checkbox" });
  const next = el("button", { class: "btn red big-btn", textContent: "Continue", disabled: true, onclick: onNext });
  saved.addEventListener("change", () => (next.disabled = !saved.checked));
  const keytext = el("span", { class: "mono", textContent: text });
  area.replaceChildren(
    el("div", { class: "keybox" }, keytext,
      el("button", { class: "btn fill", html: icon("copy") + "Copy", onclick: async () => {
        try { await navigator.clipboard.writeText(text); toast(`${label} copied`); }
        catch { const r = document.createRange(); r.selectNodeContents(keytext); getSelection().removeAllRanges(); getSelection().addRange(r); toast("Select and copy it"); }
      } })),
    el("p", { class: "warnline", textContent: "This is the only time it's shown. Save it in a password manager." }),
    el("label", { class: "check" }, saved, el("span", { textContent: `I've saved the ${label.toLowerCase()} somewhere safe` })),
    next);
}

// ---- step 1: name + host key
async function saveName() {
  const { ok, data } = await post("api/setup/host-name", { name: $("host-name").value });
  if (!ok) { toast(data.detail || "Enter your name."); $("host-name").focus(); return false; }
  return true;
}
const toStep2 = async () => { if (await saveName()) go(2); };
function renderKeyStep() {
  const area = $("key-area");
  if (info.host_key === "env") {
    area.replaceChildren(el("p", { class: "muted", textContent: "A host key is already set with the HOST_KEY environment variable. Use that one." }),
      el("button", { class: "btn red big-btn", textContent: "Continue", onclick: toStep2 }));
  } else if (info.host_key === "stored" && !newKey) {
    area.replaceChildren(el("p", { class: "muted", textContent: "A host key was already created. If you didn't save it, generate a new one (the old one stops working)." }),
      el("div", { class: "row-actions" },
        el("button", { class: "btn", textContent: "Generate a new key", onclick: () => genKey(true) }),
        el("button", { class: "btn red", textContent: "I have it, continue", onclick: toStep2 })));
  } else if (!newKey) {
    area.replaceChildren(el("button", { class: "btn red big-btn", html: icon("key") + "Generate host key", onclick: () => genKey(false) }));
  } else {
    showOnce(area, newKey, "Host key", toStep2);
  }
}
async function genKey(regenerate) {
  if (!(await saveName())) return;
  if (regenerate && !confirm("Generate a new host key? The old one will stop working.")) return;
  const { ok, data } = await post("api/setup/host-key", { regenerate });
  if (!ok) return toast(data.detail || "Couldn't create the key.");
  if (data.key) { newKey = data.key; info.host_key = "stored"; }
  renderKeyStep();
}

// ---- step 2: speaker key, shown as the speaker link (key in the #fragment, so it never reaches server logs)
const speakerLink = (k) => `${(info.public_url || APP).replace(/\/$/, "")}/speaker#k=${k}`;
function renderSpeakerStep() {
  const area = $("spk-area");
  if (info.speaker_key === "env") {
    area.replaceChildren(el("p", { class: "muted", textContent: "A speaker key is set with the SPEAKER_KEY environment variable. The speaker link is /speaker#k= followed by that key." }),
      el("button", { class: "btn red big-btn", textContent: "Continue", onclick: () => go(3) }));
  } else if (info.speaker_key === "stored" && !newSpeaker) {
    area.replaceChildren(el("p", { class: "muted", textContent: "A speaker key was already created. If you didn't save the link, generate a new one. You can also rotate it later from the dashboard." }),
      el("div", { class: "row-actions" },
        el("button", { class: "btn", textContent: "Generate a new key", onclick: () => genSpeaker(true) }),
        el("button", { class: "btn red", textContent: "I have it, continue", onclick: () => go(3) })));
  } else if (!newSpeaker) {
    area.replaceChildren(el("button", { class: "btn red big-btn", html: icon("speaker") + "Generate speaker key", onclick: () => genSpeaker(false) }));
  } else {
    showOnce(area, speakerLink(newSpeaker), "Speaker link", () => go(3));
    area.append(el("p", { class: "muted", style: "font-size:13px;margin-top:12px", textContent: "If you change the public address in the next step, the part before /speaker changes too; the key stays the same." }));
  }
}
async function genSpeaker(regenerate) {
  if (regenerate && !confirm("Generate a new speaker key? The old link stops working.")) return;
  const { ok, data } = await post("api/setup/speaker-key", { regenerate });
  if (!ok) return toast(data.detail || "Couldn't create the key.");
  if (data.key) { newSpeaker = data.key; info.speaker_key = "stored"; }
  renderSpeakerStep();
}

// ---- step 3: public URL
$("url-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const { ok, data } = await post("api/setup/public-url", { url: $("url").value });
  if (!ok) { $("url-err").textContent = data.detail || "That address didn't work."; return; }
  $("url-err").textContent = "";
  info.public_url = data.public_url;
  go(4);
});

// ---- step 4: YouTube
$("yt-next").addEventListener("click", () => go(5));
$("skip-yt").addEventListener("click", () => {
  if (linked) return go(5);
  if ($("skip-note").classList.contains("hidden")) { $("skip-note").classList.remove("hidden"); $("skip-yt").textContent = "Skip anyway"; return; }
  go(5);
});

// ---- step 5: done
$("finish").addEventListener("click", async () => {
  const { ok, data } = await post("api/setup/finish");
  if (!ok) { $("finish-err").textContent = data.detail || "Couldn't finish setup."; return; }
  if (newKey) localStorage.setItem("rubato.hostkey", newKey); // the dashboard reads it from here, not from the URL
  sessionStorage.removeItem("rubato.setup");
  location.replace(data.redirect || "host");
});

// resume after a reload in the same tab
if (token) post("api/setup/verify").then(({ ok, data }) => (ok ? start(data) : sessionStorage.removeItem("rubato.setup"))).catch(() => {});
