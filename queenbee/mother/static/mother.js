// The local API's token: handed over once in the URL the app opens (/?k=...), kept for this tab, sent with every
// /api/ request (auth.py refuses requests without it). The address bar is cleaned so the token is not copied around.
const QB_TOKEN = (() => {
  try {
    const u = new URL(location.href);
    const k = u.searchParams.get("k") || window.QB_TOKEN || "";
    if (k) {
      try { sessionStorage.setItem("qb-token", k); } catch (e) { /* private window: the token lives in this page only */ }
      if (u.searchParams.has("k")) { u.searchParams.delete("k"); history.replaceState(null, "", u.pathname + u.search + u.hash); }
      return k;
    }
    try { return sessionStorage.getItem("qb-token") || ""; } catch (e) { return ""; }
  } catch (e) { return ""; }
})();
{
  const plainFetch = window.fetch.bind(window);
  window.fetch = (url, opts) => {
    const s = typeof url === "string" ? url : String(url);
    if (s.startsWith("/api/") && QB_TOKEN) {
      opts = Object.assign({}, opts || {});
      opts.headers = Object.assign({}, opts.headers || {}, { "X-QueenBee-Token": QB_TOKEN });
    }
    return plainFetch(url, opts);
  };
}

"use strict";

const STATES = {
  run: ["run", "running"],
  wait: ["wait", "waiting"],
  preempt: ["preempt", "saving, stepping aside"],
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function mb(v) {
  return v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${Math.round(v)} MB`;
}

function hwTile(label, value, sub, pct, cls = "") {
  const t = el("div", `hwt ${cls}`);
  t.appendChild(el("div", "hwl", label));
  const v = el("div", "hwv", value);
  if (sub) v.appendChild(el("span", "hws", sub));
  t.appendChild(v);
  const bar = el("div", "hwbar");
  const f = el("div", `hwfill${pct >= 90 ? " crit" : pct >= 75 ? " warn" : ""}`);
  f.style.width = `${Math.max(0, Math.min(100, pct || 0))}%`;
  bar.appendChild(f);
  t.appendChild(bar);
  return t;
}

function renderHW(system, devices) {
  const host = document.getElementById("hw");
  host.textContent = "";
  const s = system || {};
  if (s.cpu_pct != null) host.appendChild(hwTile("CPU", `${Math.round(s.cpu_pct)}%`,
    s.cpu_cores ? ` · ${s.cpu_cores} cores` : "", s.cpu_pct, "cpu"));
  if (s.ram_total_mb) host.appendChild(hwTile("RAM", mb(s.ram_used_mb), ` / ${mb(s.ram_total_mb)}`,
    100 * s.ram_used_mb / s.ram_total_mb, "ram"));
  const gpus = (s.gpus && s.gpus.length) ? s.gpus
    : devices.map((d) => ({ index: d.index, name: d.name, mem_used_mb: d.used_mb, mem_total_mb: d.total_mb }));
  for (const g of gpus) {
    const tag = gpus.length > 1 ? ` ${g.index}` : "";
    if (g.util_pct != null) host.appendChild(hwTile(`GPU${tag}`, `${Math.round(g.util_pct)}%`, "", g.util_pct, "gpu"));
    if (g.mem_total_mb) host.appendChild(hwTile(`VRAM${tag}`, mb(g.mem_used_mb), ` / ${mb(g.mem_total_mb)}`,
      100 * g.mem_used_mb / g.mem_total_mb, "vram"));
    if (g.temp_c != null) host.appendChild(hwTile("Temp", `${Math.round(g.temp_c)}°C`, "", (g.temp_c - 30) * 100 / 60, "temp"));
    if (g.power_w != null) host.appendChild(hwTile("Power", `${Math.round(g.power_w)} W`, "", g.power_w, "power"));
  }
  const name = gpus[0] && gpus[0].name;
  host.title = name ? name.replace(/^NVIDIA /, "") : "";
}

function jobRow(role, title, stateKey, stateText, why, tags) {
  const row = el("div", "job");
  row.appendChild(el("span", `role ${role}`, role === "primary" ? "PRIMARY" : "SECONDARY"));
  const t = el("span", "title", title);
  for (const tag of tags) t.appendChild(el("span", "tag", tag));
  row.appendChild(t);
  row.appendChild(el("span", `state ${stateKey}`, stateText));
  row.appendChild(el("div", "why", why));
  return row;
}

let gpuState = { jobs: [] };

function renderJobs(data) {
  gpuState = data;
  const host = document.getElementById("jobs");
  host.textContent = "";
  const jobs = [...data.jobs].sort((a, b) => (a.role === "primary" ? 0 : 1) - (b.role === "primary" ? 0 : 1));
  for (const j of jobs) {
    const [key, text] = STATES[j.action] || ["wait", j.action];
    const tags = [];
    if (j.resumable) tags.push("resumable");
    if (j.terminal) tags.push(j.terminal);
    if (j.role === "secondary" && j.action === "run") tags.push(`batch ${j.batch}/${j.max_batch}`);
    host.appendChild(jobRow(j.role, j.name, key, text, j.reason || "", tags));
  }
  for (const b of data.between || []) {
    host.appendChild(jobRow("secondary", `${b.terminal}`, "resume", "waiting to resume",
      "Saved its state and stepped aside; starts again from there when the GPU allows.",
      [String(b.cwd || "").split(/[\\/]/).pop()]));
  }
  if (!host.childElementCount) host.appendChild(el("div", "empty", "No GPU jobs on this machine right now."));
}

/* ---- conversation ------------------------------------------------------ */

let convoTerm = null;
let convoJob = null;
let convoSig = "";

function openConversation(job) {
  openWorker(job);
}

document.getElementById("convo-close").addEventListener("click", () => {
  convoTerm = null;
  document.getElementById("convo-card").hidden = true;
  renderDraft();
});

function convoItem(it) {
  if (it.kind === "tool") {
    const n = el("div", "ci tool");
    n.appendChild(el("span", "tname", it.tool || "tool"));
    n.appendChild(el("span", "tdetail", it.detail || ""));
    return n;
  }
  if (it.kind === "result") {
    return el("div", `ci result${it.error ? " err" : ""}`, it.text || "(no output)");
  }
  const text = it.role === "user" && it.text.length > 700
    ? it.text.slice(0, 700) + " …" : it.text;
  const n = el("div", `ci ${it.role === "user" ? "user" : "agent"}`);
  n.appendChild(el("div", "who", it.role === "user" ? "Brief / user" : "Worker"));
  n.appendChild(el("div", "txt", text));
  return n;
}

async function refreshConversation() {
  if (!convoTerm) return;
  const term = convoTerm;
  let d;
  try {
    d = await (await fetch(`/api/conversation?terminal=${encodeURIComponent(term)}`, { cache: "no-store" })).json();
  } catch { return; }
  if (term !== convoTerm) return;
  const body = document.getElementById("convo");
  const st = document.getElementById("convo-state");
  st.textContent = d.state ? `${d.state}${d.reason ? " · " + d.reason.slice(0, 90) : ""}` : "";
  if (!d.ok) { body.textContent = ""; body.appendChild(el("div", "error", d.error || "unavailable")); return; }
  const items = d.items || [];
  const sig = `${items.length}:${JSON.stringify(items[items.length - 1] || {}).length}`;
  if (sig === convoSig) return;                      // nothing new: keep scroll and selection
  convoSig = sig;
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  body.textContent = "";
  if (!items.length) body.appendChild(el("div", "muted", d.note || "Nothing yet."));
  for (const it of items) body.appendChild(convoItem(it));
  if (atBottom || body.dataset.first !== term) body.scrollTop = body.scrollHeight;
  body.dataset.first = term;
}

setInterval(refreshConversation, 3000);

/* ---- plan ------------------------------------------------------------ */

let draft = null;        // the plan being reviewed: the user's edits live here
let mother = null;       // last server view of Mother

// In-window replacements for the browser's confirm()/alert(): same look as the rest of the app.
function sheet(text, { title = "", ok = "OK", cancel = "Cancel", danger = false, info = false, input = null } = {}) {
  return new Promise((resolve) => {
    const root = document.getElementById("sheet");
    const field = document.getElementById("sheet-input");
    field.hidden = input === null;
    field.value = "";
    field.placeholder = input || "";
    document.getElementById("sheet-title").textContent = title;
    document.getElementById("sheet-text").textContent = text;
    const okBtn = document.getElementById("sheet-ok"), noBtn = document.getElementById("sheet-cancel");
    okBtn.textContent = ok;
    okBtn.className = danger ? "danger-fill" : "";
    noBtn.textContent = cancel;
    noBtn.hidden = info;
    const done = (value) => {
      if (input !== null && value === true) value = field.value.trim();
      else if (input !== null) value = null;
      root.hidden = true;
      okBtn.onclick = noBtn.onclick = root.onclick = null;
      document.removeEventListener("keydown", onKey, true);
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); done(false); }
      else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); done(true); }
    };
    okBtn.onclick = () => done(true);
    noBtn.onclick = () => done(false);
    root.onclick = (e) => { if (e.target === root) done(false); };
    document.addEventListener("keydown", onKey, true);
    root.hidden = false;
    (input !== null ? field : info ? okBtn : danger ? noBtn : okBtn).focus();
  });
}
const ask = (text, opts = {}) => {
  const verb = /^(remove|clean|clear|stop)/i.exec(text);
  return sheet(text, { title: opts.title || (verb ? verb[1][0].toUpperCase() + verb[1].slice(1).toLowerCase() + "?" : "Please confirm"),
                       ok: opts.ok || (verb ? verb[1][0].toUpperCase() + verb[1].slice(1).toLowerCase() : "OK"),
                       danger: opts.danger ?? !!verb });
};
const askText = (text, placeholder, title = "Model") => sheet(text, { title, ok: "Use it", input: placeholder });
const tell = (text) => sheet(String(text), { title: "QueenBee", info: true });

async function post(url, body) {
  try {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify({ queen: currentQueen, ...body }) });
    return await r.json();
  } catch {
    return { ok: false, error: "QueenBee is not responding" };
  }
}

function field(tag, value, onInput, attrs = {}) {
  const n = document.createElement(tag);
  if (tag === "select") {
    for (const opt of attrs.options) {
      const o = document.createElement("option");
      o.value = opt; o.textContent = opt;
      n.appendChild(o);
    }
  }
  for (const [k, v] of Object.entries(attrs)) if (k !== "options") n.setAttribute(k, v);
  n.value = value ?? "";
  n.addEventListener("input", () => onInput(n.value, n));
  return n;
}

// What the user sees for a queen or worker: Q1, W1 (internally q1, P1), and the model by its full name.
// Queens are numbered by their place in the list, so removing the first ones does not leave the count starting at Q3.
const qTag = (id) => {
  const at = (typeof queens !== "undefined" ? queens : []).findIndex((q) => q.id === id);
  return at >= 0 ? `Q${at + 1}` : String(id || "").replace(/^q/i, "Q");
};
// A worker that became a queen of its own hive is numbered under the queen above it: Q2.1, and Q2.1.1
// for one inside that. Plain workers stay W1, W2, ...
function jobTag(job, jobs, queenId) {
  if (job.role !== "mother") return `W${job.priority}`;
  const byPrio = new Map((jobs || []).map((j) => [j.priority, j]));
  const upMother = (j) => {                          // the nearest queen-worker above j
    let p = byPrio.get(j.parent);
    while (p && p.role !== "mother") p = byPrio.get(p.parent);
    return p || null;
  };
  const path = (j) => {
    const up = upMother(j);
    const sibs = (jobs || []).filter((x) => x.role === "mother" && (upMother(x) || null) === up)
      .sort((a, b) => a.priority - b.priority);
    return `${up ? path(up) : qTag(queenId)}.${sibs.findIndex((x) => x.priority === j.priority) + 1}`;
  };
  return path(job);
}
const showIds = (text) => String(text || "").replace(/P(\d+)/g, "W$1");
const MODEL_NAMES = { haiku: "Haiku 4.5", sonnet: "Sonnet 5.5", opus: "Opus 5.5" };
// "claude-sonnet-5-5" -> "Sonnet 5.5": the full id comes from Claude Code's own transcripts (state.model_ids).
function prettyModel(id) {
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(id || "");
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? "." + m[3] : ""}` : "";
}
const modelName = (m) => {
  const ids = (mother && mother.model_ids) || {};
  if (!m || m === "default") return prettyModel(ids.sonnet) || MODEL_NAMES.sonnet;    // what a plain `claude` runs
  if (MODEL_NAMES[m]) return prettyModel(ids[m]) || MODEL_NAMES[m];
  return prettyModel(m) || m;                                                          // an id: "Fable 5.1", or as typed
};

function modelLabel(job) {
  if (job.direct) return "Queen · direct submit, 0 tokens";
  const r = mother && mother.efforts && mother.efforts[job.agent] && mother.efforts[job.agent][job.effort];
  if (job.agent === "codex") {
    const cm = (job.tier && job.tier !== "default") ? job.tier : ((mother && mother.codex_model) || "default model");
    return `Codex ${cm} · ${r || job.effort}`;
  }
  // A prepared job runs on its tier (the cheap worker, or the escalation model).
  const m = job.tier || (mother && mother.models && mother.models.claude && mother.models.claude[job.effort]);
  return `Claude ${modelName(m && m !== "default" ? m : "")}`;
}

function statusOf(job) {
  const st = job.status || "queued";
  const key = job.attention ? "attention"
    : st === "failed" || st === "stopped" ? "failed" : st === "done" ? "done"
    : st === "stopped early" ? "early"
    : st === "GPU job submitted" ? "submitted" : st === "queued" ? "queued" : "working";
  const label = job.attention ? "needs you"
    : st === "queued" && job.waiting_for ? `waiting for W${job.waiting_for}`
    : st === "GPU job submitted" ? "on the GPU" : st;
  return [key, label];
}

// What the queen refused this worker, until you answer: allow that exact call from now on (the worker is told), or
// agree with her. The worker has already gone on without it, so a wrong "no" would otherwise pass unseen.
function renderRefused(job) {
  const slot = document.getElementById("refused-slot");
  slot.textContent = "";
  for (const r of job.refused || []) {
    const box = el("div", "refused-box");
    box.appendChild(el("div", "rf-head", "✕ The queen said no"));
    box.appendChild(el("div", "rf-what", r.question));
    box.appendChild(el("div", "rf-why", r.why));
    const row = el("div", "rf-acts");
    const answer = async (allow) => {
      if (allow && !await ask(`Allow W${job.priority} to run this from now on?\n\n${r.question}`,
                              { title: "Allow it?", ok: "Allow", danger: true })) return;
      const d = await post("/api/refusal", { queen: currentQueen, priority: job.priority, call: r.call, allow });
      if (!d.ok) tell(d.error || "Could not answer it.");
      else if (allow && !d.told) tell(`Allowed. W${job.priority} is not running now, so it was not told; it will be allowed if it asks again.`);
      await poll();
    };
    const yes = el("button", "small danger", "Allow it");
    yes.type = "button";
    yes.addEventListener("click", () => answer(true));
    const no = el("button", "ghost small", "She was right");
    no.type = "button";
    no.addEventListener("click", () => answer(false));
    row.appendChild(yes);
    row.appendChild(no);
    box.appendChild(row);
    slot.appendChild(box);
  }
}

function askBox(job) {
  const q = job.question;
  const box = el("div", "ask");
  if (q) box.appendChild(el("div", "qtext", q.text));
  else if (job.screen && job.screen.length) box.appendChild(el("pre", "qscreen", job.screen.join("\n")));
  else box.appendChild(el("div", "qtext", job.attention.slice(0, 160)));
  for (const o of (q && q.options) || []) box.appendChild(el("div", "qopt", `${o.n}. ${o.text}`));
  box.appendChild(el("div", "qhow", `Answer in QueenBee, terminal ${job.terminal} (arrow keys + Enter, or type).`));
  return box;
}

function fmtValue(v) {
  return v == null ? "—" : (typeof v === "number" ? Number(v.toPrecision(5)).toString() : String(v));
}

// priority -> {sig, node}: a tile is drawn again only when what it shows
// changed, so finished and stopped jobs stay still while running ones update.
const tileCache = new Map();

function renderBoard(jobs) {
  const board = document.getElementById("board");
  const host = document.getElementById("tiles");
  board.hidden = false;
  if (!host.childElementCount) tileCache.clear();
  const keep = new Set();
  let index = 0;
  const results = (mother && mother.results) || null;
  const byPrio = {};
  for (const r of (results && results.rows) || []) byPrio[r.priority] = r;
  for (const job of jobs) {
    const gNow = (gpuState.jobs || []).find((x) => x.terminal && x.terminal === job.workload);
    const rNow = byPrio[job.priority];
    const sig = JSON.stringify([job.status, job.attention, job.question, job.screen, job.error, job.workload,
      job.terminal, job.terminal === convoTerm, job.agent, job.tier, job.direct, job.lane, job.value,
      gNow && [gNow.action, gNow.batch, gNow.max_batch], rNow && rNow.value, results && results.best]);
    keep.add(job.priority);
    const cached = tileCache.get(job.priority);
    if (cached && cached.sig === sig) {
      if (host.children[index] !== cached.node) host.insertBefore(cached.node, host.children[index] || null);
      index++;
      continue;
    }
    const [key, label] = statusOf(job);
    const t = el("article", `tile s-${key}${job.priority === 1 ? " p1" : ""}${job.terminal && job.terminal === convoTerm ? " open" : ""}`);
    const head = el("div", "t-head");
    head.appendChild(el("span", `prio${job.priority === 1 ? " p1" : ""}`, `W${job.priority}`));
    head.appendChild(el("span", `jstat ${key}`, label));
    t.appendChild(head);
    t.appendChild(el("div", "t-title", job.title));
    if (job.lane) {
      const up = job.lane === "up";
      t.appendChild(el("span", `lane ${up ? "up" : "down"}`, `${up ? "↑" : "↓"} ${job.lane} lane · ${job.value}`));
    }
    t.appendChild(el("span", `agent ${job.direct ? "mother" : job.agent}`, modelLabel(job)));

    // What the GPU is doing with it, from the controller.
    const g = (gpuState.jobs || []).find((x) => x.terminal && x.terminal === job.workload);
    const facts = el("div", "t-facts");
    const fact = (k, v, cls = "") => {
      const f = el("div", `fact ${cls}`);
      f.appendChild(el("span", "k", k));
      f.appendChild(el("span", "v", v));
      facts.appendChild(f);
    };
    if (g) {
      const [, text] = STATES[g.action] || ["wait", g.action];
      fact("GPU", `${text}${g.role === "secondary" && g.action === "run" ? ` · batch ${g.batch}/${g.max_batch}` : ""}`, `g-${g.action}`);
    } else if (job.workload) {
      fact("GPU", job.status === "done" ? "finished" : "starting / between runs");
    }
    const r = byPrio[job.priority];
    if (r) {
      const best = results.best === job.priority && r.value != null;
      fact(results.metric || "result", fmtValue(r.value) + (best ? " ★" : ""), best ? "best" : "");
    }
    if (facts.childElementCount) t.appendChild(facts);
    if (job.attention) t.appendChild(askBox(job));
    if (job.error) t.appendChild(el("div", "error", job.error));
    const foot = el("div", "t-foot");
    foot.appendChild(el("span", null, String(job.project_path || "").split(/[\\/]/).filter(Boolean).pop() || ""));
    foot.appendChild(el("span", null, [job.terminal, job.workload].filter(Boolean).join(" → ")));
    t.appendChild(foot);
    if (job.terminal) {
      t.classList.add("clickable");
      t.title = "Show this worker's conversation";
      t.addEventListener("click", () => openConversation(job));
    }
    if (cached) {
      host.replaceChild(t, cached.node);
    } else {
      t.classList.add("fresh");                 // only a new tile animates in
      host.insertBefore(t, host.children[index] || null);
    }
    tileCache.set(job.priority, { sig, node: t });
    index++;
  }
  for (const [prio, cached] of tileCache) {
    if (!keep.has(prio)) { cached.node.remove(); tileCache.delete(prio); }
  }
  let n = `${jobs.length} job${jobs.length === 1 ? "" : "s"}`;
  const sw = mother && mother.sweep;
  if (sw) {
    const res = sw.result || {};
    n = `sweep of ${sw.param} from both ends · ${n}`
      + (res.best != null ? ` · best ${res.best} (${fmtValue(res.score)})` : "");
  }
  const p = (draft && draft.planner) || {};
  document.getElementById("board-meta").textContent = p.model
    ? `${n} · planned by ${p.model} in ${p.seconds}s` + (p.input_tokens != null ? ` · ${fmtTok((p.input_tokens || 0) + (p.output_tokens || 0))} tokens` : "")
    : n;
  document.getElementById("board-notes").textContent = (draft && draft.notes) || "";
}

function renderDraft() {
  const card = document.getElementById("plan-card");
  const host = document.getElementById("plan-jobs");
  const running = mother && mother.running;
  const dispatch = mother && mother.dispatch;
  const board = document.getElementById("board");
  if (!draft && !dispatch) {
    card.hidden = true; board.hidden = true;
    return;
  }
  // A fresh plan under review wins; otherwise show how the started one is doing.
  const reviewing = !!draft && draft.fresh && !running;
  if (!reviewing && dispatch) {
    card.hidden = true;
    board.hidden = true;
    renderAgents(dispatch);
    return;
  }
  board.hidden = true;
  card.hidden = false;
  host.textContent = "";
  const jobs = reviewing || !dispatch ? draft.jobs : dispatch;
  const editable = reviewing;
  for (const job of jobs) {
    const row = el("div", "pjob");
    row.appendChild(el("span", `prio${job.priority === 1 ? " p1" : ""}`, `W${job.priority}`));
    if (editable) {
      row.appendChild(field("input", job.title, (v) => { job.title = v; }));
      row.appendChild(field("select", job.agent, (v) => { job.agent = v; renderMeta(); },
        { options: ["claude", "codex"] }));
      row.appendChild(field("select", job.effort, (v) => { job.effort = v; renderMeta(); },
        { options: ["low", "medium", "high"] }));
      const path = field("input", job.project_path, (v, n) => {
        job.project_path = v; n.classList.toggle("bad", !v.trim());
      }, { list: "project-paths", placeholder: "Project folder, e.g. C:\\Users\\...\\REEM\\Reemm" });
      path.classList.add("full");
      if (!job.project_path) path.classList.add("bad");
      row.appendChild(path);
      const goal = field("textarea", job.goal, (v) => { job.goal = v; }, { rows: "2" });
      goal.classList.add("full");
      row.appendChild(goal);
    } else {
      if (job.terminal) {
        row.classList.add("clickable");
        if (job.terminal === convoTerm) row.classList.add("open");
        row.title = "Show this worker's conversation";
        row.addEventListener("click", () => openConversation(job));
      }
      const title = el("span", "title", job.title + " ");
      title.appendChild(el("span", `agent ${job.direct ? "mother" : job.agent}`, modelLabel(job)));
      row.appendChild(title);
      const st = job.status || "queued";
      const key = job.attention ? "attention"
        : st === "failed" || st === "stopped" ? "failed" : st === "done" ? "done"
        : st === "GPU job submitted" ? "submitted" : st === "queued" ? "queued" : "working";
      const label = job.attention ? "needs you"
        : st === "queued" && job.waiting_for ? `waiting for W${job.waiting_for}` : st;
      row.appendChild(el("span", `jstat ${key}`, label));
      row.appendChild(el("span", "muted", job.terminal || ""));
      row.appendChild(el("div", "meta", `${job.project_path}`
        + (job.workload ? ` · GPU job ${job.workload}` : "")));
      if (job.attention) {
        const q = job.question;
        const box = el("div", "meta ask");
        if (q) box.appendChild(el("div", "qtext", q.text));
        else if (job.screen && job.screen.length) box.appendChild(el("pre", "qscreen", job.screen.join("\n")));
        else box.appendChild(el("div", "qtext", job.attention.slice(0, 160)));
        for (const o of (q && q.options) || []) box.appendChild(el("div", "qopt", `${o.n}. ${o.text}`));
        box.appendChild(el("div", "qhow", `Answer in QueenBee, terminal ${job.terminal} (arrow keys + Enter, or type).`));
        row.appendChild(box);
      }
      if (job.error) row.appendChild(el("div", "meta error", job.error));
    }
    const meta = el("div", "meta");
    meta.dataset.meta = job.priority;
    if (editable) {
      meta.textContent = modelLabel(job) + (job.same_as ? ` · starts after W${job.same_as} submits, reuses its adaptation` : "");
      row.appendChild(meta);
    }
    host.appendChild(row);
  }
  const prepared = draft && draft.prepared;
  document.getElementById("prepare-btn").hidden = !editable || !!(mother && mother.preparing);
  document.getElementById("prepare-btn").textContent = prepared ? "Prepare again" : "Prepare";
  document.getElementById("start-btn").hidden = !editable || !!(mother && mother.preparing);
  document.getElementById("start-btn").textContent = prepared ? "Start" : "Start without preparing";
  document.getElementById("start-btn").classList.toggle("ghost", !prepared);
  document.getElementById("discard-btn").hidden = !editable;
  renderPrep();
  const p = (draft && draft.planner) || {};
  document.getElementById("plan-meta").textContent = p.model
    ? `planned by ${p.model} in ${p.seconds}s · ${p.input_tokens + p.output_tokens} tokens`
      + (p.input_tokens != null ? ` · ${fmtTok((p.input_tokens || 0) + (p.output_tokens || 0))} tokens` : "")
    : "";
  document.getElementById("plan-notes").textContent = (draft && draft.notes) || "";
}

/* ---- results --------------------------------------------------------- */

function renderResults(r) {
  const card = document.getElementById("results-card");
  const host = document.getElementById("results");
  card.hidden = !r;
  if (!r) return;
  document.getElementById("results-hint").textContent =
    `${r.metric} · ${r.goal === "min" ? "lower" : "higher"} is better · read from each run's output file, no agent`;
  host.textContent = "";
  for (const row of r.rows) {
    const n = el("div", `res${row.priority === r.best ? " best" : ""}`);
    n.appendChild(el("span", "prio", `W${row.priority}`));
    n.appendChild(el("span", "title", row.title || ""));
    n.appendChild(el("span", "val", row.value == null ? (row.status === "done" ? "no value found" : "running…") : (typeof row.value === "number" ? Number(row.value.toPrecision(5)).toString() : String(row.value))));
    if (row.priority === r.best) n.appendChild(el("span", "tag", r.complete ? "best" : "best so far"));
    host.appendChild(n);
  }
}


/* ---- the tree: Mother at the top, agents below, branches below them --- */

const NODE_W = 228, NODE_H = 122, ROOT_W = 300, ROOT_H = 158, GAP_X = 22, GAP_Y = 64, ROW_GAP = 22;
const SVGNS = "http://www.w3.org/2000/svg";

function edgeClass(job) {
  if (job._cls) return job._cls;
  if (job.detached) return "detached";
  const [key] = statusOf(job);
  return { submitted: "gpu", working: "agent", attention: "failed", failed: "failed",
           done: "done", early: "done", queued: "queued" }[key] || "queued";
}

function kidsOf(jobs) {
  const kids = new Map([["M", []]]);
  const known = new Set(jobs.map((j) => j.priority));
  for (const j of jobs) {
    const parent = j.parent != null && known.has(j.parent) ? j.parent : "M";
    if (!kids.has(parent)) kids.set(parent, []);
    kids.get(parent).push(j);
  }
  return kids;
}

// The hive map: every queen side by side, each with its workers below it.
// Keys are "<queen>:M" for a queen and "<queen>:<priority>" for a worker.
let queens = [];                       // every queen's summary, from /api/state
// A link such as http://127.0.0.1:7880/?queen=q7 opens that queen; otherwise the one last used.
let currentQueen = (() => {
  const wanted = new URLSearchParams(location.search).get("queen");
  if (wanted) return wanted;
  try { return localStorage.getItem("mother.queen") || ""; } catch { return ""; }
})();
const HIVE_GAP = 150;

function hiveModel() {
  const kids = new Map(), info = new Map(), parentOf = new Map(), roots = [];
  const list = queens.length ? queens
    : mother ? [{ id: mother.queen || "q1", name: mother.queen_name || "Queen 1", primary: true, jobs: [] }] : [];
  for (const q of list) {
    const selected = !!mother && q.id === mother.queen;
    const jobs = selected ? (mother.dispatch || []) : (q.jobs || []);
    if (q.solo) {                              // a free worker: its own root, no queen box above it
      for (const j of jobs) {
        const k = `${q.id}:${j.priority}`;
        info.set(k, { kind: "job", queen: q, selected, job: j, jobs });
        kids.set(k, []);
        roots.push(k);
      }
      continue;
    }
    const root = `${q.id}:M`;
    roots.push(root);
    info.set(root, { kind: "queen", queen: q, selected, jobs });
    kids.set(root, []);
    const known = new Set(jobs.map((j) => j.priority));
    for (const j of jobs) {
      const k = `${q.id}:${j.priority}`;
      info.set(k, { kind: "job", queen: q, selected, job: j, jobs });
      kids.set(k, []);
    }
    for (const j of jobs) {
      const byP = new Map(jobs.map((k) => [k.priority, k]));
      // A sweep's runs are parallel copies of one script: side by side in
      // their own cluster, not hanging under the run that adapted it.
      const sweepRun = j.lane && (j.parent == null || (byP.get(j.parent) || {}).lane);
      let parent = !sweepRun && j.parent != null && known.has(j.parent) ? `${q.id}:${j.parent}` : root;
      const group = j.group || (sweepRun ? "sweep" : "");
      if (parent === root && group) {
        // Workers of one part hang from their cluster.
        const g = `${q.id}:g:${group}`;
        if (!info.has(g)) {
          info.set(g, { kind: "group", queen: q, selected, name: group, members: [] });
          kids.set(g, []);
          kids.get(root).push(g);
          parentOf.set(g, root);
        }
        info.get(g).members.push(j);
        parent = g;
      }
      kids.get(parent).push(`${q.id}:${j.priority}`);
      parentOf.set(`${q.id}:${j.priority}`, parent);
    }
  }
  return { kids, info, parentOf, roots };
}

const GROUP_W = 168, GROUP_H = 38;

function ownSize(model, key) {
  const kind = model.info.get(key).kind;
  return kind === "queen" ? [ROOT_W, ROOT_H] : kind === "group" ? [GROUP_W, GROUP_H] : [NODE_W, NODE_H];
}

// A cluster's link looks like its busiest worker's.
function groupClass(members) {
  const cls = members.map(edgeClass);
  for (const c of ["gpu", "agent", "failed", "queued", "done"]) if (cls.includes(c)) return c;
  return "detached";
}

function groupNode(it) {
  const m = it.members;
  const done = m.filter((j) => ["done", "stopped early"].includes(j.status)).length;
  const on = m.filter((j) => ["agent working", "GPU job submitted"].includes(j.status)).length;
  const n = el("div", `node group g-${groupClass(m)}`);
  n.appendChild(el("span", "g-name", it.name));
  n.appendChild(el("span", "g-count", `${on ? on + " on · " : ""}${done}/${m.length} done`));
  return n;
}

// Children are packed into rows no wider than the view, each child taking its
// whole subtree's width; a sweep of twelve runs, or a hive that grew its own
// workers, wraps instead of stretching across the room.
function measure(key, model, avail, memo) {
  const list = model.kids.get(key) || [];
  const [ownW, ownH] = ownSize(model, key);
  if (!list.length) { memo.set(key, { w: ownW, h: ownH, rows: [] }); return memo.get(key); }
  const sizes = list.map((k) => measure(k, model, avail, memo));
  const rows = [];
  let row = [], rowW = 0;
  list.forEach((k, i) => {
    const w = sizes[i].w;
    if (row.length && rowW + GAP_X + w > avail) { rows.push({ items: row, w: rowW }); row = []; rowW = 0; }
    rowW += (row.length ? GAP_X : 0) + w;
    row.push(k);
  });
  rows.push({ items: row, w: rowW });
  for (const r of rows) r.h = Math.max(...r.items.map((k) => memo.get(k).h));
  const w = Math.max(ownW, ...rows.map((r) => r.w));
  const h = ownH + GAP_Y + rows.reduce((t, r) => t + r.h, 0) + ROW_GAP * (rows.length - 1);
  memo.set(key, { w, h, rows });
  return memo.get(key);
}

function place(key, model, memo, x, y, out) {
  const m = memo.get(key);
  const [ownW, ownH] = ownSize(model, key);
  out.set(key, Object.assign(out.get(key) || {}, { x: x + (m.w - ownW) / 2, y, w: ownW, h: ownH }));
  let cy = y + ownH + GAP_Y;
  const wrapped = m.rows.length > 1;
  const left = x + (m.w - Math.max(...m.rows.map((r) => r.w), 0)) / 2;
  for (const r of m.rows) {
    let cx = x + (m.w - r.w) / 2;
    for (const k of r.items) {
      const sub = memo.get(k);
      // Several rows hang from a bus: down from the parent, along a gutter
      // left of the rows, then a short drop to each child.
      out.set(k, wrapped ? { bus: { gx: left - GAP_X / 2, y0: y + ownH + GAP_Y / 2 } } : {});
      place(k, model, memo, cx, cy, out);
      cx += sub.w + GAP_X;
    }
    cy += r.h + ROW_GAP;
  }
}

const nodeCache = new Map();          // key -> {sig, el}

// Tokens, compact: 1,973,583 -> "1.97M".
function fmtTok(n) {
  n = Number(n) || 0;
  return n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 1 : 2)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`;
}

// Every model a queen can direct with: all of Claude's, and Codex's when it is installed.
function queenModelOptions() {
  const provs = (mother && mother.providers) || [];
  const claude = provs.find((p) => p.id === "claude");
  const codex = provs.find((p) => p.id === "codex");
  const out = [];
  const order = ["sonnet", "opus", "claude-fable-5-1", "haiku"];          // the picks Claude Code itself lists first
  const have = (claude && claude.models) || order;
  for (const m of [...order.filter((x) => have.includes(x)), ...have.filter((x) => !order.includes(x))]) {
    out.push([m, `Claude ${modelName(m)}${m === "haiku" ? " (cheap; weaker at directing)"
      : order.includes(m) ? "" : "  · more models"}`, false]);
  }
  const ok = !!(codex && codex.installed);
  for (const m of (codex && codex.models) || ["default"]) {
    out.push([m === "default" ? "codex" : `codex:${m}`, `Codex ${m === "default" ? ((mother && mother.codex_model) || "default model") : m}${ok ? "" : " (not installed)"}`, !ok]);
  }
  for (const p of provs) {                       // the other agents run her terminal; her look-ins stay on Claude
    if (["claude", "codex"].includes(p.id)) continue;
    out.push([`agent:${p.id}`, `${p.name}${p.installed ? "" : " (not installed)"} · talks in her terminal`, !p.installed]);
  }
  out.push(["__other__", "Other model id…", false]);
  return out;
}
function fillQueenModels(sel, current) {
  cascadeSelect(sel);
  sel.textContent = "";
  let found = false;
  for (const [value, label, disabled] of queenModelOptions()) {
    const o = new Option(label, value, false, value === current);
    o.disabled = disabled;
    if (value === current) found = true;
    sel.appendChild(o);
  }
  if (current && !found) sel.appendChild(new Option(current, current, true, true));
  sel.dataset.last = sel.value;
}
// "Other model id…": ask for the id, add it to the list and keep it.
async function pickOtherModel(sel) {
  const id = await askText("Type the model id exactly as the agent's command line takes it (for example claude-sonnet-4-5, or codex:gpt-5).",
                           "model id", "Other model");
  if (id && /^[A-Za-z0-9][\w.\-:/]{0,79}$/.test(id)) {
    if (![...sel.options].some((o) => o.value === id)) sel.insertBefore(new Option(id, id), sel.options[sel.options.length - 1]);
    sel.value = id;
  } else {
    sel.value = sel.dataset.last || sel.options[0].value;
  }
  sel.dataset.last = sel.value;
  return sel.value;
}

function usageText(u, lim) {
  if (!u) return "";
  lim = lim || {};
  const of = (key, text) => (lim[key] != null ? `${text}/${key === "budget_tokens" ? fmtTok(lim[key])
    : key.endsWith("_mb") ? mb(lim[key]) : key === "cpu_pct" ? lim[key] + "%" : lim[key]}` : text);
  const parts = [];
  if (u.running || lim.workers) parts.push(of("workers", `${u.running || 0}`) + " on");
  if (u.cpu_pct || lim.cpu_pct) parts.push("CPU " + of("cpu_pct", `${Math.round(u.cpu_pct || 0)}%`));
  if (u.ram_mb || lim.ram_mb) parts.push("RAM " + of("ram_mb", mb(u.ram_mb || 0)));
  if (u.vram_mb || lim.vram_mb) parts.push("VRAM " + of("vram_mb", mb(u.vram_mb || 0)));
  if (u.tokens || lim.budget_tokens) parts.push(of("budget_tokens", `${fmtTok(u.tokens || 0)} tok`));
  if (u.held) parts.push(`⏸ ${u.held} held`);
  return parts.join(" · ");
}

// The clock on a queen's box ticks every second by itself: it does not wait for the next refresh of the map.
function paintClock(c) {
  const start = parseFloat(c.dataset.start), end = parseFloat(c.dataset.end);
  const secs = Math.max(0, Math.round((end || Date.now() / 1000) - start));
  c.textContent = `⏱ ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}${end ? " total" : ""}${c.dataset.tail || ""}`;
  c.classList.toggle("stopped", !!end);
}
setInterval(() => { for (const c of document.querySelectorAll(".n-clock")) if (!c.dataset.end) paintClock(c); }, 1000);

// The model a queen directs with, as her panel's picker names it: [agent, label].
function queenBrain(q) {
  if (q.queen_terminal_agent) {
    const p = ((mother && mother.providers) || []).find((x) => x.id === q.queen_terminal_agent);
    return [q.queen_terminal_agent, p ? p.name : q.queen_terminal_agent];
  }
  if (q.director_agent === "codex") return ["codex", agentModelLabel("codex", q.director_codex_model || "default")];
  return ["claude", agentModelLabel("claude", q.director_model || "sonnet")];
}

function queenNode(q, selected, jobs) {
  const n = el("div", `node root${selected ? " selected" : ""}`);
  const done = jobs.filter((j) => j.status === "done").length;
  const running = jobs.filter((j) => ["GPU job submitted", "agent working"].includes(j.status)).length;
  const head = el("div", "n-head");
  head.appendChild(el("span", "n-prio p1 queen-tag", qTag(q.id)));
  head.appendChild(el("span", "n-badge mother", "QUEEN BEE"));
  if (q.activity && q.activity !== "new") head.appendChild(termPill(q.activity));
  n.appendChild(head);
  const qname = el("div", "n-queen");
  qname.append(el("span", "n-queen-name", q.name || q.id));
  qname.title = q.name || q.id;
  n.appendChild(qname);
  const ready = selected && mother && mother.plan && !mother.dispatch;
  const count = jobs.length ? `${done}/${jobs.length} done · ${running} running`
    : q.planning ? "" : ready ? "plan ready — open to review" : "no workers yet · open her and give her a job";
  const plan = selected ? ((mother && mother.plan) || draft || {}) : {};
  const text = (selected ? plan.instruction || (mother && mother.instruction) : q.instruction) || "";
  n.appendChild(el("div", "n-title", (text || (selected ? mother && mother.description : q.description) || "").slice(0, 140) || "Give her a job, or add worker bees"));
  const sw = selected && mother && mother.sweep;
  const r = (sw && sw.result) || {};
  const extra = sw
    ? `sweep ${sw.param}` + (r.best != null ? ` · best ${r.best} (${fmtValue(r.score)}${r.std != null ? " ± " + fmtValue(r.std) : ""})` : "")
    : q.planning ? "planning…" : q.preparing ? "architect preparing…" : "";
  const sub = el("div", "n-sub");
  sub.appendChild(el("span", "", [q.commanding || count, extra, queens.length > 1 && q.primary ? "primary GPU" : ""]
    .filter(Boolean).join(" · ")));
  const [brainAgent, brain] = queenBrain(q);
  sub.appendChild(el("span", `agent ${brainAgent}`, brain));
  n.appendChild(sub);
  if (q.started) {
    const tail = q.manual || q.waits ? ` · ${q.waits} question${q.waits === 1 ? "" : "s"} waited for a person`
      + (q.waited_s ? ` (${Math.round(q.waited_s / 60)} min)` : "") : "";
    const c = el("div", `n-clock${q.finished ? " stopped" : ""}`);
    c.dataset.start = String(q.started);
    c.dataset.end = q.finished ? String(q.finished) : "";
    c.dataset.tail = tail;
    paintClock(c);
    n.appendChild(c);
  }
  const use = usageText(q.usage, q.limits);
  if (use) n.appendChild(el("div", `n-use${q.usage && q.usage.held ? " held" : ""}`, use));
  n.classList.add("clickable");
  n.title = `Open ${q.name || q.id}`;
  n.addEventListener("click", async () => { await selectQueen(q.id); openQueen(); });
  n.appendChild(nodeActions([["+ Worker bee", "add", async () => { await selectQueen(q.id); openAddWorker(); }]]));
  const x = el("button", "n-close", "✕");
  x.type = "button";
  x.title = `Remove ${qTag(q.id)} ${q.name || ""}`;
  x.addEventListener("click", (e) => { e.stopPropagation(); removeQueen(q.id); });
  n.appendChild(x);
  return n;
}

function jobNode(job, jobs, selected, queenId) {
  const [key, label] = statusOf(job);
  const n = el("div", `node worker s-${key}${job.detached ? " detached" : ""}${job.role === "mother" ? " sub-mother" : ""}`
                       + (job.terminal && job.terminal === convoTerm ? " open" : ""));
  const head = el("div", "n-head");
  head.appendChild(el("span", `n-prio worker-tag${job.priority === 1 ? " p1" : ""}`, jobTag(job, jobs, queenId)));
  head.appendChild(job.role === "mother" ? el("span", "n-badge mother small", "⑂ QUEEN")
                                         : el("span", "n-badge worker-bee", "WORKER BEE"));
  n.appendChild(head);
  const stat = el("div", "n-stat");
  stat.appendChild(el("span", `jstat ${key}`, job.detached ? "let go" : label));
  if (job.held && job.status === "queued") {
    const h = el("span", "tpill t-held", "⏸ held by limit");
    h.title = job.held;
    stat.appendChild(h);
  } else if (job.term_state) stat.appendChild(termPill(job.term_state, job.term_reason));
  n.appendChild(stat);
  if (job.refused && job.refused.length) {
    n.classList.add("refused");
    const r = el("div", "n-refused", job.refused.length > 1 ? `✕ queen said no ×${job.refused.length}` : "✕ queen said no");
    r.title = job.refused.map((x) => `${x.question}\n${x.why}`).join("\n\n") + "\n\nOpen the worker to allow it.";
    n.appendChild(r);
  }
  n.appendChild(el("div", "n-title", job.title || ""));
  if (job.progress && !["done", "failed", "stopped"].includes(job.status)) {
    const pr = el("div", "n-progress", job.progress);
    pr.title = job.progress;
    n.appendChild(pr);
  }
  const sub = el("div", "n-sub");
  if (job.branched) {
    const kids = jobs.filter((j) => j.parent === job.priority);
    const done = kids.filter((j) => ["done", "stopped early"].includes(j.status)).length;
    sub.appendChild(el("span", "agent mother", kids.length ? `hive · ${done}/${kids.length} done` : "splitting into worker bees…"));
  } else {
    sub.appendChild(el("span", `agent ${job.direct ? "mother" : job.agent}`, job.direct ? "no worker · 0 tokens" : modelLabel(job)));
  }
  const res = selected && (mother && mother.results && mother.results.rows || []).find((r) => r.priority === job.priority);
  if (res && res.value != null) sub.appendChild(el("span", "n-val", fmtValue(res.value)));
  n.appendChild(sub);
  n.classList.add("clickable");
  n.title = "Open this worker's chat";
  const open = async (branch) => {
    if (!selected) await selectQueen(queenId);
    openWorker(job, branch);
  };
  n.addEventListener("click", () => open(false));
  const acts = [];
  if (!job.branched && !job.lane && job.role !== "mother") acts.push(["⑂ Queen", "fork", () => open(true)]);
  if (["done", "failed", "stopped", "stopped early"].includes(job.status) && !job.lane) {
    acts.push(["Remove", "kill", async () => {
      if (!selected) await selectQueen(queenId);
      if (!await ask(`Remove W${job.priority} "${job.title}" from the hive?`)) return;
      const d = await post("/api/worker/remove", { queen: queenId, priority: job.priority });
      if (!d.ok) tell(d.error || "Could not remove it.");
      await poll();
    }]);
  }
  if (!["done", "failed", "stopped", "stopped early"].includes(job.status)) {
    acts.push(["✕ Stop", "kill", async () => {
      if (!selected) await selectQueen(queenId);
      if (!await ask(`Stop W${job.priority} "${job.title}" and every worker under it?`)) return;
      const d = await post("/api/kill", { queen: queenId, priority: job.priority });
      if (!d.ok) tell(d.error || "Could not stop it.");
      await poll();
    }]);
  }
  if (acts.length) n.appendChild(nodeActions(acts));
  return n;
}

// What the terminal is doing right now, as QueenBee reads its screen.
function termPill(state, reason) {
  if (state === "delivered" || state === "finished" || state === "needs you") {
    const d = el("span", `tpill t-${state === "needs you" ? "attention" : "delivered"}`,
                 state === "delivered" ? "✓ delivered" : state === "finished" ? "✓ finished" : "! needs you");
    d.title = state === "delivered" ? "Every worker is done and the delivery check passes"
      : state === "finished" ? "Every worker is done" : "A worker or the delivery check did not finish";
    return d;
  }
  if (state === "directing" || state === "looking in") {
    const d = el("span", "tpill t-working", state === "directing" ? "● directing" : "● looking in");
    d.title = "The queen's model is watching her workers";
    return d;
  }
  const p = el("span", `tpill t-${state}`, state === "working" ? "● working" : state === "waiting" ? "◐ waiting"
    : state === "closed" ? "○ closed" : `○ ${state}`);
  if (reason) p.title = reason;
  return p;
}

function nodeActions(list) {
  const bar = el("div", "n-actions");
  for (const [label, cls, fn] of list) {
    const b = el("button", `n-act ${cls}`, label);
    b.type = "button";
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    bar.appendChild(b);
  }
  return bar;
}

// Pan and zoom: drag the background to move, the wheel zooms toward the
// pointer, a double-click fits every queen in view.
const view = { x: 0, y: 0, k: 1, touched: false, w: 0, h: 0 };

function applyView() {
  document.getElementById("graph").style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
  const z = document.getElementById("zoom-level");
  if (z) z.textContent = `${Math.round(view.k * 100)}%`;
}

function fitView() {
  const wrap = document.querySelector(".graph-wrap");
  if (!wrap || !view.w) return;
  // Not laid out yet (a hidden tab, a window still opening): fitting now would squeeze the hive to
  // the smallest zoom and leave a dark, empty map. The resize observer fits it once it has a size.
  if (wrap.clientWidth < 200 || wrap.clientHeight < 120) return;
  const k = Math.max(0.15, Math.min(1, (wrap.clientWidth - 32) / view.w, (wrap.clientHeight - 32) / view.h));
  Object.assign(view, { k, x: (wrap.clientWidth - view.w * k) / 2, y: 16 });
  applyView();
}

function zoomBy(factor, mx, my) {
  const wrap = document.querySelector(".graph-wrap");
  if (mx == null) { mx = wrap.clientWidth / 2; my = wrap.clientHeight / 2; }
  const k = Math.max(0.15, Math.min(2.5, view.k * factor));
  view.x = mx - (mx - view.x) * (k / view.k);
  view.y = my - (my - view.y) * (k / view.k);
  view.k = k;
  view.touched = true;
  applyView();
}

(function panAndZoom() {
  const wrap = document.querySelector(".graph-wrap");
  wrap.addEventListener("wheel", (e) => {
    // The wheel, two fingers up and down on a trackpad, or a pinch: zoom toward the pointer (one notch: about a third
    // in or out). Moving the map is a drag on its background; users asked for it this way round.
    e.preventDefault();
    if (!e.deltaY) return;
    const r = wrap.getBoundingClientRect();
    zoomBy(Math.exp(-e.deltaY * 0.003), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  let drag = null;
  wrap.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest(".node, button")) return;
    drag = { x: e.clientX - view.x, y: e.clientY - view.y };
    wrap.setPointerCapture(e.pointerId);
    wrap.classList.add("panning");
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!drag) return;
    view.x = e.clientX - drag.x;
    view.y = e.clientY - drag.y;
    view.touched = true;
    applyView();
  });
  const end = () => { drag = null; wrap.classList.remove("panning"); };
  wrap.addEventListener("pointerup", end);
  wrap.addEventListener("pointercancel", end);
  wrap.addEventListener("dblclick", (e) => { if (!e.target.closest(".node")) { view.touched = false; fitView(); } });
  document.getElementById("zoom-in").addEventListener("click", () => zoomBy(1.35));
  document.getElementById("zoom-out").addEventListener("click", () => zoomBy(1 / 1.35));
  document.getElementById("zoom-fit").addEventListener("click", () => { view.touched = false; fitView(); });
  // A window that is resized (or finishes laying out after the first draw) fits the hive again,
  // unless the user has panned or zoomed by hand.
  new ResizeObserver(() => { if (!view.touched) fitView(); }).observe(wrap);
})();

function renderQueenHead() {
  const name = document.getElementById("queen-name");
  if (document.activeElement !== name) name.value = mother.queen_name || "";
  const rm = document.getElementById("remove-queen");
  const first = !!(queens[0] && queens[0].id === mother.queen);
  rm.hidden = false;
  const only = queens.length < 2;                           // the only queen cannot leave: she is cleaned, not removed
  rm.textContent = only ? "Clean queen" : "Remove queen";
  document.getElementById("clean-title").textContent = rm.textContent;
  document.getElementById("clean-sub").textContent = only
    ? "Closes her chat and forgets her plan and workers, so you start fresh. Plan files stay on disk."
    : "Takes this queen off the map with her chat, plan and workers. Plan files stay on disk.";
  rm.disabled = !!(mother.planning || mother.preparing);
}

(function queenHead() {
  const name = document.getElementById("queen-name");
  name.addEventListener("change", async () => {
    const d = await post("/api/queen", { action: "rename", name: name.value });
    if (!d.ok) tell(d.error || "Could not rename the queen.");
    else flash(`Queen renamed to ${name.value}`);
    await poll();
  });
  document.getElementById("remove-queen").addEventListener("click", () => removeQueen(mother.queen));
})();

// Remove one queen, named explicitly (the panel's, a map node's or a list item's X): what is shown is what goes.
async function removeQueen(id) {
  const q = queens.find((x) => x.id === id) || { id, name: id, jobs: [] };
  const live = (q.jobs || []).filter((j) => !["done", "failed", "stopped", "stopped early"].includes(j.status)).length;
  const last = queens.length < 2;
  if (!await ask(`Remove ${qTag(id)} ${q.name}?${live ? ` Its ${live} running worker(s) will be stopped first.` : ""}`
               + (last ? " A blank queen takes its place." : "") + " Its plan file stays on disk.")) return;
  const d = await working(`Removing ${q.name}…`, () => post("/api/queen", { action: "remove", queen: id, force: true }),
                          `${q.name} removed`);
  if (!d.ok) return;
  view.touched = false;
  const other = queens.find((x) => x.id !== id);
  if (!other) {                                    // the last queen came back blank: reload her, and forget the typed folder too
    mother = null;
    document.getElementById("folder").value = "";
    try { localStorage.removeItem("mother.folder"); } catch { /* private mode */ }
  }
  await selectQueen(other ? other.id : id);
}

async function selectQueen(id) {
  if (mother && id === mother.queen) return;
  currentQueen = id;
  try { localStorage.setItem("mother.queen", id); } catch { /* private mode */ }
  // What was on screen belonged to the other queen.
  draft = null;
  mother = null;
  for (const c of agentCache.values()) c.el.remove();
  agentCache.clear();
  forkOpen.clear(); editOpen.clear();
  document.getElementById("instruction").value = "";
  document.getElementById("plan-card").hidden = true;
  if (focus) closeFocus(true);
  convoTerm = null;
  document.getElementById("convo-card").hidden = true;
  await poll();
}

function renderHive() {
  const card = document.getElementById("graph-card");
  const model = hiveModel();
  card.hidden = !model.roots.length;
  if (!model.roots.length) return;
  const graph = document.getElementById("graph");
  const nodes = document.getElementById("nodes");
  const svg = document.getElementById("edges");
  const wrap = document.querySelector(".graph-wrap");
  const avail = Math.max(900, wrap.clientWidth - 40);
  const memo = new Map();
  const pos = new Map();
  let x = 20, tallest = 0;
  for (const root of model.roots) {
    const m = measure(root, model, avail, memo);
    place(root, model, memo, x, 20, pos);
    x += m.w + HIVE_GAP;
    tallest = Math.max(tallest, m.h);
  }
  const width = x - HIVE_GAP + 20, height = tallest + 40;
  lastPos = pos;
  graph.style.width = `${width}px`;
  graph.style.height = `${height}px`;
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const grew = view.w !== width || view.h !== height;
  Object.assign(view, { w: width, h: height });
  if (!view.touched && grew) fitView(); else applyView();

  // Nodes: drawn again only when what they show changed.
  const live = new Set();
  for (const [key, it] of model.info) {
    live.add(key);
    const job = it.job;
    if (it.kind === "group") {
      const gsig = JSON.stringify(["g", it.name, it.members.map((j) => j.status)]);
      let gc = nodeCache.get(key);
      if (!gc || gc.sig !== gsig) {
        const fresh = groupNode(it);
        if (gc) gc.el.replaceWith(fresh); else { fresh.classList.add("fresh"); nodes.appendChild(fresh); }
        gc = { sig: gsig, el: fresh };
        nodeCache.set(key, gc);
      }
      const gp = pos.get(key);
      Object.assign(gc.el.style, { left: `${gp.x}px`, top: `${gp.y}px`, width: `${gp.w}px`, height: `${gp.h}px` });
      continue;
    }
    const sig = it.kind === "queen"
      ? JSON.stringify([it.selected, it.queen.name, it.queen.primary, queens.length > 1, it.queen.planning, it.queen.preparing,
                        it.queen.commanding, usageText(it.queen.usage, it.queen.limits), it.queen.activity, it.queen.finished, it.queen.started,
                        it.queen.waits, it.queen.waited_s, it.queen.description, queenBrain(it.queen),
                        it.selected && !!mother.plan && !mother.dispatch,
                        it.selected ? [(mother.plan || {}).instruction, mother.instruction, mother.sweep && mother.sweep.result]
                                    : it.queen.instruction, it.jobs.map((j) => j.status)])
      : JSON.stringify([it.selected, job.status, job.attention, job.detached, job.role, job.branched,
                        it.jobs.filter((k) => k.parent === job.priority).map((k) => k.status),
                        job.tier, job.agent, job.direct, job.terminal, job.terminal === convoTerm, job.term_state, job.held,
                        job.progress, (job.refused || []).map((r) => r.call),
                        it.selected && (mother.results && mother.results.rows || []).find((r) => r.priority === job.priority)]);
    let c = nodeCache.get(key);
    if (!c || c.sig !== sig) {
      const fresh = it.kind === "queen" ? queenNode(it.queen, it.selected, it.jobs) : jobNode(job, it.jobs, it.selected, it.queen.id);
      if (c) c.el.replaceWith(fresh); else { fresh.classList.add("fresh"); nodes.appendChild(fresh); }
      c = { sig, el: fresh };
      nodeCache.set(key, c);
    }
    const p = pos.get(key);
    Object.assign(c.el.style, { left: `${p.x}px`, top: `${p.y}px`, width: `${p.w}px`, height: `${p.h}px` });
  }
  for (const [key, c] of nodeCache) if (!live.has(key)) { c.el.remove(); nodeCache.delete(key); }

  // Edges: a quiet line from parent to child. A link at work carries a
  // soft streak each way -- the order going down in the state's colour, the
  // report coming back up in grey -- that glides, eases out and rests before
  // the next trip. Redrawn only when the layout or a state changes, so the
  // motion never jumps.
  const links = [...model.parentOf].map(([key, parent]) => {
    const it = model.info.get(key);
    return [key, parent, it.kind === "group" ? { status: "", _cls: groupClass(it.members) } : it.job];
  });
  const edgeSig = JSON.stringify([width, height, links.map(([key, parent, job]) => [key, parent, edgeClass(job),
    Math.round(pos.get(key).x), Math.round(pos.get(key).y)])]);
  if (svg.dataset.sig !== edgeSig) {
    svg.dataset.sig = edgeSig;
    svg.textContent = "";
    links.forEach(([key, parent, job], index) => {
      const a = pos.get(parent), b = pos.get(key);
      if (!a || !b) return;
      const x1 = a.x + a.w / 2, y1 = a.y + a.h, x2 = b.x + b.w / 2, y2 = b.y;
      let d;
      if (b.bus) {
        const { gx, y0 } = b.bus, ry = y2 - 11;
        d = `M ${x1} ${y1} V ${y0} H ${gx} V ${ry} H ${x2} V ${y2}`;
      } else {
        const dy = Math.max(30, (y2 - y1) * 0.55);
        d = `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
      }
      const cls = edgeClass(job);
      const base = document.createElementNS(SVGNS, "path");
      base.setAttribute("d", d);
      base.setAttribute("class", `edge ${cls}`);
      svg.appendChild(base);
      // How lively each link is: working links carry clear traffic both
      // ways; finished ones a slow, faint heartbeat; waiting ones only the
      // order going down; links let go of carry nothing.
      const tempo = { gpu: { rest: 1.6, alpha: 1, up: true }, agent: { rest: 1.8, alpha: 1, up: true },
                      done: { rest: 6.5, alpha: 0.5, up: true }, failed: { rest: 7, alpha: 0.55, up: true },
                      queued: { rest: 5, alpha: 0.45, up: false } }[cls];
      if (!tempo) return;
      const len = base.getTotalLength() || Math.hypot(x2 - x1, y2 - y1);
      let seed = (index + 1) * 9301 + 49297;
      const rand = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
      const trip = Math.min(4.2, Math.max(2.4, len / 70)) * (tempo.alpha < 1 ? 1.35 : 1);   // seconds under way
      const period = trip + tempo.rest + rand() * 1.4;              // then a rest
      const group = document.createElementNS(SVGNS, "g");
      group.setAttribute("opacity", tempo.alpha);
      svg.appendChild(group);
      const streak = (up, begin) => {
        // A faint tail and a brighter head moving together.
        for (const [part, size] of [["tail", 44], ["head", 9]]) {
          const s = document.createElementNS(SVGNS, "path");
          s.setAttribute("d", d);
          s.setAttribute("class", `streak ${part} ${up ? "up" : cls}`);
          s.setAttribute("stroke-dasharray", `${size} ${len + size}`);
          const from = up ? -len : size, to = up ? size : -len;
          s.setAttribute("stroke-dashoffset", from);
          const an = document.createElementNS(SVGNS, "animate");
          an.setAttribute("attributeName", "stroke-dashoffset");
          an.setAttribute("values", `${from};${to};${to}`);
          an.setAttribute("keyTimes", `0;${(trip / period).toFixed(3)};1`);
          an.setAttribute("calcMode", "spline");
          an.setAttribute("keySplines", "0.4 0 0.2 1; 0 0 1 1");
          an.setAttribute("dur", `${period.toFixed(2)}s`);
          an.setAttribute("begin", `-${begin.toFixed(2)}s`);
          an.setAttribute("repeatCount", "indefinite");
          s.appendChild(an);
          group.appendChild(s);
        }
      };
      const start = rand() * period;
      streak(false, start);
      if (tempo.up) streak(true, (start + period / 2) % period);
    });
  }
  const all = links.map((l) => l[2]).filter((j) => !j._cls);
  const counts = all.reduce((c, j) => { const k = edgeClass(j); c[k] = (c[k] || 0) + 1; return c; }, {});
  document.getElementById("graph-meta").textContent =
    `${model.roots.length} queen${model.roots.length === 1 ? "" : "s"} · ${all.length} worker${all.length === 1 ? "" : "s"}`
    + (counts.gpu ? ` · ${counts.gpu} on the GPU` : "") + (counts.agent ? ` · ${counts.agent} working` : "")
    + (counts.done ? ` · ${counts.done} done` : "");
}

