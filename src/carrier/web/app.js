/* SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (C) 2026 Thomas and Carrier contributors */
/* Carrier desktop page. Hosts the three tools as tabs (Ready Room, Flight
 * Deck, Debrief).
 *
 * Talks to Python only through window.pywebview.api (see desktop/api.py).
 * Every API call resolves to {ok, data} or {ok:false, error}; `api()`
 * below turns errors into exceptions shown in the banner.
 *
 * SECURITY: diff text and model answers are untrusted. They are inserted
 * with textContent, or through md(), which escapes ALL HTML first and only
 * then adds a few safe tags (<b>, <i>, <code>, <pre>, <ul>, <li>, <p>, <br>).
 * Never assign untrusted strings to innerHTML directly.
 *
 * Sections: helpers · theme · start screen · review screen · diff &
 * selection · guide & chat · decisions · finish · settings · keyboard.
 */
"use strict";

/* ------------------------------------------------------------------ helpers */

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
};
const show = (node, on = true) => node.classList.toggle("hidden", !on);

const state = {
  projectId: null,     // project being reviewed (null: a session's own copy, see worktreeSid)
  worktreeSid: null,   // Flight Deck session whose own copy (worktree) is reviewed
  folder: null,        // the main folder's inspect_folder() result
  freshStart: false,   // user chose "start a new one" over resuming
  overview: null,      // session overview
  current: null,       // current item id
  item: null,          // current item detail
  split: false,        // side-by-side diff
  sel: null,           // current Selection (from Python) or null
  range: null,         // {anchor, cursor, side} while highlighting
  dragging: false,
  settings: null,
  explaining: new Map(), // item id -> Promise of its explanation (one call per item)
  finished: false,
  switching: false,      // true while the diff view mode is changing
  finishing: false,      // guards against a double-clicked "Finish"
};

async function api(name, ...args) {
  const res = await window.pywebview.api[name](...args);
  if (!res || !res.ok) throw new Error(res ? res.error : "No response from Carrier.");
  return res.data;
}

function banner(msg, kind = "error") {
  const b = $("#banner");
  b.textContent = msg;
  b.className = "banner" + (kind === "info" ? " info" : "");
  show(b, !!msg);
  if (msg) setTimeout(() => { if (b.textContent === msg) show(b, false); }, 9000);
}

const guarded = (fn) => async (...args) => {
  try { return await fn(...args); } catch (e) { banner(e.message || String(e)); }
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* Inline markdown on ALREADY-ESCAPED text. */
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,!?:;]|$)/g, "$1<i>$2</i>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1<i>$2</i>");
}

/* Tiny, safe markdown: escapes everything, then adds a few tags. */
function md(src) {
  const parts = String(src ?? "").split(/```[^\n]*\n?/);
  let html = "";
  parts.forEach((p, i) => {
    if (i % 2 === 1) { html += `<pre><code>${esc(p.replace(/\n$/, ""))}</code></pre>`; return; }
    for (const block of p.split(/\n{2,}/)) {
      const t = block.trim();
      if (!t) continue;
      const lines = t.split("\n");
      const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => inline(esc(c.trim())));
      if (lines.length >= 2 && lines.every((l) => /^\s*\|/.test(l)) && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[1])) {
        html += "<table><thead><tr>" + cells(lines[0]).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>" +
          lines.slice(2).map((l) => "<tr>" + cells(l).map((c) => `<td>${c}</td>`).join("") + "</tr>").join("") +
          "</tbody></table>";
      } else if (lines.every((l) => /^\s*[-*] /.test(l))) {
        html += "<ul>" + lines.map((l) => `<li>${inline(esc(l.replace(/^\s*[-*] /, "")))}</li>`).join("") + "</ul>";
      } else if (lines.every((l) => /^\s*\d+[.)] /.test(l))) {
        html += "<ol>" + lines.map((l) => `<li>${inline(esc(l.replace(/^\s*\d+[.)] /, "")))}</li>`).join("") + "</ol>";
      } else if (/^#{1,4} /.test(t)) {
        html += `<p><b>${inline(esc(t.replace(/^#+ /, "")))}</b></p>`;
      } else {
        html += `<p>${lines.map((l) => inline(esc(l))).join("<br>")}</p>`;
      }
    }
  });
  return html;
}

function setMd(node, text) { node.classList.add("md"); node.innerHTML = md(text); }

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  } else fallbackCopy(text);
  banner("Copied.", "info");
}
function fallbackCopy(text) {
  const t = el("textarea"); t.value = text; document.body.appendChild(t);
  t.select(); try { document.execCommand("copy"); } catch (_) { /* ignore */ }
  t.remove();
}

/* Python -> page events (progress while preparing). */
window.carrierEvent = (kind, data) => {
  if (kind === "progress") {
    const log = $("#progress-log");
    show(log);
    log.textContent += data + "\n";
    log.scrollTop = log.scrollHeight;
  }
};

/* -------------------------------------------------------------------- tools */

/* Which tool tab is showing. Debrief keeps its own sub-views (start/review)
 * and they are restored when you come back to the tab. */
let debriefView = "#view-start";
function activeTool() {
  const on = document.querySelector(".tool.on");
  return on ? on.dataset.tool : "debrief";
}

/* Show a Debrief sub-view, or remember it if another tool tab is open. */
function showDebriefView(sel) {
  debriefView = sel;
  if (activeTool() !== "debrief") return;
  show($("#view-start"), sel === "#view-start");
  show($("#view-review"), sel === "#view-review");
  const reviewing = sel === "#view-review";
  ["#btn-finish", "#progress-pill", "#btn-home"].forEach((s) => show($(s), reviewing));
}

function showTool(tool) {
  document.querySelectorAll(".tool").forEach((b) => {
    const on = b.dataset.tool === tool;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on);
  });
  show($("#view-readyroom"), tool === "readyroom");
  show($("#view-flightdeck"), tool === "flightdeck");
  if (tool === "flightdeck") fdOpen();
  if (fd.overview) updateAlerts(fd.overview);
  if (tool === "readyroom") rrOpen();
  const inDebrief = tool === "debrief";
  if (!inDebrief) {
    debriefView = $("#view-review").classList.contains("hidden") ? "#view-start" : "#view-review";
    show($("#view-start"), false); show($("#view-review"), false);
  } else {
    show($(debriefView));
    // Projects may have been added or changed in the other tabs: reload the list.
    if (debriefView === "#view-start") refreshReviewStart();
  }
  const reviewing = inDebrief && debriefView === "#view-review";
  ["#btn-finish", "#progress-pill", "#btn-home"].forEach((sel) => show($(sel), reviewing));
}

/* -------------------------------------------------------------------- theme */

function applyTheme(settings) {
  state.settings = settings;
  const t = settings.theme_resolved;
  const root = document.documentElement.style;
  for (const [key, value] of Object.entries(t.colors)) root.setProperty("--" + key.replace(/_/g, "-"), value);
  root.setProperty("--strike", t.strike_removed ? "line-through" : "none");
  root.setProperty("--code-size", (settings.font_size || 14) + "px");
  const widths = { normal: "960px", wide: "1440px", wider: "1920px", full: "100000px" };
  root.setProperty("--content-max", widths[settings.content_width] || widths.normal);
  document.body.classList.toggle("content-center", !!settings.content_center);
}

/* ------------------------------------------------------------- start screen */

async function initStart() {
  showDebriefView("#view-start");
  $("#project-name").textContent = "";
  await renderReviewProjects();
}

/* Projects to review (from the Ready Room). */
async function renderReviewProjects() {
  const box = $("#db-projects");
  box.replaceChildren();
  const projects = await api("db_projects");
  if (!projects.length) {
    box.appendChild(el("p", "muted", "No projects yet. Set one up in the Ready Room first."));
    const go = el("button", "ghost", "Open the Ready Room ▸");
    go.onclick = () => showTool("readyroom");
    box.appendChild(go);
    return;
  }
  for (const p of projects) {
    const b = el("button");
    b.dataset.project = p.id;
    b.classList.toggle("on", state.projectId === p.id);
    b.appendChild(el("span", "", p.name));
    const r = p.open_review;
    const extra = p.folders.length > 1 ? ` · ${p.folders.length} folders` : "";
    const status = !p.ready ? "needs setup" : r && r.status === "in_progress"
      ? `review in progress: ${r.decided}/${r.items} decided` : "";
    b.appendChild(el("span", "path", p.path + extra + (status ? ` · ${status}` : "")));
    b.onclick = guarded(async () => showProject(await api("db_project", p.id)));
    box.appendChild(b);
  }
}

const refreshReviewStart = guarded(async () => {
  await renderReviewProjects();
  if (state.projectId == null) return;
  try { showProject(await api("db_project", state.projectId)); }
  catch (_) { state.projectId = null; show($("#folder-card"), false); }  // project was removed
});

function renderChecks(c) {
  const ul = $("#checks");
  ul.replaceChildren();
  const add = (ok, text, hint) => {
    const li = el("li", ok ? "ok" : "bad", text);
    if (!ok && hint) li.appendChild(el("span", "hint", hint));
    ul.appendChild(li);
  };
  add(c.git, "Git installed", c.hints.git);
  add(c.claude_cli, "Claude Code installed", c.hints.claude);
  add(c.agent_sdk, "Claude Agent SDK available", "Reinstall Carrier.");
}

/* A project review: show every folder's status, then the start/resume box. */
function showProject(info) {
  state.projectId = info.id;
  state.worktreeSid = null;
  document.querySelectorAll("#db-projects button").forEach((b) =>
    b.classList.toggle("on", Number(b.dataset.project) === info.id));
  const list = $("#db-folders");
  list.replaceChildren();
  for (const f of info.folders) {
    const li = el("li", f.is_repo ? "ok" : "bad");
    li.appendChild(el("b", "", f.label ? f.label : "Main folder"));
    li.appendChild(el("span", "muted small", "  " + f.path));
    if (!f.exists) li.appendChild(el("span", "hint", "Folder not found — fix it in the Ready Room."));
    else if (!f.is_repo) {
      const t = el("button", "ghost small-btn", "Start tracking");
      t.title = "Start tracking this folder with git";
      t.onclick = guarded(async () => showProject(await api("db_track", info.id, f.path)));
      li.appendChild(t);
    }
    list.appendChild(li);
  }
  show(list);
  show($("#extras-note"), info.folders.length > 1);
  showFolder({ ...info, is_repo: info.ready }, info.name);
  renderScanAgents(info.id);
}

const renderScanAgents = guarded(async (pid) => {
  const box = $("#scan-agents");
  box.replaceChildren();
  show($("#scan-box"), pid != null);
  if (pid == null) return;
  for (const a of await api("db_scan_agents", pid)) {
    const lab = el("label", "check");
    const cb = Object.assign(el("input"), { type: "checkbox", value: a.name, checked: a.picked });
    lab.append(cb, el("span", "", a.title), el("span", "muted small", a.severity));
    box.appendChild(lab);
  }
});

async function startScan() {
  const agents = [...document.querySelectorAll("#scan-agents input:checked")].map((c) => c.value);
  if (!agents.length) { banner("Tick at least one agent for the scan."); return; }
  const btn = $("#btn-scan");
  btn.disabled = true;
  $("#progress-log").textContent = "";
  show($("#progress-log"));
  show($("#btn-cancel"));
  try {
    enterReview(await api("start_scan", state.projectId, agents, true));
  } catch (e) {
    banner(e.message, /Nothing to review|Cancelled/.test(e.message) ? "info" : "error");
  } finally {
    btn.disabled = false;
    show($("#btn-cancel"), false);
  }
}

/* Fill the start/resume box. ``info`` is the main folder's details. */
function showFolder(info, title) {
  if (!info) return;
  state.folder = info;
  state.freshStart = false;
  show($("#folder-card"));
  $("#folder-title").textContent = title || info.path;
  show($("#folder-untracked"), !info.is_repo);
  show($("#folder-ready"), info.is_repo);
  $("#progress-log").textContent = "";
  show($("#progress-log"), false);
  if (!info.is_repo) return;

  const r = info.open_review;
  const resumable = r && r.status === "in_progress";
  show($("#resume-box"), !!resumable);
  show($("#start-box"), !resumable);
  if (resumable) $("#resume-text").textContent =
    `You have a review in progress: ${r.decided} of ${r.items} changes decided.`;

  const sel = $("#from-select");
  sel.replaceChildren();
  const opt = (value, text) => { const o = el("option", "", text); o.value = value; sel.appendChild(o); };
  if (!info.has_commits) opt("", "Everything in the folder");
  else {
    opt("", info.checkpoint ? "Since your last review (recommended)"
                            : "The last commit plus uncommitted changes");
    opt("HEAD", "Only uncommitted changes");
    for (const c of info.commits.slice(info.checkpoint ? 0 : 1)) {
      opt(c.sha, `Everything since ${c.short} — ${c.subject} (${c.when})`);
    }
  }
}

async function startReview(fresh) {
  const btns = ["#btn-start", "#btn-resume", "#btn-new"].map($);
  btns.forEach((b) => (b.disabled = true));
  $("#progress-log").textContent = "";
  show($("#progress-log"));
  const cancel = $("#btn-cancel");
  cancel.disabled = false;
  cancel.textContent = "Cancel";
  show(cancel);
  try {
    const args = [$("#use-ai").checked, $("#from-select").value || null, fresh];
    const ov = state.projectId != null ? await api("start_project", state.projectId, ...args)
                                       : await api("start_worktree", state.worktreeSid, ...args);
    enterReview(ov);
  } catch (e) {
    banner(e.message, /Nothing to review|Cancelled/.test(e.message) ? "info" : "error");
  } finally {
    btns.forEach((b) => (b.disabled = false));
    show($("#btn-cancel"), false);
  }
}

const cancelStart = guarded(async () => {
  const b = $("#btn-cancel");
  b.disabled = true;
  b.textContent = "Cancelling…";
  await api("cancel_start");
});

/* ------------------------------------------------------------ review screen */

function enterReview(ov) {
  state.overview = ov;
  state.split = state.settings.diff_mode === "split";
  showDebriefView("#view-review");
  $("#project-name").textContent = ov.project
    ? ov.project + (ov.folders.length ? ` (+ ${ov.folders.join(", ")})` : "") : ov.repo;
  renderList();
  renderWarnings();
  const first = ov.items.find((i) => !i.decision) || ov.items[0];
  if (first) openItem(first.id);
}

const DEC = { approve: ["✓", "dec-approve"], fix: ["✎", "dec-fix"], revert: ["↺", "dec-revert"],
              skip: ["·", "dec-skip"] };
const SEV_LETTER = { high: "H", medium: "M", low: "L" };

function renderList() {
  const ov = state.overview;
  const list = $("#item-list");
  list.replaceChildren();
  for (const it of ov.items) {
    const li = el("li");
    li.dataset.id = it.id;
    li.tabIndex = 0;
    li.setAttribute("role", "option");
    const [mark, cls] = DEC[it.decision] || ["○", "dec-none"];
    const dec = el("span", "dec " + cls, mark);
    dec.title = it.decision || "not decided";
    const sev = el("span", "sev sev-" + (it.severity || "none"), it.severity ? SEV_LETTER[it.severity] : "");
    if (it.severity) sev.title = `${it.severity} — ${it.flags} flag(s)`;
    li.append(dec, sev, el("span", "title", it.title), el("span", "files", it.paths.join(", ")));
    if (it.tasks && it.tasks.length) {
      const t = el("span", "item-task", "▸ " + it.tasks.join(", "));
      t.title = "Flight Deck task that made this change";
      li.appendChild(t);
    }
    if (it.id === state.current) li.classList.add("active");
    li.onclick = () => openItem(it.id);
    list.appendChild(li);
  }
  const flagged = ov.items.filter((i) => i.severity).length;
  const decided = ov.items.filter((i) => i.decision).length;
  $("#progress-pill").textContent = `${decided}/${ov.items.length} decided · ${flagged} flagged`;
}

function renderWarnings() {
  const box = $("#warnings");
  box.replaceChildren();
  for (const w of state.overview.warnings) box.appendChild(el("div", "warn", "⚠ " + w));
}

const openItem = guarded(async (id) => {
  state.current = id;
  clearSelection();
  const item = await api("item", id, state.split);
  if (state.current !== id) return; // user moved on meanwhile
  state.item = item;
  renderList();
  const active = $(`#item-list li[data-id="${id}"]`);
  if (active) active.scrollIntoView({ block: "nearest" });
  renderHead(); renderFlags(); renderGuide(); renderDiff(); renderChat(); renderDecision();
  $("#detail-scroll").scrollTop = 0;
  ensureExplanation(id);
});

function renderHead() {
  const it = state.item, box = $("#item-head");
  box.replaceChildren(el("h2", "", `${it.id}. ${it.title}`), el("div", "files", it.paths.join(", ")));
  if (it.tasks && it.tasks.length) {
    box.appendChild(el("div", "item-task", "From task: " + it.tasks.map((t) => t.title).join(" · ")));
  }
  if (it.summary) box.appendChild(el("div", "", it.summary));
}

function renderFlags() {
  const box = $("#flags");
  box.replaceChildren();
  if (!state.item.flags.length) { box.appendChild(el("div", "muted", "No criteria flagged this change.")); return; }
  for (const f of state.item.flags) {
    const d = el("div", "flag " + f.severity);
    d.append(el("span", "label", `${f.severity.toUpperCase()} · ${f.criterion}`), el("span", "", f.reason));
    if (f.evidence) d.appendChild(el("span", "evidence", "Evidence: " + f.evidence));
    box.appendChild(d);
  }
}

/* -------------------------------------------------------------- guide & chat */

function renderGuide() {
  const box = $("#guide"), exp = state.item.explanation;
  box.replaceChildren(el("h3", "", "Guide"));
  if (!state.overview.ai) { box.appendChild(el("div", "muted", "AI is off for this review — the guide is not available.")); return; }
  if (!exp) { box.appendChild(el("div", "spinner", "The guide is reading this change…")); return; }
  const part = (label, text, source) => {
    if (!text) return;
    const p = el("div", "part");
    p.appendChild(el("b", "", label));
    const body = el("div"); setMd(body, text);
    if (source) {
      // Put the source tag at the end of the last paragraph.
      const chip = el("span", "src " + source.split(":")[0], source);
      chip.title = "Where this came from: request = your request in Claude Code; claude = what Claude said; " +
                   "notes:… = a project note; code = the code; inferred = the guide's reasoning; unknown = no record.";
      (body.lastElementChild || body).appendChild(chip);
    }
    p.appendChild(body);
    box.appendChild(p);
  };
  part("What it does", exp.what);
  part("Supposed to do", exp.intent, exp.intent_source);
  part("Before", exp.before);
  part("Why it changed", exp.why, exp.why_source);
  part("⚑ Look at", exp.attention);
  if (exp.questions && exp.questions.length) {
    const q = el("div", "part");
    q.appendChild(el("b", "", "You might ask"));
    const qs = el("div", "qs");
    for (const text of exp.questions) {
      const b = el("button", "link", text);
      b.onclick = () => { $("#ask-input").value = text; $("#ask-input").focus(); };
      qs.appendChild(b);
    }
    q.appendChild(qs);
    box.appendChild(q);
  }
}

/* One explanation request per item. If a prefetch is already running when
 * the reviewer arrives at the item, wait for that same request and render
 * its result (instead of showing the spinner forever). */
function explanationFor(id) {
  if (!state.explaining.has(id)) {
    const p = api("explain", id);
    state.explaining.set(id, p);
    p.catch(() => {}).finally(() => state.explaining.delete(id));
  }
  return state.explaining.get(id);
}

function ensureExplanation(id) {
  if (!state.overview.ai) return;
  if (state.item && state.item.id === id && state.item.explanation) { prefetchNext(id); return; }
  explanationFor(id).then((exp) => {
    if (state.current === id && state.item && state.item.id === id) {
      state.item.explanation = exp;
      renderGuide();
      prefetchNext(id); // only from the item being read: one ahead, no chain
    }
  }).catch((e) => {
    if (state.current === id) {
      const box = $("#guide"); box.replaceChildren(el("h3", "", "Guide"), el("div", "warn", "Guide failed: " + e.message));
    }
  });
}

function nextUndecided(afterId) {
  const items = state.overview.items, i = items.findIndex((x) => x.id === afterId);
  const order = items.slice(i + 1).concat(items.slice(0, i));
  return order.find((x) => !x.decision) || null;
}

function prefetchNext(id) {
  const nxt = nextUndecided(id);
  if (nxt) explanationFor(nxt.id).catch(() => {});
}

function renderChat(pending) {
  const box = $("#chat");
  box.replaceChildren();
  const turn = (q, a, label, cls = "turn") => {
    const d = el("div", cls);
    const qn = el("div", "q", q);
    if (label) qn.appendChild(el("span", "about", "about " + label));
    d.appendChild(qn);
    const ans = el("div"); setMd(ans, a); d.appendChild(ans);
    box.appendChild(d);
  };
  for (const t of state.item.chat) turn(t.question, t.answer, t.selection_label);
  for (const n of state.item.notes) turn("Note", n, null, "turn note");
  if (pending) {
    const d = el("div", "turn");
    const qn = el("div", "q", pending.q);
    if (pending.label) qn.appendChild(el("span", "about", "about " + pending.label));
    d.append(qn, el("div", "spinner", "thinking…"));
    box.appendChild(d);
  }
}

const ask = guarded(async () => {
  const input = $("#ask-input"), q = input.value.trim();
  if (!q || !state.item) return;
  if (!state.overview.ai) { banner("AI is off for this review — the guide is not available.", "info"); return; }
  const id = state.current, sel = state.sel;
  input.value = "";
  renderChat({ q, label: sel ? sel.label : null });
  $("#detail-scroll").scrollTop = $("#detail-scroll").scrollHeight;
  clearSelection();
  try {
    const turn = await api("ask", id, q, sel);
    if (state.current === id) { state.item.chat.push(turn); renderChat(); }
  } catch (e) {
    if (state.current === id) renderChat();
    throw e;
  }
  $("#detail-scroll").scrollTop = $("#detail-scroll").scrollHeight;
});

/* -------------------------------------------------------- diff & selection */

const CODE_KINDS = new Set(["context", "change", "add", "del"]);

function renderDiff() {
  $("#mode-unified").classList.toggle("on", !state.split);
  $("#mode-split").classList.toggle("on", state.split);
  const box = $("#diff"), table = el("table"), rows = state.item.rows;
  const cols = state.split ? [["4.5em"], ["1.6em"], [""], ["2px"], ["4.5em"], ["1.6em"], [""]]
                           : [["4.5em"], ["4.5em"], ["1.6em"], [""]];
  const cg = el("colgroup");
  for (const [w] of cols) { const c = el("col"); if (w) c.style.width = w; cg.appendChild(c); }
  table.appendChild(cg);
  const tb = el("tbody");
  rows.forEach((r, i) => {
    const tr = el("tr");
    tr.dataset.i = i;
    if (!CODE_KINDS.has(r.kind)) {
      tr.className = r.kind;
      const td = el("td", "", r.kind === "file" ? "▸ " + r.new : r.new);
      td.colSpan = cols.length;
      tr.appendChild(td);
    } else if (!state.split) {
      const kind = r.kind === "del" ? "del" : r.kind === "add" ? "add" : "";
      const text = r.kind === "del" ? r.old : r.new;
      tr.append(cell("no", r.old_no), cell("no", r.new_no),
                cell("mk " + kind, kind === "add" ? "+" : kind === "del" ? "−" : ""),
                codeCell("code " + kind, text));
    } else {
      const lk = r.kind === "context" ? "" : (r.old_no != null ? "del" : "empty");
      const rk = r.kind === "context" ? "" : (r.new_no != null ? "add" : "empty");
      const L = [cell("no", r.old_no), cell("mk " + lk, lk === "del" ? "−" : ""), codeCell("code " + lk, r.old)];
      const R = [cell("no", r.new_no), cell("mk " + rk, rk === "add" ? "+" : ""), codeCell("code " + rk, r.new)];
      L.forEach((c) => (c.dataset.side = "old"));
      R.forEach((c) => (c.dataset.side = "new"));
      tr.append(...L, el("td", "gap"), ...R);
    }
    tb.appendChild(tr);
  });
  table.appendChild(tb);
  box.replaceChildren(table);
}

function cell(cls, text) { return el("td", cls, text == null ? "" : String(text)); }
function codeCell(cls, text) {
  const td = el("td", cls);
  td.appendChild(el("span", "t", text == null ? "" : text));
  return td;
}

function rowIndexFromEvent(e) {
  const tr = e.target.closest("tr[data-i]");
  return tr ? Number(tr.dataset.i) : null;
}

function paintSelection() {
  document.querySelectorAll("#diff td.sel").forEach((td) => td.classList.remove("sel"));
  const r = state.range;
  if (!r) return;
  const [lo, hi] = r.anchor <= r.cursor ? [r.anchor, r.cursor] : [r.cursor, r.anchor];
  const trs = $("#diff").querySelectorAll("tr[data-i]");
  for (let i = lo; i <= hi; i++) {
    const tr = trs[i];
    if (!tr || !CODE_KINDS.has(state.item.rows[i].kind)) continue;
    tr.querySelectorAll("td").forEach((td) => {
      if (td.classList.contains("gap")) return;
      if (state.split && td.dataset.side !== r.side) return;
      td.classList.add("sel");
    });
  }
}

