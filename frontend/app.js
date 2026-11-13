const $ = (s) => document.querySelector(s);
const api = (path, opts) => fetch(path, opts).then(async (r) => {
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
});

/* ---------- tabs ---------- */
function wireTab(t) {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((x) => x.classList.remove("visible"));
    t.classList.add("active");
    $("#tab-" + t.dataset.tab).classList.add("visible");
    if (t.dataset.tab === "library") loadLibrary();
  });
}
document.querySelectorAll(".tab").forEach(wireTab);

/* ---------- optional private features (only present in the owner's copy) ---------- */
let HAS_GENERATE = false;
api("/api/status").then((s) => {
  if (!s.features?.generate) return;
  HAS_GENERATE = true;
  const sc = document.createElement("script");
  sc.src = "/static/generate.js";
  document.body.appendChild(sc);
  loadLibrary();
}).catch(() => {});

/* ---------- status bar ---------- */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const llm = $("#llm-pill");
    llm.textContent = "LLM: " + (s.llm.ready ? s.llm.backend : "none (heuristic mode)");
    llm.className = "pill " + (s.llm.ready ? "ok" : "warn");
    llm.title = s.llm.detail;
    const yt = $("#yt-pill");
    const q = s.youtube_quota;
    yt.textContent = "YouTube: " + (s.youtube_configured
      ? `${q.uploads_left}/${q.max_uploads_per_day} uploads left`
      : "not set up");
    yt.className = "pill " + (!s.youtube_configured ? "warn" : q.uploads_left === 0 ? "warn" : "ok");
    yt.title = s.youtube_configured
      ? `Free-tier quota: ${q.uploads_today} used today, resets ${q.resets_at_local} (midnight US-Pacific)`
      : "Put client_secret.json in the project root (see README)";
    const disk = $("#disk-pill");
    disk.textContent = "Disk: " + s.disk_free_gb + " GB free";
    disk.className = "pill " + (s.disk_free_gb < 2 ? "warn" : "");
  } catch { /* server restarting */ }
}
refreshStatus();
setInterval(refreshStatus, 15000);

/* ---------- job polling ---------- */
function fmtEta(s) {
  if (s == null) return "";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return ` · ~${m ? m + "m " : ""}${sec}s left`;
}

function watchJob(jobId, box, onDone) {
  box.hidden = false;
  box.innerHTML = `<div class="jobmsg">starting…</div><div class="progress"><div style="width:0%"></div></div><div class="joblog"></div>`;
  const msg = box.querySelector(".jobmsg"), bar = box.querySelector(".progress > div"),
        log = box.querySelector(".joblog");
  const timer = setInterval(async () => {
    let j;
    try { j = await api("/api/jobs/" + jobId); } catch { return; }
    msg.textContent = (j.status === "error" ? "❌ " : j.status === "done" ? "✅ " : "⏳ ") +
      (j.error || j.message || j.status) + (j.status === "running" ? fmtEta(j.eta_seconds) : "");
    msg.classList.toggle("err", j.status === "error");
    bar.style.width = Math.round(j.progress * 100) + "%";
    log.textContent = j.logs.join("\n");
    log.scrollTop = log.scrollHeight;
    if (j.status === "done" || j.status === "error") {
      clearInterval(timer);
      if (j.status === "done" && onDone) onDone(j);
    }
  }, 1200);
}

/* ---------- global upload toasts (visible from every tab) ---------- */
function watchUpload(jobId, fileName) {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.innerHTML = `
    <div class="t-title"><span>⏫ Uploading ${fileName}</span>
      <button class="t-close" title="dismiss">✕</button></div>
    <div class="t-msg">starting…</div>
    <div class="progress"><div style="width:0%"></div></div>`;
  $("#toasts").appendChild(toast);
  toast.querySelector(".t-close").addEventListener("click", () => toast.remove());
  const msg = toast.querySelector(".t-msg"), bar = toast.querySelector(".progress > div");
  const timer = setInterval(async () => {
    let j;
    try { j = await api("/api/jobs/" + jobId); } catch { return; }
    bar.style.width = Math.round(j.progress * 100) + "%";
    if (j.status === "done") {
      clearInterval(timer);
      toast.classList.add("ok");
      toast.querySelector(".t-title span").textContent = `✅ Uploaded ${fileName}`;
      const r = j.result || {};
      msg.innerHTML = `${r.channel || ""} · ${r.privacy || ""} · ` +
        (r.url ? `<a href="${r.url}" target="_blank">${r.url}</a>` : "") +
        `<br>${r.uploads_left_today ?? "?"} free upload(s) left today`;
      refreshStatus();
    } else if (j.status === "error") {
      clearInterval(timer);
      toast.classList.add("err");
      toast.querySelector(".t-title span").textContent = `❌ Upload failed: ${fileName}`;
      msg.textContent = j.error || j.message;
    } else {
      msg.textContent = j.message || j.status;
    }
  }, 1200);
}