/* ---- the agents panel: steer each agent ------------------------------- */

const agentCache = new Map();

// Anything that takes a moment says so: a spinner while it runs, then what happened. `fn` returns the API answer ({ok, error}).
async function working(label, fn, done) {
  const host = document.getElementById("toasts");
  const t = el("div", "toast working");
  t.appendChild(el("span", "spin", ""));
  t.appendChild(el("div", "toast-text", label));
  host.appendChild(t);
  const finish = (cls, text, ms) => {
    t.className = `toast ${cls}`;
    t.textContent = "";
    t.appendChild(el("div", "toast-text", text));
    setTimeout(() => { t.classList.add("gone"); setTimeout(() => t.remove(), 400); }, ms);
  };
  try {
    const r = await fn();
    if (r && r.ok === false) finish("bad", `Failed: ${r.error || "something went wrong"}`, 6000);
    else finish("good", done || "Done", 2600);
    return r;
  } catch (exc) {
    finish("bad", `Failed: ${exc && exc.message ? exc.message : "something went wrong"}`, 6000);
    return { ok: false, error: String(exc) };
  }
}

// Settings apply the moment they change; this says so for a moment, so that no OK button is missed.
function flash(text) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const t = el("div", "toast good");
  t.appendChild(el("div", "toast-text", text));
  host.appendChild(t);
  setTimeout(() => { t.classList.add("gone"); setTimeout(() => t.remove(), 400); }, 2200);
}