const finalizeSelection = guarded(async () => {
  const r = state.range;
  if (!r) return;
  const id = state.current, split = state.split;
  const sel = await api("selection", id, split, r.anchor, r.cursor, split ? r.side : null);
  // Ignore a late reply if the reviewer moved on or changed view meanwhile.
  if (state.current !== id || state.split !== split || state.range !== r) return;
  state.sel = sel;
  show($("#sel-chip"), !!sel);
  if (sel) $("#sel-label").textContent = "Asking about " + sel.label;
  else { state.range = null; paintSelection(); }
});

function clearSelection() {
  state.sel = null; state.range = null; state.dragging = false;
  show($("#sel-chip"), false);
  paintSelection();
}

function bindDiffMouse() {
  const box = $("#diff");
  box.addEventListener("mousedown", (e) => {
    const i = rowIndexFromEvent(e);
    if (i === null || e.button !== 0 || state.switching) return;
    const td = e.target.closest("td");
    const side = state.split ? (td && td.dataset.side) || "new" : null;
    if (e.shiftKey && state.range && (!state.split || state.range.side === side)) {
      state.range.cursor = i;
    } else {
      state.range = { anchor: i, cursor: i, side };
    }
    state.dragging = true;
    paintSelection();
    e.preventDefault();
    box.focus();
  });
  box.addEventListener("mouseover", (e) => {
    if (!state.dragging) return;
    const i = rowIndexFromEvent(e);
    if (i !== null && state.range) { state.range.cursor = i; paintSelection(); }
  });
  window.addEventListener("mouseup", () => {
    if (!state.dragging) return;
    state.dragging = false;
    finalizeSelection();
  });
}

const setSplit = guarded(async (split) => {
  if (state.split === split || state.switching || !state.current) return;
  state.switching = true; // the rows on screen no longer match state.split
  clearSelection();
  try {
    const id = state.current;
    const item = await api("item", id, split);
    state.split = split;
    if (state.current === id) { state.item = item; renderDiff(); }
    state.settings = await api("save_settings", { diff_mode: split ? "split" : "unified" });
  } finally {
    state.switching = false;
  }
});

/* ---------------------------------------------------------------- decisions */

function renderDecision() {
  const d = state.item.decision;
  document.querySelectorAll(".decide[data-action]").forEach((b) =>
    b.classList.toggle("current", !!d && b.dataset.action === d.action));
  let text = "";
  if (d) text = `Decided: ${d.action}` + (d.instruction ? ` — “${d.instruction}”` : "");
  $("#decision-state").textContent = text;
}

/* The shared text dialog.
 *   promptText(title, opts)        → the text, or null if cancelled
 *     opts.line: one-line input (Enter saves) instead of a text box (Ctrl+Enter)
 *   confirmWord(title, word, label) → true only if the user typed `word`
 *     (one-line input; the button stays disabled until it matches)
 */
function textDialog(title, { value = "", required = false, placeholder = "", line = false,
                             word = null, label = "Save" } = {}) {
  return new Promise((resolve) => {
    $("#prompt-title").textContent = title;
    const box = $("#prompt-input"), one = $("#prompt-line");
    const input = line ? one : box;
    show(box, !line); show(one, line);
    input.value = value; input.placeholder = placeholder;
    const ok = $("#prompt-ok");
    ok.replaceChildren(document.createTextNode(label + " "), el("kbd", "", line ? "Enter" : "Ctrl+Enter"));
    ok.classList.toggle("danger-fill", !!word);
    const matches = () => !word || input.value.trim().toLowerCase() === word;
    ok.disabled = !matches();
    input.oninput = () => { ok.disabled = !matches(); };
    openModal("#modal-prompt");
    input.focus();
    const done = (result) => {
      closeModal();
      ok.onclick = $("#prompt-cancel").onclick = input.onkeydown = input.oninput = null;
      ok.disabled = false;
      resolve(result);
    };
    ok.onclick = () => {
      const v = input.value.trim();
      if (!matches() || (required && !v)) { input.focus(); return; }
      done(v);
    };
    $("#prompt-cancel").onclick = () => done(null);
    input.onkeydown = (e) => {
      if (e.key === "Enter" && (line || e.ctrlKey || e.metaKey)) { e.preventDefault(); ok.click(); }
      if (e.key === "Escape") done(null);
    };
  });
}

const promptText = (title, opts = {}) => textDialog(title, opts);

async function confirmWord(title, word, label) {
  return (await textDialog(`${title} Type ${word} to confirm.`, { line: true, word, label, placeholder: word })) !== null;
}

const decide = guarded(async (action) => {
  if (!state.item) return;
  const id = state.current;
  let instruction = "";
  if (action === "fix") {
    instruction = await promptText("What should change? This goes to Claude Code in the fix list.",
      { value: state.item.decision ? state.item.decision.instruction : "", required: true,
        placeholder: "e.g. keep the retry but log each failure" });
    if (instruction === null) return;
  } else if (action === "revert") {
    instruction = await promptText("Revert this change. Reason (optional):", { placeholder: "why it should be undone" });
    if (instruction === null) return; // cancelled: record nothing
  }
  state.overview = await api("decide", id, action, instruction);
  renderList();
  const nxt = nextUndecided(id);
  if (nxt) openItem(nxt.id);
  else {
    state.item = await api("item", id, state.split);
    renderDecision();
    banner("Every change has a decision. Click “Finish review” when you're ready.", "info");
  }
});

const addNote = guarded(async () => {
  if (!state.item) return;
  const text = await promptText("Add a project note. Future reviews will read it.", { required: true });
  if (!text) return;
  const res = await api("note", state.current, text);
  state.item.notes = res.notes;
  renderChat();
  banner("Note saved to .carrier/notes.md", "info");
});

/* ------------------------------------------------------------------- finish */

function finishDialog() {
  const ov = state.overview, items = ov.items;
  const count = (a) => items.filter((i) => i.decision === a).length;
  const undecided = items.filter((i) => !i.decision).length;
  const body = $("#finish-body");
  body.replaceChildren();
  const counts = el("div", "finish-counts");
  counts.append(el("span", "dec-approve", `✓ ${count("approve")} approved`),
                el("span", "dec-fix", `✎ ${count("fix")} to fix`),
                el("span", "dec-revert", `↺ ${count("revert")} to revert`),
                el("span", "dec-skip", `· ${count("skip")} skipped`));
  body.appendChild(counts);
  if (undecided) body.appendChild(el("p", "warn", `${undecided} change(s) have no decision and will be recorded as not reviewed.`));
  body.appendChild(el("p", "muted", "Finishing saves a record of this review and marks these changes as reviewed, so the next review starts from here."));
  const actions = $("#finish-actions");
  actions.replaceChildren();
  const cancel = el("button", "ghost", "Keep reviewing"); cancel.onclick = closeModal;
  const ok = el("button", "primary", "Finish review"); ok.onclick = doFinish;
  actions.append(cancel, ok);
  openModal("#modal-finish");
}

const doFinish = guarded(async () => {
  if (state.finishing) return; // double click
  state.finishing = true;
  document.querySelectorAll("#finish-actions button").forEach((b) => (b.disabled = true));
  let res;
  try { res = await api("finish"); }
  finally {
    state.finishing = false;
    document.querySelectorAll("#finish-actions button").forEach((b) => (b.disabled = false));
  }
  state.finished = true; // the review is closed: any way out of the dialog goes home
  const body = $("#finish-body");
  body.replaceChildren(el("p", "", "Review finished. " + res.message), el("p", "muted small", "Record: " + res.record));
  const actions = $("#finish-actions");
  actions.replaceChildren();
  const rec = el("button", "ghost", "Open record");
  rec.onclick = guarded(() => api("open_file", res.record));
  actions.appendChild(rec);
  if (res.fixes) {
    const copy = el("button", "ghost", "Copy fix list");
    copy.onclick = () => copyText(res.fixes_text);
    actions.appendChild(copy);
    if (res.fix_task) {
      body.appendChild(el("p", "", `The fixes were added to ${res.project} as its next task: “${res.fix_task.title}”.`));
      const fly = el("button", "primary", "Open in Flight Deck ▸");
      fly.onclick = () => { closeModal(); showTool("flightdeck"); };
      actions.appendChild(fly);
    } else {
      body.appendChild(el("p", "", "Fixes were requested. Copy the fix list into a Claude Code session."));
    }
  }
  const home = el("button", res.fixes ? "ghost" : "primary", "Back to projects");
  home.onclick = closeModal; // closeModal goes home once finished
  actions.appendChild(home);
});

const goHome = guarded(async () => {
  state.overview = state.item = state.current = null;
  applyTheme(await api("settings"));
  await initStart();
  if (state.projectId != null) showProject(await api("db_project", state.projectId));
  else if (state.worktreeSid != null) showFolder(await api("db_worktree", state.worktreeSid));
});

/* ----------------------------------------------------------------- settings */

function openSettings() {
  renderSettings();
  openModal("#modal-settings");
}

function renderSettings() {
  const s = state.settings;
  const list = $("#theme-list");
  list.replaceChildren();
  for (const p of s.presets) {
    const b = el("button", "theme-opt" + (p.name === s.theme ? " on" : ""));
    b.setAttribute("aria-pressed", p.name === s.theme);
    b.appendChild(el("span", "", p.label));
    b.onclick = guarded(async () => { applyTheme(await api("save_settings", { theme: p.name })); renderSettings(); });
    list.appendChild(b);
  }
  // Live preview swatch of the current theme next to its name.
  const on = list.querySelector(".theme-opt.on");
  if (on) {
    const sw = el("span", "swatches");
    const c = s.theme_resolved.colors;
    const a = el("span", "", "+ added"); a.style.background = c.added_bg; a.style.color = c.added_fg;
    const r = el("span", "", "− removed"); r.style.background = c.removed_bg; r.style.color = c.removed_fg;
    if (s.theme_resolved.strike_removed) r.style.textDecoration = "line-through";
    const sl = el("span", "", "selected"); sl.style.background = c.selection_bg; sl.style.color = "#fff";
    sw.append(a, r, sl);
    on.appendChild(sw);
  }
  const grid = $("#color-grid");
  grid.replaceChildren();
  for (const [key, desc] of Object.entries(s.color_keys)) {
    const lab = el("label");
    const inp = el("input"); inp.type = "color"; inp.value = s.theme_resolved.colors[key];
    inp.onchange = guarded(async () => {
      const custom = Object.assign({}, state.settings.custom_colors, { [key]: inp.value });
      applyTheme(await api("save_settings", { custom_colors: custom }));
      renderSettings();
    });
    lab.append(inp, el("span", "", desc));
    if (s.custom_colors[key]) lab.appendChild(el("span", "custom", "custom"));
    grid.appendChild(lab);
  }
  const fs = $("#font-size");
  fs.value = s.font_size; $("#font-size-value").textContent = s.font_size + "px";
  $("#content-width").value = s.content_width || "normal";
  $("#alerts-flash").checked = s.alerts_flash !== false;
  $("#alerts-sound").checked = !!s.alerts_sound;
  $("#alerts-dnd").checked = !!s.alerts_dnd;
  $("#content-center").checked = !!s.content_center;
  $("#usage-plan").value = s.usage_plan || "auto";
  $("#usage-warn-at").value = s.usage_warn_at;
  $("#usage-hold-at").value = s.usage_hold_at;
  $("#usage-hold-takeoff").checked = !!s.usage_hold_takeoff;
  $("#usage-hold-routines").checked = !!s.usage_hold_routines;
  $("#usage-auto-resume").checked = !!s.usage_auto_resume;
  $("#usage-read-logs").checked = !!s.usage_read_logs;
}

/* ---------------------------------------------------------------- modals */

let openModalSel = null;
function openModal(sel) { openModalSel = sel; show($("#modal-backdrop")); show($(sel)); }
function closeModal() {
  if (openModalSel) show($(openModalSel), false);
  openModalSel = null;
  show($("#modal-backdrop"), false);
  if (state.finished) { state.finished = false; goHome(); }
}

/* ----------------------------------------------------------------- keyboard */

function onKey(e) {
  const typing = /^(TEXTAREA|INPUT|SELECT)$/.test(document.activeElement.tagName);
  if (openModalSel) {
    if (e.key === "Escape" && openModalSel !== "#modal-prompt" && openModalSel !== "#modal-confirm") closeModal();
    return;
  }
  if (e.key === "Escape") { clearSelection(); if (typing) document.activeElement.blur(); return; }
  if (typing || $("#view-review").classList.contains("hidden")) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const move = (d) => {
    const items = state.overview.items, i = items.findIndex((x) => x.id === state.current);
    const n = items[Math.max(0, Math.min(items.length - 1, i + d))];
    if (n) openItem(n.id);
  };
  const keys = {
    ArrowDown: () => move(1), j: () => move(1), ArrowUp: () => move(-1), k: () => move(-1),
    a: () => decide("approve"), f: () => decide("fix"), r: () => decide("revert"), s: () => decide("skip"),
    n: () => addNote(), v: () => setSplit(!state.split), "/": () => $("#ask-input").focus(),
    "?": () => openModal("#modal-help"),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
}

/* ============================================================ FLIGHT DECK
 * Sessions run in Python (carrier/flightdeck). This page polls fd_poll for
 * an ordered event stream and renders it; actions call fd_* methods.
 * Same rule as Debrief: untrusted text (Claude's output, tool results,
 * file names, tasks) only via textContent or md().
 */

const fd = {
  attach: [],          // images waiting to be sent: {mime, data (base64), note}
  ready: false,        // deck created (first visit to the tab)
  seq: 0,              // last event seen
  overview: null,
  current: null,       // selected session id
  detail: null,        // fd_session() result for current
  pending: new Set(),  // req_ids awaiting an answer in the current session
  skipUntil: 0,        // events up to this seq are already in the loaded transcript
  toolCards: new Map(),// tool_use id -> card element
  refreshTimer: null,
  polling: false,
};

const FD_STATUS = { starting: "starting", working: "working", needs_you: "needs you", idle: "waiting for you",
                    paused: "paused", error: "error", ended: "ended", limited: "waiting for the usage limit" };

async function fdOpen() {
  if (!fd.ready) {
    fd.ready = true;
    $("#fd-policy").value = (state.settings && state.settings.fd_policy) || "warn";
    fdPollLoop();
  }
  await fdRefresh();
}

const fdRefresh = guarded(async () => {
  fd.lastRefresh = Date.now();
  fd.overview = await api("fd_overview");
  if (activeTool() === "flightdeck") fdRenderSidebar();
  updateAlerts(fd.overview);
  if (fd.current == null && activeTool() === "flightdeck") fdRenderEmpty();
});

/* ---- alerts: badge, banner, highlight, taskbar flash, chime ---- */

const alerts = { needs: new Set(), ready: new Set(), dismissed: null };

function needsYou(ov) {
  const out = [];
  for (const p of ov.projects) {
    for (const s of p.sessions) if (s.status === "needs_you") out.push({ project: p.name, session: s });
  }
  for (const t of ov.standalone) {
    for (const s of t.sessions) if (s.status === "needs_you") out.push({ project: "Standalone", session: s });
  }
  return out;
}

function chime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    for (const [freq, at] of [[660, 0], [880, 0.16]]) {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = freq; o.type = "sine";
      g.gain.setValueAtTime(0.0001, ctx.currentTime + at);
      g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + at + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.3);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + at); o.stop(ctx.currentTime + at + 0.32);
    }
  } catch (_) { /* no audio: fine */ }
}

function updateAlerts(ov) {
  const st = state.settings || {};
  if (ov.usage) { renderUsageChip(ov.usage); usageAlerts(ov.usage); }
  const waiting = needsYou(ov);
  const onDeck = activeTool() === "flightdeck";
  // Badge on the Flight Deck tab: urgent count, or a dot for routines ready to take off.
  const ready = (ov.takeoff || []).filter((q) => !q.blockers.length && !q.backlog);
  const badge = $("#fd-badge");
  badge.textContent = waiting.length ? String(waiting.length) : ready.length ? "•" : "";
  badge.classList.toggle("info", !waiting.length && ready.length > 0);
  show(badge, waiting.length > 0 || ready.length > 0);
  $(".tool[data-tool=flightdeck]").classList.toggle("alerting", waiting.length > 0 && !onDeck);
  // New "needs you" since last time: flash and chime (unless do-not-disturb).
  const ids = new Set(waiting.map((w) => w.session.id));
  const fresh = [...ids].filter((id) => !alerts.needs.has(id));
  alerts.needs = ids;
  if (fresh.length && !st.alerts_dnd) {
    if (st.alerts_flash !== false && !document.hasFocus()) api("attention").catch(() => {});
    if (st.alerts_sound) chime();
    alerts.dismissed = null;
  }
  // A routine whose review gate just cleared: say so once.
  const nowReady = new Set(ready.map((q) => q.task_id));
  for (const q of ready) {
    if (!alerts.ready.has(q.task_id) && alerts.readySeen) {
      banner(`“${q.title}” (${q.project}) is ready for take-off.`, "info");
    }
  }
  alerts.ready = nowReady;
  alerts.readySeen = true;
  // Banner strip on the other tabs.
  const strip = $("#alert-strip");
  const first = waiting[0];
  const showStrip = !!first && !onDeck && !st.alerts_dnd && alerts.dismissed !== first.session.id;
  show(strip, showStrip);
  if (showStrip) {
    $("#alert-text").textContent = `${first.project} — “${first.session.task}” needs you` +
      (waiting.length > 1 ? ` (and ${waiting.length - 1} more)` : "") + ".";
    $("#alert-go").onclick = () => { showTool("flightdeck"); fdSelect(first.session.id); };
  }
}

/* ---- usage and limits ---- */

const usageUi = { level: "ok", seen: false, timer: null };
const USAGE_SHORT = { five_hour: "5h", seven_day: "Week", seven_day_opus: "Opus week", seven_day_sonnet: "Sonnet week" };

function fmtTokens(n) {
  if (n == null) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K";
  return String(n);
}
const fmtMoney = (x) => "$" + (x >= 100 ? x.toFixed(0) : x >= 1 ? x.toFixed(2) : x.toFixed(3));
function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return ts - Date.now() / 1000 < 20 * 3600 ? t : d.toLocaleDateString([], { weekday: "short" }) + " " + t;
}
function fmtAgo(ts) {
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 48 * 60 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
}

function renderUsageChip(u) {
  const b = $("#btn-usage");
  const known = (u.limits || []).filter((w) => w.percent != null || w.status !== "allowed");
  b.textContent = known.length ? known.map((w) => `${USAGE_SHORT[w.kind] || w.label} ` +
    (w.status === "rejected" ? "full" : w.percent != null ? Math.round(w.percent) + "%" : "near")).join(" · ") : "Usage";
  b.className = "ghost usage-chip " + u.level.state;
  b.title = (u.level.text || "Usage and limits") + (u.reached_until ? ` Work can go on at ${fmtClock(u.reached_until)}.` : "");
}

function usageAlerts(u) {
  const rank = { ok: 0, near: 1, hold: 2, reached: 3 };
  const lv = u.level.state, st = state.settings || {};
  if (usageUi.seen && rank[lv] > rank[usageUi.level]) {
    const extra = lv === "reached" ? " Nothing starts on its own until it resets; stopped tasks continue after the reset."
      : lv === "hold" ? " Automatic starts are on hold." : "";
    banner(u.level.text + extra, lv === "near" ? "info" : "error");
    if (!st.alerts_dnd) {
      if (lv === "reached" && st.alerts_flash !== false && !document.hasFocus()) api("attention").catch(() => {});
      if (st.alerts_sound) chime();
    }
  }
  usageUi.level = lv;
  usageUi.seen = true;
}

const openUsage = guarded(async () => {
  await renderUsage();
  openModal("#modal-usage");
  clearInterval(usageUi.timer);
  usageUi.timer = setInterval(() => {
    if (openModalSel !== "#modal-usage") { clearInterval(usageUi.timer); return; }
    renderUsage().catch(() => {});
  }, 3000);
});

function usageTable(box, rows, name) {
  box.replaceChildren();
  if (!rows.length) { box.appendChild(el("div", "muted small", "Nothing yet.")); return; }
  for (const r of rows) {
    const row = el("div", "usage-row");
    const n = el("span", "name", name(r));
    n.title = name(r);
    row.append(n, el("span", "num", fmtTokens(r.total)),
               el("span", "num muted", r.total - r.unpriced > 0 ? "≈ " + fmtMoney(r.cost) : ""));
    row.title = `in ${fmtTokens(r.input)} · out ${fmtTokens(r.output)} · cache write ${fmtTokens(r.cache_write)} · cache read ${fmtTokens(r.cache_read)}`;
    box.appendChild(row);
  }
}

async function renderUsage() {
  const u = await api("usage");
  const sub = u.plan === "subscription";
  $("#usage-plan-line").textContent = sub
    ? "Subscription: Claude's usage limits apply. Costs shown are what the same use would cost at API prices."
    : u.plan === "api" ? "API key: you pay per token. Costs are estimates at API list prices; Flight Deck sessions also show their exact cost."
    : "Carrier learns how you pay for Claude when a session starts (or choose it in Settings → Usage and limits).";
  const lim = $("#usage-limits");
  lim.replaceChildren();
  const wins = u.limits.filter((w) => w.kind !== "overage");
  if (!wins.length) {
    lim.appendChild(el("p", "muted small", sub || u.plan === "unknown"
      ? "No limit reports yet. Claude Code reports your limits while an agent runs." : "Usage limits apply to subscriptions."));
  }
  for (const w of wins) {
    const row = el("div", "usage-limit " + (w.status === "rejected" ? "reached" : w.status === "allowed_warning" ? "near" : ""));
    const head = el("div", "usage-limit-head");
    const state_ = w.status === "rejected" ? "reached" : w.percent != null ? `${w.percent}% used`
      : w.status === "allowed_warning" ? "close to the limit" : w.reset_at ? "reset" : "ok";
    head.append(el("b", "", w.label), el("span", "", state_));
    const bar = el("div", "usage-bar");
    const fill = el("div", "fill");
    fill.style.width = (w.status === "rejected" ? 100 : Math.min(100, w.percent || 0)) + "%";
    bar.appendChild(fill);
    const bits = [];
    if (w.resets_at) bits.push(`resets ${fmtClock(w.resets_at)}`);
    if (w.reset_at) bits.push(`reset at ${fmtClock(w.reset_at)}`);
    if (w.tokens) bits.push(`${fmtTokens(w.tokens.total)} tokens in this window`);
    if (w.seen) bits.push(`as of ${fmtAgo(w.seen)}`);
    row.append(head, bar, el("div", "muted small", bits.join(" · ")));
    lim.appendChild(row);
  }
  if (u.level.state === "reached") lim.appendChild(el("p", "warn", "Nothing starts on its own while a limit is reached. Tasks it stopped continue after the reset."));
  else if (u.hold.takeoff || u.hold.routines) lim.appendChild(el("p", "warn", "On hold: " + (u.hold.takeoff ? "auto take-off" : "") + (u.hold.takeoff && u.hold.routines ? " and " : "") + (u.hold.routines ? "routines (you'll be asked)" : "") + "."));

  const tot = $("#usage-totals");
  tot.replaceChildren();
  const cell = (label, t, note) => {
    const c = el("div", "usage-total");
    c.append(el("span", "muted small", label), el("b", "", fmtTokens(t.total) + " tokens"),
             el("span", "muted small", (t.total - t.unpriced > 0 ? "≈ " + fmtMoney(t.cost) + (sub ? " at API prices" : "") : "") + (note || "")));
    c.title = `in ${fmtTokens(t.input)} · out ${fmtTokens(t.output)} · cache write ${fmtTokens(t.cache_write)} · cache read ${fmtTokens(t.cache_read)}`;
    return c;
  };
  const other = cell("Other Claude Code (7 days)", u.other_week);
  other.title += "\nClaude Code used outside Flight Deck sessions, including Debrief reviews and Draft with Claude.";
  tot.append(cell("Today", u.today), cell("Last 7 days", u.week), cell("Flight Deck (7 days)", u.carrier_week), other);

  const days = $("#usage-days");
  days.replaceChildren();
  const max = Math.max(1, ...u.days.map((d) => d.total));
  for (const d of u.days) {
    const col = el("div", "usage-day");
    const stack = el("div", "stack");
    const other = el("div", "other"), deck = el("div", "deck");
    other.style.height = ((d.total - d.carrier) / max * 100) + "%";
    deck.style.height = (d.carrier / max * 100) + "%";
    stack.append(other, deck);
    const dt = new Date(d.day + "T12:00:00");
    col.append(stack, el("span", "muted small", dt.toLocaleDateString([], { weekday: "narrow" })));
    col.title = `${d.day}: ${fmtTokens(d.total)} tokens (Flight Deck ${fmtTokens(d.carrier)})` + (d.cost ? ` · ≈ ${fmtMoney(d.cost)}` : "");
    days.appendChild(col);
  }
  usageTable($("#usage-projects"), u.projects, (r) => r.name);
  usageTable($("#usage-tasks"), u.tasks, (r) => r.title);
  usageTable($("#usage-models"), u.models, (r) => r.model);
  const sc = u.scan;
  $("#usage-scan").textContent = !u.settings.usage_read_logs ? "Token counts from Claude Code's logs are off (Settings → Usage and limits)."
    : !sc.dirs.length ? "No Claude Code logs found, so there are no token counts yet."
    : !sc.done ? `Reading Claude Code's logs… ${sc.read} of ${sc.files} files.`
    : `Counted from ${sc.files} Claude Code log file${sc.files === 1 ? "" : "s"} in ${sc.dirs.join(", ")} (read only; only the numbers are kept, for 60 days).`;
}