/* ---------- workflow A ---------- */
function repOptions() {
  return {
    reaction_vo: $("#rep-vo").checked,
    commentary_vo: $("#rep-commentary").checked,
    skip_thumbnail: $("#rep-nothumb").checked,
    frame: $("#rep-frame").value,
  };
}

// commentary (full narration) and reaction (one-liner) are mutually exclusive
$("#rep-commentary").addEventListener("change", () => {
  if ($("#rep-commentary").checked) $("#rep-vo").checked = false;
});
$("#rep-vo").addEventListener("change", () => {
  if ($("#rep-vo").checked) $("#rep-commentary").checked = false;
});

$("#rep-url-btn").addEventListener("click", async () => {
  const url = $("#rep-url").value.trim();
  if (!url) return alert("Paste a video URL first");
  $("#rep-url-btn").disabled = true;
  try {
    const { job_id } = await api("/api/repurpose/url", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, ...repOptions() }),
    });
    watchJob(job_id, $("#rep-job"), loadLibrary);
  } catch (e) { alert(e.message); }
  $("#rep-url-btn").disabled = false;
});

const repDrop = $("#rep-drop"), repFile = $("#rep-file");
repDrop.addEventListener("dragover", (e) => { e.preventDefault(); repDrop.classList.add("drag"); });
repDrop.addEventListener("dragleave", () => repDrop.classList.remove("drag"));
repDrop.addEventListener("drop", (e) => {
  e.preventDefault(); repDrop.classList.remove("drag");
  if (e.dataTransfer.files[0]) uploadRepurpose(e.dataTransfer.files[0]);
});
repFile.addEventListener("change", () => repFile.files[0] && uploadRepurpose(repFile.files[0]));

async function uploadRepurpose(file) {
  const fd = new FormData();
  fd.append("file", file);
  const o = repOptions();
  fd.append("reaction_vo", o.reaction_vo);
  fd.append("commentary_vo", o.commentary_vo);
  fd.append("skip_thumbnail", o.skip_thumbnail);
  fd.append("frame", o.frame);
  $("#rep-job").hidden = false;
  $("#rep-job").innerHTML = `<div class="jobmsg">⏫ uploading ${file.name}…</div>`;
  try {
    const { job_id } = await api("/api/repurpose/upload", { method: "POST", body: fd });
    watchJob(job_id, $("#rep-job"), loadLibrary);
  } catch (e) { $("#rep-job").innerHTML = `<div class="jobmsg err">❌ ${e.message}</div>`; }
}

/* ---------- library ---------- */
const selected = new Map();  // "kind/file" -> item

function updateToolbar() {
  const n = selected.size;
  $("#lib-toolbar").hidden = n === 0;
  $("#lib-count").textContent = `${n} selected`;
  $("#lib-upload").disabled = n > 6;
  $("#lib-upload").textContent = n > 6
    ? "⏫ Upload selected (max 6/day)" : "⏫ Upload selected to YouTube";
}

let libFilter = "all"; // all | kids | gaming

async function loadStorage() {
  try {
    const s = await api("/api/storage");
    const f = s.folders;
    const total = (f.clips + f.renders + f.ingest + f.frames).toFixed(0);
    const rc = s.reclaimable;
    $("#storage-text").innerHTML =
      `💾 <b>${total} MB</b> in library ` +
      `(clips ${f.clips} · renders ${f.renders} · sources ${f.ingest} MB) · ` +
      `${s.disk_free_gb} GB free · ` +
      `<b>${rc.uploaded_mb} MB</b> reclaimable from ${rc.uploaded_items} uploaded`;
    const cu = $("#storage-clean-uploaded");
    cu.disabled = rc.uploaded_items === 0;
    cu.textContent = rc.uploaded_items
      ? `🧹 Free ${rc.uploaded_mb} MB from ${rc.uploaded_items} uploaded`
      : "🧹 Nothing uploaded to clean";
    $("#storage-clear-sources").disabled = rc.source_videos_mb < 1;
  } catch (e) { $("#storage-text").textContent = "storage unavailable"; }
}

$("#storage-clean-uploaded").addEventListener("click", async () => {
  if (!confirm("Delete local video files for all clips/renders already on YouTube? " +
               "Their upload record is kept; this only frees disk space.")) return;
  const r = await api("/api/library/delete-uploaded", { method: "POST" });
  alert(`Freed ${r.freed_mb} MB by deleting ${r.deleted} uploaded video(s).`);
  loadStorage(); loadLibrary();
});