async function steer(job, changes) {
  const d = await post("/api/job", { priority: job.priority, changes });
  if (!d.ok) tell(d.error || "Could not change the worker.");
  else flash(`W${job.priority} updated: ${Object.entries(changes).map(([k, v]) => `${k} ${v}`).join(", ")}`);
  await poll();
}

function modelSelect(job) {
  const provs = (mother && mother.providers) || [];
  const sel = document.createElement("select");
  sel.className = "model";
  if (job.direct && !job.terminal) {
    sel.appendChild(new Option("Queen runs it · 0 tokens", "", true, true));
    sel.disabled = true;
    return sel;
  }
  const current = job.agent !== "claude" ? `${job.agent}:${job.tier || "default"}`
    : `claude:${job.tier || (mother && mother.models && mother.models.claude && mother.models.claude[job.effort]) || "haiku"}`;
  for (const p of provs) {
    const group = document.createElement("optgroup");
    group.dataset.agent = p.id;
    group.label = p.installed ? p.name : `${p.name} (not installed)`;
    if (!p.models.length) continue;                    // an agent with nothing to choose (not driven yet)
    for (const m of p.models) {
      const o = new Option(agentModelLabel(p.id, m), `${p.id}:${m}`, false, `${p.id}:${m}` === current);
      o.disabled = !p.installed;
      group.appendChild(o);
    }
    sel.appendChild(group);
  }
  sel.addEventListener("change", () => {
    const [agent, tier] = sel.value.split(":");
    steer(job, { agent, tier });
  });
  const holder = el("span", "casc-holder");
  holder.appendChild(sel);
  cascadeSelect(sel);
  return holder;
}

function agentCard(job, depth) {
  const [key, label] = statusOf(job);
  const c = el("div", `agent-card s-${key}${job.detached ? " detached" : ""}${job.role === "mother" ? " sub-mother" : ""}`);
  c.style.marginLeft = `${Math.min(depth, 4) * 14}px`;
  const head = el("div", "a-head");
  head.appendChild(el("span", `n-prio${job.priority === 1 ? " p1" : ""}`, `W${job.priority}`));
  head.appendChild(el("span", "a-title", job.title || ""));
  head.appendChild(el("span", `jstat ${key}`, job.detached ? "let go" : label));
  c.appendChild(head);
  const row = el("div", "a-row");
  row.appendChild(modelSelect(job));
  const tog = el("label", "switch");
  tog.title = job.detached ? "Let go of: the Queen does not manage it" : "Managed by the Queen";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = !job.detached;
  box.addEventListener("change", () => steer(job, { detached: !box.checked }));
  tog.appendChild(box);
  tog.appendChild(el("span", "knob"));
  row.appendChild(tog);
  const fork = el("button", `ghost small fork${job.role === "mother" ? " on" : ""}`, "⑂");
  fork.type = "button";
  const kids = ((mother && mother.dispatch) || []).filter((j) => j.parent === job.priority && !j.lane).length;
  fork.title = job.branched ? `Queen of its own hive: ${kids} worker${kids === 1 ? "" : "s"}`
    : job.role === "mother" ? "Marked to become a queen — click to keep it a worker"
    : "Make this worker the queen of its own hive";
  fork.disabled = !!job.branched;
  fork.addEventListener("click", () => {
    if (job.role === "mother") { steer(job, { role: null }); return; }
    if (forkOpen.has(job.priority)) forkOpen.delete(job.priority); else forkOpen.add(job.priority);
    redrawCard(job);
  });
  row.appendChild(fork);
  if (!job.branched && !job.lane) {
    const edit = el("button", `ghost small edit${editOpen.has(job.priority) ? " on" : ""}`, "✎");
    edit.type = "button";
    edit.title = "Change this worker's task, tell it something, or have it redo its work";
    edit.addEventListener("click", () => {
      if (editOpen.has(job.priority)) editOpen.delete(job.priority); else editOpen.add(job.priority);
      redrawCard(job);
    });
    row.appendChild(edit);
  }
  c.appendChild(row);
  if (job.role === "mother" && job.instruction) {
    c.appendChild(el("div", "a-note branch", (job.branched ? "hive: " : "will become a queen: ") + job.instruction.slice(0, 160)));
  }
  if (job.user_notes && job.user_notes.length && !editOpen.has(job.priority)) {
    c.appendChild(el("div", "a-note you", "you: " + job.user_notes[job.user_notes.length - 1].slice(0, 140)));
  }
  if (forkOpen.has(job.priority) && job.role !== "mother") c.appendChild(forkForm(job));
  if (editOpen.has(job.priority) && !job.branched && !job.lane) c.appendChild(taskForm(job));
  if (job.attention) c.appendChild(el("div", "a-note warn", "needs you — see its node"));
  else if (job.error) c.appendChild(el("div", "a-note error", job.error.slice(0, 140)));
  return c;
}