function confirmDialog(title, text, okLabel = "OK") {
  return new Promise((resolve) => {
    const prev = openModalSel;
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    $("#confirm-ok").textContent = okLabel;
    if (prev) show($(prev), false);
    openModal("#modal-confirm");
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
    const done = (v) => {
      document.removeEventListener("keydown", onKey, true);
      show($("#modal-confirm"), false);
      openModalSel = prev;
      if (prev) show($(prev)); else show($("#modal-backdrop"), false);
      $("#confirm-ok").onclick = $("#confirm-cancel").onclick = null;
      resolve(v);
    };
    document.addEventListener("keydown", onKey, true);
    $("#confirm-ok").onclick = () => done(true);
    $("#confirm-cancel").onclick = () => done(false);
    $("#confirm-ok").focus();
  });
}

/* Start a task; near or at a usage limit, ask first. Returns the session, or null if not started. */
async function fdStartTaskApi(taskId, isolation, presetId, model, mode, sendTask, effort) {
  let s = await api("fd_start_task", taskId, isolation, presetId, model, mode, sendTask, effort, false);
  if (s && s.usage_hold) {
    if (!(await confirmDialog("Start anyway?", s.usage_hold, "Start anyway"))) return null;
    s = await api("fd_start_task", taskId, isolation, presetId, model, mode, sendTask, effort, true);
  }
  return s;
}

function fdRefreshSoon() {
  clearTimeout(fd.refreshTimer);
  fd.refreshTimer = setTimeout(fdRefresh, 250);
}

function fdProjectName(id) {
  const p = fd.overview && fd.overview.projects.find((x) => x.id === id);
  return p ? p.name : "?";
}

function fdSessionRow(s) {
  const row = el("div", "fd-sess" + (s.id === fd.current ? " active" : "") + (s.status === "ended" ? " ended" : "")
    + (s.status === "needs_you" ? " needs" : ""));
  row.tabIndex = 0;
  row.append(el("span", "dot " + s.status), el("span", "task", s.task),
             el("span", "st", (s.status === "limited" ? (s.limit_until ? "usage limit — until " + fmtClock(s.limit_until)
               : "limit reset — continue?") : FD_STATUS[s.status] || s.status) + (s.alerts ? ` · ${s.alerts}⚑` : "")));
  row.title = (s.worktree ? `Own copy on ${s.branch}\n` : "") + s.cwd;
  row.onclick = () => fdSelect(s.id);
  row.onkeydown = (e) => { if (e.key === "Enter") fdSelect(s.id); };
  return row;
}

const fdLive = (sessions) => sessions.some((s) => s.status !== "ended" && s.running);

/* Routines queued to start ("ready for take-off"), and the auto take-off switch. */
function fdRenderTakeoff() {
  const ov = fd.overview;
  const box = $("#fd-takeoff");
  const list = $("#fd-takeoff-list");
  list.replaceChildren();
  $("#fd-auto-takeoff").checked = !!ov.auto_takeoff;
  const hold = $("#fd-takeoff-hold");
  hold.textContent = ov.takeoff_hold ? "Holding automatic starts: " + ov.takeoff_hold : "";
  show(hold, !!ov.takeoff_hold && !!ov.auto_takeoff);
  for (const q of ov.takeoff || []) {
    const row = el("div", "takeoff-row");
    const info = el("div", "takeoff-info");
    info.appendChild(el("b", "", q.title));
    info.appendChild(el("span", "muted small", q.project + (q.group ? ` · ${q.group}` : "")));
    const state = q.backlog ? "backlog — start it by hand" : q.blockers.length ? "waiting: " + q.blockers.join(" ")
      : q.error ? "couldn't start: " + q.error : ov.auto_takeoff && ov.takeoff_hold ? "held: usage limit"
      : ov.auto_takeoff ? "taking off…" : "ready";
    info.appendChild(el("span", "small " + (q.blockers.length || q.error ? "warn" : "muted"), state));
    const go = el("button", "primary small-btn", "▶ Start");
    go.disabled = q.blockers.length > 0;
    go.title = q.blockers.length ? "Waiting for a clear review" : "Start it now";
    go.onclick = () => fdOpenStart(q.project_id, q.task_id);
    row.append(info, go);
    list.appendChild(row);
  }
  $("#fd-takeoff-count").textContent = (ov.takeoff || []).length ? `(${ov.takeoff.length})` : "";
  show(box, true);
  if (!(ov.takeoff || []).length) list.appendChild(el("div", "muted small", "No routines waiting."));
}

function fdRenderSidebar() {
  const box = $("#fd-projects");
  box.replaceChildren();
  const ov = fd.overview;
  fdRenderTakeoff();
  if (!ov.projects.length && !ov.standalone.length) {
    box.appendChild(el("div", "muted small", "No projects yet. Set them up in the Ready Room, or start a standalone task."));
    const go = el("button", "ghost small-btn", "Open the Ready Room ▸");
    go.onclick = () => showTool("readyroom");
    box.appendChild(go);
    return;
  }
  for (const p of ov.projects) {
    const wrap = el("div", "fd-project");
    wrap.dataset.project = p.id;
    const head = el("div", "fd-project-head");
    const name = el("span", "name", p.name);
    name.title = p.path;
    const more = el("button", "ghost small-btn", "＋ Task");
    more.title = "Start another task in this project";
    more.onclick = () => fdOpenStart(p.id, null);
    head.append(name, more);
    wrap.appendChild(head);
    if (p.links.length) wrap.appendChild(el("div", "links", "⇄ " + p.links.map((l) => l.with).join(", ")));
    const line = el("div", "fd-task-line");
    if (p.current_task) {
      const t = p.current_task;
      const ct = el("span", "ct", (t.status === "active" ? "In progress: " : "Current: ") + t.title);
      ct.title = t.details || t.title;
      const go = el("button", "primary small-btn", "▶ Start");
      go.title = "Start a session on the current task";
      go.onclick = () => fdOpenStart(p.id, t.id);
      line.append(ct, go);
    } else {
      line.appendChild(el("span", "ct", (p.groups || []).length ? "Main queue: no open tasks."
        : "No open tasks — add one with ＋ Task or in the Ready Room."));
    }
    wrap.appendChild(line);
    // Task groups run alongside the main queue: each has its own current task.
    for (const g of p.groups || []) {
      if (!g.current_task) continue;
      const t = g.current_task;
      const gl = el("div", `fd-task-line group prio-${g.priority}`);
      const ct = el("span", "ct", `${g.name}: ${t.status === "active" ? "in progress — " : ""}${t.title}`);
      ct.title = (g.priority === "backlog" ? "Backlog group. " : "") + (t.details || t.title);
      const go = el("button", g.priority === "backlog" ? "ghost small-btn" : "primary small-btn", "▶ Start");
      go.title = `Start a session on ${g.name}'s current task`;
      go.onclick = () => fdOpenStart(p.id, t.id);
      gl.append(ct, go);
      wrap.appendChild(gl);
    }
    for (const s of p.sessions) wrap.appendChild(fdSessionRow(s));
    box.appendChild(wrap);
  }
  if (ov.standalone.length) {
    box.appendChild(el("div", "fd-section-title", "Standalone tasks"));
    for (const t of ov.standalone) {
      const wrap = el("div", "fd-project standalone");
      wrap.dataset.task = t.id;
      const head = el("div", "fd-project-head");
      const name = el("span", "name", t.title);
      name.title = t.cwd || "";
      head.appendChild(name);
      wrap.appendChild(head);
      const line = el("div", "fd-task-line");
      const live = fdLive(t.sessions);
      line.appendChild(el("span", "ct", t.status === "done" ? "Done" : live ? "In progress" : "Not running"));
      const btn = (label, fn, cls = "ghost small-btn") => { const b = el("button", cls, label); b.onclick = fn; line.appendChild(b); };
      if (t.status !== "done" && !live) {
        btn("▶ Start", () => fdStartStandalone(t), "primary small-btn");
        btn("✓ Done", () => fdFinishStandalone(t));
      }
      if (!live) btn("Make project…", () => fdOpenUpgrade(t));
      if (t.status === "done") btn("Add to project…", () => fdOpenAssign(t));
      wrap.appendChild(line);
      for (const s of t.sessions) wrap.appendChild(fdSessionRow(s));
      box.appendChild(wrap);
    }
  }
}

function fdRenderEmpty() {
  show($("#fd-empty")); show($("#fd-session"), false);
  const feed = $("#fd-feed");
  feed.replaceChildren();
  const active = [];
  for (const p of fd.overview.projects) for (const s of p.sessions) if (s.status !== "ended") active.push([p.name, s]);
  for (const t of fd.overview.standalone) for (const s of t.sessions) if (s.status !== "ended") active.push(["Standalone", s]);
  if (!active.length) return;
  feed.appendChild(el("h3", "", "Sessions"));
  for (const [label, s] of active) {
    const d = el("div", "turn");
    d.append(el("div", "q", `${label} · ${s.task}`), el("div", "muted small", FD_STATUS[s.status] || s.status));
    d.style.cursor = "pointer";
    d.onclick = () => fdSelect(s.id);
    feed.appendChild(d);
  }
}

/* ---- selecting a session & rendering its transcript ---- */

const fdSelect = guarded(async (sid) => {
  fd.current = sid;
  fd.detail = await api("fd_session", sid);
  fd.ruleSets = await api("rule_sets").catch(() => []);
  show($("#fd-image-btn"), fd.detail.images !== false);
  if (fd.attach.length) { fd.attach = []; fdRenderAttach(); }
  if (fd.current !== sid) return;
  fd.pending = new Set(fd.detail.pending.map((p) => p.req_id));
  fd.skipUntil = fd.detail.seq;
  show($("#fd-empty"), false); show($("#fd-session"));
  fdRenderHead();
  fdRenderAware(fd.detail.awareness);
  const tr = $("#fd-transcript");
  tr.replaceChildren();
  fd.toolCards.clear();
  for (const ev of fd.detail.transcript) fdRenderEvent(ev, false);
  fdSetLive("");
  tr.scrollTop = tr.scrollHeight;
  fdRenderSidebar();
  $("#fd-input").focus();
});

function fdRenderHead() {
  const s = fd.detail;
  $("#fd-task").textContent = s.task_row ? s.task_row.title : s.task;
  const where = s.worktree ? `own copy · branch ${s.branch} · ${s.cwd}` : s.cwd;
  const done = s.task_row && s.task_row.status === "done" ? " · task done" : "";
  $("#fd-meta").textContent = `${s.project ? s.project.name : "Standalone"} · #${s.id} · ${where}${done}` +
    (s.cost ? ` · $${s.cost.toFixed(4)}` : "") + (s.model ? ` · ${s.model}` : "");
  fdSetStatus(s.status);
  $("#fd-mode").value = s.mode;
  $("#fd-model").value = s.model || "";
  $("#fd-effort").value = s.effort || "";
  syncEffort($("#fd-effort"), s.model || "");
}

function fdSetStatus(status) {
  if (!fd.detail) return;
  fd.detail.status = status;
  const pill = $("#fd-status");
  pill.textContent = FD_STATUS[status] || status;
  pill.className = "pill " + status;
  const live = fd.detail.running || ["working", "needs_you", "idle", "starting"].includes(status);
  const busy = status === "working" || status === "needs_you";
  show($("#fd-stop"), busy);
  $("#fd-send").disabled = busy || !live;
  $("#fd-input").disabled = !live;
  show($("#fd-resume"), status === "paused" || status === "error");
  show($("#fd-continue"), status === "limited");
  if (status === "limited") {
    pill.textContent = fd.detail.limit_until ? "usage limit — continues " + fmtClock(fd.detail.limit_until)
      : "usage limit reset — press Continue now";
  }
  show($("#fd-end"), status !== "ended");
  show($("#fd-rmcopy"), status === "ended" && !!fd.detail.branch && !!fd.detail.project);
  $("#fd-mode").disabled = status === "ended";
  // Model and effort can only change while Claude is waiting (or the session is paused).
  $("#fd-model").disabled = $("#fd-effort").disabled = status === "ended" || busy;
  fdRenderTaskButtons();
}

function fdRenderAware(a) {
  const box = $("#fd-aware");
  box.replaceChildren();
  if (!a) return;
  if (a.related.length) {
    box.appendChild(el("span", "", "Related: "));
    a.related.forEach((r, i) => {
      if (i) box.appendChild(el("span", "", " · "));
      box.appendChild(el("b", "", `${r.project} #${r.id}`));
      box.appendChild(el("span", "", ` “${r.task}” (${FD_STATUS[r.status] || r.status})` +
        (r.files.length ? ` — ${r.files.slice(0, 3).join(", ")}` : "")));
    });
  }
  if (a.pending.length) {
    box.appendChild(el("div", "", `⚑ ${a.pending.length} update(s) will be shared with Claude with your next message.`));
  }
}

function fdSetLive(text, append = false) {
  const live = $("#fd-live");
  live.textContent = append ? live.textContent + text : text;
  // Show only the tail of the streaming preview.
  if (live.textContent.length > 600) live.textContent = "…" + live.textContent.slice(-600);
  show(live, !!live.textContent);
}

function fdRenderEvent(ev, scroll = true) {
  const tr = $("#fd-transcript");
  const atBottom = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 80;
  const add = (node) => { tr.appendChild(node); };
  switch (ev.type) {
    case "user": {
      const u = el("div", "msg-user", ev.text);
      if (ev.images && ev.images.length) {
        const row = el("div", "msg-images");
        ev.images.forEach((img) => row.appendChild(imageFigure(img.mime, img.data, img.note)));
        u.appendChild(row);
      }
      add(u);
      break;
    }
    case "image": add(imageFigure(ev.mime, ev.data, ev.caption || ev.name, "msg-image")); break;
    case "skill_proposal": add(fdSkillCard(ev)); break;
    case "skill_done": {
      const card = tr.querySelector(`.rcard[data-rule="${CSS.escape(ev.id)}"]`);
      if (card) ruleDone(card, ev.decision === "applied" ? ev.text || "Saved." : "Dismissed.");
      break;
    }
    case "text": { fdSetLive(""); const d = el("div", "msg-text"); setMd(d, ev.text); add(d); break; }
    case "thinking": {
      const d = el("details", "msg-thinking");
      d.append(el("summary", "", "Thinking"), el("div", "", ev.text));
      add(d); break;
    }
    case "tool_use": {
      fdSetLive("");
      const card = el("details", "tool" + (ev.sub ? " sub" : ""));
      const sum = el("summary");
      sum.append(el("span", "tname", ev.name), el("span", "tsum", ev.summary.replace(/^\S+\s/, "")),
                 el("span", "tstate", "…"));
      card.append(sum, el("pre", "", JSON.stringify(ev.input, null, 2)));
      fd.toolCards.set(ev.id, card);
      add(card); break;
    }
    case "tool_result": {
      const card = fd.toolCards.get(ev.id);
      if (card) {
        card.querySelector(".tstate").textContent = ev.is_error ? "failed" : "done";
        if (ev.is_error) card.classList.add("err");
        if (ev.text) card.appendChild(el("pre", "", ev.text));
      }
      break;
    }
    case "permission": add(fdPermissionCard(ev)); break;
    case "question": add(fdQuestionCard(ev)); break;
    case "question_done": {
      const card = tr.querySelector(`.qcard[data-req="${CSS.escape(ev.req_id)}"]`);
      if (card) {
        questionsDone(card, ev.decision === "answered"
          ? "Answered: " + Object.entries(ev.answers || {}).map(([q, a]) => `${q} → ${a}`).join(" · ")
          : ev.decision === "skipped" ? "Skipped." : "Withdrawn by Claude Code.");
      }
      fd.pending.delete(ev.req_id);
      break;
    }
    case "rule_proposal": add(fdRuleCard(ev)); break;
    case "question_fallback": {
      const sid = fd.current;
      const card = questionCard(ev.questions || [], {
        live: true,
        onSubmit: (answers) => guarded(async () => {
          await api("fd_send", sid, Object.entries(answers).map(([q, a]) => `${q} → ${a}`).join("\n"));
          questionsDone(card, "Sent as a message.");
        })(),
      });
      card.prepend(el("div", "muted small", "Claude Code asked this without waiting for an answer, so your choice is " +
        "sent as a normal message."), el("div", "ptitle", "Claude asked"));
      add(card);
      break;
    }
    case "work_proposal": add(fdWorkCard(ev)); break;
    case "change_proposal": add(fdChangeCard(ev)); break;
    case "proposal_done": {
      const card = tr.querySelector(`.rcard[data-req="${CSS.escape(ev.req_id || "")}"]`);
      if (card) ruleDone(card, ev.text);
      fd.pending.delete(ev.req_id);
      break;
    }
    case "rule_done": {
      const card = tr.querySelector(`.rcard[data-rule="${CSS.escape(ev.id)}"]`);
      if (card) ruleDone(card, ev.decision === "applied" ? (ev.text || "Added.") + " New sessions use it." : "Dismissed.");
      break;
    }
    case "permission_done": {
      const card = tr.querySelector(`.perm[data-req="${CSS.escape(ev.req_id)}"]`);
      if (card) fdPermissionDone(card, ev.decision);
      fd.pending.delete(ev.req_id);
      break;
    }
    case "notice": {
      const warnKinds = ["same_file", "contract", "worktree_overlap", "contract_changed", "usage", "limit"];
      const cls = warnKinds.includes(ev.kind) ? "notice warn" : ev.kind === "briefing" || ev.kind === "contract_sent"
        ? "notice share" : "notice";
      const icon = warnKinds.includes(ev.kind) ? "⚑ " : ev.kind === "briefing" ? "✈ " : "";
      add(el("div", cls, icon + ev.text)); break;
    }
    case "result": {
      fdSetLive("");
      const t = ev.is_error ? "Turn ended with an error. " + (ev.text || "") : "Done";
      add(el("div", "turn-end" + (ev.is_error ? " msg-error" : ""), `— ${t}${ev.cost ? ` · $${ev.cost.toFixed(4)} so far` : ""} —`));
      break;
    }
    case "error": if (ev.code === "rate_limit") return; add(el("div", "msg-error", ev.text)); break;  // the limit notice says it
    case "system": add(el("div", "notice", ev.text)); break;
    case "live": fdSetLive(ev.text, true); return;
    case "status": fdSetStatus(ev.status); return;
    default: return;
  }
  if (scroll && atBottom) tr.scrollTop = tr.scrollHeight;
}

function fdPermissionCard(ev) {
  const card = el("div", "perm");
  card.dataset.req = ev.req_id;
  card.append(el("div", "ptitle", `Claude Code wants to use ${ev.tool}`));
  const code = el("code", "", ev.summary);
  card.appendChild(code);
  if (ev.reason) card.appendChild(el("div", "notice warn", ev.reason));
  if ((ev.programs || []).length) {  // a shell command: which of its programs are pre-approved
    const row = el("div", "pprogs small");
    row.appendChild(el("span", "muted", "Programs: "));
    ev.programs.forEach((p, i) => {
      if (i) row.appendChild(el("span", "muted", " · "));
      const s = el("span", p.listed ? "ok" : "no", (p.listed ? "✓ " : "✗ ") + p.name);
      s.title = p.listed ? "Pre-approved for this project" : (p.why || "Not pre-approved");
      row.appendChild(s);
    });
    card.appendChild(row);
  }
  if (ev.why && !ev.project_label) card.appendChild(el("div", "muted small", "Asks because " + ev.why + "."));
  const actions = el("div", "pactions");
  if (fd.pending.has(ev.req_id) || ev.live) {
    const sid = fd.current;
    const answer = (decision, message = "") => guarded(async () => {
      actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
      await api("fd_answer", sid, ev.req_id, decision, message);
    })();
    const a = el("button", "primary", "Allow"); a.onclick = () => answer("allow");
    const aa = el("button", "ghost", ev.allow_label || `Allow ${ev.tool} for this session`);
    aa.onclick = () => answer("allow_always");
    const d = el("button", "ghost", "Deny…");
    d.onclick = async () => {
      const why = await promptText("Tell Claude why (optional):", {});
      if (why !== null) answer("deny", why);
    };
    actions.append(a, aa);
    if (ev.project_label) {
      const ap = el("button", "ghost", ev.project_label);
      ap.title = "Adds this to the project's pre-approved actions (Ready Room), for all its sessions from now on";
      ap.onclick = () => answer("allow_project");
      actions.appendChild(ap);
    }
    actions.appendChild(d);
  } else {
    actions.appendChild(el("span", "muted small", "No longer waiting."));
    card.classList.add("done");
  }
  card.appendChild(actions);
  return card;
}

function fdQuestionCard(ev) {
  const sid = fd.current;
  const live = fd.pending.has(ev.req_id) || !!ev.live;
  const card = questionCard(ev.questions || [], {
    live,
    onSubmit: (answers) => guarded(async () => api("fd_answer", sid, ev.req_id, "answers", answers))(),
    onSkip: () => guarded(async () => api("fd_answer", sid, ev.req_id, "skip", ""))(),
  });
  card.dataset.req = ev.req_id;
  card.prepend(el("div", "ptitle", "Claude has a question"));
  if (!live) questionsDone(card, "No longer waiting.");
  return card;
}

function fdRuleCard(ev) {
  const sid = fd.current;
  const project = fd.detail && fd.detail.project;
  const targets = [];
  if (project) targets.push({ value: "project", label: `This project's rules (${project.name})` });
  targets.push({ value: "global", label: "Global rules (every project)" });
  for (const rs of fd.ruleSets || []) targets.push({ value: `rs:${rs.id}`, label: `Rule set: ${rs.name}` });
  targets.push({ value: "rs:new", label: "A new rule set…" });
  let initial = project ? "project" : "global";
  if (ev.scope === "global") initial = "global";
  if (ev.scope === "rule_set") {
    const match = (fd.ruleSets || []).find((r) => r.name.toLowerCase() === (ev.rule_set || "").toLowerCase());
    initial = match ? `rs:${match.id}` : "rs:new";
  }
  const card = ruleCard(ev, {
    targets, initial,
    onApply: ({ text, target, name }) => guarded(async () => {
      const [kind, id] = target.startsWith("rs:") ? ["rule_set", target.slice(3)] : [target, ""];
      const msg = await api("fd_apply_rule", sid, ev.id, text, kind, id && id !== "new" ? Number(id) : null, name);
      ruleDone(card, `${msg} New sessions use it.`);
      if (kind === "rule_set") fd.ruleSets = await api("rule_sets").catch(() => fd.ruleSets);
    })(),
    onDismiss: () => guarded(async () => { await api("fd_dismiss_rule", sid, ev.id); ruleDone(card, "Dismissed."); })(),
  });
  return card;
}

function fdPermissionDone(card, decision) {
  card.classList.add("done");
  const actions = card.querySelector(".pactions");
  actions.replaceChildren(el("span", "muted small",
    ({ deny: "Denied.", allow_always: "Allowed for this session.", allow: "Allowed.",
       allow_project: "Allowed, and pre-approved for this project.",
       cancelled: "Withdrawn by Claude Code.", interrupt: "Stopped." })[decision] || decision));
}

/* ---- polling ---- */

async function fdPollLoop() {
  if (fd.polling) return;
  fd.polling = true;
  for (;;) {
    try {
      const res = await api("fd_poll", fd.seq);
      let refresh = false;
      for (const { seq, session, event } of res.events) {
        fd.seq = Math.max(fd.seq, seq);
        const asks = ["permission", "question", "work_proposal", "change_proposal"].includes(event.type);
        if (asks) fd.pending.add(event.req_id);
        if (session === fd.current && seq > fd.skipUntil) {
          fdRenderEvent(asks ? { ...event, live: true } : event);
          if (event.type === "notice" || event.type === "result") fdRefreshAwareSoon();
        }
        if (["status", "notice", "result", "permission", "permission_done", "question", "question_done",
             "work_proposal", "change_proposal", "proposal_done"].includes(event.type)) refresh = true;
      }
      fd.seq = Math.max(fd.seq, res.seq);
      if (refresh || Date.now() - (fd.lastRefresh || 0) > 30000) fdRefreshSoon();  // usage and resets change too
    } catch (_) { /* keep polling */ }
    await new Promise((r) => setTimeout(r, activeTool() === "flightdeck" ? 250 : 1500));
  }
}