$("#storage-clear-sources").addEventListener("click", async () => {
  if (!confirm("Delete cached source downloads? They re-download automatically when needed.")) return;
  const r = await api("/api/library/clear-sources", { method: "POST" });
  alert(`Freed ${r.freed_mb} MB of cached sources.`);
  loadStorage();
});

async function loadLibrary() {
  loadStorage();
  const lib = await api("/api/library");
  const el = $("#library");
  el.innerHTML = "";
  selected.clear();
  updateToolbar();
  // section filter tabs: the kids channel library and the gameplay clip
  // library are separate workspaces
  const tabs = document.createElement("div");
  tabs.className = "libtabs";
  const showAnim = HAS_GENERATE || lib.renders.length > 0;
  if (!showAnim) libFilter = "all";
  (showAnim ? [["all", "All"], ["gaming", "✂️ Clips"], ["kids", "✨ Animations"]] : [])
    .forEach(([val, label]) => {
      const b = document.createElement("button");
      b.className = "libtab" + (libFilter === val ? " active" : "");
      b.textContent = label;
      b.addEventListener("click", () => { libFilter = val; loadLibrary(); });
      tabs.appendChild(b);
    });
  el.appendChild(tabs);
  const section = (label, items, kind) => {
    el.insertAdjacentHTML("beforeend", `<div class="sectionlabel">${label}</div>`);
    if (!items.length) {
      el.insertAdjacentHTML("beforeend", `<div class="empty">Nothing here yet.</div>`);
      return;
    }
    const grid = document.createElement("div");
    grid.className = "grid";
    items.forEach((it) => {
      const title = it.meta.title || it.meta.script?.slice(0, 60) || it.file;
      const card = document.createElement("div");
      card.className = "vcard";
      const up = it.meta.youtube;
      const key = `${kind}/${it.file}`;
      card.innerHTML = `
        <div class="vwrap">
          <video src="${it.url}" controls preload="metadata"></video>
          <input type="checkbox" class="sel" title="select for batch actions">
          ${up ? `<a class="upbadge" href="${up.url}" target="_blank"
                     title="Uploaded to ${up.channel} (${up.privacy}) on ${up.uploaded_at} — click to open">✅ On YouTube</a>` : ""}
        </div>
        <div class="vbody">
          <div class="vtitle" title="${title.replace(/"/g, "&quot;")}">${title}</div>
          <div class="vmeta">${it.size_mb} MB${it.meta.start != null ? ` · ${Math.round(it.meta.start)}s→${Math.round(it.meta.end)}s` : ""}${it.meta.duration ? ` · ${it.meta.duration}s` : ""}${up ? ` · <span class="upchan">${up.channel}</span>` : ""}</div>
          <div class="vbtns">
            <button class="yt">${up ? "▶ Upload again" : "▶ YouTube"}</button>
            <button class="del">🗑</button>
          </div>
        </div>`;
      card.querySelector(".sel").addEventListener("change", (e) => {
        if (e.target.checked) selected.set(key, { kind, item: it });
        else selected.delete(key);
        card.classList.toggle("selected", e.target.checked);
        updateToolbar();
      });
      card.querySelector(".yt").addEventListener("click", () => openYtDialog(kind, it));
      card.querySelector(".del").addEventListener("click", async () => {
        if (!confirm("Delete " + it.file + "?")) return;
        await api(`/api/library/${kind}/${encodeURIComponent(it.file)}`, { method: "DELETE" });
        loadLibrary();
      });
      grid.appendChild(card);
    });
    el.appendChild(grid);
  };
  if (libFilter !== "kids") section("✂️ Clips — repurposed Shorts", lib.clips, "clips");
  if (showAnim && libFilter !== "gaming") section("✨ Animations — generated Shorts", lib.renders, "renders");
}
loadLibrary();

/* ---------- batch actions ---------- */
function itemTitle(it) {
  const scriptTitle = it.meta.script
    ? it.meta.script.split(/[.!?]/)[0].trim().slice(0, 80) : "";
  return it.meta.title || scriptTitle || it.file.replace(/\.mp4$/, "");
}

$("#lib-clear").addEventListener("click", loadLibrary);

$("#lib-delete").addEventListener("click", async () => {
  const items = [...selected.values()];
  if (!items.length) return;
  if (!confirm(`Delete ${items.length} video(s) permanently?\n\n` +
               items.map((x) => "• " + itemTitle(x.item)).join("\n"))) return;
  for (const { kind, item } of items) {
    try { await api(`/api/library/${kind}/${encodeURIComponent(item.file)}`, { method: "DELETE" }); }
    catch (e) { alert(`Failed deleting ${item.file}: ${e.message}`); }
  }
  loadLibrary();
});

