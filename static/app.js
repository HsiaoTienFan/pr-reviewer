/* PR Reviewer frontend — three screens rendered from the live API. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
// Inline markdown for LLM-written card text: escape FIRST (the text derives from
// attacker-controlled PR content), then allow only `code` and **bold** through.
const md = (s) => esc(s)
  .replace(/`([^`\n]+)`/g, "<code class=\"mdc\">$1</code>")
  .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");

// Block-level markdown for the /code-review report: headings, fenced code,
// bullet/numbered lists, paragraphs. Same safety model as md() — escape first,
// build markup only from our own transforms. No raw HTML, no links.
function mdBlock(src) {
  const lines = String(src ?? "").split("\n");
  const out = [];
  let i = 0, para = [], list = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${md(para.join(" "))}</p>`); para = []; } };
  const flushList = () => {
    if (!list) return;
    const tag = list.ol ? "ol" : "ul";
    out.push(`<${tag}>${list.items.map((it) => `<li>${md(it)}</li>`).join("")}</${tag}>`);
    list = null;
  };
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) {
      flushPara(); flushList();
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre class="md-code">${esc(buf.join("\n"))}</pre>`);
      continue;
    }
    const h = l.match(/^(#{1,4})\s+(.*)$/);
    if (h) { flushPara(); flushList(); out.push(`<div class="md-h md-h${h[1].length}">${md(h[2])}</div>`); i++; continue; }
    const ul = l.match(/^\s*[-*]\s+(.*)$/);
    if (ul) { flushPara(); if (list?.ol) flushList(); (list ??= { ol: false, items: [] }).items.push(ul[1]); i++; continue; }
    const ol = l.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) { flushPara(); if (list && !list.ol) flushList(); (list ??= { ol: true, items: [] }).items.push(ol[1]); i++; continue; }
    if (!l.trim()) { flushPara(); flushList(); i++; continue; }
    if (list && /^\s{2,}/.test(l)) { list.items[list.items.length - 1] += " " + l.trim(); i++; continue; }
    flushList(); para.push(l.trim()); i++;
  }
  flushPara(); flushList();
  return `<div class="md-report">${out.join("")}</div>`;
}
// The report cites files by sandbox-absolute path; show repo-relative instead.
const cleanReport = (t) => String(t ?? "").replace(/\/[^\s`)]*\/\.pr-reviewer\/sandbox\/(?:repo\/)?/g, "");

const state = {
  settings: null,      // masked config
  claude: null,        // backend status
  prs: null,           // /api/prs payload
  review: null,        // currently open review
  testNotes: {},       // section → {ok, message}
  pollTimer: null,
  bugsJobs: {},        // review id → in-flight code-review job id
  bugsWatch: {},       // review id → interval handle
  addReqJobs: {},      // review id → in-flight add-requirement job id
  addReqWatch: {},     // review id → interval handle
};

/* ---------------------------------------------------------------- api */

const API_TIMEOUT_MS = 45_000;

async function api(path, opts = {}) {
  // Without this, a stalled request never settles and the caller's catch never
  // runs — the screen sits on "Loading…" forever with no way to recover.
  const { timeoutMs = API_TIMEOUT_MS, ...rest } = opts;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      signal: ctl.signal,
      ...rest,
      body: rest.body ? JSON.stringify(rest.body) : undefined,
    });
  } catch (e) {
    throw e.name === "AbortError"
      ? new Error(`request timed out after ${Math.round(timeoutMs / 1000)}s`)
      : e;
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

/* ---------------------------------------------------------------- toasts */

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), isErr ? 6000 : 3000);
}

/* ---------------------------------------------------------------- routing */

function show(screenId) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("visible"));
  $("#screen-" + screenId).classList.add("visible");
  document.querySelectorAll(".navlink").forEach((n) =>
    n.classList.toggle("active", n.dataset.nav === screenId));
  window.scrollTo(0, 0);
}

/* ---------------------------------------------------------------- helpers */

function relTime(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function ticketBadge(t) {
  const cls = t.source === "jira" ? "jira" : (t.source === "linear" ? "linear" : "ticket");
  const icon = cls === "jira" ? "JIRA " : "◆ ";
  return `<span class="badge ${cls}">${icon}${esc(t.key)}</span>`;
}

function srcBadge(source) {
  if (source === "pr-description") return `<span class="badge prdesc">PR description</span>`;
  if (source === "pr-discussion") return `<span class="badge prdesc">PR discussion</span>`;
  if (source === "reviewer") return `<span class="badge none">added by reviewer</span>`;
  const [kind, key] = source.split(":");
  return ticketBadge({ source: kind, key: key || source });
}

/* ---- requirement identity system (DESIGN.md §7.2): stable color per id ---- */

const REQ_PALETTE = ["#0969da", "#0d9488", "#8250df", "#d1247f", "#cf5b00", "#1a7f37", "#6639ba", "#bf3989", "#0550ae", "#7d4e00"];
const UNX_PALETTE = ["#b98300", "#9a6700", "#d4a72c", "#845306"];

const SEV_COLORS = {
  blocker: "#cf222e", major: "#bc4c00", minor: "#9a6700", nit: "#59636e",
  high: "#cf222e", medium: "#9a6700", low: "#59636e", // legacy, pre-migration
};
// severity = how much it matters; category = what kind of issue it is
const SEV_ORDER = ["blocker", "major", "minor", "nit"];
const SEV_LABEL = { blocker: "Blocker", major: "Major", minor: "Minor", nit: "Nit" };
const SEV_BLURB = {
  blocker: "must fix before merge",
  major: "real risk — should fix",
  minor: "worth addressing",
  nit: "optional polish",
};
const CAT_ORDER = ["correctness", "security", "performance", "testing",
                   "maintainability", "style", "docs", "other"];
const CAT_LABEL = {
  correctness: "Correctness", security: "Security", performance: "Performance",
  testing: "Testing", maintainability: "Maintainability", style: "Style",
  docs: "Docs", other: "Other",
};

function colorFor(id) {
  const r = state.review;
  if (!r) return "#59636e";
  if (id.startsWith("B")) {
    const b = (r.bugs || []).find((x) => x.id === id);
    return SEV_COLORS[b?.severity || "low"];
  }
  if (id.startsWith("R")) {
    const i = r.requirements.findIndex((q) => q.id === id);
    return REQ_PALETTE[(i >= 0 ? i : 0) % REQ_PALETTE.length];
  }
  const i = r.unexplained.findIndex((u) => u.id === id);
  // explain mode: changes are the primary cards — use the main palette
  const palette = r.mode === "explain" ? REQ_PALETTE : UNX_PALETTE;
  return palette[(i >= 0 ? i : 0) % palette.length];
}

function tagsFor(file, line) {
  const r = state.review, out = [];
  for (const l of r.links)
    if (l.anchors.some((a) => a.file === file && line >= a.start && line <= a.end)) out.push(l.requirement_id);
  for (const u of r.unexplained)
    if (u.anchors.some((a) => a.file === file && line >= a.start && line <= a.end)) out.push(u.id);
  for (const b of r.bugs || [])
    if (b.anchors.some((a) => a.file === file && line >= a.start && line <= a.end)) out.push(b.id);
  return out;
}

function textFor(id) {
  const r = state.review;
  if (!r) return "";
  return r.requirements.find((q) => q.id === id)?.text
    || r.unexplained.find((u) => u.id === id)?.label
    || (r.bugs || []).find((b) => b.id === id)?.title || "";
}

/* ---------------------------------------------------------------- claude nav status */

async function refreshClaude(full = false) {
  try {
    // this one shells out to the claude CLI (and with ?full=true does a real
    // round-trip), so it needs far longer than the default budget
    state.claude = await api("/api/claude/status" + (full ? "?full=true" : ""),
                             { timeoutMs: full ? 300_000 : 120_000 });
  } catch (e) {
    state.claude = { ready: false, checks: [], summary: "unreachable", fix: String(e.message) };
  }
  const dot = $("#nav-claude-dot"), txt = $("#nav-claude-text");
  dot.className = "status-dot " + (state.claude.ready ? "ok" : "err");
  txt.textContent = state.claude.ready ? "Claude · subscription" : `Claude · ${state.claude.summary || "not ready"}`;
  if ($("#screen-settings").classList.contains("visible")) renderSettings();
}

/* ================================================================ command center */

async function loadPRs() {
  if (state.prsLoading) return;  // don't stack requests behind a slow one
  state.searchActive = false;
  state.prsLoading = true;
  try {
    state.prs = await api("/api/prs");
  } catch (e) {
    $("#cc-content").innerHTML = `<div class="error-banner">Failed to load PRs: ${esc(e.message)}
      <button class="btn small" style="margin-left:10px" data-retry-prs>Retry</button></div>`;
    return;
  } finally {
    state.prsLoading = false;
  }
  renderCC();
}

function reviewStateHtml(pr) {
  const r = pr.review;
  if (!r) {
    const detail = pr.tickets.length ? "" : "will run in explain mode";
    return `<span class="pill idle">Not reviewed</span>${detail ? `<span class="rs-detail">${detail}</span>` : ""}`;
  }
  if (r.mode === "explain") {
    return `<span class="pill info">Explained</span>
      <span class="rs-detail">${r.unexplained} change${r.unexplained === 1 ? "" : "s"} annotated · ${r.verified}/${r.claims} verified</span>`;
  }
  const cls = r.fulfilled === r.total ? "ok" : (r.fulfilled > 0 || r.partial > 0 ? "warn" : "err");
  const mark = cls === "ok" ? "✓" : (cls === "warn" ? "◐" : "✗");
  const parts = [];
  if (r.partial) parts.push(`${r.partial} partial`);
  if (r.gaps) parts.push(`${r.gaps} gap${r.gaps === 1 ? "" : "s"}`);
  if (r.unexplained) parts.push(`${r.unexplained} unexplained`);
  if (r.verified) parts.push(`${r.verified}/${r.claims} claims verified`);
  return `<span class="pill ${cls}">${mark} ${r.fulfilled}/${r.total} fulfilled</span>
    <span class="rs-detail">${esc(parts.join(" · ") || "no claims verified yet")}</span>`;
}

function renderCC() {
  const { groups, configured } = state.prs;
  const host = (p) => (p === "github" ? "github.com" : "bitbucket.org");
  const filter = state.ccFilter || "all";
  const matches = (pr) =>
    filter === "all" || (filter === "assigned" ? pr.role === "assigned" : pr.role === "mine");
  let html = "";

  if (!groups.length) {
    const anyConfigured = configured.github || configured.bitbucket;
    html = `<div class="empty-state">
      ${anyConfigured
        ? "No repos selected yet — add repos to show PRs you authored or that await your review."
        : "No PR hosts connected yet — connect GitHub or Bitbucket to list your PRs."}
      <br><button class="btn" data-nav="settings">Open Settings</button>
      <div style="margin-top:10px;font-size:12px">…or search / paste a PR URL above.</div>
    </div>`;
    $("#cc-content").innerHTML = html;
    return;
  }

  const all = groups.flatMap((g) => g.prs);
  const counts = {
    all: all.length,
    assigned: all.filter((p) => p.role === "assigned").length,
    mine: all.filter((p) => p.role === "mine").length,
  };
  html += `<div class="filterbar">
    <span class="filter ${filter === "all" ? "active" : ""}" data-cc-filter="all">All · ${counts.all}</span>
    <span class="filter ${filter === "assigned" ? "active" : ""}" data-cc-filter="assigned">Needs my review · ${counts.assigned}</span>
    <span class="filter ${filter === "mine" ? "active" : ""}" data-cc-filter="mine">My PRs · ${counts.mine}</span>
  </div>`;

  for (const g of groups) {
    const visible = g.prs.filter(matches);
    if (filter !== "all" && !visible.length && !g.error) continue; // hide empty repos when filtered
    html += `<div class="repo-group"><div class="repo-name">${host(g.provider)}/${esc(g.repo)}</div>`;
    if (g.error) {
      html += `<div class="error-banner">Could not list PRs: ${esc(g.error)}</div>`;
    } else if (!visible.length) {
      html += `<div class="empty-state" style="padding:16px">No open PRs you authored or that await your review — search above to add specific PRs.</div>`;
    }
    for (const pr of visible) {
      const size = (pr.additions || pr.deletions) ? ` · +${pr.additions} −${pr.deletions}` : "";
      const unpin = pr.pinned
        ? `<span class="rs-detail linklike" data-unpin data-provider="${pr.provider}" data-repo="${esc(pr.repo)}" data-number="${pr.number}">✕ remove</span>`
        : "";
      html += `
      <div class="pr-row" data-provider="${pr.provider}" data-repo="${esc(pr.repo)}" data-number="${pr.number}">
        <div class="t">${esc(pr.title)} <span class="num">#${pr.number}</span>
          ${pr.tickets.map(ticketBadge).join("")}
          ${pr.tickets.length ? "" : `<span class="badge none">no ticket</span>`}
          ${pr.role === "assigned" ? `<span class="badge reviewreq">review requested</span>` : ""}
          ${pr.pinned && pr.role === "other" ? `<span class="badge none" title="Added manually">📌 added</span>` : ""}
          ${pr.stale === true ? `<span class="badge" style="background:var(--amber-bg);color:var(--amber)" title="PR changed after this review — re-run to refresh">outdated</span>` : ""}
        </div>
        <div class="sub"><code>${esc(pr.branch)}</code> · ${esc(pr.author)} · updated ${relTime(pr.updated_at)}${size}</div>
        <div class="review-state">${reviewStateHtml(pr)}${unpin}</div>
        <span class="go">›</span>
      </div>`;
    }
    if (g.truncated) {
      html += `<div class="rs-detail" style="margin:2px 4px 6px">showing the first 50 open PRs of this repo</div>`;
    }
    html += `</div>`;
  }
  $("#cc-content").innerHTML = html;
}

/* ---- search & pin individual PRs ---- */

function isPrUrl(s) {
  return /github\.com\/\S+\/pull\/\d+|bitbucket\.org\/\S+\/pull-requests\/\d+/.test(s);
}

async function runSearch(q) {
  state.searchActive = true;
  $("#cc-content").innerHTML = `<div class="empty-state"><span class="spin"></span> Searching open PRs for “${esc(q)}”…</div>`;
  let res;
  try {
    res = await api("/api/prs/search?q=" + encodeURIComponent(q));
  } catch (e) {
    $("#cc-content").innerHTML = `<div class="error-banner">Search failed: ${esc(e.message)}</div>`;
    return;
  }
  let html = `<div class="repo-group"><div class="repo-name">Search results for “${esc(q)}” · <span class="linklike" data-clear-search>clear</span></div>`;
  if (!res.results.length) {
    html += `<div class="empty-state" style="padding:16px">No open PRs match in your connected repos.</div>`;
  }
  for (const pr of res.results) {
    html += `
    <div class="pr-row" data-provider="${pr.provider}" data-repo="${esc(pr.repo)}" data-number="${pr.number}">
      <div class="t">${esc(pr.title)} <span class="num">#${pr.number}</span></div>
      <div class="sub"><code>${esc(pr.repo)}</code> · ${esc(pr.author)} · updated ${relTime(pr.updated_at)}</div>
      <div class="review-state">
        <button class="btn small" data-pin data-provider="${pr.provider}" data-repo="${esc(pr.repo)}" data-number="${pr.number}" ${pr.pinned ? "disabled" : ""}>${pr.pinned ? "✓ Added" : "+ Add to list"}</button>
        <span class="rs-detail">click row to review now</span>
      </div>
      <span class="go">›</span>
    </div>`;
  }
  html += `</div>`;
  $("#cc-content").innerHTML = html;
}

function goFromInput() {
  const v = $("#url-input").value.trim();
  if (!v) return;
  if (isPrUrl(v)) startReview({ url: v });
  else runSearch(v);
}

/* ================================================================ review pipeline job */

const STAGES = [
  ["fetch", "Fetch PR metadata & diff"],
  ["parse", "Parse diff into hunks"],
  ["tickets", "Detect & fetch tickets"],
  ["extract", "Extract requirements (LLM)"],
  ["map", "Map requirements → changes (LLM)"],
  ["validate", "Validate anchors"],
  ["save", "Save review"],
  ["findings", "Code-review findings"],
];

function renderStages(current, done, hasError) {
  const idx = STAGES.findIndex(([k]) => k === current);
  return STAGES.map(([key, label], i) => {
    const isDone = done || (idx >= 0 && i < idx);
    const isNow = !done && key === current;
    const mark = isDone ? "✓" : (isNow ? (hasError ? "✗" : `<span class="spin"></span>`) : "·");
    return `<div class="stage-row ${isDone ? "done" : ""} ${isNow ? "now" : ""}">
      <span class="mark">${mark}</span><span>${label}</span></div>`;
  }).join("");
}

async function startReview(params) {
  let job;
  try {
    job = await api("/api/reviews", { method: "POST", body: params });
  } catch (e) {
    toast(`Can't start review: ${e.message}`, true);
    return;
  }
  if (job.cached) {
    // stored review — open as-is; re-analysis only happens via explicit Re-run
    await openReview(job.review_id);
    return;
  }
  state.currentJob = job.job_id;
  $("#overlay-title").textContent = "Running review…";
  $("#overlay-sub").textContent = job.review_id;
  $("#overlay-error").innerHTML = "";
  $("#overlay-actions").style.display = "block";
  $("#overlay-cancel").style.display = "";
  $("#overlay-close").style.display = "none";
  $("#overlay-stages").innerHTML = renderStages("fetch", false, false);
  $("#overlay").classList.add("visible");

  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    let st;
    try {
      st = await api(`/api/jobs/${job.job_id}`);
    } catch { return; }
    $("#overlay-stages").innerHTML = renderStages(st.stage, st.done && !st.error, !!st.error);
    $("#overlay-sub").textContent = st.detail || job.review_id;
    if (st.error) {
      clearInterval(state.pollTimer);
      $("#overlay-title").textContent = st.error === "Cancelled" ? "Review cancelled" : "Review failed";
      $("#overlay-error").innerHTML = `<div class="error-banner">${esc(st.error)}</div>`;
      $("#overlay-actions").style.display = "block";
      $("#overlay-cancel").style.display = "none";
      $("#overlay-close").style.display = "";
      $("#overlay-close").textContent = "Close";
    } else if (st.done) {
      clearInterval(state.pollTimer);
      $("#overlay").classList.remove("visible");
      $("#overlay-cancel").style.display = "none";
      $("#overlay-close").style.display = "";
      await openReview(st.review_id);
      loadPRs(); // refresh command-center summaries in the background
    }
  }, 700);
}