let fdAwareTimer = null;
function fdRefreshAwareSoon() {
  clearTimeout(fdAwareTimer);
  fdAwareTimer = setTimeout(async () => {
    if (fd.current == null) return;
    try {
      const d = await api("fd_session", fd.current);
      if (d.id !== fd.current) return;
      fd.detail.awareness = d.awareness; fd.detail.cost = d.cost; fd.detail.files = d.files;
      fd.detail.task_row = d.task_row; fd.detail.running = d.running; fd.detail.limit_until = d.limit_until;
      fdRenderAware(d.awareness); fdRenderHead();
      if (d.status === "limited" || fd.detail.status === "limited") fdSetStatus(d.status);  // when it continues
    } catch (_) { /* ignore */ }
  }, 400);
}

/* ---- actions ---- */

const fdSend = guarded(async () => {
  const input = $("#fd-input"), text = input.value.trim();
  if ((!text && !fd.attach.length) || fd.current == null) return;
  const hits = text ? await api("fd_check_secrets", text) : [];
  if (hits.length && !(await confirmWord(`This message looks like it contains a secret (${hits[0].kind}: ` +
      `${hits[0].preview}). It would be sent to Anthropic with the conversation. Send it anyway?`, "send", "Send anyway"))) {
    return;
  }
  await api("fd_send", fd.current, text, fd.attach.map(({ mime, data, note }) => ({ mime, data, note })));
  input.value = "";
  fd.attach = [];
  fdRenderAttach();
  fdSetStatus("working");
});

/* ---- images: shown in the chat, pasted or added, marked up ---- */

const IMAGE_LIMIT = 5 * 1024 * 1024;

function dataUrl(mime, data) { return `data:${mime};base64,${data}`; }

function imageFigure(mime, data, caption, cls = "msg-thumb") {
  const fig = el("figure", cls);
  const img = el("img");
  img.alt = caption || "image";
  img.src = dataUrl(mime, data); // a data: URL set as an attribute: never parsed as HTML
  img.onclick = () => {
    $("#view-image").src = img.src;
    $("#view-caption").textContent = caption || "";
    openModal("#modal-view-image");
  };
  fig.appendChild(img);
  if (caption) fig.appendChild(el("figcaption", "", caption));
  return fig;
}

/* Read a File/Blob as {mime, data}; large pictures are scaled down to fit Claude's limit. */
function readImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Couldn't read that image."));
    reader.onload = () => {
      const url = String(reader.result);
      const img = new Image();
      img.onerror = () => reject(new Error("That file isn't an image Carrier can show."));
      img.onload = () => resolve(fitImage(img, url));
      img.src = url;
    };
    reader.readAsDataURL(file);
  });
}

function fitImage(img, url) {
  const m = /^data:(image\/(?:png|jpeg|gif|webp));base64,(.*)$/.exec(url);
  const MAX = 2400;
  if (m && img.naturalWidth <= MAX && img.naturalHeight <= MAX && m[2].length * 0.75 < IMAGE_LIMIT) {
    return { mime: m[1], data: m[2], note: "" };
  }
  const scale = Math.min(1, MAX / Math.max(img.naturalWidth, img.naturalHeight));
  const c = document.createElement("canvas");
  c.width = Math.round(img.naturalWidth * scale); c.height = Math.round(img.naturalHeight * scale);
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  return canvasImage(c);
}

function canvasImage(c) {
  let out = c.toDataURL("image/png");
  if (out.length * 0.75 > IMAGE_LIMIT) out = c.toDataURL("image/jpeg", 0.85);
  const [, mime, data] = /^data:([^;]+);base64,(.*)$/.exec(out);
  return { mime, data, note: "" };
}