function redrawCard(job) {
  const c = agentCache.get(job.priority);
  if (c) c.sig = "";
  renderAgents(mother.dispatch);
}

const forkOpen = new Set();            // agents whose branch form is open
const forkDraft = new Map();           // what was typed in it so far

const editOpen = new Set();            // workers whose task form is open
const editDraft = new Map();

const ENDED = ["done", "failed", "stopped", "stopped early"];

function taskForm(job) {
  const f = el("div", "fork-form task-form");
  const working = ["agent working", "GPU job submitted"].includes(job.status) && job.terminal && !job.direct;
  const ended = ENDED.includes(job.status);
  const ta = document.createElement("textarea");
  ta.rows = 3;
  ta.placeholder = working ? "Tell the worker something — it reads it in its own conversation."
    : ended ? "What should change? e.g. \"the header should stay fixed\" — it redoes the work."
    : "The worker's task.";
  ta.value = editDraft.has(job.priority) ? editDraft.get(job.priority) : (!working && !ended ? job.goal || "" : "");
  ta.addEventListener("input", () => editDraft.set(job.priority, ta.value));
  f.appendChild(ta);
  f.appendChild(el("div", "fork-hint", working
    ? "Sent into its terminal now. The Queen keeps answering its questions within scope."
    : ended ? "If its session is still open it redoes it there, remembering what it did; otherwise a new worker starts on the same task with your change. The Queen answers its questions."
    : "It starts with this task."));
  const go = el("button", "primary small", working ? "Send" : ended ? "Redo" : "Save");
  go.type = "button";
  go.addEventListener("click", async () => {
    if (!ta.value.trim()) return;
    editOpen.delete(job.priority);
    editDraft.delete(job.priority);
    await steer(job, { goal: ta.value });
  });
  const cancel = el("button", "ghost small", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => { editOpen.delete(job.priority); redrawCard(job); });
  const btns = el("div", "fork-btns");
  btns.appendChild(cancel);
  btns.appendChild(go);
  f.appendChild(btns);
  return f;
}

function forkForm(job) {
  const f = el("div", "fork-form");
  const ta = document.createElement("textarea");
  ta.rows = 3;
  ta.placeholder = "What should its hive do? Its own workers are planned from this.";
  ta.value = forkDraft.has(job.priority) ? forkDraft.get(job.priority) : (job.goal || "");
  ta.addEventListener("input", () => forkDraft.set(job.priority, ta.value));
  f.appendChild(ta);
  const done = ["done", "failed", "stopped", "stopped early"].includes(job.status);
  f.appendChild(el("div", "fork-hint", job.status === "queued" ? "It plans its own workers instead of starting one."
    : done ? "It plans its own workers, which continue from what it left."
    : "It finishes its current work first, then becomes a queen."));
  const go = el("button", "primary small", "Make queen");
  go.type = "button";
  go.addEventListener("click", async () => {
    forkOpen.delete(job.priority);
    forkDraft.delete(job.priority);
    await steer(job, { role: "mother", instruction: ta.value, queen_model: queenModel.value, worker_model: workerModel.value });
  });
  const cancel = el("button", "ghost small", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => { forkOpen.delete(job.priority); redrawCard(job); });
  const btns = el("div", "fork-btns");
  btns.appendChild(cancel);
  btns.appendChild(go);
  f.appendChild(btns);
  return f;
}

function renderAgents(jobs) {
  const host = document.getElementById("agents");
  // Never redraw under the user's hand.
  if (host.contains(document.activeElement) && ["SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  const kids = kidsOf(jobs);
  const order = [];
  const walk = (key, depth) => { for (const j of kids.get(key) || []) { order.push([j, depth]); walk(j.priority, depth + 1); } };
  walk("M", 0);
  if (host.firstElementChild && host.firstElementChild.classList.contains("empty")) host.textContent = "";
  const live = new Set();
  let i = 0;
  for (const [job, depth] of order) {
    live.add(job.priority);
    const sig = JSON.stringify([job.status, job.attention, job.error, job.detached, job.role, job.tier, job.agent,
                                job.direct, job.terminal, depth, (mother && mother.providers || []).length,
                                job.branched, job.instruction, forkOpen.has(job.priority), editOpen.has(job.priority),
                                (job.user_notes || []).length,
                                jobs.filter((k) => k.parent === job.priority).length]);
    let c = agentCache.get(job.priority);
    if (!c || c.sig !== sig) {
      const fresh = agentCard(job, depth);
      if (c) c.el.replaceWith(fresh);
      c = { sig, el: fresh };
      agentCache.set(job.priority, c);
    }
    if (host.children[i] !== c.el) host.insertBefore(c.el, host.children[i] || null);
    i++;
  }
  for (const [k, c] of agentCache) if (!live.has(k)) { c.el.remove(); agentCache.delete(k); }
  document.getElementById("agents-count").textContent = `${order.length}`;
}

window.addEventListener("resize", () => renderHive());

/* ---- report: the markdown Mother wrote, drawn without a library ------- */

function inline(text, into) {
  // **bold** only; everything else is text, never HTML.
  const parts = text.split(/(\*\*[^*]+\*\*)/);
  for (const part of parts) {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) into.appendChild(el("strong", null, part.slice(2, -2)));
    else if (part) into.appendChild(document.createTextNode(part));
  }
  return into;
}

function renderMarkdown(md, host) {
  host.textContent = "";
  const lines = md.split("\n");
  let list = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      i--;
      const table = el("table", "mdt");
      rows.filter((r) => !/^\|[-|\s]+\|$/.test(r)).forEach((r, n) => {
        const tr = el("tr");
        for (const cell of r.slice(1, -1).split("|")) tr.appendChild(inline(cell.trim(), el(n === 0 ? "th" : "td")));
        table.appendChild(tr);
      });
      host.appendChild(table);
      list = null;
      continue;
    }
    if (/^\s*- /.test(line)) {
      if (!list) { list = el("ul"); host.appendChild(list); }
      const li = inline(line.replace(/^\s*- /, ""), el("li"));
      if (/^\s{2,}- /.test(line)) li.className = "sub";
      list.appendChild(li);
      continue;
    }
    list = null;
    if (line.startsWith("# ")) continue;                 // the card has its own title
    if (line.startsWith("## ")) host.appendChild(el("h3", null, line.slice(3)));
    else if (line.startsWith("> ")) host.appendChild(inline(line.slice(2), el("blockquote")));
    else if (line.trim()) host.appendChild(inline(line, el("p")));
  }
}

let reportSig = "";

// The findings kept in this queen's folders, as the next brief would see them: current ones, ones that may be out of
// date (a file they name changed), ones a failed check named, and the retired. You settle them: Confirm makes one
// current again as the files are now, Retire ends it.
let findingsAt = 0, findingsBusy = false, findingsSeen = -1;

async function refreshFindings(force) {
  const card = document.getElementById("findings-card");
  if (findingsBusy || card.closest(".view").hidden) return;
  const seen = ((mother && mother.findings) || []).length;
  if (!force && seen === findingsSeen && Date.now() - findingsAt < 15000) return;
  findingsBusy = true;
  try {
    const asked = currentQueen;
    const f = await (await fetch(`/api/findings?queen=${encodeURIComponent(asked)}`, { cache: "no-store" })).json();
    if (asked === currentQueen) renderFindings(f.folders || []);
    findingsAt = Date.now();
    findingsSeen = seen;
  } catch { /* the next poll tries again */ } finally { findingsBusy = false; }
}

function findingState(r) {
  const st = r.status || "active";
  if (st === "retired") return ["gone", "retired"];
  if (st === "superseded") return ["gone", r.superseded_by ? `replaced by ${r.superseded_by}` : "replaced"];
  if (r.conflict) return ["conflict", "a check failed on its file"];
  if (st === "stale") return ["stale", "may be out of date"];
  return ["current", r.validated_by ? `checked: ${r.validated_by}` : "current"];
}

function renderFindings(folders) {
  const card = document.getElementById("findings-card");
  const host = document.getElementById("findings");
  card.hidden = !folders.some((f) => f.records.length);
  host.textContent = "";
  const order = { conflict: 0, stale: 1, current: 2, gone: 3 };
  for (const f of folders) {
    if (folders.length > 1) host.appendChild(el("li", "finding-folder", f.folder));
    const recs = f.records.map((r) => [r, ...findingState(r)])
      .sort((a, b) => order[a[1]] - order[b[1]] || (b[0].ts || 0) - (a[0].ts || 0));
    const gone = recs.filter(([, k]) => k === "gone");
    for (const [r, key, label] of recs.filter(([, k]) => k !== "gone")) host.appendChild(findingRow(f.folder, r, key, label));
    if (gone.length) {
      const more = el("details", "finding-gone");
      more.appendChild(el("summary", "", `${gone.length} retired or replaced`));
      for (const [r, key, label] of gone) more.appendChild(findingRow(f.folder, r, key, label));
      host.appendChild(more);
    }
  }
}

function findingRow(folder, r, key, label) {
  const li = el("li", `finding f-${key}`);
  li.appendChild(el("span", "finding-from", r.id || "?"));
  const body = el("div", "finding-body");
  body.appendChild(el("div", "finding-text", r.claim));
  const why = key === "conflict" ? String(r.conflict).slice(-200) : key === "stale" ? r.stale_reason || "" : "";
  body.appendChild(el("div", "finding-meta",
    `${label}${why ? ` · ${why}` : ""} · from ${r.source_job || "?"}${r.ts ? `, ${new Date(r.ts * 1000).toLocaleString()}` : ""}`));
  li.appendChild(body);
  if (key !== "gone") {
    const acts = el("div", "finding-acts");
    const settle = async (action) => {
      if (action === "retire" && !await ask(`Retire ${r.id}? The next team's brief will not carry it.\n\n${r.claim}`,
                                            { title: "Retire it?", ok: "Retire", danger: true })) return;
      const d = await post("/api/finding", { queen: currentQueen, folder, id: r.id, action });
      if (!d.ok) tell(d.error || "Could not change it.");
      await refreshFindings(true);
    };
    if (key !== "current" || !r.validated_by) {
      const ok = el("button", "ghost small", "Confirm");
      ok.type = "button";
      ok.title = "It still holds: make it current, as the files are now";
      ok.addEventListener("click", () => settle("validate"));
      acts.appendChild(ok);
    }
    const rm = el("button", "ghost small danger", "Retire");
    rm.type = "button";
    rm.title = "It no longer holds: the next team's brief will not carry it";
    rm.addEventListener("click", () => settle("retire"));
    acts.appendChild(rm);
    li.appendChild(acts);
  }
  return li;
}

function renderReport(r) {
  const card = document.getElementById("report-card");
  card.hidden = !r;
  if (!r || r.text === reportSig) return;
  reportSig = r.text;
  document.getElementById("report-path").textContent = r.path;
  renderMarkdown(r.text, document.getElementById("report"));
}

(function restoreFolder() {
  try { document.getElementById("folder").value = localStorage.getItem("mother.folder") || ""; } catch { /* ignore */ }
})();

/* ---- architect ------------------------------------------------------- */

const answers = {};       // question id -> chosen option text

function renderPrep() {
  const host = document.getElementById("prep");
  host.textContent = "";
  const p = mother && mother.prepared;
  const reviewing = draft && draft.fresh && !(mother && mother.running);
  if (!p || !reviewing || p.plan_id !== draft.plan_id) return;
  if (!p.done) {
    const folders = Object.entries(p.folders || {}).map(([f, s]) => `${f.split(/[\\/]/).pop()}: ${s}`).join(" · ");
    host.appendChild(el("div", "prep-status", `Architect (${mother.roles.architect_model}) is inspecting the project${folders ? " — " + folders : ""}… this can take a few minutes.`));
    return;
  }
  if (p.error) { host.appendChild(el("div", "error", `Architect failed: ${p.error}`)); return; }
  const cost = (p.usage || []).reduce((s, u) => s + (u.cost_usd || 0), 0);
  const tokens = (p.usage || []).reduce((s, u) => s + u.input_tokens + u.output_tokens, 0);
  host.appendChild(el("div", "prep-status done",
    `Prepared by ${mother.roles.architect_model}: ${tokens} tokens. Workers will run on ${mother.roles.worker_model}.`));
  for (const n of p.notes || []) host.appendChild(el("div", "notes", n));
  for (const q of p.questions || []) {
    const box = el("div", "question");
    box.appendChild(el("div", "qtext", `W${q.priority || "?"} · ${q.text}`));
    if (!(q.id in answers) && q.options.length) answers[q.id] = q.options[0];
    for (const opt of q.options) {
      const label = el("label", "qopt");
      const radio = document.createElement("input");
      radio.type = "radio"; radio.name = q.id; radio.checked = answers[q.id] === opt;
      radio.addEventListener("change", () => { answers[q.id] = opt; });
      label.appendChild(radio);
      label.appendChild(document.createTextNode(" " + opt));
      box.appendChild(label);
    }
    host.appendChild(box);
  }
  for (const job of draft.jobs) {
    if (!job.work_order) continue;
    const d = document.createElement("details");
    d.className = "order";
    d.appendChild(el("summary", null, `Work order W${job.priority} · ${job.title}`));
    d.appendChild(el("pre", null, job.work_order + (job.command ? `\n\nSubmit:\n${job.command}` : "")));
    host.appendChild(d);
  }
}

function adoptPrepared() {
  // Copy the architect's work orders into the plan under review, once.
  const p = mother && mother.prepared;
  if (!p || !p.done || p.error || !draft || !draft.fresh || draft.prepared) return;
  if (p.plan_id !== draft.plan_id) return;      // prepared for another plan
  for (const job of draft.jobs) {
    const src = (p.jobs || []).find((j) => j.priority === job.priority);
    if (src) { job.work_order = src.work_order || ""; job.command = src.command || ""; }
  }
  draft.prepared = true;
  renderDraft();
}

document.getElementById("prepare-btn").addEventListener("click", async () => {
  const note = document.getElementById("start-note");
  const missing = draft.jobs.filter((j) => !j.project_path.trim()).map((j) => `W${j.priority}`);
  if (missing.length) { note.textContent = `Set a project folder for ${missing.join(", ")}.`; note.className = "error"; return; }
  draft.prepared = false;
  const d = await post("/api/prepare", { jobs: draft.jobs, plan_id: draft.plan_id });
  if (!d.ok) { note.textContent = d.error || "Could not prepare."; note.className = "error"; return; }
  note.className = "muted";
  note.textContent = "The architect reads each project (read-only) and writes the work orders.";
  await poll();
});

function renderApprovals(list) {
  const card = document.getElementById("approvals-card");
  const host = document.getElementById("approvals");
  card.hidden = !list || !list.length;
  host.textContent = "";
  for (const a of [...(list || [])].reverse()) {
    const row = el("div", `appr ${a.decision === "left to you" ? "human" : "auto"}`
                          + (a.decision.startsWith("declined") ? " declined" : ""));
    row.appendChild(el("span", "when", new Date(a.ts * 1000).toLocaleTimeString()));
    row.appendChild(el("span", "who", `W${a.priority} · ${a.terminal}`));
    row.appendChild(el("span", "what", `${a.decision} — ${a.question}`));
    row.appendChild(el("span", "why", a.why));
    host.appendChild(row);
  }
}

function renderRoles() {
  const r = mother && mother.roles;
  if (!r) return;
  document.getElementById("roles").textContent =
    `Plans: ${r.planner_model} · Architect: ${r.architect_model} (read-only) · Workers: ${r.worker_model}`
    + `${mother.escalation && mother.escalation.length ? `, then ${mother.escalation.join(", ")} if one fails` : ""}`
    + ` · Answers workers: ${r.answer_model}`;
}

function renderMeta() {
  for (const job of (draft && draft.jobs) || []) {
    const n = document.querySelector(`[data-meta="${job.priority}"]`);
    if (n) n.textContent = modelLabel(job) + (job.same_as ? ` · starts after W${job.same_as} submits, reuses its adaptation` : "");
  }
}

document.getElementById("plan-btn").addEventListener("click", async () => {
  // The queen plans, prepares and starts its workers itself; nothing to review.
  const text = document.getElementById("instruction").value.trim();
  const note = document.getElementById("plan-note");
  if (!text) { note.textContent = "Write what this queen should get done."; note.className = "error"; return; }
  const folder = document.getElementById("folder").value.trim();
  try { localStorage.setItem("mother.folder", folder); } catch { /* private mode */ }
  const btn = document.getElementById("plan-btn");
  btn.disabled = true;
  const strategy = document.getElementById("strategy").value;
  try { localStorage.setItem("mother.strategy", strategy); } catch { /* private mode */ }
  const layout = document.getElementById("branches").checked ? "branches" : "team";
  try { localStorage.setItem("mother.branches", layout === "branches" ? "1" : ""); } catch { /* private mode */ }
  const d = await post("/api/command", { instruction: text, folder, strategy, layout });
  btn.disabled = false;
  if (!d.ok) { note.textContent = d.error || "The queen could not take it."; note.className = "error"; return; }
  document.getElementById("instruction").value = "";
  note.className = "muted";
  note.textContent = "The queen is planning its worker bees; they start on their own.";
  if (!focus) openQueen();
  await poll();
});

try { document.getElementById("branches").checked = !!localStorage.getItem("mother.branches"); } catch { /* private mode */ }

/* ---- the queen's own terminal: talk the job through with her like with any worker ---------------------- */

const completionSeen = new Set();

// Her completion report, in the Report tab.
function renderCompletion() {
  const c = mother && mother.completion;
  const card = document.getElementById("completion-card");
  card.hidden = !c;
  if (!c) return;
  document.getElementById("completion-hint").textContent = c.state === "writing" ? "the queen is writing it..."
    : c.state === "failed" ? "could not be written" : "written from the workers' reports and the queen's own checks";
  const body = document.getElementById("completion-body");
  body.textContent = "";
  if (c.state === "done") renderMarkdown(c.text, body);
  else if (c.state === "failed") body.appendChild(el("div", "error", c.error || "The report could not be written."));
}

function queenFolder() {
  const typed = document.getElementById("folder").value.trim();
  if (typed) return typed;
  const withFolder = ((mother && mother.dispatch) || []).find((j) => j.project_path);
  return withFolder ? withFolder.project_path : ((mother && mother.queen_term && mother.queen_term.folder) || "");
}

async function openQueenTerminal() {
  let folder = queenFolder();
  if (!folder) {
    folder = await askText("Which project folder is this about? The queen starts there and reads QUEEN.md and the README in it.",
                           "C:\\Users\\...\\my-app", "Project folder");
    if (!folder) return;
    document.getElementById("folder").value = folder;
  }
  const d = await working("Starting the queen's terminal…", () => post("/api/queen/terminal", { queen: currentQueen, folder }),
                          "Her terminal is open");
  if (!d.ok) return;
  await poll();
  if (!focus || focus.kind !== "queen") openQueen();
  renderQueenTerm();
}

async function startFromQueenPlan() {
  const qt = mother && mother.queen_term;
  if (!qt || !qt.plan) return;
  const text = qt.plan.text;
  if (!await ask(`Start with this plan? The queen splits it into workers and they start on their own.\n\n${text.length > 700 ? text.slice(0, 700) + "…" : text}`)) return;
  document.getElementById("instruction").value = qt.plan.text;
  if (!document.getElementById("folder").value.trim()) document.getElementById("folder").value = qt.folder;
  closeFocus(true);
  document.getElementById("plan-btn").click();
}

let qtermFitted = "";

let qtermKey = "";
// A terminal that is no longer hers (the queen was removed, or another agent chosen): its screen goes, so a new chat starts clean.
function dropQueenTermView(id) {
  const v = termViews.get(id);
  if (!v) return;
  try { if (v.ws) v.ws.close(); } catch { /* already closed */ }
  try { v.term.dispose(); } catch { /* nothing to free */ }
  v.box.remove();
  termViews.delete(id);
  if (termShown === id) termShown = "";
}

function renderQueenTerm() {
  const qt = mother && mother.queen_term;
  const key = qt ? `${mother.queen}:${qt.id}:${qt.started}` : "";
  if (key !== qtermKey) {
    const old = qtermKey.split(":")[1];
    if (old) dropQueenTermView(old);
    qtermKey = key;
    qtermFitted = "";
  }
  const started = !!(mother && (mother.running || (mother.dispatch && mother.dispatch.length)));
  const box = document.getElementById("qterm-box");
  const again = document.getElementById("qterm-open");
  again.hidden = !!qt && !qt.ended;                          // her terminal ended (the agent quit or failed): offer a new one
  again.classList.toggle("ended", !!(qt && qt.ended));
  again.querySelector(".chat-entry-text").textContent = qt && qt.ended
    ? "Her terminal has ended. Open a new one and carry on" : "Talk the job through with the queen";
  again.querySelector(".chat-entry-go").firstChild.textContent = qt && qt.ended ? "Start again " : "Start chatting ";
  document.getElementById("qterm-note").textContent = qt ? `${qt.agent || "agent"} · her terminal in ${qt.folder}` : "";
  box.hidden = !qt;
  document.getElementById("qplan-row").hidden = !(qt && qt.plan && !started);
  if (!qt) return;
  const v = termView(qt.id, box);
  v.box.hidden = false;
  if (document.getElementById("focus").hidden || !focus || focus.kind !== "queen") return;
  if (qtermFitted !== qt.id) {
    qtermFitted = qt.id;
    const settle = () => { termFit(v, true); v.term.scrollToBottom(); };
    requestAnimationFrame(() => requestAnimationFrame(settle));
    setTimeout(settle, 200);
    if (!v.ws) termPoll(v);
  } else termFit(v);
}