/* ================================================================ review screen */

async function openReview(rid) {
  try {
    state.review = await api(`/api/reviews/${rid}/data`);
  } catch (e) {
    toast(`Could not load review: ${e.message}`, true);
    return;
  }
  // resume tracking any code-review job still running server-side (e.g. after reload)
  try {
    const jobs = await api(`/api/reviews/${rid}/jobs`);
    if (jobs.code_review) {
      state.bugsJobs[rid] = jobs.code_review;
      watchBugsJob(rid, jobs.code_review, Date.now());
    }
    if (jobs.add_requirement) {
      state.addReqJobs[rid] = jobs.add_requirement;
      watchAddReqJob(rid, jobs.add_requirement);
    }
  } catch {}
  state.reviewTab = "summary"; // each review opens on the at-a-glance Summary tab
  show("review"); // before render: label measurement (getBBox) needs a visible SVG
  renderReview();
}

function claimIds(r) {
  return r.mode === "requirements" ? r.requirements.map((q) => q.id) : r.unexplained.map((u) => u.id);
}

/* Collapsible original source text with extraction provenance underlines. */
function sourcePanelHtml(r) {
  if (!r.sources || !r.sources.length) return "";
  const parts = r.sources.map((s) => {
    const body = esc(s.text || "(empty)");
    // collect non-overlapping quote matches on the escaped text, then build once
    const found = [];
    for (const req of r.requirements) {
      if (req.source !== s.tag || !req.quote) continue;
      const eq = esc(req.quote);
      const idx = body.toLowerCase().indexOf(eq.toLowerCase());
      if (idx < 0) continue;
      if (found.some((m) => idx < m.idx + m.len && m.idx < idx + eq.length)) continue;
      found.push({ idx, len: eq.length, id: req.id, text: req.text });
    }
    found.sort((a, b) => a.idx - b.idx);
    let out = "", pos = 0;
    for (const m of found) {
      out += body.slice(pos, m.idx)
        + `<u style="text-decoration-color:${colorFor(m.id)}" data-card="${m.id}" title="${esc(m.id + ": " + m.text)}">`
        + body.slice(m.idx, m.idx + m.len) + "</u>";
      pos = m.idx + m.len;
    }
    out += body.slice(pos);
    return `<b>${esc(s.title || s.tag)}</b><p>${out}</p>`;
  });
  const label = r.sources.map((s) => (s.title ? s.title.split(" — ")[0] : s.tag)).join(" · ");
  return `<details class="source-text"><summary>Source requirements — ${esc(label)}</summary>
    <div class="source-body">${parts.join("")}</div></details>`;
}

/* ---- change-flow diagram (DESIGN.md §7.2): client-side layered layout ---- */

/* ---- change-flow diagram: deterministic auto-layout (DECISIONS #21/#22/#23) ----
   longest-path layering → barycenter ordering → centered columns →
   distributed edge ports → white edge casing (crossings read as bridges) →
   labels measured with getBBox and placed by search that must clear every
   node, every placed label, and every sampled edge curve. */

const FLOW_NW = 200, FLOW_NH = 56, FLOW_XGAP = 205, FLOW_YGAP = 56, FLOW_M = 26, FLOW_PORT = 15;

function layoutFlow(flow) {
  const nodes = flow.nodes, edges = flow.edges;
  const byId = {};
  nodes.forEach((n) => { byId[n.id] = n; });
  const preds = {};
  nodes.forEach((n) => { preds[n.id] = []; });
  edges.forEach((e) => { if (preds[e.target] && byId[e.source]) preds[e.target].push(e.source); });

  /* layering: longest path from entry points (memoized; visiting-set guards cycles) */
  const L = {}, visiting = new Set();
  const layerOf = (id) => {
    if (L[id] != null) return L[id];
    if (visiting.has(id)) return 0;
    visiting.add(id);
    L[id] = preds[id].length ? Math.max(...preds[id].map(layerOf)) + 1 : 0;
    visiting.delete(id);
    return L[id];
  };
  nodes.forEach((n) => layerOf(n.id));
  const maxL = Math.max(0, ...nodes.map((n) => L[n.id]));
  nodes.forEach((n) => { if (n.kind === "ghost") L[n.id] = maxL; });

  /* order within a column by barycenter of predecessors (ghosts sink) */
  const layers = Array.from({ length: maxL + 1 }, () => []);
  nodes.forEach((n) => layers[L[n.id]].push(n));
  const ord = {};
  layers.forEach((col, li) => {
    if (li > 0) {
      const bary = {};
      col.forEach((n) => {
        const ps = preds[n.id].map((p) => ord[p]).filter((v) => v != null);
        bary[n.id] = n.kind === "ghost" ? 1e9 : ps.length ? ps.reduce((a, c) => a + c, 0) / ps.length : 1e8;
      });
      col.sort((a, b) => bary[a.id] - bary[b.id]);
    }
    col.forEach((n, i) => { ord[n.id] = i; });
  });

  /* coordinates — columns centered vertically */
  const rows = Math.max(1, ...layers.map((c) => c.length));
  const H = FLOW_M * 2 + rows * FLOW_NH + (rows - 1) * FLOW_YGAP;
  const W = FLOW_M * 2 + (maxL + 1) * FLOW_NW + maxL * FLOW_XGAP;
  const pos = {};
  layers.forEach((col, li) => {
    const colH = col.length * FLOW_NH + (col.length - 1) * FLOW_YGAP;
    col.forEach((n) => {
      pos[n.id] = { x: FLOW_M + li * (FLOW_NW + FLOW_XGAP), y: (H - colH) / 2 + ord[n.id] * (FLOW_NH + FLOW_YGAP) };
    });
  });
  return { pos, width: W, height: Math.max(H, 140) };
}

/* shared geometry: positions, live edges, ports, node-avoiding edge routes.
   Edges that span multiple columns are routed through waypoints placed in the
   gaps of each intermediate column, so wires never disappear under nodes. */