const fdAddImages = guarded(async (files) => {
  if (fd.detail && fd.detail.images === false) {
    banner("Images are turned off for this project (Ready Room → the project).");
    return;
  }
  for (const f of files) {
    if (!/^image\//.test(f.type)) continue;
    if (fd.attach.length >= 6) { banner("Up to 6 images per message."); break; }
    fd.attach.push(await readImage(f));
  }
  fdRenderAttach();
});

function fdRenderAttach() {
  const box = $("#fd-attach");
  box.replaceChildren();
  fd.attach.forEach((a, i) => {
    const fig = imageFigure(a.mime, a.data, a.note, "attach-thumb");
    const img = fig.querySelector("img");
    img.title = "Click to mark it up or add a note";
    img.onclick = () => openMarkup(i);
    box.appendChild(fig);
  });
  show(box, fd.attach.length > 0);
}

/* Mark-up: pen, box and arrow strokes drawn over the picture, plus a note. */
const mk = { index: -1, img: null, strokes: [], current: null };

function openMarkup(i) {
  const a = fd.attach[i];
  mk.index = i; mk.strokes = []; mk.current = null;
  $("#img-note").value = a.note || "";
  mk.img = new Image();
  mk.img.onload = () => {
    const c = $("#img-canvas");
    c.width = mk.img.naturalWidth; c.height = mk.img.naturalHeight;
    drawMarkup();
    openModal("#modal-image");
  };
  mk.img.src = dataUrl(a.mime, a.data);
}

function drawMarkup() {
  const c = $("#img-canvas"), g = c.getContext("2d");
  g.clearRect(0, 0, c.width, c.height);
  g.drawImage(mk.img, 0, 0);
  const width = Math.max(3, Math.round(Math.max(c.width, c.height) / 300));
  for (const s of [...mk.strokes, ...(mk.current ? [mk.current] : [])]) {
    g.strokeStyle = s.color; g.lineWidth = width; g.lineCap = "round"; g.lineJoin = "round";
    const [p0] = s.points, p1 = s.points[s.points.length - 1];
    g.beginPath();
    if (s.tool === "pen") { s.points.forEach((p, j) => (j ? g.lineTo(p.x, p.y) : g.moveTo(p.x, p.y))); }
    else if (s.tool === "box") { g.rect(p0.x, p0.y, p1.x - p0.x, p1.y - p0.y); }
    else {
      g.moveTo(p0.x, p0.y); g.lineTo(p1.x, p1.y);
      const ang = Math.atan2(p1.y - p0.y, p1.x - p0.x), head = width * 5;
      for (const d of [-0.5, 0.5]) {
        g.moveTo(p1.x, p1.y);
        g.lineTo(p1.x - head * Math.cos(ang + d), p1.y - head * Math.sin(ang + d));
      }
    }
    g.stroke();
  }
}

function markupPoint(e) {
  const c = $("#img-canvas"), r = c.getBoundingClientRect();
  return { x: (e.clientX - r.left) * (c.width / r.width), y: (e.clientY - r.top) * (c.height / r.height) };
}

function bindMarkup() {
  const c = $("#img-canvas");
  c.onpointerdown = (e) => {
    c.setPointerCapture(e.pointerId);
    mk.current = { tool: document.querySelector("input[name=img-tool]:checked").value,
                   color: $("#img-color").value, points: [markupPoint(e)] };
  };
  c.onpointermove = (e) => {
    if (!mk.current) return;
    const p = markupPoint(e);
    if (mk.current.tool === "pen") mk.current.points.push(p); else mk.current.points[1] = p;
    drawMarkup();
  };
  c.onpointerup = () => {
    if (mk.current && mk.current.points.length > 1) mk.strokes.push(mk.current);
    mk.current = null;
    drawMarkup();
  };
  $("#img-undo").onclick = () => { mk.strokes.pop(); drawMarkup(); };
  $("#img-clear").onclick = () => { mk.strokes = []; drawMarkup(); };
  $("#img-cancel").onclick = closeModal;
  $("#img-remove").onclick = () => { fd.attach.splice(mk.index, 1); closeModal(); fdRenderAttach(); };
  $("#img-done").onclick = () => {
    const a = fd.attach[mk.index];
    if (mk.strokes.length) Object.assign(a, canvasImage($("#img-canvas")));
    a.note = $("#img-note").value.trim();
    closeModal();
    fdRenderAttach();
  };
  $("#view-close").onclick = closeModal;
  $("#history-close").onclick = closeModal;
}

/* Where should new work go? Claude's suggestion is preselected; everything is editable. */
function queueSelect(queues, chosen, withNew = true) {
  const sel = el("select");
  for (const q of queues) {
    sel.appendChild(Object.assign(el("option", "", q.group_id == null ? "Main queue"
      : `Group: ${q.name}${q.priority ? ` (${q.priority})` : ""}`), { value: q.group_id == null ? "main" : String(q.group_id) }));
  }
  if (withNew) sel.appendChild(Object.assign(el("option", "", "A new task group…"), { value: "new" }));
  if (chosen != null) sel.value = chosen;
  return sel;
}

function positionSelect(queue, chosen) {
  const sel = el("select");
  sel.appendChild(Object.assign(el("option", "", "Next up"), { value: "next" }));
  sel.appendChild(Object.assign(el("option", "", "At the end"), { value: "end" }));
  for (const t of (queue && queue.tasks) || []) {
    sel.appendChild(Object.assign(el("option", "", `After “${t.title}”`), { value: `after:${t.id}` }));
  }
  sel.value = [...sel.options].some((o) => o.value === chosen) ? chosen : "end";
  return sel;
}

function fdWorkCard(ev) {
  const sid = fd.current;
  const live = fd.pending.has(ev.req_id) || !!ev.live;
  const sg = ev.suggest || {};
  const card = el("div", "rcard");
  card.dataset.req = ev.req_id;
  card.appendChild(el("div", "ptitle", "✎ New work came up — where should it go?"));
  if (ev.why) card.appendChild(el("div", "muted small", ev.why));
  const field = (label, node) => { card.append(el("label", "small-label", label), node); return node; };
  const title = field("Title", Object.assign(el("input"), { type: "text", value: ev.title || "" }));
  const details = field("Details", Object.assign(el("textarea"), { rows: 4, value: ev.details || "" }));
  // Where: the current task, or a queue.
  const groupByName = (n) => (ev.queues || []).find((q) => q.group_id != null && q.name.toLowerCase() === String(n || "").toLowerCase());
  const where = el("select");
  if (ev.current_task) where.appendChild(Object.assign(el("option", "", `Add to the current task (“${ev.current_task.title}”)`), { value: "current" }));
  where.appendChild(Object.assign(el("option", "", "Make it a new task"), { value: "task" }));
  field("Where", where);
  const queueRow = el("div", "agent-opts two");
  const g = groupByName(sg.group);
  const queue = queueSelect(ev.queues || [], sg.suggest === "new_group" ? "new" : g ? String(g.group_id) : "main");
  const qLabel = el("label", "field"); qLabel.append("Queue", queue);
  const posLabel = el("label", "field");
  let pos = positionSelect((ev.queues || [])[0], sg.position || "end");
  posLabel.append("Position", pos);
  queueRow.append(qLabel, posLabel);
  card.appendChild(queueRow);
  const newRow = el("div", "agent-opts two");
  const gName = Object.assign(el("input"), { type: "text", value: sg.new_group || "", placeholder: "New group name" });
  const gPrio = el("select");
  for (const p of ["high", "normal", "low", "backlog"]) gPrio.appendChild(Object.assign(el("option", "", p), { value: p }));
  gPrio.value = sg.new_group_priority || "normal";
  newRow.append(gName, gPrio);
  card.appendChild(newRow);
  const sync = () => {
    const isTask = where.value === "task";
    show(queueRow, isTask); show(newRow, isTask && queue.value === "new");
    const q = (ev.queues || []).find((x) => (x.group_id == null ? "main" : String(x.group_id)) === queue.value);
    const fresh = positionSelect(q, pos.value);
    pos.replaceWith(fresh); pos = fresh;
  };
  where.value = sg.suggest === "current" && ev.current_task ? "current" : "task";
  where.onchange = sync; queue.onchange = sync;
  sync();
  if (sg.reasoning) card.appendChild(el("div", "suggest", "Claude suggests: " +
    ({ current: "add it to the current task", main: "a new task in the main queue",
       group: `a new task in the group ${sg.group || ""}`, new_group: `a new group “${sg.new_group || ""}”` }[sg.suggest] || "")
    + ". " + sg.reasoning));
  const actions = el("div", "pactions");
  if (live) {
    const ok = el("button", "primary", "Add");
    ok.onclick = guarded(async () => {
      const choice = { title: title.value, details: details.value,
        where: where.value === "current" ? "current" : queue.value === "new" ? "new_group" : "queue",
        group_id: ["main", "new"].includes(queue.value) ? null : Number(queue.value),
        new_group: gName.value, new_group_priority: gPrio.value, position: pos.value };
      if (choice.where === "new_group" && !gName.value.trim()) { gName.focus(); return; }
      card.querySelectorAll("button").forEach((b) => (b.disabled = true));
      await api("fd_answer", sid, ev.req_id, "work", choice);
    });
    const no = el("button", "ghost", "Leave it out");
    no.onclick = guarded(async () => { await api("fd_answer", sid, ev.req_id, "decline", ""); });
    actions.append(ok, no);
  } else {
    actions.appendChild(el("span", "muted small", "No longer waiting."));
    card.classList.add("done");
  }
  card.appendChild(actions);
  return card;
}

/* Change tasks so the record matches the work: pull in (all or part of) another task, or split this one. */
function fdChangeCard(ev) {
  const sid = fd.current;
  const live = fd.pending.has(ev.req_id) || !!ev.live;
  const card = el("div", "rcard");
  card.dataset.req = ev.req_id;
  const o = ev.other_task, cur = ev.current_task;
  const head = { pull_in: `✎ Pull task “${o ? o.title : "?"}” into this task?`,
                 pull_part: `✎ Pull part of “${o ? o.title : "?"}” into this task?`,
                 split: "✎ Split part of this task into a new task?" }[ev.kind] || "✎ Change the tasks?";
  card.appendChild(el("div", "ptitle", head));
  if (ev.why) card.appendChild(el("div", "muted small", ev.why));
  const field = (label, node) => { card.append(el("label", "small-label", label), node); return node; };
  const ta = (v, rows = 4) => Object.assign(el("textarea"), { rows, value: v || "" });
  const inputs = {};
  if (ev.kind === "pull_in" && o) {
    card.appendChild(el("div", "small", `Its details are added to “${cur.title}”, and it is closed as merged.`));
    field(`“${o.title}” — what comes over`, Object.assign(ta(o.details), { readOnly: true }));
  } else if (ev.kind === "pull_part" && o) {
    inputs.part = field("The part that comes into this task", ta(ev.part));
    inputs.remaining = field(`What stays in “${o.title}”`, ta(ev.remaining || o.details));
  } else if (ev.kind === "split") {
    inputs.keep = field(`What “${cur.title}” keeps`, ta(ev.keep || cur.details));
    inputs.new_title = field("New task", Object.assign(el("input"), { type: "text", value: ev.new_title || "" }));
    inputs.new_details = field("Its details", ta(ev.new_details));
    const q = (ev.queues || []).find((x) => x.group_id != null && x.name.toLowerCase() === (ev.group || "").toLowerCase());
    inputs.queue = field("Queue", queueSelect(ev.queues || [], q ? String(q.group_id) : "main", false));
  }
  inputs.note = field("Why (kept in the task's history)", Object.assign(el("input"), { type: "text", value: ev.why || "" }));
  const actions = el("div", "pactions");
  if (live) {
    const ok = el("button", "primary", "Change the tasks");
    ok.onclick = guarded(async () => {
      const c = { note: inputs.note.value };
      for (const k of ["part", "remaining", "keep", "new_title", "new_details"]) if (inputs[k]) c[k] = inputs[k].value;
      if (inputs.queue) c.group_id = inputs.queue.value === "main" ? null : Number(inputs.queue.value);
      card.querySelectorAll("button").forEach((b) => (b.disabled = true));
      await api("fd_answer", sid, ev.req_id, "change", c);
    });
    const no = el("button", "ghost", "Keep them as they are");
    no.onclick = guarded(async () => { await api("fd_answer", sid, ev.req_id, "decline", ""); });
    actions.append(ok, no);
  } else {
    actions.appendChild(el("span", "muted small", "No longer waiting."));
    card.classList.add("done");
  }
  card.appendChild(actions);
  return card;
}

function fdSkillCard(ev) {
  const sid = fd.current;
  const card = el("div", "rcard");
  card.dataset.rule = ev.id;
  card.appendChild(el("div", "ptitle", "✎ Claude suggests a skill"));
  if (ev.why) card.appendChild(el("div", "muted small", ev.why));
  const field = (label, node) => { card.append(el("label", "small-label", label), node); return node; };
  const name = field("Name", Object.assign(el("input"), { type: "text", value: ev.name || "", spellcheck: false }));
  const desc = field("When to use it", Object.assign(el("input"), { type: "text", value: ev.description || "" }));
  const body = field("Instructions", Object.assign(el("textarea"), { rows: 10, value: ev.body || "", spellcheck: false }));
  const where = el("select");
  const project = fd.detail && fd.detail.project;
  if (project) where.appendChild(Object.assign(el("option", "", `This project's skills (${project.name})`), { value: "project" }));
  where.appendChild(Object.assign(el("option", "", "My personal skills (every project)"), { value: "personal" }));
  field("Save to", where);
  const actions = el("div", "pactions");
  const add = el("button", "primary", "Add skill");
  add.onclick = guarded(async () => {
    const msg = await api("fd_apply_skill", sid, ev.id, name.value, desc.value, body.value, where.value);
    ruleDone(card, msg);
  });
  const dismiss = el("button", "ghost", "Dismiss");
  dismiss.onclick = guarded(async () => { await api("fd_dismiss_skill", sid, ev.id); ruleDone(card, "Dismissed."); });
  actions.append(add, dismiss);
  card.appendChild(actions);
  return card;
}

/* ---- starting tasks ---- */

function fdFillPresets(sel, chosen) {
  sel.replaceChildren(el("option", "", "None (Claude Code defaults)"));
  sel.firstChild.value = "";
  for (const p of fd.overview.presets) { const o = el("option", "", p.name); o.value = p.id; sel.appendChild(o); }
  sel.value = chosen ? String(chosen) : "";
}

let fdStartProject = null;
async function fdOpenStart(projectId, taskId) {
  const p = fd.overview.projects.find((x) => x.id === projectId);
  if (!p) return;
  fdStartProject = p;
  $("#fdn-title").textContent = `Start a task — ${p.name}`;
  $("#fdn-where").textContent = p.path;
  const sel = $("#fdn-task-sel");
  sel.replaceChildren();
  for (const t of p.open_tasks) {
    const o = el("option", "", (t.status === "active" ? "(in progress) " : "") + t.title);
    o.value = t.id; sel.appendChild(o);
  }
  const n = el("option", "", "＋ A new task…"); n.value = "new"; sel.appendChild(n);
  sel.value = taskId ? String(taskId) : (p.open_tasks.length ? String(p.open_tasks[0].id) : "new");
  $("#fdn-task").value = "";
  show($("#fdn-newtask"), sel.value === "new");
  fdFillPresets($("#fdn-preset"), p.preset_id);
  $("#fdn-model").value = ""; $("#fdn-effort").value = "";
  syncAllEfforts();
  $("#fdn-send").checked = true;
  await fdUpdateIsolation();
  openModal("#modal-fd-new");
  (sel.value === "new" ? $("#fdn-task") : $("#fdn-start")).focus();
}

const fdUpdateIsolation = guarded(async () => {
  const iso = await api("fd_isolation", fdStartProject.id);
  show($("#fdn-iso"), iso.needs_choice);
  if (iso.needs_choice) {
    const others = iso.sharing.map((s) => `“${s.task}”`).join(", ");
    $("#fdn-iso-text").textContent = `This project already has a session in its folder (${others}). How should the new one work?`;
    show($("#fdn-iso-wt"), iso.can_isolate);
    show($("#fdn-iso-uncommitted"), iso.can_isolate && iso.uncommitted);
  }
  document.querySelector("input[name=fdn-iso][value=shared]").checked = true;
});

async function fdBegin(taskId, isolation, presetId, sendTask, btn, label, extra = {}) {
  btn.disabled = true; btn.textContent = "Starting…";
  try {
    const s = await fdStartTaskApi(taskId, isolation, presetId ? Number(presetId) : null,
                                   extra.model || null, null, sendTask, extra.effort || null);
    if (!s) return;
    closeModal();
    await fdRefresh();
    await fdSelect(s.id);
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

const fdStart = guarded(async () => {
  let taskId = $("#fdn-task-sel").value;
  if (taskId === "new") {
    const title = $("#fdn-task").value.trim();
    if (!title) { $("#fdn-task").focus(); return; }
    taskId = (await api("fd_new_task", fdStartProject.id, title, "")).id;
  }
  const iso = document.querySelector("input[name=fdn-iso]:checked").value;
  await fdBegin(Number(taskId), iso, $("#fdn-preset").value, $("#fdn-send").checked, $("#fdn-start"), "Start",
                { model: $("#fdn-model").value, effort: $("#fdn-effort").value });
});

let fdsFolder = null;
function fdOpenStandalone() {
  $("#fds-title").value = ""; $("#fds-details").value = "";
  fdsFolder = null; $("#fds-folder").textContent = "";
  document.querySelector("input[name=fds-where][value=scratch]").checked = true;
  fdFillPresets($("#fds-preset"), null);
  $("#fds-model").value = ""; $("#fds-effort").value = "";
  syncAllEfforts();
  openModal("#modal-fd-standalone");
  $("#fds-title").focus();
}

const fdCreateStandalone = guarded(async () => {
  const title = $("#fds-title").value.trim();
  if (!title) { $("#fds-title").focus(); return; }
  const where = document.querySelector("input[name=fds-where]:checked").value;
  if (where === "folder" && !fdsFolder) { banner("Choose a folder, or use a scratch folder."); return; }
  const t = await api("fd_standalone", title, $("#fds-details").value.trim(), where === "folder" ? fdsFolder : null);
  await fdBegin(t.id, "shared", $("#fds-preset").value, true, $("#fds-start"), "Start task",
                { model: $("#fds-model").value, effort: $("#fds-effort").value });
});

const fdStartStandalone = guarded(async (t) => {
  const s = await fdStartTaskApi(t.id, "shared", null, null, null, true, null);
  if (!s) return;
  await fdRefresh();
  await fdSelect(s.id);
});

const fdFinishStandalone = guarded(async (t) => {
  const summary = await promptText(`Mark “${t.title}” done. Summary of what was done (optional):`, {});
  if (summary === null) return;
  await api("fd_finish_standalone", t.id, summary);
  await fdRefresh();
});

/* ---- finishing, upgrading, assigning ---- */

function fdOpenDone() {
  document.querySelector("input[name=fdd-how][value=claude]").checked = true;
  $("#fdd-summary").value = ""; show($("#fdd-summary"), false);
  openModal("#modal-fd-done");
}

const fdDone = guarded(async () => {
  const how = document.querySelector("input[name=fdd-how]:checked").value;
  const summary = how === "me" ? $("#fdd-summary").value.trim() : null;
  closeModal();
  const r = await api("fd_complete", fd.current, summary);
  if (r.done) { await fdSelect(fd.current); await fdRefresh(); }
});

let fdTaskTarget = null;
function fdOpenUpgrade(t) {
  fdTaskTarget = t;
  $("#fdu-name").value = t.title; $("#fdu-goal").value = ""; $("#fdu-tags").value = "";
  openModal("#modal-fd-upgrade");
  $("#fdu-name").focus();
}

const fdUpgrade = guarded(async () => {
  const name = $("#fdu-name").value.trim();
  if (!name) { $("#fdu-name").focus(); return; }
  await api("fd_upgrade", fdTaskTarget.id, name, $("#fdu-goal").value.trim(), $("#fdu-tags").value);
  closeModal();
  await fdRefresh();
  if (fd.current != null) await fdSelect(fd.current);
  banner(`“${name}” is now a project. Add rules and tasks in the Ready Room.`, "info");
});

function fdOpenAssign(t) {
  fdTaskTarget = t;
  const sel = $("#fda-project");
  sel.replaceChildren();
  for (const p of fd.overview.projects) { const o = el("option", "", p.name); o.value = p.id; sel.appendChild(o); }
  if (!fd.overview.projects.length) { banner("There are no projects yet. Create one in the Ready Room."); return; }
  $("#fda-merge").checked = true;
  openModal("#modal-fd-assign");
}

const fdAssign = guarded(async () => {
  const pid = Number($("#fda-project").value);
  await api("fd_assign", fdTaskTarget.id, pid, $("#fda-merge").checked);
  closeModal();
  await fdRefresh();
  if (fd.current != null) await fdSelect(fd.current);
  banner(`Added to ${fdProjectName(pid)}'s history.`, "info");
});

/* Task actions in the session header depend on the session's task. */
function fdTaskForSession() {
  const d = fd.detail;
  if (!d || !d.task_row) return null;
  return d.task_row;
}

function fdRenderTaskButtons() {
  const t = fdTaskForSession();
  const s = fd.detail;
  const standalone = t && t.project_id == null;
  const live = s.running && s.status !== "ended";
  show($("#fd-done"), !!t && t.status !== "done");
  show($("#fd-upgrade"), standalone && !live);
  show($("#fd-assign"), standalone && !live && t.status === "done");
}

const fdDebriefThis = guarded(async () => {
  const t = await api("fd_debrief_target", fd.current);
  showTool("debrief");
  await initStart();
  if (t.project_id != null) {
    showProject(await api("db_project", t.project_id));
  } else {  // an own copy (worktree): review just that folder
    state.projectId = null;
    state.worktreeSid = fd.current;
    show($("#db-folders"), false); show($("#extras-note"), false);
    showFolder(await api("db_worktree", fd.current), `Own copy · ${t.task}`);
  }
  if (t.from_ref) {
    const sel = $("#from-select");
    const o = el("option", "", `Changes made during the Flight Deck session “${t.task}”`);
    o.value = t.from_ref;
    sel.insertBefore(o, sel.firstChild);
    sel.value = t.from_ref;
    show($("#resume-box"), false); show($("#start-box"));
    state.freshStart = true;
  }
});

function fdBind() {
  $("#fd-standalone-btn").onclick = () => fd.overview && fdOpenStandalone();
  $("#fd-goto-rr").onclick = () => showTool("readyroom");
  $("#fds-cancel").onclick = closeModal;
  $("#fds-start").onclick = fdCreateStandalone;
  $("#fds-folder-btn").onclick = guarded(async () => {
    const f = await api("pick_folder");
    if (f) { fdsFolder = f; $("#fds-folder").textContent = f;
             document.querySelector("input[name=fds-where][value=folder]").checked = true; }
  });
  $("#fdn-task-sel").onchange = (e) => { show($("#fdn-newtask"), e.target.value === "new"); };
  $("#fd-done").onclick = fdOpenDone;
  document.querySelectorAll("input[name=fdd-how]").forEach((r) => (r.onchange = () =>
    show($("#fdd-summary"), document.querySelector("input[name=fdd-how]:checked").value === "me")));
  $("#fdd-cancel").onclick = closeModal;
  $("#fdd-ok").onclick = fdDone;
  $("#fd-upgrade").onclick = () => fdTaskForSession() && fdOpenUpgrade(fdTaskForSession());
  $("#fd-assign").onclick = () => fdTaskForSession() && fdOpenAssign(fdTaskForSession());
  $("#fdu-cancel").onclick = closeModal;
  $("#fdu-ok").onclick = fdUpgrade;
  $("#fda-cancel").onclick = closeModal;
  $("#fda-ok").onclick = fdAssign;
  $("#fd-policy").onchange = guarded(async (e) => { await api("fd_set_policy", e.target.value); });
  $("#fdn-cancel").onclick = closeModal;
  $("#fdn-start").onclick = fdStart;
  $("#fdn-task").onkeydown = (e) => { if (e.key === "Enter") fdStart(); };
  $("#fd-send").onclick = fdSend;
  $("#fd-auto-takeoff").onchange = guarded(async (e) => {
    if (e.target.checked && !(await confirmWord("Auto take-off starts queued routines on its own — each one is a " +
        "Claude Code session that uses its project's default preset.", "yes", "Turn on"))) {
      e.target.checked = false; return;
    }
    await api("fd_set_auto_takeoff", e.target.checked);
    fdRefreshSoon();
  });
  $("#fd-image-btn").onclick = () => $("#fd-image-file").click();
  $("#fd-image-file").onchange = (e) => { fdAddImages([...e.target.files]); e.target.value = ""; };
  $("#fd-input").addEventListener("paste", (e) => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter((f) => /^image\//.test(f.type));
    if (files.length) { e.preventDefault(); fdAddImages(files); }
  });
  for (const zone of ["#fd-input", "#fd-transcript"]) {
    $(zone).addEventListener("dragover", (e) => e.preventDefault());
    $(zone).addEventListener("drop", (e) => {
      const files = [...e.dataTransfer.files].filter((f) => /^image\//.test(f.type));
      if (files.length) { e.preventDefault(); fdAddImages(files); }
    });
  }
  bindMarkup();
  $("#fd-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); fdSend(); }
  });
  $("#fd-stop").onclick = guarded(async () => { await api("fd_interrupt", fd.current); });
  $("#fd-mode").onchange = guarded(async (e) => {
    if (e.target.value === "bypassPermissions" &&
        !(await confirmWord("“Never ask” lets Claude run commands and edit files without asking.", "yes", "Allow"))) {
      e.target.value = fd.detail.mode; return;
    }
    fd.detail = { ...fd.detail, ...(await api("fd_set_mode", fd.current, e.target.value)) };
  });
  const switcher = (sel, method, key) => guarded(async () => {
    const before = fd.detail[key] || "";
    sel.disabled = true;
    try { fd.detail = { ...fd.detail, ...(await api(method, fd.current, sel.value || null)) }; }
    catch (e) { sel.value = before; throw e; }
    finally { fdSetStatus(fd.detail.status); }
  });
  $("#fd-model").onchange = switcher($("#fd-model"), "fd_set_model", "model");
  $("#fd-effort").onchange = switcher($("#fd-effort"), "fd_set_effort", "effort");
  $("#fd-resume").onclick = guarded(async () => { await api("fd_resume", fd.current); await fdSelect(fd.current); });
  $("#fd-continue").onclick = guarded(async () => { await api("fd_continue", fd.current); await fdSelect(fd.current); });
  $("#fd-end").onclick = guarded(async () => {
    if (!(await confirmWord("End this session? (The conversation stays in the list for a day.)", "yes", "End session"))) return;
    await api("fd_end", fd.current); await fdSelect(fd.current); fdRefresh();
  });
  $("#fd-rmcopy").onclick = () => {
    $("#bb-where").textContent = `Branch ${fd.detail.branch}` + (fd.detail.worktree ? ` · ${fd.detail.cwd}` : " (copy already removed)");
    openModal("#modal-bringback");
  };
  const bring = (action) => guarded(async () => {
    if (action === "discard" && !(await confirmWord("Discard the copy and its branch? Its work is deleted.", "discard", "Discard"))) return;
    closeModal();
    const r = await api("fd_bring_back", fd.current, action);
    await fdSelect(fd.current); fdRefresh();
    banner(r.text, "info");
  });
  $("#bb-merge").onclick = bring("merge");
  $("#bb-keep").onclick = bring("keep");
  $("#bb-discard").onclick = bring("discard");
  $("#bb-review").onclick = () => { closeModal(); fdDebriefThis(); };
  $("#bb-cancel").onclick = closeModal;
  $("#fd-debrief").onclick = fdDebriefThis;
}

/* ------------------------------------------------------------ models & effort
 * The model list comes from Claude Code (asked at startup; see core/models.py).
 * Effort is the user's preference: a model without that level runs at the
 * closest lower one, and a model without effort ignores it (the field is
 * greyed but stays editable, so the choice survives switching models).
 */
let MODELS = { models: [{ value: "haiku", label: "Haiku", efforts: [] }, { value: "sonnet", label: "Sonnet", efforts: [] },
                        { value: "opus", label: "Opus", efforts: [] }],
               default: { value: "default", label: "Default", efforts: [] }, checked: null,
               efforts: ["low", "medium", "high", "xhigh", "max"] };
const EFFORT_LABEL = { low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Max" };

function modelEfforts(model) {
  if (!model) return MODELS.default.efforts || [];
  const m = MODELS.models.find((x) => x.value === model);
  return m ? (m.efforts || []) : MODELS.efforts;  // unknown model: let Claude Code decide
}

function effectiveEffort(model, effort) {
  if (!effort) return null;
  const order = MODELS.efforts;
  const supported = order.filter((e) => modelEfforts(model).includes(e));
  if (!supported.length) return null;
  if (supported.includes(effort)) return effort;
  const lower = supported.filter((e) => order.indexOf(e) < order.indexOf(effort));
  return lower.length ? lower[lower.length - 1] : supported[0];
}

/* Rebuild a model <select>'s options from the catalog, keeping its value. */
function fillModelSelect(sel, firstLabel) {
  const current = sel.value;
  sel.replaceChildren(Object.assign(el("option", "", firstLabel), { value: "" }));
  for (const m of MODELS.models) {
    const o = Object.assign(el("option", "", m.label), { value: m.value });
    if (m.description) o.title = m.description;
    sel.appendChild(o);
  }
  if (current && !MODELS.models.some((m) => m.value === current)) {
    sel.appendChild(Object.assign(el("option", "", current), { value: current }));
  }
  sel.value = current;
}

/* Grey an effort <select> when the model ignores effort, and say when it falls back. */
function syncEffort(effortSel, model, inherited = "") {
  const chosen = effortSel.value || inherited;
  const supported = modelEfforts(model);
  const na = supported.length === 0;
  effortSel.classList.toggle("na", na);
  let note = effortSel.nextElementSibling;
  if (!note || !note.classList.contains("effort-note")) {
    note = el("span", "effort-note");
    effortSel.after(note);
  }
  const used = effectiveEffort(model, chosen);
  note.textContent = na ? "not used by this model" : chosen && used !== chosen ? `runs at ${EFFORT_LABEL[used] || used}` : "";
  effortSel.title = na ? "This model has no effort setting. Your choice is kept for models that do."
                       : "How hard the model thinks. Higher is more thorough, slower and costs more.";
}

function presetModel(presetId) {
  const p = fd.overview && fd.overview.presets.find((x) => String(x.id) === String(presetId));
  return p ? p.model : null;
}

/* Fill every model picker in the page and re-check the effort fields. */
function applyModels() {
  fillModelSelect($("#rrps-model"), "Claude Code default");
  fillModelSelect($("#fdn-model"), "From the preset");
  fillModelSelect($("#fds-model"), "From the preset");
  fillModelSelect($("#fd-model"), "Default");
  fillModelSelect($("#rra-model"), "Default (sonnet)");
  syncAllEfforts();
}

function syncAllEfforts() {
  syncEffort($("#rrps-effort"), $("#rrps-model").value);
  syncEffort($("#fdn-effort"), $("#fdn-model").value || presetModel($("#fdn-preset").value));
  syncEffort($("#fds-effort"), $("#fds-model").value || presetModel($("#fds-preset").value));
  syncEffort($("#fd-effort"), $("#fd-model").value);
  syncEffort($("#rra-effort"), $("#rra-model").value || "sonnet");
}

async function loadModels() {
  try {
    const before = MODELS.checked;
    MODELS = await api("models");
    if (MODELS.checked !== before || before === null) applyModels();
    if (rr.agents && rr.pane === "agents") rrRenderAgents();
  } catch (_) { /* keep the built-in list */ }
}

function bindModels() {
  for (const [m, e, extra] of [["#rrps-model", "#rrps-effort"], ["#fdn-model", "#fdn-effort", "#fdn-preset"],
                               ["#fds-model", "#fds-effort", "#fds-preset"], ["#fd-model", "#fd-effort"],
                               ["#rra-model", "#rra-effort"]]) {
    for (const id of [m, e, extra].filter(Boolean)) $(id).addEventListener("change", syncAllEfforts);
  }
  applyModels();
  loadModels();
  // Claude Code is asked in the background at startup: pick up its answer.
  setTimeout(loadModels, 8000);
  setTimeout(loadModels, 30000);
}

function sevClass(sel, value) {
  sel.classList.remove("sevsel-high", "sevsel-medium", "sevsel-low");
  if (value) sel.classList.add("sevsel-" + value);
}

/* ============================================================ READY ROOM
 * Project setup: goals, rules, task queues, links, rule sets, presets and
 * templates. Everything goes through rr_* (carrier/readyroom/manager.py).
 * Project names, goals, rules and task text are user data: textContent only.
 */

const rr = {
  ov: null,          // rr_overview()
  pane: "intro",
  pid: null,         // open project id
  proj: null,        // rr_project()
  dirty: false,
  tag: null,
  search: "",
  newFolder: null,
  edit: { rulesets: null, presets: null, templates: null },  // selected item id (or "new")
};

const tagsText = (tags) => (tags || []).join(", ");

async function rrOpen() {
  await rrLoadAll();
  if (rr.pane === "project" && rr.pid && !rr.dirty) {
    try { rr.proj = await api("rr_project", rr.pid); rrRenderProject(); }
    catch (_) { rr.pid = null; rr.pane = "intro"; }  // removed elsewhere
  }
  rrShow(rr.pane === "project" && rr.pid ? "project" : rr.pane === "project" ? "intro" : rr.pane);
}

const rrLoad = guarded(async () => {
  rr.ov = await api("rr_overview", rr.search, rr.tag);
  rrRenderList();
});

function rrShow(pane) {
  rr.pane = pane;
  for (const n of ["intro", "new", "import", "project", "global", "rulesets", "skills", "presets", "templates", "agents"]) {
    show($(`#rr-pane-${n}`), n === pane);
  }
  document.querySelectorAll(".rr-navbtn").forEach((b) => b.classList.toggle("on", b.dataset.pane === pane));
  if (pane !== "project") { rr.pid = null; rrRenderList(); }
  if (pane === "project") rrAgentsSummary();
  if (pane === "global") rrRenderGlobal();
  if (pane === "rulesets") rrRenderRuleSets();
  if (pane === "presets") rrRenderPresets();
  if (pane === "templates") rrRenderTemplates();
  if (pane === "import") imOpen();
  if (pane === "skills") skOpen();
  $("#rr-main").scrollTop = 0;
}

async function rrLeave() {
  if (!rr.dirty) return true;
  if (!(await confirmWord("This project has unsaved changes. Leave without saving?", "discard", "Discard changes"))) return false;
  rr.dirty = false;
  return true;
}

function rrRenderList() {
  if (!rr.ov) return;
  const tagBox = $("#rr-tags");
  tagBox.replaceChildren();
  for (const t of rr.ov.tags) {
    const chip = el("button", "tag" + (rr.tag === t ? " on" : ""), t);
    chip.title = rr.tag === t ? "Show all projects" : `Show projects tagged “${t}”`;
    chip.onclick = () => { rr.tag = rr.tag === t ? null : t; rrLoad(); };
    tagBox.appendChild(chip);
  }
  const box = $("#rr-list");
  box.replaceChildren();
  if (!rr.ov.projects.length) {
    box.appendChild(el("div", "muted small", rr.search || rr.tag ? "No matching projects." : "No projects yet."));
  }
  for (const p of rr.ov.projects) {
    const item = el("div", "rr-item" + (p.id === rr.pid ? " active" : ""));
    item.tabIndex = 0;
    item.dataset.id = p.id;
    item.appendChild(el("div", "n", p.name + (p.active_sessions ? " ●" : "")));
    item.appendChild(el("div", "c", p.current_task ? "Current: " + p.current_task : "No open tasks"));
    if (!p.folder_exists) item.appendChild(el("div", "c warn", "Folder missing"));
    if (p.tags.length) {
      const tags = el("div", "tags");
      for (const t of p.tags) tags.appendChild(el("span", "tag", t));
      item.appendChild(tags);
    }
    item.title = p.path;
    item.onclick = () => rrOpenProject(p.id);
    item.onkeydown = (e) => { if (e.key === "Enter") rrOpenProject(p.id); };
    box.appendChild(item);
  }
}

function rrRuleSetChecks(box, selected) {
  box.replaceChildren();
  if (!rr.ov.rule_sets.length) {
    box.appendChild(el("span", "muted small", "No rule sets yet — make them under Rule sets."));
    return;
  }
  for (const r of rr.ov.rule_sets) {
    const lab = el("label", "check inline");
    const cb = el("input"); cb.type = "checkbox"; cb.value = r.id;
    cb.checked = (selected || []).includes(r.id);
    lab.append(cb, document.createTextNode(" " + r.name));
    box.appendChild(lab);
  }
}
const rrCheckedIds = (box) => [...box.querySelectorAll("input:checked")].map((c) => Number(c.value));

function rrFillPresets(sel, chosen, none = "None (Claude Code defaults)") {
  sel.replaceChildren(el("option", "", none));
  sel.firstChild.value = "";
  for (const p of rr.ov.presets) { const o = el("option", "", p.name); o.value = p.id; sel.appendChild(o); }
  sel.value = chosen ? String(chosen) : "";
}

/* ---- new project ---- */

async function rrNewProject() {
  if (!(await rrLeave())) return;
  rr.newFolder = null;
  $("#rrn-folder").textContent = "No folder chosen";
  for (const id of ["#rrn-name", "#rrn-goal", "#rrn-task", "#rrn-task-details", "#rrn-tags", "#rrn-rules"]) $(id).value = "";
  const sel = $("#rrn-template");
  sel.replaceChildren(el("option", "", "A blank project"));
  sel.firstChild.value = "";
  for (const t of rr.ov.templates) { const o = el("option", "", t.name); o.value = t.id; sel.appendChild(o); }
  rrShow("new");
}

function rrApplyTemplate() {
  const t = rr.ov.templates.find((x) => String(x.id) === $("#rrn-template").value);
  if (!t) return;
  if (!$("#rrn-goal").value.trim()) $("#rrn-goal").value = t.goal;
  if (!$("#rrn-task").value.trim()) $("#rrn-task").value = t.first_task;
  if (!$("#rrn-tags").value.trim()) $("#rrn-tags").value = tagsText(t.tags);
  if (!$("#rrn-rules").value.trim()) $("#rrn-rules").value = t.rules || "";
}

const rrCreate = guarded(async () => {
  if (!rr.newFolder) { banner("Choose the project's folder first."); return; }
  const tpl = $("#rrn-template").value;
  const p = await api("rr_create_project", {
    name: $("#rrn-name").value.trim(), path: rr.newFolder, goal: $("#rrn-goal").value,
    first_task: $("#rrn-task").value, first_task_details: $("#rrn-task-details").value, tags: $("#rrn-tags").value,
    rules: $("#rrn-rules").value, template_id: tpl ? Number(tpl) : null,
  });
  await rrLoadAll();
  await rrOpenProject(p.id, true);
});

/* ---- project editor ---- */

const rrOpenProject = guarded(async (pid, skipCheck = false) => {
  if (!skipCheck && pid !== rr.pid && !(await rrLeave())) return;
  rr.proj = await api("rr_project", pid);
  rr.pid = pid;
  rrRenderProject();
  rrShow("project");
  rrRenderList();
});

function rrSetDirty(on) {
  rr.dirty = on;
  $("#rrp-save").disabled = !on;
  show($("#rrp-dirty"), on);
}

function rrRenderAllow() {
  const a = rr.proj.allow || {};
  document.querySelectorAll("#rrp-allow-box [data-allow]").forEach((c) => { c.checked = !!a[c.dataset.allow]; });
  document.querySelectorAll("#rrp-allow-box [data-allow-list]").forEach((i) => { i.value = (a[i.dataset.allowList] || []).join(", "); });
  $("#rrp-allow-sites").disabled = !a.web_fetch;
  const box = $("#rrp-allow-groups");
  box.replaceChildren();
  const suggest = new Set(rr.proj.allow_suggest || []);
  for (const g of rr.proj.allow_groups || []) {
    const lab = el("label", "check small allow-group");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = (a.groups || []).includes(g.id);
    cb.onchange = guarded(async () => {
      const groups = new Set(rr.proj.allow.groups || []);
      if (cb.checked) groups.add(g.id); else groups.delete(g.id);
      rr.proj = await api("rr_set_allow", rr.pid, { groups: [...groups] });
      rrRenderAllow();
    });
    lab.append(cb, el("b", "", " " + g.label + " "), el("span", "muted", "— " + g.entries.join(", ")));
    if (suggest.has(g.id)) lab.appendChild(el("span", "tag suggest", "suggested for this folder"));
    if (cb.checked) lab.appendChild(el("div", "muted group-note", g.note));  // what you've accepted
    lab.title = g.note;
    box.appendChild(lab);
  }
  const err = $("#rrp-allow-export-error");
  err.textContent = a.export ? (rr.proj.allow_export_error || "") : "";
  show(err, !!err.textContent);
  const on = [a.read && "read files", a.edit && "edit files", a.web_search && "web search",
              a.web_fetch && (a.sites.length ? "fetch from " + a.sites.length + " site" + (a.sites.length > 1 ? "s" : "") : "fetch any site"),
              (a.groups || []).length && (rr.proj.allow_groups || []).filter((g) => a.groups.includes(g.id)).map((g) => g.label).join(", "),
              a.commands.length && a.commands.join(", "), a.tools.length && a.tools.length + " other tool" + (a.tools.length > 1 ? "s" : "")]
    .filter(Boolean);
  $("#rrp-allow-sum").textContent = on.length ? "— " + on.join(" · ") : "— none; Claude asks each time";
}

function rrRenderProject() {
  const p = rr.proj;
  $("#rrp-name").value = p.name;
  $("#rrp-folder").textContent = p.path + (p.folder_exists ? "" : "  (folder missing)");
  rrRenderFolders();
  $("#rrp-goal").value = p.goal;
  $("#rrp-tags").value = tagsText(p.tags);
  $("#rrp-rules").value = p.rules;
  rrRuleSetChecks($("#rrp-rulesets"), p.rule_set_ids);
  rrFillPresets($("#rrp-preset"), p.preset_id);
  rrSetDirty(false);
  $("#rrp-images").checked = p.images !== false;
  rrRenderAllow();
  rrRenderCopies();
  rrRenderTasks();
  rrRenderLinks();
  roEdit(null);
  $("#rrp-routine-form").open = false;
  rgEdit(null);
  $("#rrp-group-form").open = false;
  rrRenderRoutines();
}

function rrRenderFolders() {
  const box = $("#rrp-folders");
  box.replaceChildren();
  if (!rr.proj.folders.length) box.appendChild(el("span", "muted", "none"));
  for (const f of rr.proj.folders) {
    // A small card: the folder's name, then its type and a remove button.
    const chip = el("span", "folder-chip" + (f.kind === "reference" ? " ref" : f.kind === "workspace" ? " ws" : ""));
    chip.title = f.path + (f.exists ? "" : " (missing)");
    const name = el("span", "fc-name", f.label);
    if (!f.exists) name.appendChild(el("span", "muted", " (missing)"));
    const row = el("span", "fc-row");
    const kind = el("select", "kind-select");
    kind.setAttribute("aria-label", `Type of ${f.label}`);
    kind.title = "Work: Claude reads and edits it, and Debrief reviews it. Reference: read only. " +
      "Workspace: read only, but a new folder Claude creates there is added as a work folder.";
    for (const [v, t] of [["work", "Work folder"], ["reference", "Reference (read only)"],
                          ["workspace", "Workspace (new folders join)"]]) {
      kind.appendChild(Object.assign(el("option", "", t), { value: v }));
    }
    kind.value = f.kind;
    kind.onchange = guarded(async () => {
      try { rr.proj = await api("rr_set_folder_kind", rr.pid, f.path, kind.value); }
      finally { rrRenderFolders(); }
    });
    const rm = el("button", "fc-remove", "Remove");
    rm.title = `Remove ${f.label} from this project (the folder is not touched)`;
    rm.onclick = guarded(async () => { rr.proj = await api("rr_remove_folder", rr.pid, f.path); rrRenderFolders(); });
    row.append(kind, rm);
    chip.append(name, row);
    box.appendChild(chip);
  }
}

/* ---- routines (readyroom/routines.py) ---- */

const ro = { editing: null };

const rrRenderRoutines = guarded(async () => {
  if (rr.pid == null) return;
  const pid = rr.pid;
  const data = await api("rr_routines", pid);
  if (rr.pid !== pid) return;
  const box = $("#rrp-routines");
  box.replaceChildren();
  if (!data.routines.length) box.appendChild(el("p", "muted small", "No routines yet."));
  for (const r of data.routines) {
    const row = el("div", "routine-row" + (r.enabled ? "" : " off"));
    const info = el("div", "routine-info");
    info.appendChild(el("b", "", r.name));
    const group = ((rr.proj && rr.proj.groups) || []).find((g) => g.id === r.group_id);
    const tags = [r.trigger === "after_task" ? "after each task" : r.trigger === "group_done" ? "when its group is finished"
                  : "manual", r.gate === "reviewed" ? "needs a clear review" : "no gate"];
    if (group) tags.push(`group: ${group.name}`);
    if (!r.enabled) tags.push("paused");
    info.appendChild(el("span", "muted small", tags.join(" · ")));
    const run = el("button", "ghost small-btn", "▶ Run");
    run.title = "Put it next in the queue";
    run.onclick = guarded(async () => { rr.proj = await api("rr_run_routine", r.id); rrRenderTasks(); rrLoad(); });
    const edit = el("button", "ghost small-btn", "Edit");
    edit.onclick = () => roEdit(r);
    const toggle = el("button", "ghost small-btn", r.enabled ? "Pause" : "Resume");
    toggle.title = "Paused routines don't queue themselves after tasks";
    toggle.onclick = guarded(async () => {
      await api("rr_save_routine", pid, { ...r, enabled: !r.enabled }, r.id); rrRenderRoutines();
    });
    const del = el("button", "ghost small-btn", "Delete");
    del.onclick = guarded(async () => {
      if (!(await confirmWord(`Delete the routine ${r.name}?`, "delete", "Delete"))) return;
      await api("rr_delete_routine", pid, r.id); rrRenderRoutines();
    });
    row.append(info, run, edit, toggle, del);
    box.appendChild(row);
  }
  rrRenderSecrets(pid, data);
  const gated = data.routines.some((r) => r.gate === "reviewed");
  $("#rrp-review-state").textContent = !gated ? "" : data.blockers.length
    ? "Review not clear yet: " + data.blockers.join(" ") : "✓ Review is clear: gated routines can start.";
  show($("#rrp-start-history"), data.blockers.some((b) => /never been reviewed/.test(b)));
});

const rg = { editing: null };

function rgEdit(g) {
  rg.editing = g;
  $("#tgf-name").value = g ? g.name : "";
  $("#tgf-priority").value = g ? g.priority : "normal";
  $("#tgf-tags").value = g ? g.tags.join(", ") : "";
  $("#rrp-group-form summary").textContent = g ? `Edit task group: ${g.name}` : "＋ New task group";
  $("#rrp-group-form").open = !!g || $("#rrp-group-form").open;
}

function rgBind() {
  $("#tgf-save").onclick = guarded(async () => {
    rr.proj = await api("rr_save_group", rr.pid, { name: $("#tgf-name").value, priority: $("#tgf-priority").value,
      tags: $("#tgf-tags").value }, rg.editing ? rg.editing.id : null);
    rgEdit(null); $("#rrp-group-form").open = false;
    rrRenderTasks(); rrLoad(); rrRenderRoutines();
  });
  $("#tgf-cancel").onclick = () => { rgEdit(null); $("#rrp-group-form").open = false; };
}

function roFillGroups(selected) {
  const sel = $("#rro-group");
  sel.replaceChildren(Object.assign(el("option", "", "None (main queue)"), { value: "" }));
  for (const g of (rr.proj && rr.proj.groups) || []) sel.appendChild(Object.assign(el("option", "", g.name), { value: g.id }));
  sel.value = selected ? String(selected) : "";
}

/* Likely secrets in the project's files, and key files git would publish. */
function rrRenderSecrets(pid, data) {
  const box = $("#rrp-secrets");
  box.replaceChildren();
  const hits = data.secrets || [], files = data.secret_files || [];
  show(box, hits.length > 0 || files.length > 0);
  if (hits.length) {
    box.appendChild(el("b", "", `⚠ ${hits.length} possible secret${hits.length === 1 ? "" : "s"} in this project's files`));
    box.appendChild(el("p", "muted small", "Routines that need a clear review won't start while these are here. " +
      "Remove the secret (use an environment variable or an ignored file), or mark a line safe if it isn't one."));
    for (const h of hits.slice(0, 30)) {
      const row = el("div", "secret-row");
      row.append(el("span", "", `${h.path}:${h.line}`), el("span", "muted small", `${h.kind} · ${h.preview}`));
      const ok = el("button", "ghost small-btn", "Mark safe");
      ok.title = "Not a real secret: don't report this exact line again (a change to it is reported)";
      ok.onclick = guarded(async () => rrRenderSecrets(pid, await api("rr_mark_secret_safe", pid, h.fingerprint)));
      row.appendChild(ok);
      box.appendChild(row);
    }
  }
  if (files.length) {
    box.appendChild(el("b", "", `⚠ Key or secret files git would publish`));
    for (const f of files) {
      box.appendChild(el("div", "secret-row", `${f.label ? f.label + "/" : ""}${f.path}` +
        (f.tracked ? " — already tracked by git: also run git rm --cached on it" : "")));
    }
    const fix = el("button", "ghost small-btn", "Add secret-file patterns to .gitignore");
    fix.onclick = guarded(async () => {
      rrRenderSecrets(pid, await api("rr_ignore_secret_files", pid));
      banner("Added .env, key and credential patterns to .gitignore.", "info");
    });
    box.appendChild(fix);
  }
}

function roEdit(r) {
  ro.editing = r;
  roFillGroups(r ? r.group_id : null);
  $("#rro-name").value = r ? r.name : "";
  $("#rro-text").value = r ? r.instructions : "";
  $("#rro-trigger").value = r ? r.trigger : "manual";
  $("#rro-gate").value = r ? r.gate : "none";
  $("#rro-mode").value = r ? r.mode || "" : "";
  $("#rrp-routine-form").open = true;
  $("#rrp-routine-form summary").textContent = r ? `Edit routine: ${r.name}` : "＋ New routine";
}

function roBind() {
  $("#rro-save").onclick = guarded(async () => {
    const r = ro.editing;
    await api("rr_save_routine", rr.pid, { name: $("#rro-name").value, instructions: $("#rro-text").value,
      trigger: $("#rro-trigger").value, gate: $("#rro-gate").value, enabled: r ? r.enabled : true,
      group_id: $("#rro-group").value || null, mode: $("#rro-mode").value }, r ? r.id : null);
    roEdit(null);
    $("#rrp-routine-form").open = false;
    rrRenderRoutines();
  });
  $("#rro-cancel").onclick = () => { roEdit(null); $("#rrp-routine-form").open = false; };
}

const PRIORITY_LABEL = { high: "High", normal: "Normal", low: "Low", backlog: "Backlog" };

function rrTaskRow(t, i, queue) {
  const p = rr.proj;
  const row = el("li", "task-row" + (i === 0 ? " current" : ""));
  row.dataset.id = t.id;
  row.appendChild(el("span", "badge-cur", i === 0 ? "CURRENT" : `${i + 1}.`));
  const body = el("div");
  const tt = el("div", "t", t.title + (t.status === "active" ? "  · in progress" : ""));
  if (t.routine_id) tt.appendChild(el("span", "routine-tag", "routine"));
  if (t.fix_review) tt.appendChild(el("span", "routine-tag", "review fixes"));
  body.appendChild(tt);
  if (t.details) body.appendChild(el("div", "d", t.details));
  if (t.origin === "standalone" && t.origin_path) body.appendChild(el("div", "origin", "Files: " + t.origin_path));
  const acts = el("div", "acts");
  const refresh = async (proj) => { rr.proj = proj; rrRenderTasks(); rrLoad(); };
  const btn = (label, title, fn, disabled = false) => {
    const b = el("button", "ghost", label); b.title = title; b.disabled = disabled;
    b.onclick = guarded(fn); acts.appendChild(b);
  };
  btn("↑", "Move up", async () => refresh(await api("rr_move_task", t.id, "up")), i === 0);
  btn("↓", "Move down", async () => refresh(await api("rr_move_task", t.id, "down")), i === queue.length - 1);
  if (i) btn("Make current", "Move to the top of its queue", async () => refresh(await api("rr_move_task", t.id, "top")));
  if ((p.groups || []).length && t.status !== "active") {
    const move = el("select", "move-group");
    move.title = "Move to another queue";
    move.appendChild(Object.assign(el("option", "", "Move to…"), { value: "" }));
    if (t.group_id) move.appendChild(Object.assign(el("option", "", "Main queue"), { value: "main" }));
    for (const g of p.groups) if (g.id !== t.group_id) move.appendChild(Object.assign(el("option", "", g.name), { value: g.id }));
    move.onchange = guarded(async () => {
      if (!move.value) return;
      await refresh(await api("rr_set_task_group", t.id, move.value === "main" ? null : Number(move.value)));
    });
    acts.appendChild(move);
  }
  btn("Changes", "Everything that changed in this task, and why", async () => showTaskHistory(t));
  btn("Edit", "Edit this task", async () => {
    const title = await promptText("Task:", { value: t.title, required: true, line: true });
    if (title === null) return;
    const details = await promptText("Details (optional — Ctrl+Enter to save):", { value: t.details });
    if (details === null) return;
    await refresh(await api("rr_update_task", t.id, title, details));
  });
  btn("Delete", "Delete this task", async () => {
    if (!(await confirmWord(`Delete “${t.title}”?`, "yes", "Delete"))) return;
    await refresh(await api("rr_delete_task", t.id));
  }, t.status === "active");
  row.append(body, acts);
  return row;
}

const EDIT_KIND = { created: "Created", edited: "Edited", scope_added: "Scope added", merged_in: "Pulled in work",
  merged_away: "Work moved out", split_out: "Split", moved: "Moved", status: "Status", deleted: "Deleted" };

const showTaskHistory = guarded(async (t) => {
  const edits = await api("rr_task_history", t.id);
  const box = $("#history-list");
  box.replaceChildren();
  $("#history-title").textContent = `Changes to “${t.title}”`;
  if (!edits.length) box.appendChild(el("p", "muted", "No changes recorded."));
  for (const e of [...edits].reverse()) {
    const row = el("div", "hist-row");
    const head = el("div", "hist-head");
    head.append(el("b", "", EDIT_KIND[e.kind] || e.kind),
                el("span", "muted small", ` ${new Date(e.ts * 1000).toLocaleString()} · by ${e.by}`));
    row.appendChild(head);
    if (e.note) row.appendChild(el("div", "small", e.note));
    if (e.after && e.after.title && e.before && e.before.title && e.after.title !== e.before.title) {
      row.appendChild(el("div", "small", `Title: “${e.before.title}” → “${e.after.title}”`));
    }
    if (e.removed) {
      row.appendChild(el("div", "removed-label", "Removed from the task:"));
      row.appendChild(el("pre", "removed", e.removed));
    }
    if (e.after && typeof e.after.details === "string" && e.kind !== "created") {
      const d = el("details", "small");
      d.append(el("summary", "", "Details after this change"), el("pre", "", e.after.details));
      row.appendChild(d);
    }
    box.appendChild(row);
  }
  openModal("#modal-history");
});

const rrRenderCopies = guarded(async () => {
  const pid = rr.pid;
  const copies = await api("rr_copies", pid);
  if (rr.pid !== pid) return;
  const box = $("#rrp-copies");
  box.replaceChildren();
  show(box, copies.length > 0);
  if (!copies.length) return;
  box.appendChild(el("span", "muted small", "Own copies not brought back yet: "));
  for (const c of copies) {
    const b = el("button", "link", `“${c.task}” (${c.branch})`);
    b.title = c.folder || "Copy removed; the branch is kept";
    b.onclick = () => { showTool("flightdeck"); fdSelect(c.id); };
    box.appendChild(b);
  }
});

const showProjectLog = guarded(async () => {
  const log = await api("rr_project_log", rr.pid);
  const box = $("#history-list");
  box.replaceChildren();
  $("#history-title").textContent = `Activity log — ${rr.proj.name}`;
  if (!log.length) box.appendChild(el("p", "muted", "Nothing recorded yet."));
  for (const e of log) {
    const row = el("div", "hist-row");
    const head = el("div", "hist-head");
    const what = e.subject === "task" ? `Task “${e.title}”: ${(EDIT_KIND[e.kind] || e.kind).toLowerCase()}`
                                      : e.action[0].toUpperCase() + e.action.slice(1);
    head.append(el("b", "", what),
                el("span", "muted small", ` ${new Date(e.ts * 1000).toLocaleString()} · by ${e.by}`));
    row.appendChild(head);
    if (e.detail) row.appendChild(el("pre", "small", e.detail));
    if (e.removed) {
      row.appendChild(el("div", "removed-label", "Removed from the task:"));
      row.appendChild(el("pre", "removed", e.removed));
    }
    box.appendChild(row);
  }
  openModal("#modal-history");
});

function rrRenderTasks() {
  const p = rr.proj;
  const groups = p.groups || [];
  const list = $("#rrp-tasks");
  list.replaceChildren();
  const main = p.tasks.filter((t) => !t.group_id);
  if (!main.length) list.appendChild(el("li", "muted small", groups.length ? "Main queue: no open tasks."
    : "No open tasks. Add the next thing to work on below."));
  main.forEach((t, i) => list.appendChild(rrTaskRow(t, i, main)));
  // Task groups: queues that run alongside the main one.
  const gbox = $("#rrp-groups");
  gbox.replaceChildren();
  for (const g of groups) {
    const sec = el("section", `task-group prio-${g.priority}`);
    const head = el("div", "group-head");
    head.append(el("b", "", g.name), el("span", `prio prio-${g.priority}`, PRIORITY_LABEL[g.priority] || g.priority));
    for (const tag of g.tags) head.appendChild(el("span", "tag", tag));
    head.appendChild(el("span", "spacer"));
    const edit = el("button", "ghost small-btn", "Edit");
    edit.onclick = () => rgEdit(g);
    const del = el("button", "ghost small-btn", "Delete group");
    del.title = "Its tasks move to the main queue";
    del.onclick = guarded(async () => {
      if (!(await confirmWord(`Delete the group ${g.name}? Its tasks move to the main queue.`, "delete", "Delete"))) return;
      rr.proj = await api("rr_delete_group", rr.pid, g.id); rrRenderTasks(); rrLoad(); rrRenderRoutines();
    });
    head.append(edit, del);
    const ol = el("ol", "task-list");
    const q = p.tasks.filter((t) => t.group_id === g.id);
    if (!q.length) ol.appendChild(el("li", "muted small", "No open tasks in this group."));
    q.forEach((t, i) => ol.appendChild(rrTaskRow(t, i, q)));
    sec.append(head, ol);
    gbox.appendChild(sec);
  }
  const sel = $("#rrp-task-group"), keep = sel.value;
  sel.replaceChildren(Object.assign(el("option", "", "Main queue"), { value: "" }));
  for (const g of groups) sel.appendChild(Object.assign(el("option", "", g.name), { value: g.id }));
  sel.value = groups.some((g) => String(g.id) === keep) ? keep : "";
  show(sel, groups.length > 0);
  roFillGroups($("#rro-group").value);  // the routine form's group list follows the groups
  const hist = $("#rrp-history");
  hist.replaceChildren();
  $("#rrp-history-count").textContent = p.history.length ? `(${p.history.length})` : "(none yet)";
  for (const t of [...p.history].reverse()) {
    const row = el("li", "task-row");
    row.appendChild(el("span", "badge-cur", "✓"));
    const body = el("div");
    body.appendChild(el("div", "t", t.title));
    if (t.origin === "standalone") body.appendChild(el("div", "origin", "Done as a standalone task" +
      (t.origin_path ? ` in ${t.origin_path}` : "")));
    if (t.summary) body.appendChild(el("div", "d", t.summary));
    const acts = el("div", "acts");
    const re = el("button", "ghost", "Reopen");
    re.onclick = guarded(async () => { rr.proj = await api("rr_reopen_task", t.id); rrRenderTasks(); rrLoad(); });
    acts.appendChild(re);
    row.append(body, acts);
    hist.appendChild(row);
  }
}

const rrAddTask = guarded(async () => {
  const title = $("#rrp-task-title").value.trim();
  if (!title) { $("#rrp-task-title").focus(); return; }
  const gid = $("#rrp-task-group").value;
  rr.proj = await api("rr_add_task", rr.pid, title, $("#rrp-task-details").value, $("#rrp-task-first").checked,
                      gid ? Number(gid) : null);
  $("#rrp-task-title").value = ""; $("#rrp-task-details").value = ""; $("#rrp-task-first").checked = false;
  rrRenderTasks(); rrLoad();
});

function rrRenderLinks() {
  const p = rr.proj;
  const box = $("#rrp-links");
  box.replaceChildren();
  if (!p.links.length) box.appendChild(el("div", "muted small", "Not linked to any project."));
  for (const l of p.links) {
    const row = el("div", "link-row");
    const t = el("div", "lt");
    t.appendChild(el("b", "", `⇄ ${l.other}`));
    if (l.note) t.appendChild(el("div", "small", l.note));
    const c = [];
    if (l.mine.length) c.push(`here: ${l.mine.join(", ")}`);
    if (l.theirs.length) c.push(`there: ${l.theirs.join(", ")}`);
    if (c.length) t.appendChild(el("div", "muted small", "Shared files — " + c.join(" · ")));
    const rm = el("button", "ghost small-btn", "Remove");
    rm.onclick = guarded(async () => { rr.proj = await api("rr_remove_link", l.id, rr.pid); rrRenderLinks(); });
    row.append(t, rm);
    box.appendChild(row);
  }
  const sel = $("#rrl-other");
  sel.replaceChildren();
  const linked = new Set(p.links.map((l) => l.other_id));
  // All projects, not just the filtered list.
  for (const o of rr.allProjects || []) {
    if (o.id === p.id || linked.has(o.id)) continue;
    const opt = el("option", "", o.name); opt.value = o.id; sel.appendChild(opt);
  }
  const none = !sel.options.length;
  $("#rrl-add").disabled = none;
  if (none) { const opt = el("option", "", "No other projects to link"); opt.value = ""; sel.appendChild(opt); }
}

const rrAddLink = guarded(async () => {
  const other = Number($("#rrl-other").value);
  if (!other) return;
  rr.proj = await api("rr_add_link", rr.pid, other, $("#rrl-note").value.trim(),
                      $("#rrl-mine").value, $("#rrl-theirs").value);
  ["#rrl-note", "#rrl-mine", "#rrl-theirs"].forEach((s) => ($(s).value = ""));
  rrRenderLinks();
});

const rrSave = guarded(async () => {
  const preset = $("#rrp-preset").value;
  rr.proj = await api("rr_update_project", rr.pid, {
    name: $("#rrp-name").value, goal: $("#rrp-goal").value, tags: $("#rrp-tags").value,
    rules: $("#rrp-rules").value, rule_set_ids: rrCheckedIds($("#rrp-rulesets")),
    preset_id: preset ? Number(preset) : null,
  });
  rrRenderProject();
  await rrLoadAll();
});

/* The link picker needs every project, even when the list is filtered. */
const rrLoadAll = guarded(async () => {
  const all = await api("rr_overview", "", null);
  rr.allProjects = all.projects;
  if (!rr.search && !rr.tag) { rr.ov = all; }
  else { rr.ov = await api("rr_overview", rr.search, rr.tag); }
  rrRenderList();
  if (rr.pane === "project" && rr.proj) rrRenderLinks();
});

/* ---- global rules ---- */

function rrRenderGlobal() {
  $("#rrg-rules").value = rr.ov.global.rules;
  rrRuleSetChecks($("#rrg-rulesets"), rr.ov.global.rule_set_ids);
}

const rrSaveGlobal = guarded(async () => {
  rr.ov.global = await api("rr_set_global", $("#rrg-rules").value, rrCheckedIds($("#rrg-rulesets")));
  banner("Global rules saved. They apply to sessions started from now on.", "info");
});

/* ---- rule sets / presets / templates: a list on the left, an editor on the right ---- */

function rrPicker(box, items, kind, onPick) {
  box.replaceChildren();
  if (!items.length) box.appendChild(el("div", "muted small", "None yet."));
  for (const it of items) {
    const b = el("button", "ghost" + (rr.edit[kind] === it.id ? " on" : ""), it.name);
    b.onclick = () => { rr.edit[kind] = it.id; onPick(); };
    box.appendChild(b);
  }
}

function rrRenderRuleSets() {
  rrPicker($("#rrs-list"), rr.ov.rule_sets, "rulesets", rrRenderRuleSets);
  const id = rr.edit.rulesets;
  const it = rr.ov.rule_sets.find((x) => x.id === id);
  show($("#rrs-editor"), id === "new" || !!it);
  $("#rrs-name").value = it ? it.name : "";
  $("#rrs-body").value = it ? it.body : "";
  show($("#rrs-delete"), !!it);
}

const rrSaveRuleSet = guarded(async () => {
  const id = rr.edit.rulesets === "new" ? null : rr.edit.rulesets;
  const name = $("#rrs-name").value.trim();
  rr.ov.rule_sets = await api("rr_save_rule_set", name, $("#rrs-body").value, id);
  const saved = rr.ov.rule_sets.find((x) => (id ? x.id === id : x.name === name));
  rr.edit.rulesets = saved ? saved.id : null;
  rrRenderRuleSets();
});

const rrDeleteRuleSet = guarded(async () => {
  if (!(await confirmWord("Delete this rule set? Projects and presets using it will stop using it.", "yes", "Delete"))) return;
  rr.ov.rule_sets = await api("rr_delete_rule_set", rr.edit.rulesets);
  rr.edit.rulesets = null;
  await rrLoad();
  rrRenderRuleSets();
});

function rrRenderPresets() {
  rrPicker($("#rrps-list"), rr.ov.presets, "presets", rrRenderPresets);
  const id = rr.edit.presets;
  const it = rr.ov.presets.find((x) => x.id === id);
  show($("#rrps-editor"), id === "new" || !!it);
  $("#rrps-name").value = it ? it.name : "";
  $("#rrps-model").value = it && it.model ? it.model : "";
  $("#rrps-effort").value = it && it.effort ? it.effort : "";
  syncEffort($("#rrps-effort"), $("#rrps-model").value);
  $("#rrps-mode").value = it ? it.mode : "default";
  $("#rrps-policy").value = it && it.policy ? it.policy : "";
  const rs = $("#rrps-ruleset");
  rs.replaceChildren(el("option", "", "None")); rs.firstChild.value = "";
  for (const r of rr.ov.rule_sets) { const o = el("option", "", r.name); o.value = r.id; rs.appendChild(o); }
  rs.value = it && it.rule_set_id ? String(it.rule_set_id) : "";
  show($("#rrps-delete"), !!it);
}

const rrSavePreset = guarded(async () => {
  const id = rr.edit.presets === "new" ? null : rr.edit.presets;
  const name = $("#rrps-name").value.trim();
  const rs = $("#rrps-ruleset").value;
  rr.ov.presets = await api("rr_save_preset", {
    name, model: $("#rrps-model").value || null, effort: $("#rrps-effort").value || null,
    mode: $("#rrps-mode").value,
    policy: $("#rrps-policy").value || null, rule_set_id: rs ? Number(rs) : null,
  }, id);
  const saved = rr.ov.presets.find((x) => (id ? x.id === id : x.name === name));
  rr.edit.presets = saved ? saved.id : null;
  rrRenderPresets();
});

const rrDeletePreset = guarded(async () => {
  if (!(await confirmWord("Delete this preset?", "yes", "Delete"))) return;
  rr.ov.presets = await api("rr_delete_preset", rr.edit.presets);
  rr.edit.presets = null;
  rrRenderPresets();
});

function rrRenderTemplates() {
  rrPicker($("#rrt-list"), rr.ov.templates, "templates", rrRenderTemplates);
  const id = rr.edit.templates;
  const it = rr.ov.templates.find((x) => x.id === id);
  show($("#rrt-editor"), id === "new" || !!it);
  $("#rrt-name").value = it ? it.name : "";
  $("#rrt-goal").value = it ? it.goal : "";
  $("#rrt-rules").value = it ? it.rules : "";
  $("#rrt-tags").value = it ? tagsText(it.tags) : "";
  $("#rrt-task").value = it ? it.first_task : "";
  rrRuleSetChecks($("#rrt-rulesets"), it ? it.rule_set_ids : []);
  rrFillPresets($("#rrt-preset"), it ? it.preset_id : null, "None");
  show($("#rrt-delete"), !!it);
  const x = (it && it.extras) || {};
  const parts = [["task group", x.groups], ["routine", x.routines], ["skill", x.skills], ["review agent", x.agents]]
    .filter(([, l]) => l && l.length).map(([n, l]) => `${l.length} ${n}${l.length === 1 ? "" : "s"}`);
  $("#rrt-extras").textContent = parts.length ? `Also sets up: ${parts.join(", ")} (from the project it was made from).`
    : "Made from a project with “Save as template”, a template also copies its task groups, routines, skills and review agents.";
}

const rrSaveTemplate = guarded(async () => {
  const id = rr.edit.templates === "new" ? null : rr.edit.templates;
  const name = $("#rrt-name").value.trim();
  const preset = $("#rrt-preset").value;
  rr.ov.templates = await api("rr_save_template", {
    name, goal: $("#rrt-goal").value, rules: $("#rrt-rules").value, tags: $("#rrt-tags").value,
    first_task: $("#rrt-task").value, rule_set_ids: rrCheckedIds($("#rrt-rulesets")),
    preset_id: preset ? Number(preset) : null,
  }, id);
  const saved = rr.ov.templates.find((x) => (id ? x.id === id : x.name === name));
  rr.edit.templates = saved ? saved.id : null;
  rrRenderTemplates();
});

const rrDeleteTemplate = guarded(async () => {
  if (!(await confirmWord("Delete this template? Projects made from it are not affected.", "yes", "Delete"))) return;
  rr.ov.templates = await api("rr_delete_template", rr.edit.templates);
  rr.edit.templates = null;
  rrRenderTemplates();
});

/* ---- review agents (Debrief criteria), global or for one project ---- */

rr.agentScope = null;   // null = global agents; a project id = that project's
rr.agents = null;       // rr_agents() result
rr.edit.agents = null;  // selected agent id, or "new"

async function rrOpenAgents(pid) {
  if (pid == null && rr.pane === "project" && !(await rrLeave())) return;
  rr.agentScope = pid;
  rr.agents = await api("rr_agents", pid);
  rr.edit.agents = null;
  const keepPid = rr.pid;
  rrShow("agents");
  if (pid != null) rr.pid = keepPid;  // stay "inside" the project
  rrRenderAgents();
}

function rrRenderAgents() {
  const scoped = rr.agentScope != null;
  const name = scoped && rr.proj ? rr.proj.name : "";
  $("#rra-title").textContent = scoped ? `Review agents — ${name}` : "Review agents";
  show($("#rra-back"), scoped);
  $("#rra-intro").textContent = scoped
    ? "Debrief runs these agents over this project's changes and flags what deserves a close look. " +
      "Switch built-ins off for this project, or add agents only this project needs."
    : "Debrief runs these agents over every project's changes and flags what deserves a close look. " +
      "Agents made here apply to all projects; projects can add their own.";
  $("#rra-own-title").textContent = scoped ? "This project's agents" : "Agents for every project";
  const box = $("#rra-builtins");
  box.replaceChildren();
  const scope = rr.agentScope;
  for (const b of rr.agents.builtins) {
    const row = el("div", "builtin-row");
    row.dataset.name = b.name;
    const cb = el("input"); cb.type = "checkbox"; cb.checked = b.enabled; cb.disabled = b.off_globally;
    cb.setAttribute("aria-label", `Use ${b.title}`);
    cb.onchange = guarded(async () => { rr.agents = await api("rr_builtin", b.name, cb.checked, scope); rrRenderAgents(); });
    const t = el("div");
    t.appendChild(el("b", "", b.title));
    if (b.off_globally) t.appendChild(el("span", "muted small", "  — switched off for all projects"));
    row.append(cb, t);
    // Severity, model and effort: empty = inherit (shown in the first option).
    const opts = el("div", "builtin-opts");
    const choices = { severity: ["high", "medium", "low"], model: rr.agents.models, effort: rr.agents.efforts };
    const labels = { severity: "Flags as", model: "Model", effort: "Effort" };
    for (const key of ["severity", "model", "effort"]) {
      const inh = b.inherited[key] || (key === "model" ? rr.agents.default_model : "default");
      const sel = el("select");
      sel.dataset.key = key;
      sel.appendChild(Object.assign(el("option", "", `${scope != null ? "Inherit" : "Default"} (${inh})`), { value: "" }));
      for (const c of choices[key]) sel.appendChild(Object.assign(el("option", "", c), { value: c }));
      if (key === "model") {
        for (const o of sel.options) {
          const m = MODELS.models.find((x) => x.value === o.value);
          if (m) { o.textContent = m.label; if (m.description) o.title = m.description; }
        }
      }
      sel.value = b.own[key] || "";
      sel.classList.toggle("set", !!b.own[key]);
      if (key === "severity") sevClass(sel, b.own.severity || b.inherited.severity);
      sel.onchange = guarded(async () => {
        rr.agents = await api("rr_builtin_option", b.name, key, sel.value || null, scope);
        rrRenderAgents();
      });
      const lab = el("label", "", labels[key]);
      lab.appendChild(sel);
      opts.appendChild(lab);
    }
    row.appendChild(opts);
    const effSel = opts.querySelector("select[data-key='effort']");
    syncEffort(effSel, b.own.model || b.inherited.model || rr.agents.default_model, b.inherited.effort || "");
    const d = el("details");
    d.append(el("summary", "", "What it looks for"), el("pre", "", b.prompt));
    row.appendChild(d);
    box.appendChild(row);
  }
  const note = $("#rra-global-note");
  const globals = rr.agents.global_agents || [];
  show(note, scoped && globals.length > 0);
  if (scoped) note.textContent = "Also used here (from Review agents for every project): " +
    globals.map((g) => g.title + (g.enabled ? "" : " (off)")).join(", ");
  rrPicker($("#rra-list"), rr.agents.agents.map((a) => ({ ...a, name: a.title + (a.enabled ? "" : " (off)") })),
           "agents", rrRenderAgents);
  const id = rr.edit.agents;
  const it = rr.agents.agents.find((x) => x.id === id);
  show($("#rra-editor"), id === "new" || !!it);
  $("#rra-title-in").value = it ? it.title : "";
  $("#rra-prompt").value = it ? it.prompt : "";
  $("#rra-severity").value = it ? it.severity : "medium";
  const fill = (sel, values, none, chosen) => {
    sel.replaceChildren(Object.assign(el("option", "", none), { value: "" }));
    for (const v of values) sel.appendChild(Object.assign(el("option", "", v), { value: v }));
    sel.value = chosen || "";
  };
  fillModelSelect($("#rra-model"), `Default (${rr.agents.default_model})`);
  $("#rra-model").value = (it && it.model) || "";
  fill($("#rra-effort"), rr.agents.efforts, "Default", it && it.effort);
  for (const o of $("#rra-effort").options) if (o.value) o.textContent = EFFORT_LABEL[o.value] || o.value;
  sevClass($("#rra-severity"), $("#rra-severity").value);
  syncEffort($("#rra-effort"), $("#rra-model").value || rr.agents.default_model);
  $("#rra-notes").checked = it ? it.uses_notes : true;
  $("#rra-rationale").checked = it ? it.sees_rationale : false;
  $("#rra-explore").checked = it ? it.explore : false;
  $("#rra-enabled").checked = it ? it.enabled : true;
  show($("#rra-delete"), !!it);
}

const rrSaveAgent = guarded(async () => {
  const id = rr.edit.agents === "new" ? null : rr.edit.agents;
  const title = $("#rra-title-in").value.trim();
  rr.agents = await api("rr_save_agent", {
    title, prompt: $("#rra-prompt").value, severity: $("#rra-severity").value,
    model: $("#rra-model").value || null, effort: $("#rra-effort").value || null,
    uses_notes: $("#rra-notes").checked, sees_rationale: $("#rra-rationale").checked,
    explore: $("#rra-explore").checked, enabled: $("#rra-enabled").checked,
  }, id, rr.agentScope);
  const saved = rr.agents.agents.find((x) => (id ? x.id === id : x.title === title));
  rr.edit.agents = saved ? saved.id : null;
  rrRenderAgents();
  banner(`Saved “${title}”. Debrief uses it from the next review.`, "info");
});

const rrDeleteAgent = guarded(async () => {
  if (!(await confirmWord("Delete this review agent?", "yes", "Delete"))) return;
  rr.agents = await api("rr_delete_agent", rr.edit.agents, rr.agentScope);
  rr.edit.agents = null;
  rrRenderAgents();
});

async function rrAgentsSummary() {
  try {
    const v = await api("rr_agents", rr.pid);
    const on = v.builtins.filter((b) => b.enabled).length;
    const own = v.agents.filter((a) => a.enabled).length;
    const glob = (v.global_agents || []).filter((a) => a.enabled).length;
    $("#rrp-agents-summary").textContent =
      `${on} of ${v.builtins.length} built-in agents on` + (glob ? ` · ${glob} for every project` : "") +
      ` · ${own} for this project only.`;
  } catch (_) { /* shown when opened */ }
}

/* ---- ✨ Draft with Claude ---- */

const DRAFT_TITLES = { project_rules: "Draft project rules", global_rules: "Draft global rules",
                       rule_set: "Draft a rule set", agent: "Draft a review agent", task: "Draft a task",
                       routine: "Draft a routine", project: "Draft a new project" };
const DRAFT_HINTS = { task: "e.g. the save loader crashes on old files — make it handle them and add a test",
                      routine: "e.g. after every task, update README and the docs folder to match what changed",
                      project: "e.g. A Python tool that converts my game's save files to JSON and back. It must never "
                               + "lose data from old saves. First, get loading the old format working with tests." };

/* The new-project form as text, for "revise the current text". */
function rrnFormText() {
  const parts = [["Name", "#rrn-name"], ["Goal", "#rrn-goal"], ["Starting task", "#rrn-task"],
                 ["Starting task details", "#rrn-task-details"], ["Tags", "#rrn-tags"], ["Project rules", "#rrn-rules"]];
  return parts.filter(([, id]) => $(id).value.trim()).map(([k, id]) => `${k}: ${$(id).value.trim()}`).join("\n");
}
let draftCtx = null;

function openDraft(kind, target) {
  const field = $(target);
  draftCtx = { kind, field };
  $("#draft-go").textContent = "Write draft";
  $("#draft-title").textContent = DRAFT_TITLES[kind] || "Draft with Claude";
  $("#draft-desc").value = "";
  const project = kind === "project";
  const hasText = project ? !!rrnFormText() : !!field.value.trim();
  show($("#draft-project-row"), project);
  show($("#draft-project-note"), project && !!rr.newFolder);
  $("#draft-text-label").textContent = project ? "Project rules" : "Draft";
  $("#draft-text").rows = project ? 5 : 12;
  show($("#draft-revise-row"), hasText);
  $("#draft-revise").checked = hasText;
  show($("#draft-result"), false);
  $("#draft-text").value = "";
  $("#draft-use").disabled = true;
  const agent = kind === "agent", named = ["agent", "task", "routine"].includes(kind);
  show($("#draft-agent-title-row"), named); show($("#draft-agent-sev-row"), agent);
  show($("#draft-routine-row"), kind === "routine");
  $("#draft-name-label").textContent = kind === "task" ? "Title" : "Name";
  $("#draft-desc").placeholder = DRAFT_HINTS[kind] ||
    "e.g. Don't add new libraries without asking. Keep the old save format loading.";
  openModal("#modal-draft");
  $("#draft-desc").focus();
}

const runDraft = guarded(async () => {
  const desc = $("#draft-desc").value.trim();
  if (!desc) { $("#draft-desc").focus(); return; }
  const go = $("#draft-go");
  go.disabled = true; show($("#draft-busy"));
  try {
    const project = draftCtx.kind === "project";
    const current = $("#draft-revise").checked ? (project ? rrnFormText() : draftCtx.field.value) : "";
    const pid = ["project_rules", "task", "routine"].includes(draftCtx.kind) ? rr.pid
              : draftCtx.kind === "agent" ? rr.agentScope : null;
    const r = await api("rr_draft", draftCtx.kind, desc, current, pid, project ? (rr.newFolder || "") : "");
    if (project) {
      $("#draft-p-name").value = r.name;
      $("#draft-p-goal").value = r.goal;
      $("#draft-p-task").value = r.first_task;
      $("#draft-p-details").value = r.first_task_details;
      $("#draft-p-tags").value = (r.tags || []).join(", ");
      $("#draft-text").value = r.rules;
    } else if (draftCtx.kind === "task") {
      $("#draft-agent-title").value = r.title;
      $("#draft-text").value = r.details;
    } else if (draftCtx.kind === "routine") {
      $("#draft-agent-title").value = r.name || $("#rro-name").value;
      $("#draft-text").value = r.instructions;
      $("#draft-trigger").value = r.trigger;
      $("#draft-gate").value = r.gate;
    } else if (draftCtx.kind === "agent") {
      $("#draft-text").value = r.prompt;
      $("#draft-agent-title").value = r.title || $("#rra-title-in").value;
      $("#draft-agent-sev").value = r.severity || "medium";
    } else {
      $("#draft-text").value = r.text;
    }
    show($("#draft-result"));
    $("#draft-use").disabled = false;
    go.textContent = "Write again";
  } finally {
    go.disabled = false; show($("#draft-busy"), false);
  }
});

function useDraft() {
  const { kind, field } = draftCtx;
  if (kind === "project") {
    $("#rrn-name").value = $("#draft-p-name").value.trim();
    $("#rrn-goal").value = $("#draft-p-goal").value.trim();
    $("#rrn-task").value = $("#draft-p-task").value.trim();
    $("#rrn-task-details").value = $("#draft-p-details").value.trim();
    $("#rrn-tags").value = $("#draft-p-tags").value.trim();
    $("#rrn-rules").value = $("#draft-text").value.trim();
    closeModal();
    $("#rrn-name").focus();
    banner(rr.newFolder ? "Draft added. Review it, then press Create project."
           : "Draft added. Choose the project's folder, review the form, then press Create project.", "info");
    return;
  }
  field.value = $("#draft-text").value;
  if (kind === "agent") {
    if ($("#draft-agent-title").value.trim()) $("#rra-title-in").value = $("#draft-agent-title").value.trim();
    $("#rra-severity").value = $("#draft-agent-sev").value;
  }
  if (kind === "task") $("#rrp-task-title").value = $("#draft-agent-title").value.trim();
  if (kind === "routine") {
    if (!ro.editing) roEdit(null);
    $("#rro-name").value = $("#draft-agent-title").value.trim();
    $("#rro-text").value = $("#draft-text").value;  // roEdit cleared it
    $("#rro-trigger").value = $("#draft-trigger").value;
    $("#rro-gate").value = $("#draft-gate").value;
    $("#rrp-routine-form").open = true;
  }
  if (kind !== "task" && kind !== "routine") field.dispatchEvent(new Event("input"));  // unsaved project changes
  closeModal();
  field.focus();
  banner(kind === "task" ? "Draft added. Review it, then press Add task."
         : kind === "routine" ? "Draft added. Review it, then press Save routine."
         : "Draft added. Review it, then press Save.", "info");
}

function rrBindAgentsAndDrafts() {
  $("#rrp-agents-btn").onclick = guarded(async () => {
    if (rr.dirty && !(await rrLeave())) return;
    await rrOpenAgents(rr.pid);
  });
  $("#rra-back").onclick = guarded(async () => { await rrOpenProject(rr.agentScope, true); });
  $("#rra-new").onclick = () => { rr.edit.agents = "new"; rrRenderAgents(); $("#rra-title-in").focus(); };
  $("#rra-save").onclick = rrSaveAgent;
  $("#rra-severity").addEventListener("change", (e) => sevClass(e.target, e.target.value));
  $("#rra-delete").onclick = rrDeleteAgent;
  document.querySelectorAll(".draft-btn").forEach((b) => (b.onclick = () => openDraft(b.dataset.draft, b.dataset.target)));
  $("#draft-go").onclick = runDraft;
  $("#draft-desc").onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) runDraft(); };
  $("#draft-use").onclick = useDraft;
  $("#draft-cancel").onclick = closeModal;
}

/* ---- binding ---- */

/* ---------------- Shared cards: Claude's questions and proposed rules ---------------- */

/* Questions as clickable options, like Claude Code's AskUserQuestion. Every question
   also takes a typed answer. onSubmit gets {question: answer}; answers to
   multi-select questions are joined with ", ". */
function questionCard(questions, { live = true, onSubmit, onSkip, answered = null } = {}) {
  const card = el("div", "qcard");
  const picks = questions.map(() => new Set());
  const other = questions.map(() => "");
  const submit = el("button", "primary", "Send answers");
  const ready = () => questions.every((_, i) => picks[i].size || other[i].trim());
  const sync = () => { submit.disabled = !ready(); };
  questions.forEach((q, i) => {
    const box = el("div", "q");
    const head = el("div", "qhead");
    if (q.header) head.appendChild(el("span", "chip", q.header));
    head.appendChild(el("span", "qtext", q.question));
    if (q.multiSelect) head.appendChild(el("span", "muted small", "pick any"));
    box.appendChild(head);
    const opts = el("div", "qopts");
    q.options.forEach((o) => {
      const b = el("button", "qopt");
      b.type = "button";
      b.appendChild(el("span", "qlabel", o.label));
      if (o.description) b.appendChild(el("span", "qdesc", o.description));
      b.disabled = !live;
      b.setAttribute("aria-pressed", "false");
      b.onclick = () => {
        if (q.multiSelect) { picks[i].has(o.label) ? picks[i].delete(o.label) : picks[i].add(o.label); }
        else { picks[i].clear(); picks[i].add(o.label); other[i] = ""; otherIn.value = ""; }
        opts.querySelectorAll(".qopt").forEach((x) =>
          x.setAttribute("aria-pressed", String(picks[i].has(x.querySelector(".qlabel").textContent))));
        sync();
      };
      if (answered && String(answered[q.question] || "").split(", ").includes(o.label)) b.setAttribute("aria-pressed", "true");
      opts.appendChild(b);
    });
    const otherIn = Object.assign(el("input", "qother"), { type: "text", placeholder: "Or type your own answer…",
                                                         disabled: !live });
    otherIn.oninput = () => {
      other[i] = otherIn.value;
      if (otherIn.value.trim() && !q.multiSelect) {
        picks[i].clear();
        opts.querySelectorAll(".qopt").forEach((x) => x.setAttribute("aria-pressed", "false"));
      }
      sync();
    };
    otherIn.onkeydown = (e) => { if (e.key === "Enter" && ready()) submit.click(); };
    box.append(opts, otherIn);
    card.appendChild(box);
  });
  const actions = el("div", "pactions");
  if (live) {
    submit.disabled = true;
    submit.onclick = () => {
      const answers = {};
      questions.forEach((q, i) => {
        const parts = [...picks[i]];
        if (other[i].trim()) parts.push(other[i].trim());
        answers[q.question] = parts.join(", ");
      });
      card.querySelectorAll("button, input").forEach((x) => (x.disabled = true));
      onSubmit(answers);
    };
    actions.appendChild(submit);
    if (onSkip) {
      const skip = el("button", "ghost", "Skip");
      skip.onclick = () => { card.querySelectorAll("button, input").forEach((x) => (x.disabled = true)); onSkip(); };
      actions.appendChild(skip);
    }
  } else {
    card.classList.add("done");
  }
  card.appendChild(actions);
  return card;
}

function questionsDone(card, text) {
  card.classList.add("done");
  card.querySelectorAll("button, input").forEach((x) => (x.disabled = true));
  card.querySelector(".pactions").replaceChildren(el("span", "muted small", text));
}

/* A rule Claude proposed, with the same fields as the rules editors. targets:
   [{value, label}]; a value ending in ":new" asks for a name. onApply gets
   {text, target, name}. */
function ruleCard(p, { targets, initial, live = true, status = "", onApply, onDismiss }) {
  const card = el("div", "rcard");
  card.dataset.rule = p.id ?? "";
  card.appendChild(el("div", "ptitle", "✎ Claude suggests a rule"));
  if (p.why) card.appendChild(el("div", "muted small", p.why));
  const where = el("select");
  for (const t of targets) where.appendChild(Object.assign(el("option", "", t.label), { value: t.value }));
  if (initial && targets.some((t) => t.value === initial)) where.value = initial;
  const name = Object.assign(el("input"), { type: "text", placeholder: "New rule set name", value: p.rule_set || "",
                                            spellcheck: false });
  const syncName = () => show(name, where.value.endsWith(":new"));
  where.onchange = syncName;
  const text = Object.assign(el("textarea"), { rows: Math.min(8, Math.max(2, (p.text || "").split("\n").length + 1)),
                                               value: p.text || "" });
  const row = el("label", "small-label", "Add to");
  card.append(row, where, name, text);
  syncName();
  const actions = el("div", "pactions");
  if (live && !status) {
    const apply = el("button", "primary", "Add rule");
    apply.onclick = () => {
      if (!text.value.trim()) { text.focus(); return; }
      if (where.value.endsWith(":new") && !name.value.trim()) { name.focus(); return; }
      onApply({ text: text.value.trim(), target: where.value, name: name.value.trim() });
    };
    const dismiss = el("button", "ghost", "Dismiss");
    dismiss.onclick = onDismiss;
    actions.append(apply, dismiss);
  } else {
    ruleDone(card, status === "dismissed" ? "Dismissed." : status === "applied" ? "Added." : status || "No longer open.");
  }
  card.appendChild(actions);
  return card;
}

function ruleDone(card, text) {
  card.classList.add("done");
  card.querySelectorAll("button, input, select, textarea").forEach((x) => (x.disabled = true));
  const actions = card.querySelector(".pactions");
  if (actions) actions.replaceChildren(el("span", "muted small", text));
}

/* ---------------- Import existing work (readyroom/importer.py) ---------------- */

const im = { folder: "", export: "", found: null, id: null, view: null, plan: null, approved: new Set() };

const imOpen = guarded(async () => {
  if (im.id) return; // a conversation is open: keep it
  const recent = await api("im_recent");
  const box = $("#im-recent");
  box.replaceChildren();
  if (recent.length) {
    box.appendChild(el("span", "muted", "Unfinished imports:"));
    for (const r of recent) {
      const b = el("button", "ghost small-btn", `${r.root.split(/[\\/]/).filter(Boolean).pop()} (${r.turns} messages)`);
      b.title = r.root;
      b.onclick = guarded(async () => imShowTalk(await api("im_get", r.id)));
      box.appendChild(b);
    }
  }
  show(box, recent.length > 0);
  fillModelSelect($("#im-model"), "Default (Sonnet)");
  if (!$("#im-model").value && MODELS.models.some((m) => m.value === "sonnet")) $("#im-model").value = "sonnet";
});

function imReset() {
  Object.assign(im, { found: null, id: null, view: null, plan: null, approved: new Set() });
  show($("#im-setup")); show($("#im-talk"), false); show($("#im-found"), false);
  $("#im-log").replaceChildren();
}

const imScan = guarded(async () => {
  const btn = $("#im-scan");
  btn.disabled = true; show($("#im-scan-busy"));
  try {
    im.found = await api("im_scan", im.folder, im.export);
  } finally {
    btn.disabled = !im.folder; show($("#im-scan-busy"), false);
  }
  const f = im.found;
  const list = $("#im-found-list");
  list.replaceChildren();
  const add = (text) => list.appendChild(el("li", "", text));
  const g = f.git;
  add(!g.is_repo ? "Not tracked by git (Debrief will offer to start tracking each project)."
      : g.sub ? `Part of the git repository ${g.repo} (${g.commits} recent commit${g.commits === 1 ? "" : "s"} here).`
      : !g.has_commits ? "A git repository with no commits yet."
      : `A git repository with ${g.commits} recent commit${g.commits === 1 ? "" : "s"}${g.dirty ? " and uncommitted changes" : ""}.`);
  add(f.units.length ? `Folders that look like separate pieces of work: ${f.units.join(", ")}`
      : `${f.folders} folders; none stands out as a separate piece of work.`);
  add(f.claude_md.length ? `CLAUDE.md: ${f.claude_md.join(", ")}` : "No CLAUDE.md files.");
  if (f.settings.length) add(`Claude Code settings: ${f.settings.join(", ")}`);
  add(f.sessions ? `${f.sessions} past Claude Code sessions here.` : "No past Claude Code sessions found for this folder.");
  if (f.projects_inside.length) add(`Already Carrier projects here: ${f.projects_inside.join(", ")} — the plan will leave their folders alone.`);
  const chats = $("#im-chat-list");
  chats.replaceChildren();
  for (const c of f.chats) {
    const lab = el("label");
    const cb = Object.assign(el("input"), { type: "checkbox", value: c.id, checked: f.chats_selected.includes(c.id) });
    lab.append(cb, el("span", "", c.name), el("span", "muted small", `${c.date} · ${c.messages} messages`));
    chats.appendChild(lab);
  }
  show($("#im-chats"), f.chats.length > 0);
  show($("#im-found"));
});

const imStart = guarded(async () => {
  const mode = document.querySelector("input[name=im-mode]:checked").value;
  const chatIds = [...document.querySelectorAll("#im-chat-list input:checked")].map((c) => c.value);
  $("#im-start").disabled = true; show($("#im-start-busy"));
  try {
    imShowTalk(await api("im_start", im.folder, mode, im.export, im.found.chats.length ? chatIds : null,
                         $("#im-model").value || "sonnet"));
  } finally {
    $("#im-start").disabled = false; show($("#im-start-busy"), false);
  }
});

function imShowTalk(view) {
  im.id = view.id; im.view = view;
  show($("#im-setup"), false); show($("#im-recent"), false); show($("#im-talk"));
  $("#im-talk-root").textContent = `${view.root} · ${{ ask: "deciding with Claude", in_place: "in place",
    reorganize: "may reorganise" }[view.mode]}`;
  if (view.plan) {
    im.plan = JSON.parse(JSON.stringify(view.plan));
    im.approved = new Set((im.plan.moves || []).map((_, i) => i));
    imRenderPlan();
    if (im.history === undefined || im.history.id !== view.id) {
      api("im_history", view.id).then((h) => {
        im.history = { ...h, id: view.id };
        show($("#im-history"), !h.has_history);
        $("#im-ignore").value = (h.suggest || []).join("\n");
      }).catch(() => {});
    }
  }
  const log = $("#im-log");
  log.replaceChildren();
  view.messages.forEach((m, mi) => {
    const box = el("div", `im-msg ${m.role}`);
    box.appendChild(el("div", "who", m.role === "user" ? "You" : "Claude"));
    const body = el("div", "md");
    body.innerHTML = md(m.text); // md() escapes everything first
    box.appendChild(body);
    const last = mi === view.messages.length - 1;
    if (m.questions && m.questions.length) {
      box.appendChild(questionCard(m.questions, {
        live: last,
        onSubmit: (answers) => imSendText(Object.entries(answers).map(([q, a]) => `**${q}** ${a}`).join("\n\n")),
      }));
      if (!last) questionsDone(box.lastElementChild, "Answered below.");
    }
    (m.rules || []).forEach((r, ri) => box.appendChild(imRuleCard(r, mi, ri)));
    log.appendChild(box);
  });
  show($("#im-plan"), !!view.plan);
  log.lastElementChild?.scrollIntoView({ block: "nearest" });
}

const imSendText = guarded(async (text) => {
  if (!text.trim()) return;
  $("#im-send").disabled = true; show($("#im-send-busy"));
  try {
    if (im.plan) await api("im_check", im.id, imPlanToSend(false)); // Claude builds on the user's edits
    imShowTalk(await api("im_send", im.id, text));
    return true;
  } finally {
    $("#im-send").disabled = false; show($("#im-send-busy"), false);
  }
});

const imSend = guarded(async () => {
  if (await imSendText($("#im-text").value.trim())) $("#im-text").value = "";
});

/* A rule Claude proposed while importing: it goes into the plan (the projects don't exist yet). */
function imRuleCard(r, mi, ri) {
  const plan = im.plan;
  const targets = [];
  let initial = "";
  if (plan) {
    (plan.projects || []).forEach((p, i) => {
      targets.push({ value: `p:${i}`, label: `Project rules: ${p.name || `project ${i + 1}`}` });
      if (!initial && r.project && (p.name || "").toLowerCase() === r.project.toLowerCase()) initial = `p:${i}`;
    });
    (plan.rule_sets || []).forEach((rs, i) => {
      targets.push({ value: `s:${i}`, label: `Rule set: ${rs.name}` });
      if (r.rule_set && rs.name.toLowerCase() === r.rule_set.toLowerCase()) initial = `s:${i}`;
    });
    targets.push({ value: "s:new", label: "A new rule set…" });
    if (!initial) initial = r.rule_set ? "s:new" : (plan.projects || []).length ? "p:0" : "s:new";
  } else {
    targets.push({ value: "ask", label: "Ask Claude to put it in the plan" });
    initial = "ask";
  }
  const card = ruleCard(r, {
    targets, initial, status: r.status === "proposed" ? "" : r.status,
    onApply: ({ text, target, name }) => guarded(async () => {
      if (target === "ask") {
        await api("im_rule_done", im.id, mi, ri, "applied");
        await imSendText(`Please add this rule to the plan:\n\n${text}`);
        return;
      }
      const [kind, idx] = target.split(":");
      const add = (old) => ((old || "").trim() ? `${old.trim()}\n${text}` : text);
      if (kind === "p") plan.projects[Number(idx)].rules = add(plan.projects[Number(idx)].rules);
      else if (idx === "new") {
        plan.rule_sets.push({ name, rules: text });
        if (r.project) {
          const proj = plan.projects.find((p) => (p.name || "").toLowerCase() === r.project.toLowerCase());
          if (proj) (proj.rule_sets ||= []).push(name);
        }
      } else plan.rule_sets[Number(idx)].rules = add(plan.rule_sets[Number(idx)].rules);
      await api("im_check", im.id, imPlanToSend(false));
      await api("im_rule_done", im.id, mi, ri, "applied");
      imRenderPlan();
      ruleDone(card, "Added to the plan.");
    })(),
    onDismiss: () => guarded(async () => {
      await api("im_rule_done", im.id, mi, ri, "dismissed");
      ruleDone(card, "Dismissed.");
    })(),
  });
  return card;
}

/* The plan as edited on the page; unticked moves are left out when applying. */
function imPlanToSend(onlyApproved) {
  const p = JSON.parse(JSON.stringify(im.plan));
  if (onlyApproved) p.moves = (p.moves || []).filter((_, i) => im.approved.has(i));
  return p;
}

function imInput(value, onInput, placeholder = "") {
  const i = Object.assign(el("input"), { type: "text", value: value ?? "", placeholder, spellcheck: false });
  i.oninput = () => onInput(i.value);
  return i;
}

function imArea(value, onInput, rows = 2, placeholder = "") {
  const t = Object.assign(el("textarea"), { rows, value: value ?? "", placeholder });
  t.oninput = () => onInput(t.value);
  return t;
}

function imLabeled(label, node) {
  const wrap = el("div");
  wrap.append(el("label", "small-label", label), node);
  return wrap;
}

function imRenderPlan() {
  const plan = im.plan;
  plan.moves ||= []; plan.rule_sets ||= []; plan.projects ||= [];
  $("#im-summary").textContent = plan.summary || "";
  // moves
  const moves = $("#im-move-list");
  moves.replaceChildren();
  plan.moves.forEach((m, i) => {
    const row = el("label", "im-move");
    const cb = Object.assign(el("input"), { type: "checkbox", checked: im.approved.has(i) });
    cb.onchange = () => (cb.checked ? im.approved.add(i) : im.approved.delete(i));
    row.append(cb, el("code", "", m.from), el("span", "", "→"), el("code", "", m.to));
    if (m.why) row.appendChild(el("span", "why", m.why));
    moves.appendChild(row);
  });
  show($("#im-moves"), plan.moves.length > 0);
  // rule sets
  const sets = $("#im-set-list");
  sets.replaceChildren();
  plan.rule_sets.forEach((rs) => {
    const card = el("div", "im-card");
    card.append(imInput(rs.name, (v) => (rs.name = v), "Rule set name"),
                imArea(rs.rules, (v) => (rs.rules = v), 3, "- one rule per line"));
    sets.appendChild(card);
  });
  show($("#im-sets"), plan.rule_sets.length > 0);
  // projects
  const box = $("#im-projects");
  box.replaceChildren();
  plan.projects.forEach((p, pi) => {
    p.folders ||= []; p.tasks ||= []; p.rule_sets ||= []; p.tags ||= [];
    p.folders = p.folders.map((f) => (typeof f === "string" ? { path: f, kind: "work", label: "" } : f));
    p.tasks = p.tasks.map((t) => (typeof t === "string" ? { title: t, details: "" } : t));
    const card = el("div", "im-card");
    const head = el("div", "head");
    const del = el("button", "ghost small-btn", "Remove project");
    del.onclick = () => { plan.projects.splice(pi, 1); imRenderPlan(); };
    head.append(imInput(p.name, (v) => (p.name = v), "Project name"), del);
    card.append(head,
      imLabeled("Main folder (relative to the workspace)", imInput(p.main, (v) => (p.main = v), "e.g. mods/MyMod")),
      imLabeled("Goal", imArea(p.goal, (v) => (p.goal = v), 3)),
      imLabeled("Rules (added to your global rules)", imArea(p.rules, (v) => (p.rules = v), 3, "- one rule per line")),
      imLabeled("Rule sets (comma-separated names)", imInput(p.rule_sets.join(", "),
        (v) => (p.rule_sets = v.split(",").map((x) => x.trim()).filter(Boolean)))),
      imLabeled("Tags (comma-separated, just for you)", imInput(p.tags.join(", "),
        (v) => (p.tags = v.split(",").map((x) => x.trim()).filter(Boolean)))));
    // folders
    const folders = el("div");
    folders.appendChild(el("label", "small-label", "Extra folders"));
    p.folders.forEach((f, fi) => {
      const row = el("div", "im-row");
      const kind = el("select");
      for (const k of ["work", "reference", "workspace"]) kind.appendChild(Object.assign(el("option", "", k), { value: k }));
      kind.value = ["reference", "workspace"].includes(f.kind) ? f.kind : "work";
      kind.onchange = () => (f.kind = kind.value);
      const rm = el("button", "ghost small-btn", "✕");
      rm.title = "Remove this folder";
      rm.onclick = () => { p.folders.splice(fi, 1); imRenderPlan(); };
      row.append(imInput(f.path, (v) => (f.path = v), "path"), kind, imInput(f.label, (v) => (f.label = v), "label"), rm);
      folders.appendChild(row);
    });
    const addF = el("button", "ghost small-btn", "＋ Folder");
    addF.onclick = () => { p.folders.push({ path: "", kind: "work", label: "" }); imRenderPlan(); };
    folders.appendChild(addF);
    // tasks
    const tasks = el("div");
    tasks.appendChild(el("label", "small-label", "Tasks, in order"));
    p.tasks.forEach((t, ti) => {
      const row = el("div", "im-row task");
      const rm = el("button", "ghost small-btn", "✕");
      rm.title = "Remove this task";
      rm.onclick = () => { p.tasks.splice(ti, 1); imRenderPlan(); };
      row.append(imInput(t.title, (v) => (t.title = v), "title"), imInput(t.details, (v) => (t.details = v), "details"), rm);
      tasks.appendChild(row);
    });
    const addT = el("button", "ghost small-btn", "＋ Task");
    addT.onclick = () => { p.tasks.push({ title: "", details: "" }); imRenderPlan(); };
    tasks.appendChild(addT);
    card.append(folders, tasks);
    box.appendChild(card);
  });
}

const imCheck = guarded(async () => {
  const r = await api("im_check", im.id, imPlanToSend(true));
  const ul = $("#im-problems");
  ul.replaceChildren(...r.problems.map((t) => el("li", "", t)));
  show(ul, r.problems.length > 0);
  if (!r.problems.length) banner("The plan looks good.", "info");
  return r.problems.length === 0;
});

const imApply = guarded(async () => {
  if (!(await imCheck())) return;
  const plan = imPlanToSend(true);
  const n = plan.projects.length;
  const what = `Create ${n} project${n === 1 ? "" : "s"}` +
    (plan.moves.length ? ` and move ${plan.moves.length} folder${plan.moves.length === 1 ? "" : "s"} on disk` : "") + "?";
  if (plan.moves.length ? !(await confirmWord(what, "move", "Move and create"))
                        : !(await confirmWord(what, "create", "Create projects"))) return;
  $("#im-apply").disabled = true;
  try {
    const history = im.history && !im.history.has_history && $("#im-first-commit").checked;
    const out = await api("im_apply", im.id, plan, $("#im-baseline").checked, !!history,
                          history ? $("#im-ignore").value.split("\n") : null);
    for (const w of out.warnings) banner(w);
    banner(`Created ${out.projects.length} project${out.projects.length === 1 ? "" : "s"}.`, "info");
    imReset();
    await rrLoad();
    if (out.projects.length) await rrOpenProject(out.projects[0], true);
  } finally {
    $("#im-apply").disabled = false;
  }
});

function imBind() {
  $("#im-folder-btn").onclick = guarded(async () => {
    const f = await api("pick_folder");
    if (!f) return;
    im.folder = f;
    $("#im-folder").textContent = f;
    $("#im-scan").disabled = false;
    show($("#im-found"), false);
  });
  $("#im-export-btn").onclick = guarded(async () => {
    const f = await api("im_pick_export");
    if (!f) return;
    im.export = f;
    $("#im-export").textContent = f;
    show($("#im-export-clear"));
    show($("#im-found"), false);
  });
  $("#im-export-clear").onclick = () => {
    im.export = "";
    $("#im-export").textContent = "None";
    show($("#im-export-clear"), false);
    show($("#im-found"), false);
  };
  $("#im-scan").onclick = imScan;
  $("#im-start").onclick = imStart;
  $("#im-send").onclick = imSend;
  $("#im-text").onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) imSend(); };
  $("#im-check").onclick = imCheck;
  $("#im-apply").onclick = imApply;
  $("#im-add-project").onclick = () => {
    im.plan.projects.push({ name: "", main: "", goal: "", rules: "", rule_sets: [], tags: [], folders: [], tasks: [] });
    imRenderPlan();
  };
  $("#im-discard").onclick = guarded(async () => {
    if (!(await confirmWord("Discard this import? The conversation is deleted; nothing on disk changes.", "discard",
                            "Discard"))) return;
    await api("im_discard", im.id);
    imReset();
    imOpen();
  });
}