document.getElementById("qterm-open").addEventListener("click", openQueenTerminal);
document.getElementById("qplan-start").addEventListener("click", startFromQueenPlan);

function renderBeekeeper() {
  const bk = mother && mother.beekeeper;
  const body = document.getElementById("bk-body"), ask = document.getElementById("bk-ask");
  const agentSel = document.getElementById("bk-agent"), rulesBox = document.getElementById("bk-rules");
  if (agentSel.dataset.n !== String(((mother && mother.providers) || []).length)) {     // every agent, installed or not
    agentSel.dataset.n = String(((mother.providers) || []).length);
    agentSel.textContent = "";
    agentSel.appendChild(new Option("Codex (default)", ""));
    for (const p of (mother.providers || []).filter((x) => x.id !== "codex")) {
      const o = new Option(`${p.name}${p.installed ? "" : " (not installed)"}${["claude", "codex"].includes(p.id) ? "" : " · works in a terminal"}`, p.id);
      o.disabled = !p.installed;
      agentSel.appendChild(o);
    }
  }
  if (bk && document.activeElement !== agentSel) agentSel.value = bk.agent || "";
  fillBeekeeperModels(bk && bk.model);
  if (bk && document.activeElement !== rulesBox) rulesBox.value = bk.rules || "";
  ask.disabled = !!(bk && bk.busy);
  ask.textContent = bk && bk.busy ? "Checking…" : "Check now";
  const r = bk && bk.report;
  const tbox = document.getElementById("bk-term");
  tbox.hidden = !(bk && bk.terminal);
  if (bk && bk.terminal) {
    const v = termView(bk.terminal, tbox);
    v.box.hidden = false;
    if (!document.getElementById("focus").hidden) termFit(v);
  }
  body.hidden = !(bk && (r || bk.error || bk.busy));
  if (body.hidden) return;
  body.textContent = "";
  if (bk.error) body.appendChild(el("div", "error", bk.error));
  if (!r) { if (bk.busy) body.appendChild(el("div", "muted", `The beekeeper (${bk.agent}) is reading the rules and what happened…`)); return; }
  const tone = r.score == null ? "mid" : r.score >= 80 ? "good" : r.score >= 50 ? "mid" : "low";
  const sc = el("div", `bk-score ${tone}`);
  sc.append(el("b", "", r.score == null ? "?" : String(r.score)), el("span", "muted", `/ 100 · ${r.summary || ""}`));
  body.appendChild(sc);
  const finds = r.findings || [];
  const by = (st) => finds.filter((f) => f.status === st);
  const chips = el("div", "bk-chips");
  for (const [st, mark, word] of [["missed", "✘", "missed"], ["unclear", "?", "unclear"], ["followed", "✔", "followed"]]) {
    if (by(st).length) chips.appendChild(el("span", `bk-chip ${st}`, `${mark} ${by(st).length} ${word}`));
  }
  if (finds.length) body.appendChild(chips);
  const rowFor = (f) => {
    const row = el("div", `bk-find ${f.status}`);
    row.appendChild(el("span", "m", f.status === "followed" ? "✔" : f.status === "missed" ? "✘" : "?"));
    const t = el("div", "");
    t.append(el("div", "", f.rule), el("div", "ev", f.evidence));
    row.appendChild(t);
    return row;
  };
  for (const f of by("missed")) body.appendChild(rowFor(f));         // what needs the user's eye is always open
  for (const [st, word] of [["unclear", "cannot be told yet"], ["followed", "followed"]]) {
    if (!by(st).length) continue;
    const det = el("details", "bk-fold");
    det.appendChild(el("summary", "", `${by(st).length} ${word}`));
    for (const f of by(st)) det.appendChild(rowFor(f));
    body.appendChild(det);
  }
  if ((r.advice || []).length) {
    const ul = el("ul", "bk-advice");
    for (const a of r.advice) ul.appendChild(el("li", "", a));
    body.appendChild(ul);
  }
}
// The models of the chosen beekeeper agent (Codex when none is chosen), as that agent lists them.
function fillBeekeeperModels(current) {
  const sel = document.getElementById("bk-model");
  const id = document.getElementById("bk-agent").value || "codex";
  const prov = ((mother && mother.providers) || []).find((p) => p.id === id);
  const models = ((prov && prov.models) || ["default"]).filter((m) => m !== "default");
  const sig = id + "|" + models.join(",");
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    sel.textContent = "";
    sel.appendChild(new Option("default", ""));
    for (const m of models) sel.appendChild(new Option(modelName(m) || m, m));
  }
  if (document.activeElement !== sel) sel.value = [...sel.options].some((o) => o.value === (current || "")) ? (current || "") : "";
}

async function saveBeekeeper(agentChanged) {
  if (agentChanged === true) document.getElementById("bk-model").value = "";           // the old agent's model means nothing to the new one
  const d = await post("/api/beekeeper", { action: "set", agent: document.getElementById("bk-agent").value,
                                           model: document.getElementById("bk-model").value,
                                           rules: document.getElementById("bk-rules").value });
  if (!d.ok) tell(d.error || "Could not save the beekeeper's settings.");
  await poll();
}
document.getElementById("bk-agent").addEventListener("change", () => { fillBeekeeperModels(""); saveBeekeeper(true); });
document.getElementById("bk-model").addEventListener("change", () => saveBeekeeper());
document.getElementById("bk-rules").addEventListener("change", saveBeekeeper);
document.getElementById("bk-ask").addEventListener("click", async () => {
  const d = await working("The beekeeper is checking the queen…", () => post("/api/beekeeper", { queen: mother.queen }),
                          "The beekeeper is on it; the report appears in the card");
  await poll();
});

function shownFolder() {
  // The folder the selected queen actually works in, before anything typed for a next job.
  const withFolder = ((mother && mother.dispatch) || []).find((j) => j.project_path);
  if (withFolder) return withFolder.project_path;
  if (mother && mother.queen_term && mother.queen_term.folder) return mother.queen_term.folder;
  return document.getElementById("folder").value.trim();
}

// The folder lives on each queen's card: with several queens at once, one folder in the header read as the whole
// window's.
function renderProject() {
  const box = document.getElementById("proj-folder");
  if (box && document.activeElement !== box) box.value = shownFolder();
}
document.getElementById("proj-folder").addEventListener("change", (e) => {
  const folder = e.target.value.trim();
  document.getElementById("folder").value = folder;
  try { localStorage.setItem("mother.folder", folder); } catch { /* private mode */ }
  flash(folder ? "Project folder set" : "Project folder cleared");
  renderProject();
});

function renderTalks() { renderCompletion(); renderQueenTerm(); renderBeekeeper(); renderProject(); }

try { document.getElementById("branches").checked = !!localStorage.getItem("mother.branches"); } catch { /* private mode */ }

document.getElementById("discard-btn").addEventListener("click", () => { draft = null; renderDraft(); });

document.getElementById("start-btn").addEventListener("click", async () => {
  const note = document.getElementById("start-note");
  const missing = draft.jobs.filter((j) => !j.project_path.trim()).map((j) => `W${j.priority}`);
  if (missing.length) { note.textContent = `Set a project folder for ${missing.join(", ")}.`; note.className = "error"; return; }
  if (!await ask(`Start ${draft.jobs.length} worker bee(s)? Each opens in its own terminal and works on its project.`)) return;
  const d = await post("/api/start", { jobs: draft.jobs, answers, plan_id: draft.plan_id });
  if (!d.ok) { note.textContent = d.error || "Could not start."; note.className = "error"; return; }
  note.className = "muted";
  note.textContent = "Started. Workers open in QueenBee; the Queen answers what is in scope.";
  draft = { ...draft, fresh: false };
  await poll();
});

async function poll() {
  const pill = document.getElementById("host");
  try {
    const asked = currentQueen;
    const r = await fetch(`/api/state?queen=${encodeURIComponent(asked)}`, { cache: "no-store" });
    const data = await r.json();
    if (asked !== currentQueen) return;          // the user switched queens meanwhile: this answer is the old queen's
    if (!data.host) {
      pill.className = "pill off"; pill.textContent = "Host offline";
      pill.title = "QueenBee's host (the process that holds the terminals) is not running.";
    } else {
      pill.className = "pill on";
      pill.textContent = data.telemetry_fresh ? "Host online" : "Host online · no GPU telemetry yet";
      pill.title = "QueenBee's host is running: the terminals, the agents and the machine readings come from it.";
    }
    renderJobs(data);
    document.getElementById("gpu-count").textContent = (data.jobs || []).length || "";
    // Most people run no GPU job: the tab appears when there is one (or while it is the open view).
    const gpuTab = document.getElementById("gpu-tab");
    gpuTab.hidden = !(data.jobs || []).length && !gpuTab.classList.contains("on");
    renderHW(data.mother && data.mother.system, data.devices || []);
    if (data.mother) {
      const first = mother === null;
      mother = data.mother;
      queens = data.queens || [];
      askHeld();
      currentQueen = mother.queen || currentQueen;
      renderQueenHead();
      // The first-run card: a demo queen with workers, and a queen of your own with a job, tick their steps.
      startState.demo = startState.demo || queens.some((q) => /demo/i.test(q.name || "") && (q.jobs || []).length);
      startState.queen = startState.queen || queens.some((q) => !/demo/i.test(q.name || "") && ((q.jobs || []).length || q.instruction));
      renderStart();
      const list = document.getElementById("project-paths");
      if (first) for (const p of mother.projects || []) {
        const o = document.createElement("option"); o.value = p; list.appendChild(o);
      }
      // A plan already running (e.g. the window was reopened) is shown as is.
      if (!draft && mother.plan && mother.dispatch) draft = { ...mother.plan, fresh: false };
      renderRoles();
      renderApprovals(mother.approvals);
      renderResults(mother.results);
      renderReport(mother.report);
      const top = document.getElementById("project-paths-top");
      if (!top.childElementCount) for (const p of mother.projects || []) {
        const o = document.createElement("option"); o.value = p; top.appendChild(o);
      }
      adoptPrepared();
      if (mother.running || (mother.dispatch && !(draft && draft.fresh))) renderDraft();
      else if (draft && draft.fresh && (mother.preparing || (mother.prepared && !draft.prepared))) renderPrep();
      if (!mother.dispatch) {
        // A queen with no plan started yet has no workers to list.
        const host = document.getElementById("agents");
        if (!draft || !draft.fresh) {
          for (const c of agentCache.values()) c.el.remove();
          agentCache.clear();
          if (!host.childElementCount) host.appendChild(el("div", "empty", "Worker bees appear here once this queen's plan starts."));
          document.getElementById("agents-count").textContent = "";
        }
      }
      renderHive();
      renderQueenList();
      renderFocus();
      renderTalks();
      renderNotices();
      document.getElementById("decisions-count").textContent = (mother.approvals || []).length || "";
    }
    document.getElementById("stamp").textContent = new Date().toLocaleTimeString();
    refreshTimeline();
    refreshChanges(false);
    refreshFindings(false);
  } catch {
    pill.className = "pill off"; pill.textContent = "QueenBee is not responding";
  }
}

poll();
setInterval(poll, 2000);

/* ---- the agent menu: four agents, each with an arrow to its own versions ---------------------------- */

function agentModelLabel(agent, m) {
  if (agent === "claude") return `Claude ${modelName(m)}`;
  const name = (AGENT_ORDER.find((a) => a[0] === agent) || [agent, agent])[1];
  if (agent === "codex") return `Codex ${m === "default" ? ((mother && mother.codex_model) || "default model") : m}`;
  return `${name} ${m === "default" ? "default model" : m}`;
}

function agentGroupOf(o) {
  const g = o.parentElement && o.parentElement.dataset && o.parentElement.dataset.agent;
  if (g) return g;
  const v = (o.value || "").replace(/^agent:/, "");          // "agent:gemini" = Gemini talking in the queen's terminal
  for (const id of ["codex", "gemini", "kimi", "opencode", "cursor", "mistral", "grok"]) if (v === id || v.startsWith(id + ":")) return id;
  return "claude";
}

let openCascadeClose = null;
document.addEventListener("pointerdown", (e) => { if (openCascadeClose && !e.target.closest(".casc")) openCascadeClose(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openCascadeClose) openCascadeClose(); });

// A two-pane menu: the agents on the left (each with an arrow), the chosen agent's versions on the right. `source()` gives
// the entries as {agent, value, label, disabled}; `current()` the value and label to show; `pick(entry)` takes the choice.
function agentMenu(source, current, pick, lead = null) {
  const wrap = el("span", "casc");
  const btn = el("button", "casc-btn"); btn.type = "button";
  const pop = el("div", "casc-pop"); pop.hidden = true;
  wrap.append(btn, pop);
  const refresh = () => { btn.textContent = `${current().label || "choose"}  ▾`; };
  const close = () => { pop.hidden = true; if (openCascadeClose === close) openCascadeClose = null; };
  const open = () => {
    if (openCascadeClose) openCascadeClose();
    pop.textContent = "";
    const entries = source();
    const left = el("div", "casc-agents"), right = el("div", "casc-versions");
    pop.append(left, right);
    let active = (entries.find((x) => x.value === current().value) || entries[0] || {}).agent || "claude";
    const show = (agent) => {
      for (const r of left.querySelectorAll(".casc-agent")) r.classList.toggle("on", r.dataset.agent === agent);
      right.textContent = "";
      for (const x of entries.filter((y) => y.agent === agent)) {
        const item = el("button", "casc-item" + (x.value === current().value ? " on" : ""), x.label);
        item.type = "button"; item.disabled = !!x.disabled;
        if (x.disabled) item.title = "Install this agent first (Agents on this computer, top left)";
        item.addEventListener("click", () => { close(); pick(x); refresh(); });
        right.appendChild(item);
      }
    };
    if (lead) {
      const row = el("button", "casc-agent", lead.label); row.type = "button"; row.dataset.agent = "";
      row.addEventListener("click", () => { close(); lead.pick(); refresh(); });
      left.appendChild(row);
    }
    for (const [id, name] of AGENT_ORDER) {
      if (!entries.some((x) => x.agent === id)) continue;
      const prov = ((mother && mother.providers) || []).find((p) => p.id === id);
      const row = el("button", "casc-agent"); row.type = "button"; row.dataset.agent = id;
      row.append(el("span", "casc-name", name));
      if (prov && prov.experimental) row.append(el("span", "tl-model", "experimental"));
      if (prov && !prov.installed) row.append(el("span", "casc-off", "not installed"));
      row.append(el("span", "casc-arrow", "›"));
      row.addEventListener("mouseenter", () => show(id));
      row.addEventListener("click", () => show(id));
      left.appendChild(row);
    }
    show(active);
    pop.hidden = false;
    openCascadeClose = close;
  };
  btn.addEventListener("click", () => (pop.hidden ? open() : close()));
  wrap.refresh = refresh;
  refresh();
  return wrap;
}

// The same menu over a <select> that other code already reads and fills: the select stays (hidden) as the value.
function cascadeSelect(sel) {
  if (sel.dataset.casc) return;
  sel.dataset.casc = "1";
  sel.style.display = "none";
  const info = () => { const o = sel.selectedOptions[0]; return o ? { value: o.value, label: o.textContent } : { value: "", label: "" }; };
  const menu = agentMenu(
    () => [...sel.options].map((o) => ({ agent: agentGroupOf(o), value: o.value, label: o.textContent, disabled: o.disabled })),
    info,
    (x) => { sel.value = x.value; sel.dispatchEvent(new Event("change", { bubbles: true })); });
  sel.after(menu);
  new MutationObserver(() => requestAnimationFrame(menu.refresh)).observe(sel, { childList: true });
  sel.addEventListener("change", menu.refresh);
}

/* ---- the agents on this computer: install and sign in without leaving the window ------------------------- */

const AGENT_ORDER = [["claude", "Claude"], ["codex", "Codex"], ["gemini", "Gemini"], ["kimi", "Kimi"],
                     ["opencode", "OpenCode"], ["cursor", "Cursor Agent"], ["mistral", "Mistral Vibe"], ["grok", "Grok Build"]];
let setupAgents = [], setupBusy = false, setupFirst = true;

async function refreshSetup() {
  if (setupBusy) return;
  setupBusy = true;
  try {
    const d = await (await fetch("/api/agents", { cache: "no-store" })).json();
    if (d.ok) { setupAgents = d.agents; renderSetup(); }
  } catch { /* the next poll tries again */ } finally { setupBusy = false; }
}

function agentState(a) {
  if (!a.binary) return ["missing", "Not installed"];
  if (a.signed_in === false) return ["signed_out", "Sign in needed"];
  return ["ready", a.supported === false || a.note ? "Installed" : "Ready"];
}

async function setupAction(a, action) {
  const cmd = action === "install" ? a.install_command : a.login_command;
  if (action === "install" && !a.needs_ok) {
    const where = { npm: "Node.js from https://nodejs.org", uv: "uv from https://docs.astral.sh/uv/" }[a.needs] || a.needs;
    tell(`${a.label} needs ${a.needs} on this computer first. Install ${where}, then try again.`);
    return;
  }
  const what = action === "install" ? `Install ${a.label}` : `Sign in to ${a.label}`;
  if (!await ask(`${what}? This opens a terminal window and runs:\n\n${cmd}\n\nYou can watch it and answer anything it asks.`)) return;
  const d = await post("/api/agent/setup", { agent: a.id, action });
  if (!d.ok) { tell(d.error || "Could not start it."); return; }
  if (d.hint) tell(d.hint);
  setTimeout(refreshSetup, 3000);
}

function renderSetup() {
  const host = document.getElementById("setup-rows");
  host.textContent = "";
  let ready = 0, shown = 0;
  for (const [id, name] of AGENT_ORDER) {
    const a = setupAgents.find((x) => x.id === id);
    if (!a) continue;
    const [key, text] = agentState(a);
    if (key === "ready") ready += 1;
    shown += 1;
    const row = el("div", `setup-row ${key}`);
    row.appendChild(el("span", "setup-name", name));
    if (a.note && a.supported !== false && id !== "claude") row.appendChild(el("span", "tl-model", "experimental"));
    row.appendChild(el("span", `setup-state ${key}`, text));
    if (key === "missing" && a.install_command) {
      const b = el("button", "ghost small", "Install"); b.type = "button";
      b.addEventListener("click", () => setupAction(a, "install"));
      row.appendChild(b);
    } else if (key === "signed_out") {
      const b = el("button", "small", "Sign in"); b.type = "button";
      b.addEventListener("click", () => setupAction(a, "login"));
      row.appendChild(b);
    }
    host.appendChild(row);
  }
  document.getElementById("setup-sum").textContent = `· ${ready} of ${shown} ready`;
  startState.agent = ready > 0;
  renderStart();
  const box = document.getElementById("setup");
  if (setupFirst) { box.open = ready === 0; setupFirst = false; }       // a computer with no agent opens on this card
}

refreshSetup();
setInterval(refreshSetup, 6000);

/* ---- the timeline: which worker sets the finish time ---------------------- */

let timelineBusy = false;

function fmtSpan(s) {
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

async function refreshTimeline() {
  const host = document.getElementById("timeline");
  if (timelineBusy || !host || host.closest(".view").hidden) return;
  timelineBusy = true;
  try {
    const asked = currentQueen;
    const t = await (await fetch(`/api/timeline?queen=${encodeURIComponent(asked)}`, { cache: "no-store" })).json();
    if (asked === currentQueen) renderTimeline(t, host);
  } catch { /* the next poll tries again */ } finally { timelineBusy = false; }
}

function renderTimeline(t, host) {
  host.textContent = "";
  const ws = t.workers || [];
  if (!ws.length) { host.appendChild(el("div", "empty", "No team is running yet. Once this queen's workers start, each one gets a bar here: when it began, how long it ran and what it cost. The bar that ends last sets the finish time.")); return; }
  const origin = t.started || Math.min(...ws.map(w => w.start));
  const span = Math.max(1, (t.end || Math.max(...ws.map(w => w.end))) - origin);
  const known = ws.filter(w => w.tokens != null).reduce((a, w) => a + w.tokens, 0);
  const crit = ws.find(w => w.priority === t.critical);
  const summary = el("div", "tl-summary");
  summary.appendChild(el("span", "tl-total", `${fmtSpan(span)} ${t.finished ? "in all" : "so far"}`));
  if (crit && ws.length > 1) {
    const others = ws.filter(w => w !== crit && w.ended).map(w => w.end - origin);
    summary.appendChild(el("span", "tl-note",
      `W${crit.priority} ${crit.title} finished last (${fmtSpan(crit.end - origin)})` +
      (others.length ? `; the others were done between ${fmtSpan(Math.min(...others))} and ${fmtSpan(Math.max(...others))}` : "")));
  }
  if (known) summary.appendChild(el("span", "tl-note", `${fmtTok(known)} tokens read from the workers' own transcripts`));
  host.appendChild(summary);
  const axis = el("div", "tl-axis");
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const tick = el("span", "tl-tick", fmtSpan(span * i / ticks));
    tick.style.left = `${100 * i / ticks}%`;
    axis.appendChild(tick);
  }
  const head = el("div", "tl-row tl-head");
  head.append(el("div", "tl-label", ""), axis, el("div", "tl-tokens", "tokens"));
  host.appendChild(head);
  for (const w of ws) {
    const row = el("div", "tl-row" + (w.priority === t.critical ? " critical" : ""));
    const label = el("div", "tl-label");
    const family = (w.model || "").replace(/^./, c => c.toUpperCase());
    label.append(el("b", "", `W${w.priority}`), el("span", "tl-title", w.title));
    label.appendChild(el("span", "tl-model", w.agent === "codex" ? (w.model || "Codex") : family));
    const lane = el("div", "tl-lane");
    const bar = el("div", `tl-bar ${w.ended ? (w.status === "done" ? "done" : "bad") : "run"}`);
    bar.style.left = `${100 * (w.start - origin) / span}%`;
    bar.style.width = `${Math.max(0.6, 100 * (w.end - w.start) / span)}%`;
    bar.title = `W${w.priority} ${w.title}: ${fmtSpan(w.start - origin)} to ${fmtSpan(w.end - origin)} (${fmtSpan(w.end - w.start)}), ${w.status}`;
    bar.appendChild(el("span", "tl-dur", fmtSpan(w.end - w.start)));
    lane.appendChild(bar);
    row.append(label, lane, el("div", "tl-tokens", w.tokens == null ? "n/a" : fmtTok(w.tokens)));
    host.appendChild(row);
  }
  host.appendChild(el("div", "tl-foot", "n/a: Codex workers share their folder's sessions, so their tokens are not told apart."));
}