function flowGeometry(r) {
  const flow = r.flow;
  if (!flow || !flow.nodes.length) return null;
  const { pos, width, height } = layoutFlow(flow);
  const cy = (id) => pos[id].y + FLOW_NH / 2;
  const live = flow.edges.filter((e) => pos[e.source] && pos[e.target]);
  const outE = {}, inE = {};
  live.forEach((e) => {
    (outE[e.source] = outE[e.source] || []).push(e);
    (inE[e.target] = inE[e.target] || []).push(e);
  });
  Object.values(outE).forEach((l) => l.sort((a, b) => cy(a.target) - cy(b.target)));
  Object.values(inE).forEach((l) => l.sort((a, b) => cy(a.source) - cy(b.source)));
  const portY = new Map();
  live.forEach((e) => {
    const o = outE[e.source], t = inE[e.target];
    portY.set(e, {
      sy: cy(e.source) + (o.indexOf(e) - (o.length - 1) / 2) * FLOW_PORT,
      ty: cy(e.target) + (t.indexOf(e) - (t.length - 1) / 2) * FLOW_PORT,
    });
  });

  /* waypoint routing */
  const colXs = [...new Set(Object.values(pos).map((p) => p.x))].sort((a, b) => a - b);
  const colRects = {};
  flow.nodes.forEach((n) => {
    const p = pos[n.id];
    if (p) (colRects[p.x] = colRects[p.x] || []).push(p);
  });
  Object.values(colRects).forEach((l) => l.sort((a, b) => a.y - b.y));

  const edgePts = new Map();
  live.forEach((e) => {
    const { sy, ty } = portY.get(e);
    const srcLeft = pos[e.source].x, srcRight = srcLeft + FLOW_NW;
    const tgtLeft = pos[e.target].x, tgtRight = tgtLeft + FLOW_NW;
    const dipY = height - 12;
    let pts;
    if (tgtLeft >= srcRight - 1) {
      // forward edge: right face → left face, weaving through intermediate-column gaps
      const x0 = srcRight, x3 = tgtLeft;
      pts = [{ x: x0, y: sy }];
      for (const cx of colXs) {
        if (cx <= srcLeft || cx >= tgtLeft) continue; // only columns strictly between
        const wx = cx + FLOW_NW / 2;
        const yline = sy + (ty - sy) * ((wx - x0) / (x3 - x0));
        const rects = colRects[cx] || [];
        const cands = [];
        if (rects.length) {
          cands.push(rects[0].y - 18); // above the column
          for (let i = 0; i < rects.length - 1; i++)
            cands.push((rects[i].y + FLOW_NH + rects[i + 1].y) / 2); // gaps between nodes
          cands.push(rects[rects.length - 1].y + FLOW_NH + 18); // below the column
        } else {
          cands.push(yline);
        }
        const inBounds = cands.filter((y) => y > 8 && y < height - 8);
        const pool = inBounds.length ? inBounds : cands;
        const y = pool.reduce((best, c) => (Math.abs(c - yline) < Math.abs(best - yline) ? c : best));
        pts.push({ x: wx, y });
      }
      pts.push({ x: x3, y: ty });
    } else if (tgtRight < srcLeft - 1) {
      // back edge: drop in the channel beside the source, run under the graph,
      // rise in the channel beside the target — clears every column's nodes
      pts = [{ x: srcLeft, y: sy }, { x: srcLeft - 40, y: dipY }, { x: tgtRight + 40, y: dipY }, { x: tgtRight, y: ty }];
    } else {
      // same/overlapping column: under-loop with two waypoints
      pts = [{ x: srcLeft, y: sy }, { x: srcLeft - 50, y: dipY }, { x: tgtRight + 50, y: dipY }, { x: tgtRight, y: ty }];
    }
    edgePts.set(e, pts);
  });

  /* lane assignment — edges sharing a waypoint (same gap) or the under-graph
     dip band fan out into parallel lanes instead of collapsing onto one line */
  let finalHeight = height;
  {
    const dipY = height - 12;
    // shared intermediate waypoints → spread ±12px, ordered by target port
    const groups = new Map();
    live.forEach((e) => {
      const pts = edgePts.get(e);
      for (let i = 1; i < pts.length - 1; i++) {
        if (pts[i].y === dipY) continue; // dip lanes handled below
        const key = Math.round(pts[i].x) + ":" + Math.round(pts[i].y);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push({ e, i });
      }
    });
    groups.forEach((list) => {
      if (list.length < 2) return;
      list.sort((a, b) => (portY.get(a.e).ty - portY.get(b.e).ty) || (portY.get(a.e).sy - portY.get(b.e).sy));
      list.forEach((item, idx) => {
        edgePts.get(item.e)[item.i].y += (idx - (list.length - 1) / 2) * 12;
      });
    });
    // under-graph dips → one lane per edge, canvas grows to fit
    const dippers = live.filter((e) => edgePts.get(e).some((p) => p.y === dipY));
    dippers.sort((a, b) => edgePts.get(a)[1].x - edgePts.get(b)[1].x);
    dippers.forEach((e, idx) => {
      edgePts.get(e).forEach((p) => { if (p.y === dipY) p.y = dipY + idx * 11; });
    });
    if (dippers.length) finalHeight = height + (dippers.length - 1) * 11 + 8;
  }

  /* piecewise cubic through the waypoints — horizontal tangents at each point */
  const seg = (a, b, lt) => {
    const k = Math.max(24, Math.abs(b.x - a.x) * 0.45) * Math.sign(b.x - a.x || 1);
    const x1 = a.x + k, x2 = b.x - k, u = 1 - lt;
    return { x: u * u * u * a.x + 3 * u * u * lt * x1 + 3 * u * lt * lt * x2 + lt * lt * lt * b.x,
             y: u * u * u * a.y + 3 * u * u * lt * a.y + 3 * u * lt * lt * b.y + lt * lt * lt * b.y };
  };
  const bez = (e, t) => {
    const pts = edgePts.get(e);
    const nseg = pts.length - 1;
    const scaled = Math.min(0.9999, Math.max(0, t)) * nseg;
    const i = Math.floor(scaled);
    return seg(pts[i], pts[i + 1], scaled - i);
  };
  const pathD = (e) => {
    const pts = edgePts.get(e);
    let d = `M${pts[0].x},${pts[0].y}`;
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i];
      const k = Math.max(24, Math.abs(b.x - a.x) * 0.45) * Math.sign(b.x - a.x || 1);
      d += ` C${a.x + k},${a.y} ${b.x - k},${b.y} ${b.x},${b.y}`;
    }
    return d;
  };
  const edgeColor = (e) => (e.missing ? "#b98300" : (e.requirement_ids.length ? colorFor(e.requirement_ids[0]) : "#59636e"));
  return { flow, pos, width, height: finalHeight, live, portY, bez, pathD, edgeColor };
}