/* ---------------- Skills (core/skills.py) ---------------- */

const sk = { pid: null, scope: "project", list: { project: [], personal: [] }, editing: null };

const skOpen = guarded(async (pid) => {
  if (pid !== undefined) sk.pid = pid;
  const ov = rr.ov || (await api("rr_overview", "", null));
  const sel = $("#sk-scope");
  sel.replaceChildren();
  for (const p of ov.projects) sel.appendChild(Object.assign(el("option", "", p.name), { value: `p:${p.id}` }));
  sel.appendChild(Object.assign(el("option", "", "Personal (every project)"), { value: "personal" }));
  if (sk.pid == null && ov.projects.length) sk.pid = ov.projects[0].id;
  sel.value = sk.scope === "personal" || sk.pid == null ? "personal" : `p:${sk.pid}`;
  sk.scope = sel.value === "personal" ? "personal" : "project";
  await skLoad();
});

async function skLoad() {
  sk.list = await api("rr_skills", sk.scope === "project" ? sk.pid : null);
  const items = sk.list[sk.scope] || [];
  const box = $("#sk-list");
  box.replaceChildren();
  if (!items.length) box.appendChild(el("p", "muted", "No skills here yet."));
  for (const it of items) {
    const row = el("button", "sk-item");
    row.append(el("b", "", it.name), el("span", "muted small", it.description || "(no description)"));
    row.title = it.path;
    row.onclick = () => skEdit(it);
    box.appendChild(row);
  }
  show($("#sk-edit"), false);
}