/* ---- changes: what the workers did to the files ---------------------------- */

let changesBusy = false, changesAt = 0;
const changesOpen = new Set();

async function refreshChanges(force) {
  const host = document.getElementById("changes");
  if (changesBusy || !host || host.closest(".view").hidden) return;
  if (!force && Date.now() - changesAt < 4000) return;
  changesBusy = true;
  try {
    const asked = currentQueen;
    const c = await (await fetch(`/api/changes?queen=${encodeURIComponent(asked)}`, { cache: "no-store" })).json();
    if (asked === currentQueen) renderChanges(c, host);
    changesAt = Date.now();
  } catch { /* the next poll tries again */ } finally { changesBusy = false; }
}

function diffLine(text) {
  const kind = text.startsWith("+++") || text.startsWith("---") ? "meta" : text.startsWith("@@") ? "hunk"
    : text.startsWith("+") ? "add" : text.startsWith("-") ? "del" : "ctx";
  return el("div", `df ${kind}`, text || " ");
}

function renderChanges(c, host) {
  host.textContent = "";
  const folders = c.folders || [];
  const total = folders.reduce((a, f) => a + (f.total || 0), 0);
  document.getElementById("changes-count").textContent = total || "";
  if (!folders.length) { host.appendChild(el("div", "empty", "No team is running yet. Once workers start, every file they add or change is listed here, grouped by worker, with the diff.")); return; }
  for (const f of folders) {
    const head = el("div", "ch-head");
    head.appendChild(el("span", "ch-root", f.root));
    head.appendChild(el("span", "tl-note", f.note || `${f.total} file${f.total === 1 ? "" : "s"} changed` +
      (f.total > (f.files || []).length ? `, the first ${f.files.length} shown` : "")));
    host.appendChild(head);
    if (!(f.files || []).length) { host.appendChild(el("div", "empty", f.note || "Nothing has changed yet.")); continue; }
    for (const file of f.files) {
      const key = `${f.root}|${file.path}`;
      const box = el("details", "ch-file");
      box.open = changesOpen.has(key);
      box.addEventListener("toggle", () => { box.open ? changesOpen.add(key) : changesOpen.delete(key); });
      const sum = el("summary");
      sum.appendChild(el("span", `ch-status ${file.status}`, file.status));
      sum.appendChild(el("span", "ch-path", file.path));
      if (file.binary) sum.appendChild(el("span", "tl-note", "binary or large: not shown"));
      else sum.appendChild(el("span", "ch-count", `+${file.added} -${file.removed}`));
      for (const w of file.workers || []) sum.appendChild(el("span", "tl-model", w));
      box.appendChild(sum);
      const body = el("div", "ch-diff");
      for (const line of file.diff || []) body.appendChild(diffLine(line));
      box.appendChild(body);
      host.appendChild(box);
    }
  }
}

/* ---- tabs -------------------------------------------------------------- */

function showView(name) {
  if (name === "timeline" || name === "changes") name = "report";    // both live under Report now (an old saved choice)
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("on", t.dataset.view === name);
  for (const v of document.querySelectorAll(".view")) v.hidden = v.dataset.view !== name;
  try { localStorage.setItem("mother.view", name); } catch { /* private mode */ }
  if (name === "hive") renderHive();
  if (name === "report") { refreshTimeline(); refreshChanges(true); refreshFindings(true); }
  if (name === "gpu") document.getElementById("gpu-tab").hidden = false;
}

for (const t of document.querySelectorAll(".tab")) t.addEventListener("click", () => showView(t.dataset.view));
for (const b of document.querySelectorAll(".es-go")) b.addEventListener("click", () => showView(b.dataset.go));
try { const v = localStorage.getItem("mother.view"); if (v) showView(v); } catch { /* private mode */ }

/* ---- the queens, on the left ---------------------------------------------- */

function queenCounts(jobs) {
  const c = { working: 0, done: 0, failed: 0, waiting: 0 };
  for (const j of jobs) {
    const [key] = statusOf(j);
    if (key === "working" || key === "submitted") c.working++;
    else if (key === "done" || key === "early") c.done++;
    else if (key === "failed" || key === "attention") c.failed++;
    else c.waiting++;
  }
  return c;
}

function renderQueenList() {
  const host = document.getElementById("queen-list");
  const sig = JSON.stringify([mother && mother.queen, queens.map((q) => [q.id, q.name, q.agent, q.model, q.planning, usageText(q.usage, q.limits), q.activity,
    q.instruction, (q.jobs || []).map((j) => j.status)])]);
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  host.textContent = "";
  for (const q of queens) {
    const item = el("button", `queen-item${mother && q.id === mother.queen ? " on" : ""}`);
    item.type = "button";
    const top = el("div", "qi-top");
    top.appendChild(el("span", "n-prio p1 queen-tag", qTag(q.id)));
    top.appendChild(el("span", "qi-name", q.name || q.id));
    const c = queenCounts(q.jobs || []);
    const act = q.planning ? "planning" : q.activity || ((q.jobs || []).length ? "idle" : "new");
    const xq = el("span", "qi-close", "✕");
    xq.title = `Remove ${qTag(q.id)} ${q.name || ""}`;
    xq.addEventListener("click", (e) => { e.stopPropagation(); removeQueen(q.id); });
    top.appendChild(el("span", `qi-state s-${String(act).replace(" ", "-")}`, act));
    top.appendChild(xq);
    item.appendChild(top);
    item.appendChild(el("div", "qi-sub", q.solo ? `Free worker · ${q.agent || "one agent"}, no queen`
      : (q.instruction || q.description) ? (q.instruction || q.description).slice(0, 70) : "Give her a job, or add worker bees"));
    const meta = el("div", "qi-meta");
    meta.appendChild(el("span", null, q.agent ? `${q.agent}${q.model ? " · " + q.model : ""}` : "default workers"));
    const use = usageText(q.usage, q.limits);
    const n = (q.jobs || []).length;
    meta.appendChild(el("span", null, n ? `${n} worker${n === 1 ? "" : "s"} · ${c.working} on · ${c.done} done` + (c.failed ? ` · ${c.failed} failed` : "") : ""));
    item.appendChild(meta);
    if (use) item.appendChild(el("div", "qi-use", use));
    item.addEventListener("click", async () => {
      await selectQueen(q.id);
      showView("hive");
      centerOn(`${q.id}:M`);
    });
    host.appendChild(item);
  }
}

var lastPos = new Map();   // var: renderHive may run before this line

function centerOn(key) {
  const p = lastPos.get(key);
  const wrap = document.querySelector(".graph-wrap");
  if (!p || !wrap) return;
  view.k = Math.max(view.k, 0.75);
  view.x = wrap.clientWidth / 2 - (p.x + p.w / 2) * view.k;
  view.y = 40 - p.y * view.k;
  view.touched = true;
  applyView();
}

/* ---- the open chat: a queen or a worker, large --------------------------- */

let focus = null;              // {kind: "queen"} or {kind: "job", priority, branch}

function openQueen() {
  focus = { kind: "queen" };
  convoTerm = null;
  document.getElementById("focus").hidden = false;
  renderFocus();
  const ta = document.getElementById("instruction");
  if (!(mother && mother.dispatch) && !(draft && draft.fresh)) ta.focus();
}

function openWorker(job, branch = false) {
  focus = { kind: "job", priority: job.priority, branch, pane: null };
  convoTerm = null;
  document.getElementById("say").value = "";
  // Closing hides the terminals; the header's signature would still match on reopening and skip showing this worker's.
  document.getElementById("focus-title").dataset.sig = "";
  document.getElementById("focus").hidden = false;
  renderFocus();
}

function closeFocus(quiet) {
  focus = null;
  convoTerm = null;
  hideTerminals();
  document.getElementById("focus").hidden = true;
  if (!quiet) renderHive();
}

document.getElementById("focus-close").addEventListener("click", () => closeFocus());
document.getElementById("focus").addEventListener("click", (e) => { if (e.target.id === "focus") closeFocus(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!document.getElementById("dialog").hidden) closeDialog();
  else if (focus) closeFocus();
});

/* ---- a worker's terminal, live ----------------------------------------------- */

// Each worker bee is a terminal: its agent's own screen, typed into directly.
// QueenBee's xterm.js draws it; keystrokes and output go straight to the host
// over a WebSocket, with polling through this server as the fallback.
const termViews = new Map();           // terminal id -> view
let termWs = null;                     // {port, token} from the host
let termShown = null;

const TERM_THEME = {
  background: "#0c0c0c", foreground: "#e6e6e6", cursor: "#f2b02e", cursorAccent: "#0c0c0c",
  selectionBackground: "rgba(232,112,58,0.30)",
  black: "#1a1a1a", red: "#e5534b", green: "#3fb950", yellow: "#e3b341", blue: "#539bf5",
  magenta: "#d06fbf", cyan: "#39c5cf", white: "#b8b8b8", brightBlack: "#6b6b6b", brightRed: "#ff7b72",
  brightGreen: "#56d364", brightYellow: "#f2cc60", brightBlue: "#79c0ff", brightMagenta: "#e89ad8",
  brightCyan: "#56d4dd", brightWhite: "#f0f0f0",
};

// Replayed scrollback holds old terminal queries; xterm answers them again,
// and those answers must not be typed into the agent.
function isTerminalReply(data) {
  if (!data) return false;
  return data.replace(/\x1b\[\??[0-9;]*c/g, "").replace(/\x1b\[[0-9]+;[0-9]+R/g, "")
    .replace(/\x1b\](?:1[0-2]|4;\d+);rgb:[0-9a-f/]*(?:\x07|\x1b\\)/gi, "").length === 0;
}

async function terminalSocketInfo() {
  if (termWs) return termWs;
  try {
    const d = await (await fetch("/api/terminal/ws", { cache: "no-store" })).json();
    termWs = d.ok ? d.ws : null;
  } catch { termWs = null; }
  return termWs;
}

function termWrite(v, data, offset) {
  const replay = !v.hydrated;
  if (replay) v.replayDepth += 1;
  v.term.write(data, () => {
    if (offset !== undefined) v.offset = offset;
    if (replay) { v.replayDepth = Math.max(0, v.replayDepth - 1); if (!v.replayDepth) v.hydrated = true; }
    const buf = v.term.buffer.active;
    if (!v.scrolledAway && buf.viewportY !== buf.baseY) v.term.scrollToBottom();
  });
}

function termExited(v) {
  if (v.exited) return;
  v.exited = true;
  v.term.write("\r\n\x1b[90m[the worker's terminal has closed]\x1b[0m\r\n");
  if (focus && focus.kind === "job") { document.getElementById("focus-title").dataset.sig = ""; renderFocus(); }
}

async function termConnect(v) {
  const info = await terminalSocketInfo();
  if (!info || v.ws || v.exited || v.disposed || !window.WebSocket) { termPoll(v); return; }
  let ws;
  try {
    ws = new WebSocket(`ws://127.0.0.1:${info.port}/ws/terminal?id=${encodeURIComponent(v.id)}`
                       + `&since=${v.offset}&token=${encodeURIComponent(info.token)}`);
  } catch { termPoll(v); return; }
  v.ws = ws;
  ws.onopen = () => {
    v.retry = 0;
    if (v.term.cols && v.term.rows) ws.send(JSON.stringify({ t: "r", c: v.term.cols, r: v.term.rows }));
    if (v.pending) { ws.send(JSON.stringify({ t: "i", d: v.pending })); v.pending = ""; }
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.t === "o") termWrite(v, msg.d, msg.o);
    else if (msg.t === "x") termExited(v);
  };
  ws.onclose = () => {
    if (v.ws === ws) v.ws = null;
    if (v.exited || v.disposed) return;
    v.retry = Math.min((v.retry || 0) + 1, 6);
    termPoll(v);
    setTimeout(() => termConnect(v), 500 * 2 ** (v.retry - 1));
  };
}

// Without a socket: read through this server.
async function termPoll(v) {
  if (v.polling || v.disposed || v.exited || (v.ws && v.ws.readyState === WebSocket.OPEN)) return;
  v.polling = true;
  try {
    const d = await (await fetch(`/api/terminal/read?id=${encodeURIComponent(v.id)}&since=${v.offset}`,
                                 { cache: "no-store" })).json();
    if (d.data) termWrite(v, d.data, d.offset);
    if (d.ok && d.running === false) termExited(v);
  } catch { /* next time */ } finally {
    v.polling = false;
  }
  if (!v.disposed && !v.exited && termShown === v.id && !(v.ws && v.ws.readyState === WebSocket.OPEN)) {
    setTimeout(() => termPoll(v), 400);
  }
}

function termSend(v, data) {
  if (v.ws && v.ws.readyState === WebSocket.OPEN) { v.ws.send(JSON.stringify({ t: "i", d: data })); return; }
  post("/api/terminal/write", { id: v.id, data }).then(() => termPoll(v));
}

function termFit(v, force) {
  if (!v || !v.fit || v.box.hidden || v.static) return;
  // A box that is not really on screen yet (the panel still opening, the window hidden, a zero-sized pane)
  // measures a handful of columns; resizing the agent's terminal to that squeezes its screen into a thin strip
  // that stays garbled or blank until the agent redraws. Never fit to such a size.
  if (v.box.clientWidth < 240 || v.box.clientHeight < 100) return;
  const before = `${v.term.cols}x${v.term.rows}`;
  const oldCols = v.term.cols, oldRows = v.term.rows;
  try { v.fit.fit(); } catch { return; }
  if (v.term.cols < 40 || v.term.rows < 8) { v.term.resize(oldCols, oldRows); return; }
  if (!force && `${v.term.cols}x${v.term.rows}` === before) return;
  if (v.ws && v.ws.readyState === WebSocket.OPEN) v.ws.send(JSON.stringify({ t: "r", c: v.term.cols, r: v.term.rows }));
  else post("/api/terminal/resize", { id: v.id, cols: v.term.cols, rows: v.term.rows });
}

function termView(id, inlineHost) {
  let v = termViews.get(id);
  if (v) return v;
  const box = el("div", inlineHost ? "term-box inline" : "term-box");
  (inlineHost || document.getElementById("term-host")).appendChild(box);
  const t = new Terminal({ theme: TERM_THEME, fontFamily: "ui-monospace, 'Cascadia Mono', Consolas, monospace",
                           fontSize: 14, lineHeight: 1.2, cursorBlink: false, cursorStyle: "bar", cursorWidth: 2,
                           scrollback: 5000, allowProposedApi: true });
  const fit = window.FitAddon ? new FitAddon.FitAddon() : null;
  if (fit) t.loadAddon(fit);
  t.open(box);
  box.addEventListener("pointerdown", () => setTimeout(() => t.focus(), 0));
  // Windows-style clipboard: Ctrl+V pastes, Ctrl+C copies a selection,
  // right-click copies or pastes.
  const copy = () => { const x = t.getSelection(); if (!x) return false; navigator.clipboard.writeText(x).catch(() => {}); t.clearSelection(); return true; };
  t.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown" || !e.ctrlKey || e.altKey || e.metaKey) return true;
    const k = e.key.toLowerCase();
    if (k === "v") return false;
    if (k === "c" && (e.shiftKey || t.hasSelection())) { copy(); e.preventDefault(); return false; }
    return true;
  });
  box.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    if (!copy()) navigator.clipboard.readText().then((x) => { if (x) t.paste(x); }).catch(() => {});
  });
  v = { id, term: t, fit, box, offset: 0, hydrated: false, replayDepth: 0, pending: "", exited: false, inline: !!inlineHost };
  box.addEventListener("wheel", () => requestAnimationFrame(() => {
    const buf = t.buffer.active; v.scrolledAway = buf.viewportY < buf.baseY;
  }), { passive: true });
  t.onData((data) => {
    if (v.replayDepth > 0 && isTerminalReply(data)) return;
    // The colour answers (OSC 10/11/12) to a question the agent asked long ago arrive as plain text in its input box;
    // an agent copes without them, so they are never sent.
    if (/\x1b\](?:1[0-2]);rgb:/.test(data) && isTerminalReply(data)) return;
    if (!isTerminalReply(data)) v.scrolledAway = false;
    if (v.ws && v.ws.readyState === WebSocket.OPEN) v.ws.send(JSON.stringify({ t: "i", d: data }));
    else if (v.ws) v.pending += data;
    else termSend(v, data);
  });
  termViews.set(id, v);
  if (id.startsWith("saved-")) { v.static = true; v.exited = true; return v; }     // a replay: nothing to connect to
  termConnect(v);
  return v;
}

// A closed session's last screen, replayed at the size it had, so it looks as it did while it ran.
async function showSavedScreen(job) {
  const key = `saved-${currentQueen}-${job.priority}`;
  let v = termViews.get(key);
  if (!v) {
    let d;
    try { d = await (await fetch(`/api/screen?queen=${encodeURIComponent(currentQueen)}&job=${job.priority}`, { cache: "no-store" })).json(); }
    catch { return false; }
    if (!d.ok || !d.data) return false;
    v = termView(key);
    v.term.resize(d.cols || 120, d.rows || 32);
    // xterm writes later, and a box still hidden has no size: scrolling right away left the screen at its top.
    v.term.write(d.data, () => { if (termShown === key) v.term.scrollToBottom(); });
  }
  if (!focus || focus.priority !== job.priority) return true;
  for (const [other, view] of termViews) view.box.hidden = other !== key;
  document.getElementById("term-empty").hidden = true;
  v.box.hidden = false;
  termShown = key;
  // The screen was saved at the agent's size (often 32 rows); in a shorter panel its last rows fell below the box and
  // were cut off. Its height follows the box (its width stays, or the agent's drawing would wrap), then the end shows.
  const settle = () => {
    if (termShown !== key) return;
    const p = v.fit && v.box.clientHeight >= 100 ? v.fit.proposeDimensions() : null;
    if (p && p.rows >= 8 && p.rows !== v.term.rows) v.term.resize(v.term.cols, p.rows);
    v.term.scrollToBottom();
  };
  settle();
  requestAnimationFrame(() => requestAnimationFrame(settle));
  setTimeout(settle, 200);
  return true;
}

function showTerminal(id) {
  const empty = document.getElementById("term-empty");
  empty.hidden = true;
  for (const [other, view] of termViews) if (!view.inline) view.box.hidden = other !== id;
  const v = termView(id);
  v.box.hidden = false;
  if (termShown !== id) {
    termShown = id;
    // xterm measures its font a moment after opening; fit once it has, and
    // again after the panel's own opening animation.
    const settle = () => { if (termShown !== id) return; termFit(v); v.term.scrollToBottom(); v.term.focus(); };
    requestAnimationFrame(() => requestAnimationFrame(settle));
    setTimeout(settle, 150);
    setTimeout(settle, 450);
    if (!v.ws) termPoll(v);
  }
  return v;
}

// A worker whose terminal is gone is shown as its conversation, read from the agent's own transcript.
const transcriptState = { key: "", at: 0 };
async function showTranscript(job) {
  termShown = null;
  for (const view of termViews.values()) view.box.hidden = true;
  const empty = document.getElementById("term-empty");
  empty.hidden = false;
  empty.classList.add("term-transcript");
  const key = `${job.priority}:${job.status}`;
  if (transcriptState.key === key && Date.now() - transcriptState.at < 5000) return;
  transcriptState.key = key; transcriptState.at = Date.now();
  let d;
  try { d = await (await fetch(`/api/conversation?queen=${encodeURIComponent(currentQueen)}&job=${job.priority}`, { cache: "no-store" })).json(); }
  catch { d = { ok: false, error: "unavailable" }; }
  if (!document.getElementById("focus").hidden && focus && focus.priority === job.priority) {
    empty.textContent = "";
    const note = el("div", "muted transcript-note",
      "This worker's terminal has closed. Below is its conversation, read from its saved transcript.");
    empty.appendChild(note);
    if (!d.ok) empty.appendChild(el("div", "error", d.error || "unavailable"));
    else if (!(d.items || []).length) empty.appendChild(el("div", "muted", d.note || "Nothing was saved for this worker."));
    else for (const it of d.items) empty.appendChild(convoItem(it));
    empty.scrollTop = empty.scrollHeight;
  }
}

function hideTerminals(message) {
  termShown = null;
  for (const view of termViews.values()) if (!view.inline) view.box.hidden = true;
  const empty = document.getElementById("term-empty");
  empty.hidden = !message;
  empty.classList.remove("term-transcript");
  empty.textContent = message || "";
}

new ResizeObserver(() => { const v = termShown && termViews.get(termShown); if (v) termFit(v); })
  .observe(document.getElementById("term-host"));