function flowSvg(r) {
  const geo = flowGeometry(r);
  if (!geo) return "";
  const { flow, pos, width, height, live, pathD, edgeColor } = geo;
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const clipTail = (s, n) => (s.length > n ? "…" + s.slice(-(n - 1)) : s);
  const KIND = { new: "#1a7f37", modified: "#9a6700", store: "#1a7f37" };
  const KTAG = { new: "NEW", modified: "MOD", store: "STORE" };

  const colors = [...new Set(live.map(edgeColor))];
  const defs = colors.map((c, i) =>
    `<marker id="fa${i}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${c}"/></marker>`).join("");

  /* edges: white casing pass first, then colored pass */
  let body = "";
  for (const e of live) body += `<path class="fedge-casing" d="${pathD(e)}"/>`;
  for (const e of live) {
    const clickable = e.missing && e.requirement_ids.length ? ` data-card="${e.requirement_ids[0]}" style="cursor:pointer"` : "";
    body += `<path class="fedge${e.missing ? " missing" : ""}"${clickable} d="${pathD(e)}" stroke="${edgeColor(e)}" marker-end="url(#fa${colors.indexOf(edgeColor(e))})"/>`;
  }

  /* requirement dots per node — non-missing edges only */
  const dots = {};
  for (const e of live)
    if (!e.missing)
      for (const rid of e.requirement_ids)
        for (const nid of [e.source, e.target]) (dots[nid] = dots[nid] || new Set()).add(rid);

  for (const n of flow.nodes) {
    const p = pos[n.id];
    if (!p) continue;
    if (n.kind === "ghost") {
      body += `<g class="fnode ghost" data-card="${n.requirement_id}" data-label="${esc(n.label)}" data-loc="${esc(n.requirement_id + ": " + textFor(n.requirement_id))}">
        <rect class="body" x="${p.x}" y="${p.y}" width="${FLOW_NW}" height="${FLOW_NH}"/>
        <text class="name" x="${p.x + 13}" y="${p.y + 24}">${esc(clip(n.label, 26))}</text>
        <text class="loc" x="${p.x + 13}" y="${p.y + 42}">${n.requirement_id} — no implementation in PR</text>
      </g>`;
      continue;
    }
    /* layout inside the box: kind tag top-right, name row, loc row sharing
       space with the dot cluster bottom-right — every run clipped to the px
       it actually has (mono ≈ 7.3px/char at 12px, 6.05px/char at 10px). */
    const dotList = [...(dots[n.id] || [])].sort();
    const dotsW = dotList.length ? dotList.length * 13 + 6 : 0;
    const nameChars = Math.floor((FLOW_NW - 26 - 34) / 7.3);            // reserve kind tag
    const locChars = Math.floor((FLOW_NW - 26 - dotsW) / 6.05);
    const dotSvg = dotList.map((rid, i) =>
      `<circle cx="${p.x + FLOW_NW - 12 - i * 13}" cy="${p.y + FLOW_NH - 14}" r="5" fill="${colorFor(rid)}"><title>${esc(rid + ": " + textFor(rid))}</title></circle>`).join("");
    body += `<g class="fnode" data-label="${esc(n.label)}" data-loc="${esc(`${n.file}:${n.line}`)}" data-anchors='${esc(JSON.stringify([{ file: n.file, start: n.line, end: n.line }]))}'>
      <rect class="body" x="${p.x}" y="${p.y}" width="${FLOW_NW}" height="${FLOW_NH}"/>
      <rect x="${p.x}" y="${p.y}" width="4" height="${FLOW_NH}" fill="${KIND[n.kind]}"/>
      <text class="name" x="${p.x + 13}" y="${p.y + 24}"><title>${esc(n.label)}</title>${esc(clip(n.label, nameChars))}</text>
      <text class="loc" x="${p.x + 13}" y="${p.y + 42}"><title>${esc(`${n.file}:${n.line}`)}</title>${esc(clipTail(`${n.file}:${n.line}`, locChars))}</text>
      <text class="kindtag" text-anchor="end" x="${p.x + FLOW_NW - 8}" y="${p.y + 16}" fill="${KIND[n.kind]}">${KTAG[n.kind]}</text>
      ${dotSvg}
    </g>`;
  }

  body += `<g class="flow-labels"></g>`;
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" style="min-width:${Math.round(width * 0.9)}px" xmlns="http://www.w3.org/2000/svg">
    <defs>${defs}</defs>${body}</svg>`;
}

/* second pass, after DOM insertion: measured labels placed clear of nodes,
   labels, and every sampled edge curve (DECISIONS #23) */
function placeFlowLabels(r, attempt = 0) {
  const geo = flowGeometry(r);
  if (!geo) return;
  const svg = document.querySelector(".flow-panel svg");
  const labelsG = svg && svg.querySelector(".flow-labels");
  if (!labelsG) return;
  if (!svg.getBoundingClientRect().width) {
    // hidden → getBBox measures 0; retry once the screen is visible
    if (attempt < 10) requestAnimationFrame(() => placeFlowLabels(r, attempt + 1));
    return;
  }
  labelsG.innerHTML = "";
  const { flow, pos, width, height, live, bez, edgeColor } = geo;
  const SVGNS = "http://www.w3.org/2000/svg";
  const hits = (a, b, pad) => a.x < b.x + b.w + pad && b.x < a.x + a.w + pad &&
                              a.y < b.y + b.h + pad && b.y < a.y + a.h + pad;
  const samples = live.map((e) => Array.from({ length: 41 }, (_, i) => bez(e, i / 40)));
  const nodeRects = flow.nodes.filter((n) => pos[n.id]).map((n) => ({ x: pos[n.id].x, y: pos[n.id].y, w: FLOW_NW, h: FLOW_NH }));
  const placed = [];
  const isClear = (rect) => {
    if (nodeRects.some((nr) => hits(rect, nr, 4))) return false;
    if (placed.some((o) => hits(o, rect, 5))) return false;
    for (const pts of samples)
      for (const p of pts)
        if (p.x > rect.x - 3 && p.x < rect.x + rect.w + 3 && p.y > rect.y - 3 && p.y < rect.y + rect.h + 3) return false;
    return true;
  };
  /* measure all labels first so the widest are seated first (they have the
     fewest viable spots); then place each with the search, and if the search
     truly finds nothing, spiral outward to GUARANTEED-clear space and draw a
     leader line back to the curve. Never place on top of anything. */
  const jobs = [];
  for (const e of live) {
    const c = edgeColor(e);
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", "elabel-g");
    if (e.missing && e.requirement_ids.length) {
      g.setAttribute("data-card", e.requirement_ids[0]);
      g.style.cursor = "pointer";
    }
    const text = document.createElementNS(SVGNS, "text");
    text.setAttribute("class", "elabel");
    text.setAttribute("fill", c);
    const reqTag = e.requirement_ids.length ? ` (${e.requirement_ids.join(",")})` : "";
    text.textContent = (e.missing ? "✕ " : "") + e.label + reqTag;
    g.appendChild(text);
    labelsG.appendChild(g);
    jobs.push({ e, g, text, w: text.getBBox().width + 12, h: 16 });
  }
  jobs.sort((a, b) => b.w - a.w);
  const T_CANDIDATES = [];
  for (let t = 0.5, d = 0; T_CANDIDATES.length < 17; d += 0.05) {
    T_CANDIDATES.push(0.5 + d); if (d) T_CANDIDATES.splice(T_CANDIDATES.length - 1, 0, 0.5 - d);
    if (d > 0.4) break;
  }
  const inCanvas = (r) => r.x >= 4 && r.x + r.w <= width - 4 && r.y >= 2 && r.y + r.h <= height - 2;
  for (const job of jobs) {
    const { e, g, text, w, h } = job;
    let best = null, leader = null;
    outer:
    for (const dy of [-24, 10, -40, 26, -56, 42, -72, 58]) {
      for (const t of T_CANDIDATES) {
        if (t < 0.06 || t > 0.94) continue;
        const p = bez(e, t);
        const rect = { x: p.x - w / 2, y: p.y + dy, w, h };
        if (!inCanvas(rect)) continue;
        if (isClear(rect)) { best = rect; break outer; }
      }
    }
    if (!best) {
      /* spiral out from the curve midpoint until a clear spot exists —
         canvas margins included, so a spot always exists on a finite canvas */
      const mid = bez(e, 0.5);
      spiral:
      for (let rad = 24; rad <= Math.max(width, height); rad += 14) {
        for (let a = 0; a < 16; a++) {
          const ang = (a / 16) * 2 * Math.PI;
          const rect = { x: mid.x + rad * Math.cos(ang) - w / 2, y: mid.y + rad * Math.sin(ang) - h / 2, w, h };
          if (!inCanvas(rect)) continue;
          if (isClear(rect)) { best = rect; leader = mid; break spiral; }
        }
      }
      if (!best) { best = { x: 4, y: 2, w, h }; } // degenerate canvas; keep in-frame
    }
    placed.push(best);
    if (leader) {
      const lx = Math.max(best.x, Math.min(leader.x, best.x + w));
      const ly = leader.y > best.y + h ? best.y + h : (leader.y < best.y ? best.y : best.y + h / 2);
      const line = document.createElementNS(SVGNS, "line");
      line.setAttribute("class", "elabel-leader");
      line.setAttribute("x1", lx); line.setAttribute("y1", ly);
      line.setAttribute("x2", leader.x); line.setAttribute("y2", leader.y);
      g.insertBefore(line, text);
    }
    const rect = document.createElementNS(SVGNS, "rect");
    rect.setAttribute("class", "elabel-bg");
    rect.setAttribute("rx", "3");
    rect.setAttribute("x", best.x);
    rect.setAttribute("y", best.y);
    rect.setAttribute("width", w);
    rect.setAttribute("height", h);
    g.insertBefore(rect, text);
    text.setAttribute("x", best.x + 6);
    text.setAttribute("y", best.y + 12);
  }
}

function flowSummaryHtml(r) {
  const lines = r.flow?.summary || [];
  if (!lines.length) return "";
  // linkify R#/U#/B# mentions into colored, clickable identity chips
  const linkify = (line) => esc(line).replace(/\b([RUB]\d+)\b/g, (m, id) =>
    `<span class="rid flow-ref" data-card="${id}" style="background:${colorFor(id)};font-size:9.5px;line-height:1.6;cursor:pointer">${id}</span>`);
  return `<div class="flow-summary">
    <span class="fs-label">How the flow meets the requirements</span>
    <ul>${lines.map((l) => `<li>${linkify(l)}</li>`).join("")}</ul>
  </div>`;
}

function showReviewTab(name) {
  state.reviewTab = name;
  for (const t of ["summary", "findings", "diff", "flow"]) {
    const body = document.getElementById("review-body-" + t);
    if (body) body.style.display = t === name ? "" : "none";
  }
  document.querySelectorAll(".review-tabs .filter").forEach((f) =>
    f.classList.toggle("active", f.dataset.reviewTab === name));
  window.scrollTo(0, 0);
  if (name === "flow") placeFlowLabels(state.review); // labels can't measure while hidden
}

/* ---- Findings tab: code-review items as a severity-grouped table ---- */

function findingsTabHtml(r) {
  const bugs = r.bugs || [];

  if (!r.bugs_ran) {
    return `<div class="empty-tab">
      <h3>No findings for this review</h3>
      <p>Findings now run automatically as part of every review. This review predates
      that (or its findings pass failed) — hit <b>↻ Findings</b> in the toolbar, or
      ↻ Re-run the whole review.</p>
    </div>`;
  }
  const reportHtml = r.bugs_report
    ? `<div class="fnd-report"><div class="sum-section">Full report — /code-review output (the table is a compression of this)</div>
       ${mdBlock(cleanReport(r.bugs_report))}</div>`
    : "";

  if (!bugs.length) {
    return `<div class="empty-tab">
      <h3>No findings</h3>
      <p>The code review pass completed and reported nothing worth flagging.</p>
    </div>${reportHtml}`;
  }

  const sevCounts = {}, catCounts = {};
  for (const b of bugs) {
    const s = SEV_LABEL[b.severity] ? b.severity : "minor";
    sevCounts[s] = (sevCounts[s] || 0) + 1;
    const c = CAT_LABEL[b.category] ? b.category : "other";
    catCounts[c] = (catCounts[c] || 0) + 1;
  }

  // headline strip: one pill per severity present, most severe first
  const pills = SEV_ORDER.filter((s) => sevCounts[s]).map((s) => `
    <span class="fnd-pill" style="--sev:${SEV_COLORS[s]}">
      <b>${sevCounts[s]}</b> ${esc(SEV_LABEL[s])}${sevCounts[s] > 1 ? "s" : ""}
      <i>${esc(SEV_BLURB[s])}</i>
    </span>`).join("");

  const cats = CAT_ORDER.filter((c) => catCounts[c]);
  const chips = [`<span class="fnd-chip active" data-fnd-cat="all">All · ${bugs.length}</span>`]
    .concat(cats.map((c) =>
      `<span class="fnd-chip" data-fnd-cat="${c}">${esc(CAT_LABEL[c])} · ${catCounts[c]}</span>`))
    .join("");

  let rows = "";
  for (const sev of SEV_ORDER) {
    const group = bugs.filter((b) => (SEV_LABEL[b.severity] ? b.severity : "minor") === sev);
    if (!group.length) continue;
    rows += `<tr class="fnd-group"><td colspan="4">
      <span class="fnd-dot" style="background:${SEV_COLORS[sev]}"></span>
      ${esc(SEV_LABEL[sev])} <span class="fnd-group-n">${group.length}</span>
      <span class="fnd-group-blurb">${esc(SEV_BLURB[sev])}</span></td></tr>`;

    for (const b of group) {
      const cat = CAT_LABEL[b.category] ? b.category : "other";
      const a = (b.anchors || [])[0];
      const loc = a
        ? `<a class="fnd-loc" data-goto-file="${esc(a.file)}" data-goto-line="${a.start}"
             title="${esc(a.file)}">${esc(a.file.split("/").pop())}<i>:${a.start}${
               a.end && a.end !== a.start ? `–${a.end}` : ""}</i></a>`
        : (b.cited_file
          ? `<span class="fnd-loc none" title="${esc(b.cited_file)}:${b.cited_line || "?"} — cited by the report but outside the changed lines">${esc(b.cited_file.split("/").pop())}<i>${b.cited_line ? `:${b.cited_line}` : ""} · off-diff</i></span>`
          : `<span class="fnd-loc none" title="The report gave no location">—</span>`);
      rows += `<tr class="fnd-row" data-cat="${cat}" data-goto-card="${esc(b.id)}">
        <td class="fnd-id"><span class="fnd-badge" style="background:${SEV_COLORS[sev]}">${esc(b.id)}</span></td>
        <td class="fnd-cat"><span class="fnd-cat-tag c-${cat}">${esc(CAT_LABEL[cat])}</span></td>
        <td class="fnd-what">
          <div class="fnd-title">${md(b.title)}</div>
          ${b.detail ? `<div class="fnd-detail">${md(b.detail)}</div>` : ""}
          ${b.suggestion ? `<div class="fnd-fix"><b>Fix</b> ${md(b.suggestion)}</div>` : ""}
        </td>
        <td class="fnd-where">${loc}</td>
      </tr>`;
    }
  }

  return `
    ${r.bugs_stale ? `<div class="error-banner" style="background:var(--amber-bg);color:var(--amber);border-color:#eed888;margin:10px 0 0">⚠ These findings were carried from a previous run — re-run the code review to refresh them.</div>` : ""}
    <div class="fnd-strip">${pills}</div>
    <div class="fnd-chips">${chips}</div>
    <table class="fnd-table">
      <thead><tr><th></th><th>Category</th><th>Finding</th><th>Where</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="fnd-none" style="display:none">No findings in this category.</div>
    ${reportHtml}`;
}

/* ---- Summary tab: the whole review as a scannable grid ---- */

function overflowNote(r, key, label) {
  const n = (r.overflow || {})[key];
  return n ? `<div class="sum-muted" style="margin:4px 2px 0;font-size:12px">+${n} more ${label} returned by Claude but dropped by the cap — Re-run won't recover them; raise the cap if this recurs.</div>` : "";
}