function skEdit(it) {
  sk.editing = it;
  $("#sk-edit-title").textContent = it ? `Edit ${it.name}` : "New skill";
  $("#sk-name").value = it ? it.name : "";
  $("#sk-desc").value = it ? it.description : "";
  $("#sk-body").value = it ? it.body : "";
  $("#sk-describe").value = "";
  show($("#sk-delete"), !!it);
  show($("#sk-edit"));
  $("#sk-edit").scrollIntoView({ block: "nearest" });
}

function skBind() {
  $("#sk-scope").onchange = guarded(async (e) => {
    const v = e.target.value;
    sk.scope = v === "personal" ? "personal" : "project";
    if (v.startsWith("p:")) sk.pid = Number(v.slice(2));
    await skLoad();
  });
  $("#sk-new").onclick = () => skEdit(null);
  $("#sk-cancel").onclick = () => show($("#sk-edit"), false);
  $("#sk-draft").onclick = guarded(async () => {
    const desc = $("#sk-describe").value.trim();
    if (!desc) { $("#sk-describe").focus(); return; }
    $("#sk-draft").disabled = true; show($("#sk-draft-busy"));
    try {
      const r = await api("rr_draft", "skill", desc, $("#sk-body").value, sk.scope === "project" ? sk.pid : null);
      if (r.name && !$("#sk-name").value.trim()) $("#sk-name").value = r.name;
      if (r.description) $("#sk-desc").value = r.description;
      $("#sk-body").value = r.body;
    } finally { $("#sk-draft").disabled = false; show($("#sk-draft-busy"), false); }
  });
  $("#sk-save").onclick = guarded(async () => {
    sk.list = await api("rr_save_skill", sk.scope === "project" ? sk.pid : null, $("#sk-name").value,
                        $("#sk-desc").value, $("#sk-body").value, sk.scope, sk.editing ? sk.editing.folder : "");
    banner("Skill saved.", "info");
    await skLoad();
  });
  $("#sk-delete").onclick = guarded(async () => {
    if (!sk.editing || !(await confirmWord(`Delete the skill ${sk.editing.name}?`, "delete", "Delete"))) return;
    await api("rr_delete_skill", sk.scope === "project" ? sk.pid : null, sk.editing.folder, sk.scope);
    await skLoad();
  });
}