// Which screen a worker shows: its agent's terminal, or its GPU run's.
function renderWorkerTerminal(job) {
  const panes = [];
  if (job.terminal && !job.direct) panes.push(["agent", "Worker", job.terminal]);
  if (job.workload) panes.push(["gpu", "GPU run", job.workload]);
  const tabs = document.getElementById("term-tabs");
  if (!focus.pane || !panes.some((p) => p[0] === focus.pane)) focus.pane = panes.length ? panes[0][0] : null;
  tabs.hidden = panes.length < 2;
  tabs.textContent = "";
  for (const [key, label] of panes) {
    const b = el("button", `term-tab${key === focus.pane ? " on" : ""}`, label);
    b.type = "button";
    b.addEventListener("click", () => { focus.pane = key; document.getElementById("focus-title").dataset.sig = ""; renderFocus(); });
    tabs.appendChild(b);
  }
  const pane = panes.find((p) => p[0] === focus.pane);
  let live = false;
  if (pane) {
    const v = showTerminal(pane[2]);
    live = !v.exited;
    // A closed terminal with nothing on its screen is not a bug to puzzle over: say what happened.
    const buf = v.term.buffer.active;
    let written = false;
    for (let i = 0; i < Math.min(buf.length, 60) && !written; i++) {
      const line = buf.getLine(i);
      written = !!(line && line.translateToString().trim());
    }
    if (!written && (job.term_state === "closed" || ["done", "failed", "stopped", "stopped early"].includes(job.status))) {
      // no live screen: replay the saved one, or else show what the worker said and did
      showSavedScreen(job).then((ok) => { if (!ok) showTranscript(job); });
      live = false;
    }
  } else {
    hideTerminals(job.status === "queued" ? "This worker has not started yet. Change its task below, or wait: the queen starts it."
      : job.branched ? "A queen of its own hive: open its workers on the map."
      : "No terminal for this worker.");
  }
  // Typing goes into the terminal; the box below is for when there is none.
  document.getElementById("say-row").hidden = live;
  document.getElementById("say-hint").hidden = live;
}

function focusJob() {
  return focus && focus.kind === "job" && ((mother && mother.dispatch) || []).find((j) => j.priority === focus.priority);
}

// Who directs, by name, for the line under the queen's settings: the agent in her terminal, Codex (and its model) or a Claude model.
function directorName() {
  const prov = ((mother && mother.providers) || []).find((p) => p.id === mother.queen_terminal_agent);
  if (mother.queen_terminal_agent) return prov ? prov.name : mother.queen_terminal_agent;
  if (mother.director_agent === "codex") return "Codex" + (mother.director_codex_model ? ` ${mother.director_codex_model}` : "");
  return modelName(mother.director_model);
}

function renderFocus() {
  if (!focus || !mother) return;
  const title = document.getElementById("focus-title");
  const actions = document.getElementById("focus-actions");
  const qBody = document.getElementById("focus-queen"), wBody = document.getElementById("focus-worker");
  qBody.hidden = focus.kind !== "queen";
  wBody.hidden = focus.kind !== "job";
  if (focus.kind === "queen") {
    const sig = JSON.stringify(["q", mother.queen, mother.queen_name, mother.queen_agent, mother.queen_model, queens.length, mother.activity,
                                mother.running, mother.planning, mother.director_agent, mother.director_model,
                                mother.director_codex_model, mother.queen_terminal_agent]);
    if (title.dataset.sig !== sig) {
      title.dataset.sig = sig;
      title.textContent = "";
      title.appendChild(el("span", "n-prio p1 queen-tag", qTag(mother.queen)));
      title.appendChild(el("span", "n-badge mother", "QUEEN BEE"));
      title.appendChild(el("span", "ft-name", mother.queen_name || mother.queen));
      if (mother.activity && mother.activity !== "new") title.appendChild(termPill(mother.activity));
      actions.textContent = "";
      const add = el("button", "small", "+ Worker bee");
      add.type = "button";
      add.addEventListener("click", () => openAddWorker());
      actions.appendChild(add);
      document.getElementById("queen-agent").textContent = mother.queen_agent
        ? `workers: ${mother.queen_agent === "auto" ? "the queen advises" : mother.queen_agent}${mother.queen_model ? " · " + (mother.queen_model === "auto" ? "models by part" : mother.queen_model) : ""}` : directorName() + " directs: she looks in on the hive every couple of minutes. Each worker bee has its own model.";
    }
    renderQueenLog();
    renderLimits();
    renderAnswers();
    return;
  }
  document.getElementById("say-row").hidden = false;
  const job = focusJob();
  if (!job) { closeFocus(); return; }
  const [key, label] = statusOf(job);
  const working = ["agent working", "GPU job submitted"].includes(job.status) && job.terminal && !job.direct;
  const ended = ["done", "failed", "stopped", "stopped early"].includes(job.status);
  const sig = JSON.stringify(["j", job.priority, job.status, job.term_state, job.attention, job.detached, job.role, job.branched, job.tier,
                              job.agent, job.terminal, focus.branch, (mother.providers || []).length,
                              (job.refused || []).map((r) => r.call)]);
  if (title.dataset.sig !== sig) {
    title.dataset.sig = sig;
    title.textContent = "";
    title.appendChild(el("span", `n-prio worker-tag${job.priority === 1 ? " p1" : ""}`,
                         jobTag(job, (mother && mother.dispatch) || [], mother && mother.queen)));
    if (job.role === "mother") title.appendChild(el("span", "n-badge mother small", "⑂ QUEEN"));
    title.appendChild(el("span", "ft-name", job.title || ""));
    title.appendChild(el("span", `jstat ${key}`, job.detached ? "let go" : label));
    if (job.term_state) title.appendChild(termPill(job.term_state, job.term_reason));
    actions.textContent = "";
    if (!job.branched && !job.lane) actions.appendChild(modelSelect(job));
    const tog = el("label", "switch");
    tog.title = job.detached ? "Let go of: the Queen does not manage it" : "Managed by the Queen";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !job.detached;
    box.addEventListener("change", () => steer(job, { detached: !box.checked }));
    tog.appendChild(box);
    tog.appendChild(el("span", "knob"));
    actions.appendChild(tog);
    if (!job.branched && !job.lane && job.role !== "mother") {
      const f = el("button", "ghost small", "⑂ Make queen");
      f.type = "button";
      f.addEventListener("click", () => { focus.branch = !focus.branch; title.dataset.sig = ""; renderFocus(); });
      actions.appendChild(f);
    }
    if (ended && !job.lane) {
      const rm = el("button", "ghost small danger", "Remove worker");
      rm.type = "button";
      rm.addEventListener("click", async () => {
        if (!await ask(`Remove W${job.priority} from the hive?`)) return;
        const d = await post("/api/worker/remove", { queen: currentQueen, priority: job.priority });
        if (!d.ok) { tell(d.error || "Could not remove it."); return; }
        closeFocus();
        await poll();
      });
      actions.appendChild(rm);
    }
    if (!ended) {
      const k = el("button", "ghost small danger", "✕ Stop");
      k.type = "button";
      k.addEventListener("click", async () => {
        if (!await ask(`Stop W${job.priority} and every worker under it?`)) return;
        const d = await post("/api/kill", { queen: currentQueen, priority: job.priority });
        if (!d.ok) tell(d.error || "Could not stop it.");
        await poll();
      });
      actions.appendChild(k);
    }
    renderWorkerTask(job);
    renderWorkerTerminal(job);
    renderRefused(job);
    const slot = document.getElementById("fork-slot");
    slot.textContent = "";
    if (focus.branch && !job.branched && job.role !== "mother") slot.appendChild(branchForm(job));
    else if (job.role === "mother" && job.instruction) slot.appendChild(el("div", "a-note branch",
      (job.branched ? "Queen of its own hive: " : "Will become a queen: ") + job.instruction));
    // What the text box does depends on where the worker is.
    const say = document.getElementById("say");
    const btn = document.getElementById("say-btn");
    const hint = document.getElementById("say-hint");
    const can = !job.branched && !job.lane && !(job.direct && !ended);
    say.disabled = btn.disabled = !can;
    say.placeholder = !can ? (job.lane ? "A sweep value: the Queen runs these itself." : job.branched
      ? "A queen of its own hive: talk to its workers." : "The Queen runs this one itself; nothing to say to it.")
      : working ? "Write to the worker — it reads this in its own conversation."
      : ended ? "Not what you wanted? Say what to change — it redoes the work."
      : "Change its task before it starts.";
    btn.textContent = working ? "Send" : ended ? "Redo" : "Save task";
    say.rows = 2;
    hint.textContent = !can ? "" : working ? "Typed into its terminal. The Queen keeps answering its questions within scope."
      : ended ? "If its session is still open it redoes it there, remembering what it did; otherwise a new worker starts on the same task with your change."
      : "It starts with this task.";
  }
}

// What this worker was asked to do, with its checks, its folder and why it is waiting, above its terminal.
let wkOpen = false;
function renderWorkerTask(job) {
  document.getElementById("wk-title").textContent = job.title ? `Task · ${job.title}` : "Task";
  const bits = [`W${job.priority}`, job.agent ? `${job.agent}${job.tier ? " " + job.tier : ""}` : "", statusOf(job)[1]];
  if (job.waiting_for) bits.push(`waiting for W${job.waiting_for}`);
  if (job.held) bits.push(`held: ${job.held}`);
  document.getElementById("wk-meta").textContent = bits.filter(Boolean).join(" · ");
  const goal = document.getElementById("wk-goal");
  goal.textContent = job.goal || job.work_order || "(no task text)";
  goal.classList.toggle("clamp", !wkOpen);
  const more = document.getElementById("wk-more");
  more.hidden = (job.goal || "").length < 260;
  more.textContent = wkOpen ? "Show less" : "Show all";
  const extra = document.getElementById("wk-extra");
  extra.textContent = "";
  if (job.project_path) extra.appendChild(el("div", "wk-line", `Folder: ${job.project_path}`));
  const checks = (job.acceptance || []).map((c) => c.type === "command" ? `runs ${c.run}` : c.type === "file_exists" ? `${c.path} exists`
    : c.type === "file_contains" ? `${c.path} contains "${c.text}"` : c.type);
  if (checks.length) extra.appendChild(el("div", "wk-line", `Done when: ${checks.join("; ")}`));
}
document.getElementById("wk-more").addEventListener("click", () => { wkOpen = !wkOpen; if (mother) renderFocus(); });

function branchForm(job) {
  const f = el("div", "fork-form");
  f.appendChild(el("div", "fork-hint", "Make this worker the queen of its own hive: it plans its own workers from this."));
  const ta = document.createElement("textarea");
  ta.rows = 3;
  ta.value = job.goal || "";
  f.appendChild(ta);
  // A branch queen is a model of its own: the user decides which, and which model her workers run on.
  const pick = (label, options) => {
    const wrap = el("label", "fork-pick", label + " ");
    const sel = document.createElement("select");
    for (const [value, text] of options) sel.appendChild(new Option(text, value));
    wrap.appendChild(sel);
    f.appendChild(wrap);
    return sel;
  };
  const queenModel = pick("Queen's model", []);
  fillQueenModels(queenModel, "sonnet");
  queenModel.addEventListener("change", () => { if (queenModel.value === "__other__") pickOtherModel(queenModel); else queenModel.dataset.last = queenModel.value; });
  const workerModel = pick("Her workers", [["auto", "Let the queen advise"],
    ...((((mother && mother.providers) || []).find((p) => p.id === "claude") || { models: [] }).models.map((m) => [m, `Claude ${modelName(m)}`]))]);
  const btns = el("div", "fork-btns");
  const cancel = el("button", "ghost small", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => { focus.branch = false; document.getElementById("focus-title").dataset.sig = ""; renderFocus(); });
  const go = el("button", "primary small", "Make queen");
  go.type = "button";
  go.addEventListener("click", async () => {
    focus.branch = false;
    await steer(job, { role: "mother", instruction: ta.value });
  });
  btns.appendChild(cancel);
  btns.appendChild(go);
  f.appendChild(btns);
  return f;
}

document.getElementById("say-btn").addEventListener("click", async () => {
  const job = focusJob();
  const text = document.getElementById("say").value.trim();
  if (!job || !text) return;
  document.getElementById("say").value = "";
  const d = await post("/api/job", { priority: job.priority, changes: { goal: text } });
  if (!d.ok) tell(d.error || "Could not reach the worker.");
  document.getElementById("focus-title").dataset.sig = "";
  await poll();
  const now = focusJob();
  if (now && now.terminal && now.terminal !== convoTerm) { convoTerm = now.terminal; convoSig = ""; }
  refreshConversation();
});
document.getElementById("say").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) document.getElementById("say-btn").click();
});

// Who answers the workers' prompts. The queen by her rule, or the user in each worker's terminal while she only watches.
const ANSWERS_HINT = {
  queen: "She answers by her rule: yes inside the project folder, no to deleting, downloading, installing or writing outside it. A refusal is marked on the worker until you look.",
  me: "The workers ask you in their own terminals (Claude Code before every edit and command, Codex when it needs to leave its sandbox), and a waiting worker shows \"needs you\" on the map. The queen only watches: she keeps the board, nudges a stalled worker and runs the check at the end.",
};

let answersPending = null;  // the mode just clicked, shown at once while the server is told (a poll in flight is older)

// A team that was at work when QueenBee closed, with no worker still running: nothing starts again by itself.
// The user is asked once per queen whether to carry on or stop.
const heldAsked = new Set();
let heldAsking = false;
async function askHeld() {
  if (heldAsking) return;
  const q = queens.find((x) => x.held && !heldAsked.has(x.id));
  if (!q) return;
  heldAsked.add(q.id);
  heldAsking = true;
  const left = (q.jobs || []).filter((j) => !ENDED.includes(j.status)).length;
  const go = await sheet(`${q.name || "A queen"}'s team was at work when QueenBee closed, and none of its workers is running now`
    + ` (${left} not finished). Continue where it left off, or stop it? Stopping keeps the work in its folder.`,
    { title: "Continue the work?", ok: "Continue", cancel: "Stop it" });
  const d = await post("/api/queen", { action: "resume", queen: q.id, go: !!go });
  heldAsking = false;
  if (!d.ok) tell(d.error || "Could not do that.");
  await poll();
}

function renderAnswers() {
  const mode = answersPending || (mother && mother.answers) || "queen";
  for (const b of document.querySelectorAll("#answers-seg button")) b.classList.toggle("on", b.dataset.mode === mode);
  document.getElementById("answers-hint").textContent = ANSWERS_HINT[mode];
}

for (const b of document.querySelectorAll("#answers-seg button")) {
  b.addEventListener("click", async () => {
    if (!mother || (answersPending || mother.answers || "queen") === b.dataset.mode) return;
    const running = ((mother.dispatch) || []).some((j) => j.kind === "task" && j.agent !== "claude"
      && ["agent working", "queued"].includes(j.status));
    if (running && !await ask("Claude Code workers follow this at once. Workers on other agents keep the way they started; "
                              + "the ones that start from now on follow it.", { title: "Change who answers?", ok: "Change it" })) return;
    answersPending = b.dataset.mode;
    renderAnswers();
    const d = await post("/api/queen", { action: "answers", queen: currentQueen, mode: b.dataset.mode });
    if (d.ok) mother.answers = d.answers;
    answersPending = null;
    renderAnswers();
    if (!d.ok) tell(d.error || "Could not change it.");
  });
}

function renderLimits() {
  const form = document.getElementById("limits-form");
  if (form.contains(document.activeElement)) return;           // never under the user's hand
  const lim = mother.limits || {};
  for (const input of form.querySelectorAll("input[data-limit]")) {
    const key = input.dataset.limit;
    const v = key === "budget_m" ? lim.budget_tokens && lim.budget_tokens / 1e6 : key === "ram_gb" ? lim.ram_mb && lim.ram_mb / 1024 : key === "vram_gb" ? lim.vram_mb && lim.vram_mb / 1024 : lim[key];
    input.value = v != null ? String(Math.round(v * 100) / 100) : "";
  }
  document.getElementById("limits-note").textContent = mother.usage && mother.usage.held
    ? `${mother.usage.held} worker${mother.usage.held === 1 ? "" : "s"} waiting for room under these limits.`
    : "At a limit the queen starts no new worker; running ones go on.";
}

document.getElementById("limits-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const limits = {};
  for (const input of e.target.querySelectorAll("input[data-limit]")) {
    const v = parseFloat(input.value);
    if (!(v > 0)) continue;
    const key = input.dataset.limit;
    if (key === "budget_m") limits.budget_tokens = Math.round(v * 1e6);
    else if (key === "ram_gb") limits.ram_mb = v * 1024;
    else if (key === "vram_gb") limits.vram_mb = v * 1024;
    else limits[key] = v;
  }
  const d = await post("/api/queen", { action: "limits", limits });
  if (!d.ok) { tell(d.error || "Could not set the limits."); return; }
  document.activeElement && document.activeElement.blur();
  await poll();
});

function renderBoardTargets() {
  const sel = document.getElementById("board-to");
  if (!sel) return;
  const active = ((mother && mother.dispatch) || []).filter((j) => j.kind === "task"
    && !["done", "failed", "stopped", "stopped early"].includes(j.status));
  const sig = active.map((j) => j.priority).join(",");
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  const keep = sel.value;
  sel.textContent = "";
  sel.appendChild(new Option("all workers", "all"));
  for (const j of active) sel.appendChild(new Option(`W${j.priority} ${j.title || ""}`.slice(0, 40), `W${j.priority}`));
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

document.getElementById("board-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = document.getElementById("board-text");
  const note = document.getElementById("board-note");
  if (!text.value.trim()) return;
  const d = await post("/api/say", { to: document.getElementById("board-to").value, text: text.value, as: "user" });
  note.textContent = d.ok ? "Sent." : (d.error || "Could not send.");
  if (d.ok) text.value = "";
  await poll();
});

document.getElementById("queen-model").addEventListener("change", async (e) => {
  if (e.target.value === "__other__") await pickOtherModel(e.target);
  const d = await post("/api/queen", { action: "director", model: e.target.value });
  if (!d.ok) { tell(d.error || "Could not change the queen's model."); await poll(); return; }
  flash("The queen's model is changed; it applies from now on");
  await poll();
});
function renderDirector() {
  const sel = document.getElementById("queen-model");
  const want = mother && mother.queen_terminal_agent ? `agent:${mother.queen_terminal_agent}`
    : mother && mother.director_agent === "codex"
    ? (mother.director_codex_model ? `codex:${mother.director_codex_model}` : "codex") : (mother && mother.director_model) || "sonnet";
  if (document.activeElement !== sel && sel.dataset.want !== want + "|" + ((mother && mother.providers) || []).length) {
    fillQueenModels(sel, want);
    sel.dataset.want = want + "|" + ((mother && mother.providers) || []).length;
  }
  const dir = mother && mother.director;
  const line = document.getElementById("director-line");
  if (!mother || !mother.director_on) { line.textContent = ""; return; }
  line.textContent = !dir ? "She starts looking in as soon as a worker bee is added."
    : `Look-ins · ${dir.looks} look${dir.looks === 1 ? "" : "s"} · ${fmtTok(dir.tokens)} tokens`
      + (dir.busy ? " · looking in now…" : ` · next look in ${dir.next_in}s (only if something changed)`)
      + (dir.summary ? ` — last: ${dir.summary}` : "");
}
// The task the user gave this queen: always shown in her panel (three lines, "Show all" for the rest), editable.
let taskEditing = false, taskOpen = false;
function renderTask() {
  const text = (mother && (mother.description || mother.instruction)) || "";
  const box = document.getElementById("qt-text");
  if (!taskEditing) {
    box.textContent = text || "No task written yet. Click Edit to write what this queen should get done; it stays here for reference.";
    box.classList.toggle("empty", !text);
    box.classList.toggle("clamp", !taskOpen);
    const more = document.getElementById("qt-more");
    more.hidden = !text || (!taskOpen && box.scrollHeight <= box.clientHeight + 2 && text.length < 260);
    more.textContent = taskOpen ? "Show less" : "Show all";
  }
  document.getElementById("qt-edit").hidden = taskEditing;
  document.getElementById("qt-form").hidden = !taskEditing;
  box.hidden = taskEditing;
}
document.getElementById("qt-more").addEventListener("click", () => { taskOpen = !taskOpen; renderTask(); });
document.getElementById("qt-edit").addEventListener("click", () => {
  taskEditing = true;
  const area = document.getElementById("qt-area");
  area.value = (mother && (mother.description || mother.instruction)) || "";
  renderTask();
  area.focus();
});
document.getElementById("qt-cancel").addEventListener("click", () => { taskEditing = false; renderTask(); });
document.getElementById("qt-area").addEventListener("input", (e) => {
  document.getElementById("qt-count").textContent = `${e.target.value.length} / 4000`;
});
document.getElementById("qt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = await post("/api/queen", { action: "describe", queen: mother.queen, text: document.getElementById("qt-area").value });
  if (!d.ok) { tell(d.error || "Could not save the task."); return; }
  taskEditing = false;
  await poll();
});