function summaryTabHtml(r) {
  const isExplain = r.mode === "explain";
  const verified = new Set(r.verified);
  const glyph = { fulfilled: ["✓ Fulfilled", "var(--green)"], partial: ["◐ Partial", "var(--amber)"], notfound: ["✗ Not found", "var(--red)"] };
  const confDot = (c) => `<span class="conf ${c}" title="${c} confidence"><span class="dot"></span></span>`;
  const ridChip = (id) => `<span class="rid" style="background:${colorFor(id)};font-size:9.5px;line-height:1.6">${id}</span>`;
  const whereChips = (anchors, max = 2) => {
    if (!anchors.length) return `<span class="sum-muted">—</span>`;
    const chips = anchors.slice(0, max).map((a) =>
      `<span class="anchor" data-file="${esc(a.file)}" data-start="${a.start}" data-end="${a.end}">${esc(a.file.split("/").pop())}:${a.start}</span>`).join(" ");
    return chips + (anchors.length > max ? ` <span class="sum-more">+${anchors.length - max}</span>` : "");
  };

  let html = `<div class="sum-wrap">`;
  if (r.net_effect.length) {
    html += `<div class="sum-net"><b>Net effect</b><ul>${r.net_effect.map((l) => `<li>${md(l)}</li>`).join("")}</ul>${overflowNote(r, "net_effect", "net-effect lines")}</div>`;
  }

  if (!isExplain && r.requirements.length) {
    html += `<div class="sum-section">Requirements · ${r.requirements.length}</div>
    <table class="sum"><thead><tr>
      <th class="sum-fit" title="Mark verified">✓</th><th class="sum-fit">ID</th><th class="sum-fit">Status</th>
      <th>Requirement</th><th>How / what's missing</th><th>Where</th><th class="sum-fit"></th>
    </tr></thead><tbody>`;
    for (const req of r.requirements) {
      const link = r.links.find((l) => l.requirement_id === req.id) || { status: "notfound", confidence: "low", anchors: [], mechanism: "", missing: "" };
      const [label, color] = glyph[link.status];
      const how = link.status === "fulfilled"
        ? md(link.mechanism)
        : `${link.mechanism ? md(link.mechanism) + " " : ""}<span class="sum-missing ${link.status === "notfound" ? "red" : ""}">${md(link.missing)}</span>`;
      html += `<tr data-goto-card="${req.id}">
        <td class="sum-fit"><input type="checkbox" class="sum-check" data-verify-card="${req.id}" ${verified.has(req.id) ? "checked" : ""}></td>
        <td class="sum-fit">${ridChip(req.id)}</td>
        <td class="sum-fit sum-status" style="color:${color}">${label}</td>
        <td>${md(req.text)}</td>
        <td>${how}</td>
        <td>${whereChips(link.anchors)}</td>
        <td class="sum-fit">${confDot(link.confidence)}</td>
      </tr>`;
    }
    html += `</tbody></table>` + overflowNote(r, "requirements", "requirements");
  }

  if (r.unexplained.length) {
    html += `<div class="sum-section">${isExplain ? "Changes" : "Unexplained changes"} · ${r.unexplained.length}</div>
    <table class="sum"><thead><tr>
      ${isExplain ? `<th class="sum-fit" title="Mark verified">✓</th>` : ""}<th class="sum-fit">ID</th><th>Change</th><th>What it does</th><th>Where</th>
    </tr></thead><tbody>`;
    for (const u of r.unexplained) {
      html += `<tr data-goto-card="${u.id}">
        ${isExplain ? `<td class="sum-fit"><input type="checkbox" class="sum-check" data-verify-card="${u.id}" ${verified.has(u.id) ? "checked" : ""}></td>` : ""}
        <td class="sum-fit">${ridChip(u.id)}</td>
        <td>${esc(u.label)}</td>
        <td class="sum-muted">${md(u.mechanism)}</td>
        <td>${whereChips(u.anchors)}</td>
      </tr>`;
    }
    html += `</tbody></table>`;
  }

  if (r.bugs_ran) {
    const sevPill = { high: "err", medium: "warn", low: "idle" };
    html += `<div class="sum-section">Code review findings · ${r.bugs.length}${r.bugs_stale ? ` <span class="sum-missing">⚠ carried from a previous run</span>` : ""}</div>`;
    if (!r.bugs.length) {
      html += `<div class="sum-net sum-muted">No findings — clean pass.</div>`;
    } else {
      html += `<table class="sum"><thead><tr>
        <th class="sum-fit">Severity</th><th>Finding</th><th>Fix</th><th>Where</th>
      </tr></thead><tbody>`;
      for (const b of r.bugs) {
        html += `<tr data-goto-card="${b.id}">
          <td class="sum-fit"><span class="pill ${sevPill[b.severity]}">${b.severity}</span></td>
          <td><b>${md(b.title)}</b>${b.detail ? `<div class="sum-muted">${md(b.detail)}</div>` : ""}</td>
          <td class="sum-muted">${esc(b.suggestion || "—")}</td>
          <td>${whereChips(b.anchors)}</td>
        </tr>`;
      }
      html += `</tbody></table>`;
    }
    html += overflowNote(r, "findings", "findings");
  }

  if (r.files.length) {
    const coverage = (f) => {
      const ids = [
        ...r.links.filter((l) => l.anchors.some((a) => a.file === f.path)).map((l) => l.requirement_id),
        ...r.unexplained.filter((u) => u.anchors.some((a) => a.file === f.path)).map((u) => u.id),
      ];
      return ids.length ? ids.map(ridChip).join(" ") : `<span class="sum-muted">—</span>`;
    };
    const statusLabel = { new: "NEW", mod: "MODIFIED", del: "DELETED", renamed: "RENAMED" };
    html += `<div class="sum-section">Files · ${r.files.length}</div>
    <table class="sum"><thead><tr>
      <th class="sum-fit">Status</th><th>File</th><th>Covered by</th>
    </tr></thead><tbody>`;
    r.files.forEach((f, fi) => {
      html += `<tr data-goto-file="${fi}">
        <td class="sum-fit"><span class="file-status ${f.status}">${statusLabel[f.status] || "MODIFIED"}</span></td>
        <td style="font-family:var(--mono);font-size:12px">${esc(f.path)}</td>
        <td>${coverage(f)}</td>
      </tr>`;
    });
    html += `</tbody></table>`;
  }

  const u = r.llm_usage || {};
  if (u.calls) {
    const secs = Math.round((u.ms || 0) / 1000);
    html += `<div class="sum-muted" style="margin-top:16px;font-size:12px" title="Runs on your Claude subscription — the dollar figure is what the tokens would cost at API list prices, shown for scale only">
      Generated by ${u.calls} Claude call${u.calls === 1 ? "" : "s"} · ≈$${(u.cost_usd || 0).toFixed(2)} API-equivalent · ${secs}s</div>`;
  }

  html += `</div>`;
  return html;
}

function flowPanelHtml(r) {
  const svg = flowSvg(r);
  if (!svg) {
    return `
    <div class="flow-section" style="padding-top:16px">
      <div class="empty-state">This review has no change-flow diagram — it was generated before the flow feature existed, or the flow stage failed on its last run.
        <br><span style="font-size:12px">Hit <b>↻ Re-run</b> in the header to regenerate the review with a diagram (verified state is preserved).</span>
      </div>
    </div>`;
  }
  return `
  <div class="flow-section" style="padding-top:16px">
    <div class="flow-panel">
      <div class="flow-head">
        <span class="fh-title">Change flow</span>
        <span class="fh-hint">how the new code interacts, colored by requirement — click a node to jump to its lines in the Review tab</span>
        ${r.flow.partial ? `<span class="pill warn" title="This PR's diff was too large for one pass — the graph covers only its first chunk">partial — first chunk of a large diff</span>` : ""}
        ${r.flow.dropped ? `<span class="pill warn" title="Claude proposed more nodes than the 12-node cap">+${r.flow.dropped} nodes dropped</span>` : ""}
      </div>
      <div class="flow-svg-wrap">${svg}</div>
      ${flowSummaryHtml(r)}
      <div class="flow-legend">Solid arrow = interaction in this PR, colored by requirement &nbsp;·&nbsp; dashed amber = expected but missing (partial) &nbsp;·&nbsp; dashed red node = requirement with no implementation &nbsp;·&nbsp; green stripe = new symbol, amber = modified</div>
    </div>
  </div>`;
}

/* hover-expansion for truncated node cards: overlay with the full name + path */
document.addEventListener("mouseover", (e) => {
  const fn = e.target.closest?.(".fnode");
  const wrap = fn && fn.closest(".flow-svg-wrap");
  if (!fn || !wrap || !fn.dataset.label) return;
  let tip = wrap.querySelector(".flow-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "flow-tip";
    wrap.appendChild(tip);
  }
  tip.innerHTML = `<div class="ft-name">${esc(fn.dataset.label)}</div>` +
    (fn.dataset.loc ? `<div class="ft-loc">${esc(fn.dataset.loc)}</div>` : "");
  const wr = wrap.getBoundingClientRect(), nr = fn.getBoundingClientRect();
  tip.style.left = Math.max(4, nr.left - wr.left - 4) + "px";
  tip.style.top = Math.max(2, nr.top - wr.top - 6) + "px";
});
document.addEventListener("mouseout", (e) => {
  if (e.target.closest?.(".fnode") && !e.relatedTarget?.closest?.(".fnode")) {
    document.querySelector(".flow-tip")?.remove();
  }
});

function legendHtml(r) {
  const isExplain = r.mode === "explain";
  const glyphs = { fulfilled: "✓", partial: "◐", notfound: "✗" };
  const items = [
    ...(!isExplain ? r.requirements.map((req) => {
      const link = r.links.find((l) => l.requirement_id === req.id) || { status: "notfound" };
      return `<span class="lg-item" data-card="${req.id}">
        <span class="rid" style="background:${colorFor(req.id)};font-size:9.5px;line-height:1.6">${req.id}</span>
        <span class="lg-glyph ${link.status}">${glyphs[link.status]}</span>${esc(req.text)}</span>`;
    }) : []),
    ...r.unexplained.map((u) =>
      `<span class="lg-item" data-card="${u.id}">
        <span class="rid" style="background:${colorFor(u.id)};font-size:9.5px;line-height:1.6">${u.id}</span>
        <span class="lg-glyph ${isExplain ? "change" : "unexplained"}">${isExplain ? "◆" : "❓"}</span>${esc(u.label)}</span>`),
  ];
  return items.length ? `<div class="req-legend">${items.join("")}</div>` : "";
}