function rrBind() {
  let searchTimer = null;
  $("#rr-search").oninput = (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { rr.search = e.target.value; rrLoad(); }, 200);
  };
  $("#rr-new-btn").onclick = rrNewProject;
  $("#rr-intro-new").onclick = rrNewProject;
  $("#rr-intro-import").onclick = guarded(async () => { if (await rrLeave()) rrShow("import"); });
  imBind();
  skBind();
  roBind();
  rgBind();
  $("#rrp-log-btn").onclick = showProjectLog;
  $("#rrp-start-history").onclick = guarded(async () => {
    if (!(await confirmWord("Count everything in this project up to now as reviewed? Reviews and the review gate " +
        "then look only at work from here on. (A whole-project scan in Debrief can still check the existing code.)",
        "yes", "Start from now"))) return;
    const out = await api("rr_start_history", rr.pid);
    for (const w of out.warnings || []) banner(w);
    rrRenderRoutines();
  });
  document.querySelectorAll("#rrp-allow-box [data-allow]").forEach((c) => {
    c.onchange = guarded(async () => {
      rr.proj = await api("rr_set_allow", rr.pid, { [c.dataset.allow]: c.checked });
      rrRenderAllow();
    });
  });
  document.querySelectorAll("#rrp-allow-box [data-allow-list]").forEach((i) => {
    i.onchange = guarded(async () => {
      try {
        rr.proj = await api("rr_set_allow", rr.pid, { [i.dataset.allowList]: i.value });
      } finally { rrRenderAllow(); }
    });
  });
  $("#rrp-images").onchange = guarded(async (e) => {
    rr.proj = await api("rr_set_images", rr.pid, e.target.checked);
    banner(e.target.checked ? "Images are on for this project's sessions." :
      "Images are off for this project: its sessions are text only.", "info");
  });
  document.querySelectorAll(".rr-navbtn").forEach((b) => (b.onclick = guarded(async () => {
    if (rr.pane === "project" && !(await rrLeave())) return;
    if (b.dataset.pane === "agents") { await rrOpenAgents(null); return; }
    rrShow(b.dataset.pane);
  })));
  // new project
  $("#rrn-template").onchange = rrApplyTemplate;
  $("#rrn-folder-btn").onclick = guarded(async () => {
    const f = await api("pick_folder");
    if (!f) return;
    rr.newFolder = f;
    $("#rrn-folder").textContent = f;
    if (!$("#rrn-name").value.trim()) $("#rrn-name").placeholder = f.split(/[\\/]/).filter(Boolean).pop() || "";
  });
  $("#rrn-create").onclick = rrCreate;
  $("#rrn-cancel").onclick = () => rrShow("intro");
  // project editor
  for (const id of ["#rrp-name", "#rrp-goal", "#rrp-tags", "#rrp-rules"]) $(id).oninput = () => rrSetDirty(true);
  $("#rrp-rulesets").onchange = () => rrSetDirty(true);
  $("#rrp-preset").onchange = () => rrSetDirty(true);
  $("#rrp-save").onclick = rrSave;
  $("#rrp-folder-btn").onclick = guarded(async () => {
    const f = await api("pick_folder");
    if (!f) return;
    rr.proj = await api("rr_update_project", rr.pid, { path: f });
    $("#rrp-folder").textContent = rr.proj.path;
    rrLoad();
  });
  for (const [btn, kind] of [["#rrp-addfolder-btn", "work"], ["#rrp-addref-btn", "reference"],
                             ["#rrp-addws-btn", "workspace"]]) {
    $(btn).onclick = guarded(async () => {
      const p = await api("rr_add_folder", rr.pid, kind);
      if (p) { rr.proj = p; rrRenderFolders(); }
    });
  }
  $("#rrp-fly").onclick = async () => { if (await rrLeave()) showTool("flightdeck"); };
  $("#rrp-task-add").onclick = rrAddTask;
  $("#rrp-task-title").onkeydown = (e) => { if (e.key === "Enter") rrAddTask(); };
  $("#rrl-add").onclick = rrAddLink;
  $("#rrp-preview").onclick = guarded(async () => {
    if (rr.dirty) await rrSave();
    $("#preview-text").textContent = await api("rr_preview", rr.pid, null, null);
    openModal("#modal-preview");
  });
  $("#preview-close").onclick = closeModal;
  $("#rrp-totemplate").onclick = guarded(async () => {
    const name = await promptText("Template name:", { value: `${rr.proj.name} template`, required: true, line: true });
    if (name === null) return;
    rr.ov.templates = await api("rr_template_from_project", rr.pid, name);
    banner(`Saved template “${name}”.`, "info");
  });
  $("#rrp-remove").onclick = guarded(async () => {
    if (!(await confirmWord(`Remove “${rr.proj.name}” from Carrier? Its tasks and history are deleted; ` +
                            "the folder and files are not touched.", "remove", "Remove project"))) return;
    await api("rr_remove_project", rr.pid);
    rr.dirty = false; rr.pid = null; rr.proj = null;
    await rrLoadAll();
    rrShow("intro");
  });
  // global, rule sets, presets, templates
  $("#rrg-save").onclick = rrSaveGlobal;
  $("#rrs-new").onclick = () => { rr.edit.rulesets = "new"; rrRenderRuleSets(); $("#rrs-name").focus(); };
  $("#rrs-save").onclick = rrSaveRuleSet;
  $("#rrs-delete").onclick = rrDeleteRuleSet;
  $("#rrps-new").onclick = () => { rr.edit.presets = "new"; rrRenderPresets(); $("#rrps-name").focus(); };
  $("#rrps-save").onclick = rrSavePreset;
  $("#rrps-delete").onclick = rrDeletePreset;
  $("#rrt-new").onclick = () => { rr.edit.templates = "new"; rrRenderTemplates(); $("#rrt-name").focus(); };
  $("#rrt-save").onclick = rrSaveTemplate;
  $("#rrt-delete").onclick = rrDeleteTemplate;
}

/* --------------------------------------------------------------------- init */

async function init() {
  try {
    applyTheme(await api("settings"));
  } catch (e) { banner(e.message); }
  setTimeout(() => {  // Flight Deck alerts work from every tab
    if (!fd.ready) { fd.ready = true; $("#fd-policy").value = (state.settings && state.settings.fd_policy) || "warn"; fdPollLoop(); }
    fdRefresh();
  }, 300);
  api("upgrade_notice").then((path) => {
    if (path) banner(`Carrier updated its data for this version. A backup of the old data is at ${path}.`, "info");
  }).catch(() => {});
  $("#btn-start").onclick = () => startReview(state.freshStart);
  $("#btn-scan").onclick = startScan;
  $("#btn-cancel").onclick = cancelStart;
  $("#btn-resume").onclick = () => startReview(false);
  $("#btn-new").onclick = () => { state.freshStart = true; show($("#resume-box"), false); show($("#start-box")); };
  $("#btn-test-claude").onclick = guarded(async () => {
    const out = $("#test-claude-result");
    out.textContent = "Testing…";
    const r = await api("test_claude");
    out.textContent = r.ok ? "✓ Claude Code is working." : "✗ " + r.error;
  });
  $("#btn-home").onclick = goHome;
  document.querySelectorAll(".tool").forEach((b) => (b.onclick = () => showTool(b.dataset.tool)));
  fdBind();
  rrBind();
  rrBindAgentsAndDrafts();
  bindModels();
  $("#btn-finish").onclick = finishDialog;
  $("#btn-settings").onclick = openSettings;
  $("#btn-help").onclick = () => openModal("#modal-help");
  $("#help-close").onclick = closeModal;
  $("#settings-close").onclick = closeModal;
  $("#modal-backdrop").onclick = () => { if (openModalSel !== "#modal-prompt" && openModalSel !== "#modal-confirm") closeModal(); };
  $("#colors-reset").onclick = guarded(async () => { applyTheme(await api("save_settings", { custom_colors: {} })); renderSettings(); });
  // Preview while dragging; save once when the slider is released.
  $("#font-size").oninput = (e) => {
    $("#font-size-value").textContent = e.target.value + "px";
    document.documentElement.style.setProperty("--code-size", e.target.value + "px");
  };
  $("#font-size").onchange = guarded(async (e) => {
    applyTheme(await api("save_settings", { font_size: Number(e.target.value) }));
  });
  for (const [id, key] of [["#alerts-flash", "alerts_flash"], ["#alerts-sound", "alerts_sound"], ["#alerts-dnd", "alerts_dnd"]]) {
    $(id).onchange = guarded(async (e) => {
      applyTheme(await api("save_settings", { [key]: e.target.checked }));
      if (key === "alerts_sound" && e.target.checked) chime();
      if (fd.overview) updateAlerts(fd.overview);
    });
  }
  $("#content-width").onchange = guarded(async (e) => {
    applyTheme(await api("save_settings", { content_width: e.target.value }));
  });
  $("#content-center").onchange = guarded(async (e) => {
    applyTheme(await api("save_settings", { content_center: e.target.checked }));
  });
  for (const [id, key] of [["#usage-hold-takeoff", "usage_hold_takeoff"], ["#usage-hold-routines", "usage_hold_routines"],
                           ["#usage-auto-resume", "usage_auto_resume"], ["#usage-read-logs", "usage_read_logs"]]) {
    $(id).onchange = guarded(async (e) => {
      applyTheme(await api("save_settings", { [key]: e.target.checked }));
      fdRefreshSoon();
    });
  }
  $("#usage-plan").onchange = guarded(async (e) => { applyTheme(await api("save_settings", { usage_plan: e.target.value })); });
  for (const [id, key] of [["#usage-warn-at", "usage_warn_at"], ["#usage-hold-at", "usage_hold_at"]]) {
    $(id).onchange = guarded(async (e) => {
      const v = Math.max(1, Math.min(100, Math.round(Number(e.target.value) || 0)));
      e.target.value = v;
      applyTheme(await api("save_settings", { [key]: v }));
      fdRefreshSoon();
    });
  }
  $("#btn-usage").onclick = openUsage;
  $("#usage-close").onclick = closeModal;
  $("#usage-settings").onclick = () => { closeModal(); openSettings(); };
  $("#mode-unified").onclick = () => setSplit(false);
  $("#mode-split").onclick = () => setSplit(true);
  $("#btn-ask").onclick = ask;
  $("#ask-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); }
  });
  $("#sel-clear").onclick = clearSelection;
  $("#sel-copy").onclick = () => state.sel && copyText(state.sel.text);
  document.querySelectorAll(".decide[data-action]").forEach((b) => (b.onclick = () => decide(b.dataset.action)));
  $("#btn-note").onclick = addNote;
  bindDiffMouse();
  document.addEventListener("keydown", onKey);
  // Open where the work is: Ready Room until there is a project, then Flight
  // Deck. Decided first, so a slow call below can't leave the app on Debrief.
  try {
    const ov = await api("rr_overview", "", null);
    showTool(ov.projects.length ? "flightdeck" : "readyroom");
  } catch (e) { banner(e.message); showTool("readyroom"); }
  // These don't block anything: each fills its own part of the page.
  guarded(async () => renderChecks(await api("checks")))();
  guarded(initStart)();
}

if (window.pywebview && window.pywebview.api) init();
else window.addEventListener("pywebviewready", init, { once: true });