function renderQueenLog() {
  renderTask();
  renderDirector();
  renderBoardTargets();
  renderDeliveryCheck();
  const host = document.getElementById("queen-log");
  const jobs = (mother && mother.dispatch) || [];
  const lines = [];
  const said = (mother.plan && mother.plan.instruction) || mother.instruction;
  for (const j of jobs) for (const [t, text] of j.events || []) lines.push([t, `W${j.priority}`, text]);
  for (const c of mother.commands || []) lines.push([c.ts, c.text ? "you" : "queen", c.text || c.queen]);
  for (const [t, text] of (mother.sweep && mother.sweep.log) || []) lines.push([t, "", text]);
  lines.sort((a, b) => a[0] - b[0]);
  const sig = JSON.stringify([said, lines.length, lines.length && lines[lines.length - 1], mother.commanding]);
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  const atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 40;
  host.textContent = "";
  if (said) {
    const you = el("div", "ci user");
    you.appendChild(el("div", "who", "You"));
    you.appendChild(el("div", "txt", said));
    host.appendChild(you);
  }
  if (!lines.length && !said) host.appendChild(el("div", "muted", "Add worker bees with + Worker bee and tell each its task; she looks in on them, settles what they disagree on, approves what is safe and checks what they report. Or open Planner mode below and she splits a job herself."));
  for (const [t, who, text] of lines.slice(-150)) {
    const row = el("div", `log-row${who === "you" ? " you" : who === "queen" ? " queen" : ""}`);
    row.appendChild(el("span", "log-t", new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
    if (who) row.appendChild(el("span", "log-who", who));
    row.appendChild(el("span", "log-text", showIds(text)));
    host.appendChild(row);
  }
  if (mother.commanding) host.appendChild(el("div", "log-now", `${mother.queen_name} is ${mother.commanding}…`));
  if (atBottom || !host.dataset.seen) host.scrollTop = host.scrollHeight;
  host.dataset.seen = "1";
}

/* ---- dialogs: create a queen, add a worker -------------------------------- */

let dialogMode = null;

function fillModels(agent, current) {
  const sel = document.getElementById("dlg-model");
  sel.textContent = "";
  // The workers' model can be left to the queen: she rates each part and picks the least capable model that fits.
  sel.appendChild(new Option("Let the queen advise", "auto", false, current === "auto"));
  if (agent === "auto") return;
  const p = ((mother && mother.providers) || []).find((x) => x.id === agent);
  for (const m of (p && p.models) || []) {
    sel.appendChild(new Option(agentModelLabel(agent, m), m, false, m === current));
  }
  sel.appendChild(new Option("Other model id…", "__other__"));
  sel.dataset.last = sel.value;
}
document.getElementById("dlg-model").addEventListener("change", (e) => {
  if (e.target.value === "__other__") pickOtherModel(e.target); else e.target.dataset.last = e.target.value;
});

let dlgMenu = null;
function dialogMenu() {
  const agentSel = document.getElementById("dlg-agent"), modelSel = document.getElementById("dlg-model");
  if (!dlgMenu) {
    const row = agentSel.closest(".dlg-row");
    for (const l of row.querySelectorAll("label")) l.hidden = true;
    const label = el("label", "", "Agent and version ");
    dlgMenu = agentMenu(
      () => {
        const out = [];
        for (const p of (mother && mother.providers) || []) {
          for (const m of p.models || []) out.push({ agent: p.id, value: `${p.id}:${m}`, label: agentModelLabel(p.id, m), disabled: !p.installed });
        }
        return out;
      },
      () => {
        if (agentSel.value === "auto") return { value: "auto", label: "Let the queen advise" };
        return { value: `${agentSel.value}:${modelSel.value}`, label: `${agentModelLabel(agentSel.value, modelSel.value)}` };
      },
      (x) => {
        const [agent, model] = [x.agent, x.value.slice(x.agent.length + 1)];
        agentSel.value = agent;
        fillModels(agent, model);
        modelSel.value = model;
        modelSel.dataset.last = model;
      },
      { label: "Let the queen advise", pick: () => { agentSel.value = "auto"; fillModels("auto", "auto"); } });
    label.appendChild(dlgMenu);
    row.appendChild(label);
  }
  dlgMenu.refresh();
}

function openDialog(mode) {
  dialogMode = mode;
  const queen = mode === "queen";
  const free = mode === "free";
  document.getElementById("dialog-title").textContent = queen ? "Create a queen bee"
    : free ? "Create a free worker" : `Add a worker bee to ${mother.queen_name}`;
  document.getElementById("dlg-name-row").hidden = false;
  document.getElementById("dlg-name-row").firstChild.textContent = queen ? "Name " : "Worker name (short, shown on the map) ";
  document.getElementById("dlg-desc-row").hidden = !queen;
  document.getElementById("dlg-desc").value = "";
  document.getElementById("dlg-limits").hidden = !queen;
  document.getElementById("dlg-queen-model-row").hidden = !queen;
  fillQueenModels(document.getElementById("dlg-queen-model"), "sonnet");
  document.getElementById("dlg-mode-row").hidden = !queen;
  document.getElementById("dlg-mode").value = "guard";
  document.getElementById("dlg-check-row").hidden = queen;
  document.getElementById("dlg-check").value = "";
  const codexOk = ((mother && mother.providers) || []).some((p) => p.id === "codex" && p.installed);
  for (const o of document.querySelectorAll("#dlg-queen-model option[value=codex]")) {
    o.disabled = !codexOk;
    o.textContent = codexOk ? "Codex (directs through the Codex CLI)" : "Codex (install Codex to use it)";
  }
  applyMode();
  document.getElementById("dlg-workers").value = "";
  document.getElementById("dlg-budget").value = "";
  document.getElementById("dlg-name").value = "";
  document.getElementById("dlg-name").placeholder = queen ? "e.g. Web app" : "e.g. Search";
  document.getElementById("dlg-text-label").textContent = queen ? "What should this queen get done?" : "The worker's task";
  const text = document.getElementById("dlg-text");
  text.value = "";
  text.placeholder = queen ? "e.g. Build a small web shop: product list, cart and a checkout API."
                           : "e.g. Make the header sticky and add a dark mode toggle.";
  const jobs = (mother && mother.dispatch) || [];
  let folder = "";
  try { folder = localStorage.getItem("mother.folder") || ""; } catch { /* private mode */ }
  if (!queen && jobs.length) folder = jobs[jobs.length - 1].project_path || folder;
  document.getElementById("dlg-folder").value = folder;
  const agents = document.getElementById("dlg-agent");
  agents.textContent = "";
  const provs = (mother && mother.providers) || [];
  const want = !queen && mother.queen_agent ? mother.queen_agent : "claude";
  agents.appendChild(new Option("Let the queen advise (Claude or Codex, per part)", "auto", false, want === "auto"));
  for (const p of provs) {
    const o = new Option(p.installed ? p.name : `${p.name} (not installed)`, p.id, false, p.id === want);
    o.disabled = !p.installed || !p.models.length;
    agents.appendChild(o);
  }
  if (free) {                                    // no queen to advise: a real agent, Codex first
    agents.querySelector('option[value="auto"]').remove();
    const pick = [...agents.options].find((o) => o.value === "codex" && !o.disabled) || [...agents.options].find((o) => !o.disabled);
    if (pick) agents.value = pick.value;
  }
  fillModels(agents.value, !queen && !free && mother.queen_model ? mother.queen_model
    : agents.value === "claude" ? ((mother.roles && mother.roles.worker_model) || "haiku") : "default");
  dialogMenu();
  document.getElementById("dlg-ok").textContent = queen ? "Create queen" : free ? "Create free worker" : "Add worker";
  if (free) document.getElementById("dlg-text-label").textContent = "What should this worker do?";
  document.getElementById("dlg-error").textContent = "";
  document.getElementById("dialog").hidden = false;
  (queen ? document.getElementById("dlg-name") : text).focus();
}

// A guard queen has no instruction of her own: the user adds the worker bees and tells each its task.
function applyMode() {
  const queen = dialogMode === "queen";
  const guard = queen && document.getElementById("dlg-mode").value === "guard";
  for (const id of ["dlg-text-row", "dlg-folder-row"]) document.getElementById(id).hidden = guard;
  document.getElementById("dlg-desc-row").hidden = !guard;
  document.getElementById("dlg-text").required = !guard;
  document.getElementById("dlg-folder").required = !guard;
  document.getElementById("dlg-queen-model-row").hidden = !queen;             // the director's own model
  for (const el_ of document.querySelectorAll("#dialog .dlg-row")) {
    if (el_.id === "dlg-queen-model-row" || el_.id === "dlg-limits") continue;
    el_.hidden = guard;                                                        // the workers' agent/model: asked per worker
  }
  document.getElementById("dlg-ok").textContent = queen ? (guard ? "Create queen" : "Create queen and plan") : "Add worker";
}
document.getElementById("dlg-mode").addEventListener("change", applyMode);

function closeDialog() { document.getElementById("dialog").hidden = true; dialogMode = null; }

function openAddWorker() { openDialog("worker"); }

const startState = { agent: false, demo: false, queen: false };
function renderStart() {
  const card = document.getElementById("start-card");
  let dismissed = false;
  try { dismissed = localStorage.getItem("mother.start") === "done"; } catch { /* private mode */ }
  const all = startState.agent && startState.demo && startState.queen;
  if (all) { try { localStorage.setItem("mother.start", "done"); } catch { /* private mode */ } }
  card.hidden = dismissed || all;
  for (const [id, ok] of [["start-agent", startState.agent], ["start-demo", startState.demo], ["start-queen", startState.queen]]) {
    document.getElementById(id).classList.toggle("done", !!ok);
  }
}
document.getElementById("start-dismiss").addEventListener("click", () => {
  try { localStorage.setItem("mother.start", "done"); } catch { /* private mode */ }
  document.getElementById("start-card").hidden = true;
});
document.getElementById("create-queen").addEventListener("click", () => openDialog("queen"));
document.getElementById("create-free").addEventListener("click", () => openDialog("free"));
for (const [id, kind] of [["demo-btn", "notes"]]) document.getElementById(id).addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Setting up the demo…";
  try {
    const d = await post("/api/demo", { kind });
    if (!d.ok) { tell(d.error || "Could not set up the demo."); return; }
    view.touched = false;
    await selectQueen(d.queen);
    await poll();
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});

// The delivery check: one test command the queen runs over the whole folder when every worker is in.
document.getElementById("check-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = await working("Saving the delivery check…", () => post("/api/verify", { command: document.getElementById("check-cmd").value }),
                          "Delivery check saved");
  document.getElementById("check-note").textContent = d.ok ? "Saved." : (d.error || "Could not save it.");
  await poll();
});
function renderDeliveryCheck() {
  const v = mother && mother.verify;
  const input = document.getElementById("check-cmd");
  if (document.activeElement !== input) input.value = (v && v.command) || "";
  const text = !v ? "Optional. One result for the whole delivery."
    : v.state === "waiting" ? "Runs when every worker is done."
    : v.state === "running" ? "Running now…"
    : v.state === "passed" ? "✓ Delivered: the whole project's check passes."
    : v.state === "failed" ? "✗ Still failing after her fixes; see the log."
    : v.state === "fixing" ? "Failed; a worker is making the parts fit…" : v.state;
  const note = document.getElementById("check-note");
  if (!note.dataset.hold || Date.now() > +note.dataset.hold) note.textContent = text;
}
document.getElementById("dlg-agent").addEventListener("change", (e) => fillModels(e.target.value,
  e.target.value === "claude" ? "haiku" : "default"));
document.getElementById("dlg-cancel").addEventListener("click", closeDialog);
document.getElementById("dialog").addEventListener("click", (e) => { if (e.target.id === "dialog") closeDialog(); });
document.getElementById("dialog-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("dlg-error");
  const text = document.getElementById("dlg-text").value.trim();
  const folder = document.getElementById("dlg-folder").value.trim();
  const agent = document.getElementById("dlg-agent").value;
  const model = document.getElementById("dlg-model").value;
  const guardQueen = dialogMode === "queen" && document.getElementById("dlg-mode").value === "guard";
  if (!guardQueen && (!text || !folder)) { err.textContent = "Fill in the text and the folder."; return; }
  try { localStorage.setItem("mother.folder", folder); } catch { /* private mode */ }
  const ok = document.getElementById("dlg-ok");
  ok.disabled = true;
  try {
    if (dialogMode === "queen") {
      const limits = {};
      const w = parseFloat(document.getElementById("dlg-workers").value);
      const b = parseFloat(document.getElementById("dlg-budget").value);
      if (w > 0) limits.workers = w;
      if (b > 0) limits.budget_tokens = Math.round(b * 1e6);
      // The queen's own thinking and the strong worker run on the model chosen here.
      // The queen's own model is the user's choice. The workers' agent and model are the user's too,
      // or "Let the queen advise" (agent "auto" / model "auto"): then she picks per part.
      let qm = document.getElementById("dlg-queen-model").value;
      const termAgent = qm.startsWith("agent:") ? qm.slice(6) : "";   // Gemini, Kimi…: her terminal; the rest stays on Claude
      if (termAgent) qm = "sonnet";
      const viaCodex = qm === "codex" || qm.startsWith("codex:");   // Codex as the queen: it directs; Claude names stay for the rest
      const codexModel = qm.startsWith("codex:") ? qm.slice(6) : "";
      if (viaCodex) qm = "sonnet";
      const roles = { planner_model: qm, architect_model: qm, strong_model: qm, ...(termAgent ? { queen_agent: termAgent } : {}),
                      ...(viaCodex ? { director_agent: "codex", ...(codexModel ? { director_model: codexModel } : {}) } : {}) };
      const d = await post("/api/queen", { action: "add", name: document.getElementById("dlg-name").value,
                                           agent: guardQueen ? "" : agent, model: guardQueen ? "" : model, limits,
                                           roles,
                                           description: guardQueen ? document.getElementById("dlg-desc").value : "",
                                           director: true });
      if (!d.ok) { err.textContent = d.error || "Could not create the queen."; return; }
      closeDialog();
      view.touched = false;
      await selectQueen(d.queen);
      openQueen();
      if (guardQueen) return;                                // she waits for the user to add worker bees
      document.getElementById("instruction").value = text;
      document.getElementById("folder").value = folder;
      document.getElementById("plan-btn").click();           // the new queen plans and starts on its own
    } else if (dialogMode === "free") {
      const check = document.getElementById("dlg-check").value.trim();
      closeDialog();
      const d = await working("Starting the free worker…", () => post("/api/queen", { action: "free", name: document.getElementById("dlg-name").value,
                                                                                    agent, model, folder, goal: text, check }),
                              "The free worker is running");
      if (!d.ok) return;
      view.touched = false;
      await selectQueen(d.queen);
      await poll();
    } else {
      const check = document.getElementById("dlg-check").value.trim();
      closeDialog();
      const d = await working("Adding the worker…", () => post("/api/worker", { goal: text, folder, agent, model,
                                                                                 title: document.getElementById("dlg-name").value,
                                                                                 acceptance: check ? [{ type: "command", run: check }] : [] }),
                              "The worker is added");
      if (!d.ok) return;
      await poll();
    }
  } finally {
    ok.disabled = false;
  }
});

/* ---- notices: what happened, as it happens -------------------------------- */

const seenFeed = new Map();            // queen -> last event time shown

// What deserves a notice, in a few words: a check passed or failed, a worker stuck, failed or moved up, the queen's
// delivery check. The rest (the workers talking, an agent starting) goes to the bell's list only.
function noticeOf(text) {
  const t = String(text || "");
  const after = (re) => (t.replace(re, "").split("\n").find((l) => l.trim()) || "").replace(/^\s*-\s*/, "");
  let m;
  if (/^acceptance passed|^done: its checks pass now/.test(t)) return ["good", "Passed its check", ""];
  if (/^acceptance failed/.test(t)) return ["warn", "Check failed, sent back", after(/^acceptance failed[^:]*:\s*/)];
  if (/^the queen checked the whole package/.test(t)) return ["good", "Delivered: the whole check passes", ""];
  if (/^the queen's check still fails|^the delivery check was not run/.test(t)) return ["bad", "Delivery check failed", t];
  if (/^failed; needs you/.test(t)) return ["bad", "Failed, needs you", after(/^failed; needs you:\s*/)];
  if (/^failed:/.test(t)) return ["bad", "Failed", after(/^failed:\s*/)];
  if (/^reported blocked/.test(t)) return ["warn", "Blocked by another part", ""];
  if ((m = /^moved up to (\S+?):/.exec(t))) return ["info", `Moved up to ${modelName(m[1])}`, ""];
  if (/^moved from codex to claude/.test(t)) return ["info", "Moved to Claude Code", ""];
  if (/^held by the queen's limit/.test(t)) return ["warn", "Held by a limit", after(/^held by the queen's limit:\s*/)];
  if (/^stopped early/.test(t)) return ["warn", "Stopped early", after(/^stopped early:\s*/)];
  if (/^(sweep|branch) finished/.test(t)) return ["good", t.split(":")[0].replace(/^./, (c) => c.toUpperCase()), ""];
  return null;
}

const NOTICE_ICON = { good: "M7.5 12.5l3 3 6-6.5", warn: "M12 7.5v5.5M12 16.2v.3", bad: "M8.8 8.8l6.4 6.4M15.2 8.8l-6.4 6.4",
                      info: "M12 16.5V11M12 7.8v.3" };
function noticeIcon(kind) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "nt-ico");
  svg.innerHTML = `<circle cx="12" cy="12" r="9"/><path d="${NOTICE_ICON[kind] || NOTICE_ICON.info}"/>`;
  return svg;
}
const clockOf = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const whoOf = (q, prio, title) => `${q.name}${prio ? ` · W${prio}` : ""}${title ? " " + title : ""}`;

function openNotice(q, prio) {
  return async () => {
    await selectQueen(q.id);
    const job = prio && ((mother && mother.dispatch) || []).find((j) => j.priority === prio);
    if (job) openWorker(job); else openQueen();
  };
}

function notify(q, row, kind, head, body) {
  const [ts, prio, title] = row;
  const host = document.getElementById("toasts");
  const t = el("div", `toast notice k-${kind}`);
  t.setAttribute("role", "status");
  t.appendChild(noticeIcon(kind));
  const main = el("div", "nt-main");
  const top = el("div", "nt-top");
  top.appendChild(el("span", "nt-head", head));
  top.appendChild(el("span", "nt-time", clockOf(ts)));
  main.appendChild(top);
  main.appendChild(el("div", "nt-who", whoOf(q, prio, title)));
  if (body) main.appendChild(el("div", "nt-body", showIds(body)));
  t.appendChild(main);
  const x = el("button", "nt-x", "×");
  x.type = "button";
  x.title = "Dismiss";
  t.appendChild(x);
  const go = openNotice(q, prio);
  let timer = 0;
  const leave = () => { t.classList.add("gone"); setTimeout(() => t.remove(), 300); };
  const arm = () => { clearTimeout(timer); timer = setTimeout(leave, kind === "bad" ? 12000 : 6000); };
  t.addEventListener("mouseenter", () => clearTimeout(timer));    // read at the user's pace
  t.addEventListener("mouseleave", arm);
  x.addEventListener("click", (e) => { e.stopPropagation(); leave(); });
  t.addEventListener("click", () => { t.remove(); go(); });
  host.appendChild(t);
  for (const old of [...host.querySelectorAll(".toast.notice")].slice(0, -3)) old.remove();
  arm();
}

// The bell: everything that happened, newest first, with the unread ones counted.
const bellRows = [];
let bellSeen = 0;
function bellAdd(q, row, notice) {
  bellRows.unshift({ q, row, notice });
  if (bellRows.length > 100) bellRows.length = 100;
}
function renderBell() {
  const unread = bellRows.filter((r) => r.row[0] > bellSeen).length;
  const n = document.getElementById("bell-n");
  n.hidden = !unread;
  n.textContent = unread > 99 ? "99+" : String(unread);
  const panel = document.getElementById("bell-panel");
  if (panel.hidden) return;
  const list = document.getElementById("bell-list");
  list.textContent = "";
  if (!bellRows.length) list.appendChild(el("div", "bp-empty", "Nothing yet. What the queens and their workers do shows up here."));
  for (const { q, row, notice } of bellRows) {
    const [ts, prio, title, text] = row;
    const item = el("button", `bp-item${notice ? " k-" + notice[0] : ""}`);
    item.type = "button";
    const top = el("div", "nt-top");
    top.appendChild(el("span", "bp-who", whoOf(q, prio, title)));
    top.appendChild(el("span", "nt-time", clockOf(ts)));
    item.appendChild(top);
    item.appendChild(el("div", "bp-text", notice ? notice[1] + (notice[2] ? ": " + showIds(notice[2]) : "") : showIds(text)));
    item.addEventListener("click", () => { panel.hidden = true; openNotice(q, prio)(); });
    list.appendChild(item);
  }
}
document.getElementById("bell").addEventListener("click", (e) => {
  e.stopPropagation();
  const panel = document.getElementById("bell-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) renderBell();
  bellSeen = Math.max(bellSeen, ...bellRows.map((r) => r.row[0]), 0);
  renderBell();
});
document.getElementById("bell-panel").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => { document.getElementById("bell-panel").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.getElementById("bell-panel").hidden = true; });

function renderNotices() {
  for (const q of queens) {
    const feed = q.feed || [];
    const last = feed.length ? feed[feed.length - 1][0] : 0;
    const since = seenFeed.has(q.id) ? seenFeed.get(q.id) : null;
    // The queen's word to all her workers is written on each of them: one row for her, not one per worker.
    const count = new Map();
    for (const row of feed) count.set(`${row[0]}|${row[3]}`, (count.get(`${row[0]}|${row[3]}`) || 0) + 1);
    const shown = new Set();
    for (let row of feed) {
      const text = String(row[3]), key = `${row[0]}|${text}`;
      if (text.startsWith("progress:") || (since !== null && row[0] <= since) || shown.has(key)) continue;
      shown.add(key);
      if (count.get(key) > 1) row = [row[0], null, "", text];
      const notice = noticeOf(text);
      bellAdd(q, row, notice);
      if (since !== null && notice) notify(q, row, ...notice);          // no flood on opening
    }
    if (since === null) bellSeen = Math.max(bellSeen, last);
    seenFeed.set(q.id, Math.max(since || 0, last));
  }
  bellRows.sort((a, b) => b.row[0] - a.row[0]);
  renderBell();
}

/* ---- the result card: the run's facts as a few lines to paste anywhere ----------------------------------- */

document.getElementById("card-copy").addEventListener("click", async () => {
  const btn = document.getElementById("card-copy");
  const pre = document.getElementById("result-card");
  let text = "";
  try {
    const r = await fetch(`/api/card?queen=${encodeURIComponent(currentQueen)}`, { cache: "no-store" });
    text = ((await r.json()).text || "").trim();
  } catch { text = ""; }
  pre.textContent = text || "The card is written from the run's facts once the team has stopped.";
  pre.hidden = false;
  let copied = false;
  if (text) {
    try { await navigator.clipboard.writeText(text); copied = true; } catch { copied = false; }
  }
  if (!copied && text) {
    const range = document.createRange();
    range.selectNodeContents(pre);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  btn.textContent = copied ? "Copied" : "Copy the result card";
  if (copied) setTimeout(() => { btn.textContent = "Copy the result card"; }, 2000);
});