function renderReview() {
  const r = state.review;
  const verified = new Set(r.verified);
  const claims = claimIds(r);
  const isExplain = r.mode === "explain";
  const tab = state.reviewTab || "diff";

  /* ---- diff panel ---- */
  const BIG_FILE_ROWS = 300; // above this, render collapsed and build on demand
  const fileTableHtml = (f) => {
    let rowsHtml = "";
    for (const row of f.rows) {
      if (row.gap) { rowsHtml += `<tr class="hunk-gap"><td colspan="6">${esc(row.gap)}</td></tr>`; continue; }
      const o = row.o, n = row.n;
      const kind = o && n ? (o[1] === n[1] ? "ctx" : "pair") : (o ? "del" : "add");
      const oCls = kind === "del" || kind === "pair" ? "side-del" : "";
      const nCls = kind === "add" || kind === "pair" ? "side-add" : "";
      const tags = n ? tagsFor(f.path, n[0]) : [];
      const tagsHtml = tags.map((t) =>
        `<span class="rtag" data-card="${t}" style="background:${colorFor(t)}" title="${esc(t + ": " + textFor(t))}">${t}</span>`).join("");
      rowsHtml += `<tr data-file="${esc(f.path)}" ${n ? `data-line="${n[0]}"` : ""}>
        <td class="lineno old-no">${o ? o[0] : ""}</td>
        <td class="code ${oCls}">${o ? `<span class="marker">${oCls ? "-" : " "}</span> ${esc(o[1])}` : ""}</td>
        <td class="sep"></td>
        <td class="lineno new-no">${n ? n[0] : ""}</td>
        <td class="code ${nCls}">${n ? `<span class="marker">${nCls ? "+" : " "}</span> ${esc(n[1])}` : ""}</td>
        <td class="tags">${tagsHtml}</td>
      </tr>`;
    }
    return `<table class="diff"><colgroup>
          <col style="width:42px"><col><col style="width:1px"><col style="width:42px"><col><col style="width:62px">
        </colgroup>${rowsHtml}</table>`;
  };
  state.buildFileTable = fileTableHtml;

  let diffHtml = "";
  r.files.forEach((f, fi) => {
    const linkedReqs = r.links.filter((l) => l.anchors.some((a) => a.file === f.path)).map((l) => l.requirement_id);
    const unx = r.unexplained.filter((u) => u.anchors.some((a) => a.file === f.path));
    const chips = [
      ...linkedReqs.map((id) => `<span class="chip" data-card="${id}" style="color:${colorFor(id)};background:${colorFor(id)}1c">${id}</span>`),
      ...unx.map((u) => `<span class="chip" data-card="${u.id}" style="color:${colorFor(u.id)};background:${colorFor(u.id)}1c">${isExplain ? u.id : u.id + " ❓"}</span>`),
    ].join("");
    const statusLabel = { new: "NEW", mod: "MODIFIED", del: "DELETED", renamed: "RENAMED" }[f.status] || "MODIFIED";
    const body = f.rows.length > BIG_FILE_ROWS
      ? `<div class="file-collapsed-note">Large file — <span class="linklike" data-expand-file="${fi}">show diff (${f.rows.length} lines)</span></div>`
      : fileTableHtml(f);
    diffHtml += `
      <div class="file" data-file-idx="${fi}">
        <div class="file-header">
          <span class="file-status ${f.status}">${statusLabel}</span>
          <span>${esc(f.path)}</span>
          <span class="file-chips">${chips}</span>
        </div>
        ${body}
      </div>`;
  });

  /* ---- rail ---- */
  const statusLabel = { fulfilled: "✓ Fulfilled", partial: "◐ Partial", notfound: "✗ Not found" };
  let railHtml = "";
  if (!isExplain) {
    railHtml += `<div class="rail-section-label">Requirements · ${r.requirements.length}</div>`;
    for (const req of r.requirements) {
      const link = r.links.find((l) => l.requirement_id === req.id) || { status: "notfound", confidence: "low", anchors: [], mechanism: "", missing: "" };
      const anchors = link.anchors.map((a) =>
        `<span class="anchor" data-file="${esc(a.file)}" data-start="${a.start}" data-end="${a.end}">${esc(a.file)}:${a.start}–${a.end}</span>`).join("");
      railHtml += `
        <div class="card ${verified.has(req.id) ? "verified" : ""}" id="card-${req.id}" data-card-id="${req.id}" style="border-left:4px solid ${colorFor(req.id)}">
          <div class="card-top">
            <span class="rid" style="background:${colorFor(req.id)}">${req.id}</span>
            <span class="status-pill ${link.status}">${statusLabel[link.status]}</span>
            <div>
              <div class="req-text">${md(req.text)}</div>
              <div class="src">${srcBadge(req.source)}</div>
            </div>
          </div>
          ${link.mechanism ? `<div class="mechanism">${md(link.mechanism)}</div>` : ""}
          ${link.why ? `<div class="why-note"><span class="why-label">Why</span> ${md(link.why)}</div>` : ""}
          ${link.missing ? `<div class="missing-note ${link.status === "notfound" ? "red" : ""}">${md(link.missing)}</div>` : ""}
          ${anchors ? `<div class="anchors">${anchors}</div>` : ""}
          <div class="card-foot">
            <span class="conf ${link.confidence}"><span class="dot"></span>${link.confidence} confidence</span>
            <button class="verify-btn">${verified.has(req.id) ? "✓ Verified" : "Mark verified"}</button>
          </div>
        </div>`;
    }
  }
  if (!isExplain) {
    railHtml += state.addReqJobs?.[r.id]
      ? `<div class="card add-req" style="cursor:default"><span class="spin"></span> Mapping new requirement…</div>`
      : `<div class="card add-req" id="add-req-card">+ Add requirement…</div>`;
  }

  const unxLabel = isExplain ? `Changes · ${r.unexplained.length}` : `Unexplained changes · ${r.unexplained.length}`;
  if (r.unexplained.length || isExplain) {
    railHtml += `<div class="rail-section-label">${unxLabel}</div>`;
  }
  for (const u of r.unexplained) {
    const anchors = u.anchors.map((a) =>
      `<span class="anchor" data-file="${esc(a.file)}" data-start="${a.start}" data-end="${a.end}">${esc(a.file)}:${a.start}–${a.end}</span>`).join("");
    railHtml += `
      <div class="card ${verified.has(u.id) ? "verified" : ""}" id="card-${u.id}" data-card-id="${u.id}" style="border-left:4px solid ${colorFor(u.id)}">
        <div class="card-top">
          <span class="rid" style="background:${colorFor(u.id)}">${u.id}</span>
          <span class="status-pill ${isExplain ? "change" : "unexplained"}">${isExplain ? "◆ Change" : "❓ Unexplained"}</span>
          <div><div class="req-text">${esc(u.label)}</div></div>
        </div>
        ${u.mechanism ? `<div class="mechanism">${md(u.mechanism)}</div>` : ""}
        <div class="anchors">${anchors}</div>
        ${isExplain ? `<div class="card-foot"><button class="verify-btn">${verified.has(u.id) ? "✓ Verified" : "Mark verified"}</button></div>` : ""}
      </div>`;
  }

  /* ---- code review (bug findings) section ---- */
  const sevPill = { high: "err", medium: "warn", low: "idle" };
  const sevLabel = { high: "▲ High", medium: "◆ Medium", low: "● Low" };
  if (r.bugs_ran) {
    railHtml += `<div class="rail-section-label">Code review · ${(r.bugs || []).length}</div>`;
    if (r.bugs_stale) {
      railHtml += `<div class="missing-note" style="margin:0 2px 8px">⚠ Carried from a previous run — the diff has changed since these findings. Use "↻ Findings" to refresh.</div>`;
    }
    if (!(r.bugs || []).length) {
      railHtml += `<div class="empty-state" style="padding:12px;font-size:12.5px">No findings — clean pass.</div>`;
    }
    for (const b of r.bugs || []) {
      const anchors = b.anchors.map((a) =>
        `<span class="anchor" data-file="${esc(a.file)}" data-start="${a.start}" data-end="${a.end}">${esc(a.file)}:${a.start}–${a.end}</span>`).join("");
      railHtml += `
        <div class="card" id="card-${b.id}" data-card-id="${b.id}" style="border-left:4px solid ${colorFor(b.id)}">
          <div class="card-top">
            <span class="rid" style="background:${colorFor(b.id)}">${b.id}</span>
            <span class="pill ${sevPill[b.severity]}" style="margin-top:2px">${sevLabel[b.severity]}</span>
            <div><div class="req-text">${esc(b.title)}</div></div>
          </div>
          ${b.detail ? `<div class="mechanism">${md(b.detail)}</div>` : ""}
          ${b.suggestion ? `<div class="missing-note">Fix: ${md(b.suggestion)}</div>` : ""}
          ${anchors ? `<div class="anchors">${anchors}</div>` : ""}
        </div>`;
    }
  }

  /* ---- header + assembly ---- */
  const pr = r.pr;
  const verifiedCount = claims.filter((id) => verified.has(id)).length;
  const netEffect = r.net_effect.length
    ? `<div class="net-effect"><span class="ne-label">Net effect</span>
        <ul>${r.net_effect.map((l) => `<li>${md(l)}</li>`).join("")}</ul></div>`
    : "";

  $("#review-content").innerHTML = `
    <header class="review-head">
      <div class="crumb" data-nav="command-center">‹ Command Center</div>
      <div class="pr-title">${esc(pr.title)} <span class="num">#${pr.number}</span></div>
      <div class="pr-meta">
        <span><code>${esc(pr.branch)}</code> → <code>${esc(pr.base_branch)}</code></span>
        ${pr.tickets.map(ticketBadge).join("")}
        ${r.mode === "requirements" ? `<span class="badge prdesc">PR description</span>` : `<span class="badge none">explain mode</span>`}
        <span class="verify-progress">
          <span><b id="verified-count">${verifiedCount}</b>/${claims.length} claims verified</span>
          <button class="btn small" id="rerun-btn">↻ Re-run</button>
          <button class="btn small" id="bugs-btn" ${state.bugsJobs[r.id] ? "disabled" : ""}>${
            state.bugsJobs[r.id] ? `<span class="spin"></span> Findings…` : "↻ Findings"
          }</button>
          ${pr.provider === "github" ? `<button class="btn small" id="publish-btn" title="${r.published_url ? esc("Already posted " + (r.published_at || "").slice(0, 10) + " — click to post again") : "Post this review to the PR on GitHub"}">${r.published_url ? "↗ Published" : "↑ Publish"}</button>` : ""}
          <button class="btn small" id="delete-btn" title="Delete this stored review">🗑</button>
          <a class="btn small" href="${esc(pr.url)}" target="_blank" rel="noopener">Open PR ↗</a>
        </span>
      </div>
      <div class="filterbar review-tabs">
        <span class="filter ${tab === "summary" ? "active" : ""}" data-review-tab="summary">Summary</span>
        <span class="filter ${tab === "diff" ? "active" : ""}" data-review-tab="diff">Review</span>
        <span class="filter ${tab === "findings" ? "active" : ""}" data-review-tab="findings">Findings${(r.bugs || []).length ? ` · ${r.bugs.length}` : " ·—"}</span>
        <span class="filter ${tab === "flow" ? "active" : ""}" data-review-tab="flow">⇄ Change flow${r.flow?.nodes?.length ? "" : " ·—"}</span>
      </div>
    </header>
    <div id="review-body-summary" ${tab === "summary" ? "" : 'style="display:none"'}>${summaryTabHtml(r)}</div>
    <div id="review-body-findings" ${tab === "findings" ? "" : 'style="display:none"'}>${findingsTabHtml(r)}</div>
    <div id="review-body-diff" ${tab === "diff" ? "" : 'style="display:none"'}>
      <div class="review-subhead">
        ${r.stale === true ? `<div class="error-banner" style="background:var(--amber-bg);color:var(--amber);border-color:#eed888;margin-top:10px">⚠ The PR has changed since this review was generated — results may be outdated. Use ↻ Re-run to refresh (verified state and reviewer-added requirements are preserved).</div>` : ""}
        ${netEffect}
        ${legendHtml(r)}
        ${sourcePanelHtml(r)}
      </div>
      <div class="main">
        <div class="diff-panel" id="diff-panel">${diffHtml}</div>
        <aside class="rail" id="rail">${railHtml}</aside>
      </div>
    </div>
    <div id="review-body-flow" ${tab === "flow" ? "" : 'style="display:none"'}>
      ${flowPanelHtml(r)}
    </div>`;

  placeFlowLabels(r); // measured-label pass — needs the SVG visible in the DOM (DECISIONS #23)

  // amber gutter for unexplained lines (requirements mode only — explain mode marks nothing)
  if (!isExplain) {
    for (const u of r.unexplained)
      for (const a of u.anchors)
        for (let ln = a.start; ln <= a.end; ln++)
          document.querySelector(`tr[data-file="${CSS.escape(a.file)}"][data-line="${ln}"]`)?.classList.add("unx");
  }

  $("#rerun-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    startReview({ provider: pr.provider, repo: pr.repo, number: pr.number, force: true });
  });
  $("#bugs-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    runCodeReviewJob(r.id);
  });
  $("#delete-btn").addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!confirm("Delete this stored review? Verified state and findings will be lost.")) return;
    try {
      await api(`/api/reviews/${r.id}`, { method: "DELETE" });
      state.review = null;
      toast("Review deleted");
      show("command-center");
      loadPRs();
    } catch (err) {
      toast(`Delete failed: ${err.message}`, true);
    }
  });
  $("#publish-btn")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    const nComments = r.links.filter((l) => l.anchors.length).length + Math.min(r.unexplained.length, 5);
    const dupWarning = r.published_url ? `\n\n⚠ Already published ${(r.published_at || "").slice(0, 10)} — this will post a SECOND review.` : "";
    if (!confirm(`Post this review to ${pr.repo}#${pr.number} on GitHub?\n\nThis publishes a summary comment plus up to ${nComments} inline comments, visible to everyone on the PR.${dupWarning}`)) return;
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.innerHTML = `<span class="spin"></span> Publishing…`;
    try {
      const res = await api(`/api/reviews/${r.id}/publish`, { method: "POST", body: { dry_run: false } });
      if (res.ok) {
        toast(res.message || "Published ✓");
        r.published_url = res.url || r.published_url || "posted";
        r.published_at = res.published_at || "";
        renderReview();
        if (res.url) window.open(res.url, "_blank");
      } else {
        toast(`Publish failed: ${res.message}`, true);
      }
    } catch (err) {
      toast(`Publish failed: ${err.message}`, true);
    }
    btn.disabled = false;
    btn.textContent = "↑ Publish";
  });
}

async function runCodeReviewJob(rid) {
  let job;
  try {
    job = await api(`/api/reviews/${rid}/code-review`, { method: "POST" });
  } catch (e) {
    toast(`Can't start code review: ${e.message}`, true);
    return;
  }
  state.bugsJobs[rid] = job.job_id;
  state.currentJob = job.job_id;
  if (state.review?.id === rid) renderReview(); // button → spinner

  $("#overlay-title").textContent = "Running code review…";
  $("#overlay-sub").textContent = "Starting — a full /code-review run usually takes 2–10 minutes";
  $("#overlay-error").innerHTML = "";
  $("#overlay-stages").innerHTML = `<div class="stage-row now"><span class="mark"><span class="spin"></span></span><span>Claude Code /code-review</span></div>`;
  $("#overlay-actions").style.display = "block";
  $("#overlay-cancel").style.display = "";
  $("#overlay-close").style.display = "";
  $("#overlay-close").textContent = "Continue in background";
  $("#overlay").classList.add("visible");

  watchBugsJob(rid, job.job_id, Date.now());
}

async function submitNewRequirement(rid, text) {
  let job;
  try {
    job = await api(`/api/reviews/${rid}/requirements`, { method: "POST", body: { text } });
  } catch (e) {
    toast(`Could not add requirement: ${e.message}`, true);
    return;
  }
  state.addReqJobs[rid] = job.job_id;
  if (state.review?.id === rid) renderReview(); // card → spinner
  watchAddReqJob(rid, job.job_id);
}