$("#lib-upload").addEventListener("click", async () => {
  const items = [...selected.values()];
  if (!items.length) return;
  if (items.length > 6) return alert("YouTube's free tier allows 6 uploads/day — select at most 6.");
  $("#batch-n").textContent = items.length;
  try {
    const { channels, quota } = await api("/api/youtube/channels");
    const q = $("#batch-quota");
    q.textContent = quota.uploads_left >= items.length
      ? `Quota: ${quota.uploads_left} of ${quota.max_uploads_per_day} uploads left today`
      : `⚠ Only ${quota.uploads_left} upload(s) left today (resets ${quota.resets_at_local}) — the rest will be refused.`;
    q.classList.toggle("exhausted", quota.uploads_left < items.length);
    const def = pickChannel(channels, items.every((x) => x.kind === "renders") ? "kids" : "gaming");
    $("#batch-channels").innerHTML = channels.map((c) => `
      <label class="chan">
        <input type="radio" name="batch-channel" value="${c.key}" ${c.key === def ? "checked" : ""}>
        <span>${c.label}${c.made_for_kids ? " 👶" : " 🎮"}
          <small>${c.authorized ? `authorized: ${c.channel_title || "linked"}` : "first upload opens Google sign-in"}</small></span>
      </label>`).join("");
  } catch (e) { return alert(e.message); }
  $("#batch-dialog").showModal();
});

$("#batch-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok") return;
  const items = [...selected.values()];
  const channel = document.querySelector('input[name="batch-channel"]:checked')?.value;
  const privacy = $("#batch-privacy").value;
  for (const { kind, item } of items) {
    try {
      const { job_id } = await api("/api/youtube/upload", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind, file: item.file,
          title: itemTitle(item),
          description: item.meta.description || item.meta.script?.slice(0, 300) || "",
          hashtags: item.meta.hashtags || ["#shorts"],
          privacy, channel,
        }),
      });
      watchUpload(job_id, item.file);
      await new Promise((r) => setTimeout(r, 800));  // stagger politely
    } catch (err) {
      alert(`${item.file}: ${err.message}`);
      if (String(err.message).toLowerCase().includes("quota")) break;
    }
  }
});

/* ---------- youtube upload dialog ---------- */
// preferred channel key if configured (channels.json), else the first channel
function pickChannel(channels, wanted) {
  return channels.some((c) => c.key === wanted) ? wanted : channels[0]?.key;
}
let ytTarget = null;
async function openYtDialog(kind, item) {
  ytTarget = { kind, item };
  const scriptTitle = item.meta.script
    ? item.meta.script.split(/[.!?]/)[0].trim().slice(0, 80) : "";
  $("#yt-title").value = item.meta.title || scriptTitle || item.file.replace(/\.mp4$/, "");
  $("#yt-desc").value = item.meta.description || item.meta.script?.slice(0, 300) || "";
  $("#yt-tags").value = (item.meta.hashtags || ["#shorts"]).join(", ");

  try {
    const { channels, quota } = await api("/api/youtube/channels");
    const qline = $("#yt-quota");
    qline.textContent = quota.uploads_left > 0
      ? `Free-tier quota: ${quota.uploads_left} of ${quota.max_uploads_per_day} uploads left today (resets ${quota.resets_at_local})`
      : `Daily free quota exhausted (${quota.uploads_today}/${quota.max_uploads_per_day} used). Resets ${quota.resets_at_local} — midnight US-Pacific.`;
    qline.classList.toggle("exhausted", quota.uploads_left === 0);
    $("#yt-go").disabled = quota.uploads_left === 0;
    // default: renders (animations) -> kids channel, clips (gaming) -> gaming channel
    const def = pickChannel(channels, kind === "renders" ? "kids" : "gaming");
    $("#yt-channels").innerHTML = channels.map((c) => `
      <label class="chan">
        <input type="radio" name="yt-channel" value="${c.key}" ${c.key === def ? "checked" : ""}>
        <span>${c.label}${c.made_for_kids ? " 👶" : " 🎮"}
          <small>${c.authorized
            ? `authorized: ${c.channel_title || "channel linked"}`
            : "first upload opens Google sign-in — pick this channel there"}</small>
        </span>
      </label>`).join("");
  } catch (e) {
    $("#yt-quota").textContent = e.message;
  }
  $("#yt-dialog").showModal();
}
$("#yt-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok" || !ytTarget) return;
  const target = ytTarget;
  ytTarget = null;  // ignore repeat submissions of the same dialog
  try {
    const { job_id } = await api("/api/youtube/upload", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: target.kind, file: target.item.file,
        title: $("#yt-title").value, description: $("#yt-desc").value,
        hashtags: $("#yt-tags").value.split(",").map((s) => s.trim()).filter(Boolean),
        privacy: $("#yt-privacy").value,
        channel: document.querySelector('input[name="yt-channel"]:checked')?.value,
      }),
    });
    watchUpload(job_id, target.item.file);
  } catch (err) { alert(err.message); }
});