function watchAddReqJob(rid, jobId) {
  if (state.addReqWatch[rid]) return;
  state.addReqWatch[rid] = setInterval(async () => {
    let st;
    try {
      st = await api(`/api/jobs/${jobId}`);
    } catch {
      clearInterval(state.addReqWatch[rid]);
      delete state.addReqWatch[rid];
      delete state.addReqJobs[rid];
      if (state.review?.id === rid) renderReview();
      toast("Requirement-mapping job was lost (server restarted) — try again.", true);
      return;
    }
    if (!st.done) return;
    clearInterval(state.addReqWatch[rid]);
    delete state.addReqWatch[rid];
    delete state.addReqJobs[rid];
    if (st.error) {
      toast(`Requirement mapping failed: ${st.error}`, true);
      if (state.review?.id === rid) renderReview();
    } else {
      toast("Requirement mapped ✓");
      if (state.review?.id === rid) await openReview(rid);
    }
  }, 1500);
}

function watchBugsJob(rid, jobId, startedMs) {
  if (state.bugsWatch[rid]) return; // already watching
  state.bugsWatch[rid] = setInterval(async () => {
    let st;
    try {
      st = await api(`/api/jobs/${jobId}`);
    } catch {
      // job vanished (e.g. server restart) — stop pretending it's running
      clearInterval(state.bugsWatch[rid]);
      delete state.bugsWatch[rid];
      delete state.bugsJobs[rid];
      if (state.review?.id === rid) renderReview();
      toast("Code review job was lost (server restarted) — run it again.", true);
      return;
    }
    const mins = Math.floor((Date.now() - startedMs) / 60000);
    const elapsed = mins ? ` · running ${mins}m` : "";
    if ($("#overlay").classList.contains("visible")) {
      $("#overlay-sub").textContent = (st.detail || rid) + elapsed;
    }
    if (!st.done) return;

    clearInterval(state.bugsWatch[rid]);
    delete state.bugsWatch[rid];
    delete state.bugsJobs[rid];
    $("#overlay").classList.remove("visible");
    $("#overlay-close").textContent = "Close";
    if (st.error) {
      toast(`Code review failed: ${st.error}`, true);
      if (state.review?.id === rid) renderReview();
    } else {
      toast("Code review finished ✓");
      if (state.review?.id === rid) await openReview(rid);
      loadPRs();
    }
  }, 1500);
}

/* ---- review interactions (shared with mockup behavior) ---- */

function clearHl() {
  document.querySelectorAll("tr.hl").forEach((el) => el.classList.remove("hl"));
  document.querySelectorAll(".card.active").forEach((el) => el.classList.remove("active"));
}

function expandFile(fi) {
  const wrap = document.querySelector(`.file[data-file-idx="${fi}"]`);
  if (!wrap || wrap.querySelector("table.diff")) return;
  const note = wrap.querySelector(".file-collapsed-note");
  if (!note || !state.buildFileTable) return;
  note.outerHTML = state.buildFileTable(state.review.files[fi]);
  const r = state.review;
  if (r.mode !== "explain") {  // re-apply amber gutter marks for this file
    for (const u of r.unexplained)
      for (const a of u.anchors)
        if (a.file === r.files[fi].path)
          for (let ln = a.start; ln <= a.end; ln++)
            document.querySelector(`tr[data-file="${CSS.escape(a.file)}"][data-line="${ln}"]`)?.classList.add("unx");
  }
}

function highlightAnchors(anchors, scroll = true) {
  // anchors inside collapsed large files need their tables built first
  for (const a of anchors) {
    const fi = state.review?.files.findIndex((f) => f.path === a.file);
    if (fi >= 0) expandFile(fi);
  }
  let first = null;
  for (const a of anchors) {
    for (let ln = a.start; ln <= a.end; ln++) {
      const tr = document.querySelector(`tr[data-file="${CSS.escape(a.file)}"][data-line="${ln}"]`);
      if (tr) { tr.classList.add("hl"); if (!first) first = tr; }
    }
  }
  if (scroll && first) first.scrollIntoView({ behavior: "smooth", block: "center" });
}

function activateCard(cardId, scroll = true) {
  clearHl();
  const card = document.getElementById("card-" + cardId);
  if (!card) return;
  card.classList.add("active");
  const r = state.review;
  const link = r.links.find((l) => l.requirement_id === cardId)
    || r.unexplained.find((u) => u.id === cardId)
    || (r.bugs || []).find((b) => b.id === cardId);
  if (link) highlightAnchors(link.anchors, scroll);
}

async function toggleVerified(card) {
  const r = state.review;
  const id = card.dataset.cardId;
  const nowVerified = !card.classList.contains("verified");
  card.classList.toggle("verified", nowVerified);
  card.querySelector(".verify-btn").textContent = nowVerified ? "✓ Verified" : "Mark verified";
  try {
    const res = await api(`/api/reviews/${r.id}/verify`, {
      method: "POST",
      body: { card_id: id, verified: nowVerified },
    });
    r.verified = res.verified;
  } catch (e) {
    toast(`Could not save verification: ${e.message}`, true);
    card.classList.toggle("verified", !nowVerified);
    return;
  }
  const claims = claimIds(r);
  const count = claims.filter((c) => r.verified.includes(c)).length;
  const el = $("#verified-count");
  if (el) el.textContent = count;
}

/* ================================================================ settings */

function pillFor(section) {
  const note = state.testNotes[section];
  if (note) return note.ok ? `<span class="pill ok">● Connected</span>` : `<span class="pill err">● Error</span>`;
  const s = state.settings[section];
  const configured = {
    github: () => s.token.set || (s.gh_cli && s.gh_cli.active),
    bitbucket: () => s.username && s.app_password.set,
    linear: () => s.api_key.set,
    jira: () => s.site_url && s.email && s.api_token.set,
  }[section]();
  return configured ? `<span class="pill ok">● Connected</span>` : `<span class="pill idle">○ Not configured</span>`;
}

function testNoteHtml(section) {
  const note = state.testNotes[section];
  if (!note) return "";
  return `<div class="int-note ${note.ok ? "ok" : "err"}">${note.ok ? "✓ " : ""}${esc(note.message)}</div>`;
}

function secretInput(section, field, placeholder) {
  const v = state.settings[section][field];
  return `<input type="password" data-field="${field}" placeholder="${v.set ? esc(v.hint) : placeholder}" autocomplete="off">`;
}

function repoTags(section) {
  const repos = state.settings[section].repos || [];
  return `<div class="int-note">Repos shown in Command Center:</div>
    <div class="repo-tags" data-section="${section}">
      ${repos.map((r) => `<span class="repo-tag">${esc(r)} <span class="x" data-remove="${esc(r)}">×</span></span>`).join("")}
      <span class="repo-tag add" data-add-repo="${section}">+ add repo</span>
    </div>`;
}

function renderSettings() {
  if (!state.settings) return;
  const c = state.claude || { ready: false, checks: [], summary: "checking…", fix: "" };
  const model = state.settings.claude.model || "sonnet";

  $("#settings-content").innerHTML = `
    <div class="set-section-label">Claude backend</div>
    <div class="integration" data-section="claude">
      <div class="int-head">
        <span class="int-icon" style="background:#D97757">✳</span>
        <div>
          <div class="int-name">Claude Code (subscription)</div>
          <div class="int-desc">Runs extract &amp; map via <code>claude -p</code> on your claude.ai login — no API key.</div>
        </div>
        ${c.ready ? `<span class="pill ok">● Ready</span>` : `<span class="pill err">● Not ready</span>`}
      </div>
      ${c.checks.map((ch) => `<div class="checkline"><span class="${ch.ok ? "ok" : "bad"}">${ch.ok ? "✓" : "✗"}</span> ${esc(ch.label)}</div>`).join("")}
      ${c.fix ? `<div class="int-note err">${esc(c.fix)}</div>` : `<div class="int-note">Not logged in? Run <code>claude login</code> in a terminal, then hit Re-check.</div>`}
      <div class="int-body">
        <button class="btn" id="claude-recheck">Re-check</button>
        <select id="claude-model" title="Model used for extract & map">
          ${["sonnet", "opus", "haiku"].map((m) => `<option value="${m}" ${m === model ? "selected" : ""}>${m[0].toUpperCase() + m.slice(1)}</option>`).join("")}
        </select>
      </div>
      <div class="int-body" style="margin-top:8px">
        <select id="claude-skill" title="Which flow produces the Findings tab" style="max-width:280px">
          <option value="">/code-review — Claude Code&#39;s review skill (default)</option>
          ${(state.skills?.skills || []).map((sk) =>
            `<option value="${esc(sk.name)}" ${sk.name === (state.skills?.selected || "") ? "selected" : ""} title="${esc(sk.description)}">/${esc(sk.name)}</option>`).join("")}
        </select>
        <input type="text" id="claude-skills-dir" placeholder="skills dir (default ~/.claude/skills)"
          value="${esc(state.settings.claude.skills_dir || "")}" autocomplete="off" style="flex:1">
      </div>
      <div class="int-note">Findings are produced by Claude Code's <code>/code-review</code> skill by default;
        pick one of your own skills to replace it. Discovered live from
        ${esc(state.skills?.dir || "~/.claude/skills")} (${(state.skills?.skills || []).length} found);
        a skill's own <code>allowed-tools</code> applies when declared.</div>
    </div>

    <div class="set-section-label">PR hosts</div>
    <div class="integration" data-section="github">
      <div class="int-head">
        <span class="int-icon" style="background:#1f2328"></span>
        <div>
          <div class="int-name">GitHub</div>
          <div class="int-desc">Personal access token with <code>repo</code> read scope.</div>
        </div>
        ${pillFor("github")}
      </div>
      ${state.settings.github.gh_cli?.active
        ? `<div class="checkline"><span class="ok">✓</span> Signed in via GitHub CLI browser login — no token needed</div>
           <div class="int-note">Using your <code>gh auth login</code> session. Paste a token below only to override it.</div>`
        : (!state.settings.github.token.set
          ? `<div class="int-note">Tip: install the GitHub CLI and run <code>gh auth login</code> for normal browser login — or paste a token below.</div>`
          : "")}
      <div class="int-body">
        ${secretInput("github", "token", "ghp_… personal access token")}
        <button class="btn" data-save="github">Save</button>
        <button class="btn" data-test="github">Test connection</button>
      </div>
      ${testNoteHtml("github")}
      ${repoTags("github")}
    </div>
    <div class="integration" data-section="bitbucket">
      <div class="int-head">
        <span class="int-icon" style="background:#2684FF">B</span>
        <div>
          <div class="int-name">Bitbucket</div>
          <div class="int-desc">Username + app password with pull request read scope.</div>
        </div>
        ${pillFor("bitbucket")}
      </div>
      <div class="int-body">
        <input type="text" data-field="username" placeholder="username" value="${esc(state.settings.bitbucket.username)}" autocomplete="off">
        ${secretInput("bitbucket", "app_password", "ATBB… app password")}
        <button class="btn" data-save="bitbucket">Save</button>
        <button class="btn" data-test="bitbucket">Test connection</button>
      </div>
      ${testNoteHtml("bitbucket")}
      ${repoTags("bitbucket")}
    </div>

    <div class="set-section-label">Ticket sources</div>
    <div class="integration" data-section="linear">
      <div class="int-head">
        <span class="int-icon" style="background:#5E6AD2">◆</span>
        <div>
          <div class="int-name">Linear</div>
          <div class="int-desc">Detects refs like <code>ENG-142</code> in branch, title, or description.</div>
        </div>
        ${pillFor("linear")}
      </div>
      <div class="int-body">
        ${secretInput("linear", "api_key", "lin_api_… personal API key")}
        <button class="btn" data-save="linear">Save</button>
        <button class="btn" data-test="linear">Test connection</button>
      </div>
      ${testNoteHtml("linear")}
    </div>
    <div class="integration" data-section="jira">
      <div class="int-head">
        <span class="int-icon" style="background:#0052CC">J</span>
        <div>
          <div class="int-name">Jira</div>
          <div class="int-desc">Detects refs like <code>BILL-203</code>. Site URL + account email + API token.</div>
        </div>
        ${pillFor("jira")}
      </div>
      <div class="int-body">
        <input type="text" data-field="site_url" placeholder="https://your-site.atlassian.net" value="${esc(state.settings.jira.site_url)}" autocomplete="off">
        <input type="text" data-field="email" placeholder="account email" value="${esc(state.settings.jira.email)}" autocomplete="off">
        ${secretInput("jira", "api_token", "API token")}
        <button class="btn" data-save="jira">Save</button>
        <button class="btn" data-test="jira">Test connection</button>
      </div>
      ${testNoteHtml("jira")}
    </div>
    <div class="integration">
      <div class="int-head">
        <span class="int-icon" style="background:#59636e">¶</span>
        <div>
          <div class="int-name">PR description</div>
          <div class="int-desc">Always on — used alone when no ticket is linked (explain mode).</div>
        </div>
        <span class="pill ok">● Built-in</span>
      </div>
    </div>`;

  $("#claude-recheck").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.innerHTML = `<span class="spin"></span> Checking…`;
    await refreshClaude(true);
    btn.disabled = false;
    btn.textContent = "Re-check";
  });
  $("#claude-model").addEventListener("change", async (e) => {
    await api("/api/settings/claude", { method: "PUT", body: { values: { model: e.target.value } } });
    toast(`Model set to ${e.target.value}`);
    state.settings.claude.model = e.target.value;
  });
  $("#claude-skill")?.addEventListener("change", async (e) => {
    await api("/api/settings/claude", { method: "PUT", body: { values: { review_skill: e.target.value } } });
    toast(e.target.value ? `Findings will use /${e.target.value}` : "Findings will use built-in /code-review");
    if (state.skills) state.skills.selected = e.target.value;
  });
  $("#claude-skills-dir")?.addEventListener("change", async (e) => {
    await api("/api/settings/claude", { method: "PUT", body: { values: { skills_dir: e.target.value.trim() } } });
    state.settings.claude.skills_dir = e.target.value.trim();
    state.skills = await api("/api/skills").catch(() => state.skills);
    renderSettings();  // re-list skills from the new directory
    toast("Skills directory updated");
  });
}

async function saveSection(section, card) {
  const values = {};
  card.querySelectorAll("input[data-field]").forEach((inp) => {
    if (inp.type === "password") {
      if (inp.value) values[inp.dataset.field] = inp.value;  // blank = keep stored secret
    } else {
      values[inp.dataset.field] = inp.value.trim();
    }
  });
  try {
    state.settings = await api(`/api/settings/${section}`, { method: "PUT", body: { values } });
    delete state.testNotes[section];
    renderSettings();
    toast("Saved");
    loadPRs();
  } catch (e) {
    toast(`Save failed: ${e.message}`, true);
  }
}

async function testSection(section, btn) {
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span> Testing…`;
  try {
    state.testNotes[section] = await api(`/api/settings/${section}/test`, { method: "POST" });
  } catch (e) {
    state.testNotes[section] = { ok: false, message: e.message };
  }
  renderSettings();
}

async function changeRepo(section, add, remove) {
  try {
    state.settings = await api(`/api/settings/${section}/repos`, {
      method: "POST",
      body: { add: add || null, remove: remove || null },
    });
    renderSettings();
    loadPRs();
  } catch (e) {
    toast(`Repo change failed: ${e.message}`, true);
  }
}

/* ================================================================ global events */

document.addEventListener("click", (e) => {
  const nav = e.target.closest("[data-nav]");
  if (nav) {
    const target = nav.dataset.nav;
    if (target === "settings") renderSettings();
    if (target === "command-center") loadPRs();
    show(target);
    return;
  }

  const pinBtn = e.target.closest("[data-pin]");
  if (pinBtn) {
    api("/api/pins", { method: "POST", body: {
      provider: pinBtn.dataset.provider, repo: pinBtn.dataset.repo,
      number: Number(pinBtn.dataset.number), pinned: true,
    } }).then(() => {
      pinBtn.textContent = "✓ Added";
      pinBtn.disabled = true;
      toast("Added to Command Center");
    }).catch((err) => toast(`Could not add: ${err.message}`, true));
    e.stopPropagation();
    return;
  }

  const unpinEl = e.target.closest("[data-unpin]");
  if (unpinEl) {
    api("/api/pins", { method: "POST", body: {
      provider: unpinEl.dataset.provider, repo: unpinEl.dataset.repo,
      number: Number(unpinEl.dataset.number), pinned: false,
    } }).then(loadPRs).catch((err) => toast(`Could not remove: ${err.message}`, true));
    e.stopPropagation();
    return;
  }

  const clearSearch = e.target.closest("[data-clear-search]");
  if (clearSearch) {
    $("#url-input").value = "";
    loadPRs();
    return;
  }

  const expandBtn = e.target.closest("[data-expand-file]");
  if (expandBtn) {
    expandFile(Number(expandBtn.dataset.expandFile));
    return;
  }

  if (e.target.closest("[data-retry-prs]")) {
    $("#cc-content").innerHTML = `<div class="empty-state">Loading…</div>`;
    loadPRs();
    return;
  }

  const fndChip = e.target.closest("[data-fnd-cat]");
  if (fndChip) {
    const cat = fndChip.dataset.fndCat;
    document.querySelectorAll("[data-fnd-cat]").forEach((c) =>
      c.classList.toggle("active", c === fndChip));
    let shown = 0;
    document.querySelectorAll(".fnd-row").forEach((row) => {
      const on = cat === "all" || row.dataset.cat === cat;
      row.style.display = on ? "" : "none";
      if (on) shown++;
    });
    // hide a severity heading once every row under it is filtered out
    document.querySelectorAll(".fnd-group").forEach((h) => {
      let any = false;
      for (let n = h.nextElementSibling; n && !n.classList.contains("fnd-group");
           n = n.nextElementSibling) {
        if (n.style.display !== "none") { any = true; break; }
      }
      h.style.display = any ? "" : "none";
    });
    const none = document.querySelector(".fnd-none");
    if (none) none.style.display = shown ? "none" : "";
    return;
  }

  const reviewTab = e.target.closest("[data-review-tab]");
  if (reviewTab) {
    showReviewTab(reviewTab.dataset.reviewTab);
    return;
  }

  const ccFilter = e.target.closest("[data-cc-filter]");
  if (ccFilter) {
    state.ccFilter = ccFilter.dataset.ccFilter;
    renderCC();
    return;
  }

  const rtag = e.target.closest(".rtag");
  if (rtag) {
    activateCard(rtag.dataset.card, false);
    document.getElementById("card-" + rtag.dataset.card)?.scrollIntoView({ behavior: "smooth", block: "center" });
    e.stopPropagation();
    return;
  }

  const fnode = e.target.closest(".fnode, .fedge, .elabel-g");
  if (fnode) {
    if (state.reviewTab === "flow") showReviewTab("diff"); // flow clicks land in the Review tab
    if (fnode.dataset.card) {
      activateCard(fnode.dataset.card, false);
      document.getElementById("card-" + fnode.dataset.card)?.scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (fnode.dataset.anchors) {
      clearHl();
      highlightAnchors(JSON.parse(fnode.dataset.anchors));
    }
    return;
  }

  const lgItem = e.target.closest(".lg-item, .source-body u, .flow-ref");
  if (lgItem) {
    if (state.reviewTab === "flow") showReviewTab("diff"); // summary chips land on their card
    activateCard(lgItem.dataset.card, false);
    document.getElementById("card-" + lgItem.dataset.card)?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }

  const prRow = e.target.closest(".pr-row");
  if (prRow) {
    startReview({
      provider: prRow.dataset.provider,
      repo: prRow.dataset.repo,
      number: Number(prRow.dataset.number),
    });
    return;
  }

  const verifyBtn = e.target.closest(".verify-btn");
  if (verifyBtn) {
    toggleVerified(verifyBtn.closest(".card"));
    e.stopPropagation();
    return;
  }

  const sumCheck = e.target.closest(".sum-check");
  if (sumCheck) {
    const id = sumCheck.dataset.verifyCard;
    api(`/api/reviews/${state.review.id}/verify`, { method: "POST", body: { card_id: id, verified: sumCheck.checked } })
      .then((res) => {
        state.review.verified = res.verified;
        const claims = claimIds(state.review);
        const el = $("#verified-count");
        if (el) el.textContent = claims.filter((c) => res.verified.includes(c)).length;
        const card = document.getElementById("card-" + id);
        if (card) {
          card.classList.toggle("verified", sumCheck.checked);
          const vb = card.querySelector(".verify-btn");
          if (vb) vb.textContent = sumCheck.checked ? "✓ Verified" : "Mark verified";
        }
      })
      .catch((err) => {
        toast(`Could not save: ${err.message}`, true);
        sumCheck.checked = !sumCheck.checked;
      });
    e.stopPropagation();
    return;
  }

  const gotoCard = e.target.closest("[data-goto-card]");
  if (gotoCard && !e.target.closest(".anchor")) {
    showReviewTab("diff");
    activateCard(gotoCard.dataset.gotoCard, true);
    document.getElementById("card-" + gotoCard.dataset.gotoCard)?.scrollIntoView({ block: "nearest" });
    return;
  }

  const gotoFile = e.target.closest("[data-goto-file]");
  if (gotoFile) {
    const fi = Number(gotoFile.dataset.gotoFile);
    showReviewTab("diff");
    expandFile(fi);
    document.querySelector(`.file[data-file-idx="${fi}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }

  const anchor = e.target.closest(".anchor");
  if (anchor) {
    if (state.reviewTab && state.reviewTab !== "diff") showReviewTab("diff"); // summary anchors land in the diff
    clearHl();
    anchor.closest(".card")?.classList.add("active");
    highlightAnchors([{ file: anchor.dataset.file, start: +anchor.dataset.start, end: +anchor.dataset.end }]);
    e.stopPropagation();
    return;
  }

  const chip = e.target.closest(".chip");
  if (chip) {
    activateCard(chip.dataset.card, false);
    document.getElementById("card-" + chip.dataset.card)?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }

  const addReq = e.target.closest("#add-req-card");
  if (addReq) {
    const rid = state.review.id;
    addReq.outerHTML = `
      <div class="card add-req-edit">
        <textarea class="add-req-input" rows="2" placeholder='e.g. "Must not change the public API" — mapped against the diff like extracted requirements'></textarea>
        <div class="card-foot">
          <button class="btn small primary" id="add-req-go">Map it</button>
          <button class="btn small" id="add-req-cancel">Cancel</button>
        </div>
      </div>`;
    const input = document.querySelector(".add-req-input");
    input.focus();
    document.getElementById("add-req-go").addEventListener("click", () => {
      const text = input.value.trim();
      if (text) submitNewRequirement(rid, text);
      else renderReview();
    });
    document.getElementById("add-req-cancel").addEventListener("click", () => renderReview());
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey) && input.value.trim()) submitNewRequirement(rid, input.value.trim());
      if (ev.key === "Escape") renderReview();
    });
    return;
  }

  const card = e.target.closest(".card");
  if (card && card.dataset.cardId) { activateCard(card.dataset.cardId); return; }

  const saveBtn = e.target.closest("[data-save]");
  if (saveBtn) { saveSection(saveBtn.dataset.save, saveBtn.closest(".integration")); return; }

  const testBtn = e.target.closest("[data-test]");
  if (testBtn) { testSection(testBtn.dataset.test, testBtn); return; }

  const removeRepo = e.target.closest("[data-remove]");
  if (removeRepo) {
    const section = removeRepo.closest(".repo-tags").dataset.section;
    changeRepo(section, null, removeRepo.dataset.remove);
    return;
  }

  const addRepo = e.target.closest("[data-add-repo]");
  if (addRepo) {
    const section = addRepo.dataset.addRepo;
    const input = document.createElement("input");
    input.className = "repo-add-input";
    input.placeholder = "owner/repo — Enter to add";
    addRepo.replaceWith(input);
    input.focus();
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && input.value.trim()) changeRepo(section, input.value.trim(), null);
      if (ev.key === "Escape") renderSettings();
    });
    input.addEventListener("blur", () => setTimeout(renderSettings, 150));
    return;
  }
});

$("#url-go").addEventListener("click", goFromInput);
$("#url-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") goFromInput();
});
$("#cc-refresh").addEventListener("click", () => { loadPRs(); toast("Refreshing…"); });
$("#overlay-close").addEventListener("click", () => $("#overlay").classList.remove("visible"));
$("#overlay-cancel").addEventListener("click", async () => {
  if (!state.currentJob) return;
  try {
    await api(`/api/jobs/${state.currentJob}/cancel`, { method: "POST" });
    toast("Cancelling…");
  } catch (e) {
    toast(`Cancel failed: ${e.message}`, true);
  }
});

/* ================================================================ init */

(async function init() {
  show("command-center");
  state.settings = await api("/api/settings").catch(() => null);
  state.skills = await api("/api/skills").catch(() => null);
  refreshClaude(false);
  loadPRs();
  // keep the Command Center fresh: closed/merged PRs drop off without a manual refresh
  setInterval(() => {
    if (document.querySelector("#screen-command-center.visible") && !state.searchActive) loadPRs();
  }, 60_000);
})();
