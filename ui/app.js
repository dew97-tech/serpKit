/* SERP KIT viewer logic: one scrape, seven block views plus raw JSON.
   Chrome-only i18n via t()/setLang() (ui/i18n.js). Result content stays in
   the SERP's own language. Live runs use the SSE endpoint for real stage
   progress; mock runs use the synchronous endpoint. */
"use strict";

const $ = (id) => document.getElementById(id);
const form = $("scrapeForm"), statusEl = $("status"), tabsEl = $("tabs");
const runBtn = $("runBtn");
let lastData = null;
let presets = {};
let running = false;
let runTimeoutMs = 660000;

const TAB_IDS = ["organic", "paa", "ai", "related", "maps", "auto", "raw"];

const STAGE_KEYS = {
  session: "pSession",
  autocomplete: "pAutocomplete",
  organic: "pOrganic",
  ai: "pAi",
  paa: "pPaa",
  related: "pRelated",
  pages: "pPages",
  maps: "pMaps",
  finalize: "pFinalize",
};

async function loadPresets() {
  const r = await fetch("/api/locales");
  const data = await r.json();
  presets = data.presets || {};
  applyPreset($("preset").value || data.default_preset || "us");
}

/* Preset switching updates locale fields only. The keyword is user owned and
   is never overwritten by preset changes, demo data, or scrape lifecycle. */
function applyPreset(name) {
  const p = presets[name];
  if (!p) return;
  $("hl").value = p.hl; $("gl").value = p.gl;
  $("domain").value = p.google_domain; $("location").value = "";
  $("preset").title = `${p.google_domain} | hl=${p.hl} | gl=${p.gl}${p.city ? " | " + p.city : ""}`;
  loadSession();
}

function setStatus(kind, html) {
  statusEl.hidden = false;
  statusEl.className = `status ${kind}`;
  statusEl.innerHTML = html;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function truncate(s, n = 120) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n - 1) + "..." : s;
}

/* --- inline SVG icons (no emoji): open, copy, check, chevron --- */

const ICONS = {
  open: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M6.5 3.5h-3v9h9v-3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M9.5 3.5h3v3M12.2 3.8 7.5 8.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  copy: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><rect x="5.5" y="5.5" width="7" height="7" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.5 5.5v-2a1.5 1.5 0 0 0-1.5-1.5H4.5A1.5 1.5 0 0 0 3 3.5V8a1.5 1.5 0 0 0 1.5 1.5h1" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  check: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="m3 8.5 3.2 3.2L13 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  chev: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="m4 6 4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

function domainOf(link) {
  try {
    return new URL(link).hostname.replace(/^www\./, "");
  } catch { return ""; }
}

async function copyText(s) {
  const text = String(s ?? "");
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      return true;
    } catch { return false; }
  }
}

function flashIconBtn(btn) {
  if (!btn || btn.dataset.done) return;
  const orig = btn.innerHTML;
  btn.dataset.done = "1";
  btn.classList.add("is-done");
  btn.innerHTML = ICONS.check;
  setTimeout(() => {
    btn.innerHTML = orig;
    btn.classList.remove("is-done");
    delete btn.dataset.done;
  }, 1400);
}

function errLabel(code) {
  const labels = t("errLabels");
  return (labels && labels[code]) || code;
}

function shortError(e) {
  const code = e.code || "error";
  let msg = String(e.message || "");
  msg = msg.replace(
    new RegExp("^" + code.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ":\\s*"), "");
  msg = msg.replace(/https?:\/\/\S+/g, (u) => truncate(u, 90));
  return { code, msg: truncate(msg, 220) };
}

function solveSummary(d) {
  const s = d.solves || [];
  if (!s.length) return "";
  const solved = s.filter((x) => x.outcome === "solved").length;
  const last = s[s.length - 1];
  return last.outcome === "solved"
    ? `${t("solvePrefix")}: ${solved}/${s.length} solved`
    : `${t("solvePrefix")}: ${last.outcome}${last.error ? ", " + truncate(last.error, 90) : ""}`;
}

/* --- session and solver status pills --- */

async function loadStatus() {
  try {
    const h = await (await fetch("/api/health")).json();
    const s = h.solver || {};
    $("solverText").textContent = s.configured ? t("solverReady") : t("solverOff");
    $("solverPill").title = t("solverTitle", s.balance);
    $("solverPill").classList.toggle("pill-warn", !s.configured);
    if (h.run_timeout_s) runTimeoutMs = (h.run_timeout_s * 1000) + 60000;
  } catch { /* status pill is progressive enhancement */ }
}

function renderSessionPill(s) {
  const pill = $("sessionPill");
  if (!s || !s.active) {
    $("sessionText").textContent = t("sessionNone");
    pill.title = t("sessionNone");
    pill.classList.add("pill-warn");
    return;
  }
  const mins = Math.max(0, Math.ceil((s.expires_in_s || 0) / 60));
  $("sessionText").textContent = `${t("session")} ${mins}m`;
  pill.classList.remove("pill-warn");
  pill.title =
    `sessid ${s.sessid}\n` +
    `exit IP ${s.exit_ip || "?"} (${s.exit_geo || "?"})\n` +
    `age ${Math.floor((s.age_s || 0) / 60)}m / ttl ${Math.floor((s.ttl_s || 0) / 60)}m\n` +
    `runs ${s.runs}, ok ${s.successes}, solve-fail ${s.solve_failures}, cookies ${s.cookies}\n` +
    (s.user_agent ? `UA ${s.user_agent}` : "");
}

async function loadSession() {
  if (running) return;
  try {
    const p = presets[$("preset").value] || {};
    const key = p.hl && p.gl ? `${p.hl}-${p.gl}` : "";
    const r = await fetch(`/api/session${key ? `?preset=${encodeURIComponent(key)}` : ""}`);
    const data = await r.json();
    renderSessionPill((data.presets || [])[0]);
  } catch { /* session pill is progressive enhancement */ }
}

function reactSession(detail) {
  const pill = $("sessionPill");
  if (!detail) return;
  pill.classList.remove("pill-warn");
  $("sessionText").textContent = detail.includes("(new)")
    ? `${t("session")} ${t("sessionNew")}`
    : t("session");
  pill.title = detail;
}

/* --- progress checklist driven by real server events --- */

function stagesForRun(params) {
  const stages = ["session", "autocomplete", "organic", "ai", "paa", "related"];
  if (parseInt(params.get("pages") || "3", 10) > 1) stages.push("pages");
  stages.push("maps", "finalize");
  return stages;
}

function buildProgress(stageIds) {
  const list = $("progressList");
  list.innerHTML = "";
  stageIds.forEach((id) => {
    const li = document.createElement("li");
    li.dataset.stage = id;
    li.innerHTML = `<i class="mark"></i><span class="label"></span><span class="detail"></span>`;
    li.querySelector(".label").textContent = t(STAGE_KEYS[id] || id);
    list.appendChild(li);
  });
  $("progressNote").hidden = true;
  $("progressNote").textContent = "";
}

function updateStage(stage, state, detail) {
  const li = $("progressList").querySelector(`li[data-stage="${stage}"]`);
  if (!li) return;
  li.className = state ? `st-${state}` : "";
  li.querySelector(".detail").textContent = detail || "";
}

function renderNote(detail) {
  const note = $("progressNote");
  note.hidden = !detail;
  note.textContent = detail || "";
}

function relabelProgress() {
  $("progressList").querySelectorAll("li[data-stage]").forEach((li) => {
    li.querySelector(".label").textContent = t(STAGE_KEYS[li.dataset.stage] || li.dataset.stage);
  });
}

function setFormDisabled(flag) {
  ["preset", "q", "pages", "location", "hl", "gl", "domain", "mock", "solve", "runBtn"]
    .forEach((id) => { $(id).disabled = flag; });
  form.setAttribute("aria-busy", flag ? "true" : "false");
}

/* Runs the streamed scrape. Resolves with the result payload. */
function runStreamed(params) {
  return new Promise((resolve, reject) => {
    if (typeof EventSource === "undefined") {
      reject(new Error(t("stNetErr", "EventSource unsupported")));
      return;
    }
    buildProgress(stagesForRun(params));
    $("progressCard").hidden = false;
    const es = new EventSource(`/api/scrape/stream?${params}`);
    let settled = false;
    const timer = setTimeout(() => finish(new Error(t("timeout"))), runTimeoutMs);

    function finish(err) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      es.close();
      if (err) reject(err);
    }

    es.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "stage") {
        if (msg.stage === "note") {
          renderNote(msg.detail || "");
        } else {
          updateStage(msg.stage, msg.state, msg.detail || "");
          if (msg.stage === "session" && msg.state === "done") {
            reactSession(msg.detail || "");
          }
        }
      } else if (msg.type === "result") {
        finish(null);
        resolve(msg.result);
      } else if (msg.type === "error") {
        finish(new Error(msg.message || "error"));
      }
    };
    es.onerror = () => {
      if (!settled) finish(new Error(t("stNetErr", "stream")));
    };
  });
}

/* --- result rendering --- */

function renderAll(d) {
  lastData = d;
  tabsEl.hidden = false;

  const org = d.organic || [];
  const rel = d.related_searches || [];
  $("cOrganic").textContent = org.length ? `(${org.length})` : "";
  $("pagesNote").textContent = `${d.pages_fetched ?? 0} pages, has_next=${d.has_next}`;
  const byPage = new Map();
  org.forEach((r) => {
    const p = r.page || 1;
    if (!byPage.has(p)) byPage.set(p, []);
    byPage.get(p).push(r);
  });
  const orgItems = [];
  for (const [pageNo, items] of [...byPage.entries()].sort((a, b) => a[0] - b[0])) {
    if (byPage.size > 1) {
      orgItems.push(`<li class="org-sep" aria-hidden="true">${esc(t("pageN", pageNo))} (${items.length})</li>`);
    }
    orgItems.push(...items.map((r) => {
      const dom = domainOf(r.link);
      const long = String(r.snippet || "").length > 160;
      return `
      <li class="org-item" data-pos="${esc(r.position)}">
        <div class="org-top">
          <span class="rank">#${esc(r.position)}</span>
          ${dom ? `<span class="domain">${esc(dom)}</span>` : ""}
          ${byPage.size > 1 ? `<span class="page-tag">${esc(t("pageN", pageNo))}</span>` : ""}
        </div>
        <a class="org-title" href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.title)}</a>
        <div class="org-url-row">
          <span class="org-url">${esc(r.link)}</span>
          <span class="org-icons">
            <button class="icon-btn" type="button" data-act="open" data-link="${esc(r.link)}" aria-label="${esc(t("openResult"))}" title="${esc(t("openResult"))}">${ICONS.open}</button>
            <button class="icon-btn" type="button" data-act="copy-link" data-link="${esc(r.link)}" aria-label="${esc(t("copyLink"))}" title="${esc(t("copyLink"))}">${ICONS.copy}</button>
            <button class="icon-btn" type="button" data-act="copy-snip" data-snip="${esc(r.snippet || "")}" aria-label="${esc(t("copySnippet"))}" title="${esc(t("copySnippet"))}">${ICONS.copy}</button>
          </span>
        </div>
        ${r.snippet ? `<p class="org-snip${long ? " clamp" : ""}">${esc(r.snippet)}</p>
        ${long ? `<button class="more-btn" type="button" data-act="toggle">${esc(t("showMore"))}</button>` : ""}` : ""}
      </li>`;
    }));
  }
  $("orgList").innerHTML = orgItems.join("")
    || `<li class="empty">${esc(t("emptyOrganic"))}</li>`;
  const hasLong = $("orgList").querySelector(".org-snip.clamp");
  $("orgExpandAll").hidden = !hasLong;
  $("orgExpandAll").textContent = t("expandAll");
  $("orgExpandAll").setAttribute("aria-label", t("expandAll"));

  const paa = d.related_questions || [];
  $("cPaa").textContent = paa.length ? `(${paa.length})` : "";
  $("paaList").innerHTML = paa.map((x) => `
    <details><summary>${esc(x.question)}</summary>
    <div class="a-body">${x.snippet ? `<p>${esc(x.snippet)}</p>` : `<p class="empty">${esc(t("noSnippet"))}</p>`}
    ${x.link ? `<a href="${esc(x.link)}" target="_blank" rel="noopener">${esc(t("openSource"))}</a>` : ""}</div></details>`).join("")
    || `<p class="empty">${esc(t("emptyPaa"))}</p>`;

  const ai = d.ai_overview;
  const aiBlocks = (ai && ai.text_blocks) || [];
  const aiLinks = (ai && ai.links) || [];
  $("aiBody").innerHTML = aiBlocks.length
    ? `<div class="ai-card"><div class="ai-paras">${aiBlocks.map((x) => `
        <div class="ai-para">
          <p>${esc(x)}</p>
          <button class="icon-btn" type="button" data-act="copy-block" data-text="${esc(x)}" aria-label="${esc(t("copyBlock"))}" title="${esc(t("copyBlock"))}">${ICONS.copy}</button>
        </div>`).join("")}</div>`
      + (aiLinks.length ? `<div class="ai-sources">${aiLinks.map((l) => {
        const dom = domainOf(l.link);
        return `<a class="src-chip" href="${esc(l.link)}" target="_blank" rel="noopener">`
          + `${dom ? `<span class="src-dom">${esc(dom)}</span>` : ""}`
          + `<span class="src-title">${esc(l.title || l.link)}</span>${ICONS.open}</a>`;
      }).join("")}</div>` : "")
      + `</div>`
    : `<p class="empty">${esc(t("emptyAi"))}</p>`;
  $("aiCopyAll").hidden = !aiBlocks.length;

  $("relChips").innerHTML = rel.map((s) => `<span>${esc(s)}</span>`).join("")
    || `<p class="empty">${esc(t("emptyRelated"))}</p>`;

  const lr = d.local_results || [];
  $("localBody").innerHTML = lr.length
    ? `<div class="map-grid">${lr.map((b) => `
      <div class="map-card"><h3>${esc(b.title)}</h3>
      ${b.address ? `<p><b>Address:</b> ${esc(b.address)}</p>` : ""}
      ${b.phone ? `<p><b>Phone:</b> ${esc(b.phone)}</p>` : ""}
      <p><b>Rating:</b> ${b.rating ?? "-"} <span class="snip">(${b.reviews ?? 0} reviews)</span></p>
      ${b.website ? `<p><b>Website:</b> <a href="${esc(b.website)}" target="_blank" rel="noopener">${esc(b.website)}</a></p>` : ""}</div>`).join("")}</div>`
    : `<p class="empty">${esc(t("emptyLocal"))}</p>`;

  const maps = d.maps_local_results || [];
  $("cMaps").textContent = maps.length ? `(${maps.length})` : "";
  $("mapsGrid").innerHTML = maps.map((m) => `
    <div class="map-card"><h3>#${m.position} ${esc(m.title)}</h3>
    ${m.address ? `<p><b>Address:</b> ${esc(m.address)}</p>` : ""}
    <p><b>Rating:</b> ${m.rating ?? "-"} <span class="snip">(${m.reviews ?? 0} reviews)</span></p>
    ${m.hours ? `<p><b>Hours:</b> ${esc(m.hours)}</p>` : ""}
    ${m.phone ? `<p><b>Phone:</b> ${esc(m.phone)}</p>` : ""}
    ${m.website ? `<p><b>Website:</b> <a href="${esc(m.website)}" target="_blank" rel="noopener">${esc(m.website)}</a></p>` : ""}</div>`).join("")
    || `<p class="empty">${esc(t("emptyMaps"))}</p>`;

  $("autoList").innerHTML = (d.autocomplete || []).map((s) => `<li>${esc(s)}</li>`).join("")
    || `<li class="empty">${esc(t("emptyAuto"))}</li>`;

  const tm = d.timing_ms || {};
  const solves = d.solves || [];
  const solvedN = solves.filter((s) => s.outcome === "solved").length;
  $("timingLine").textContent =
    `total ${tm.total_ms ?? "-"}ms, search ${tm.search_ms ?? "-"}ms, autocomplete ${tm.autocomplete_ms ?? "-"}ms, maps ${tm.maps_ms ?? "-"}ms` +
    (solves.length ? `, ${t("solvesLabel", solvedN)}` : "") +
    (d.mock ? ", MOCK" : "");

  const heads = tm.heads || {};
  const dupes = tm.dupes_dropped || 0;
  const headChip = (label, on) =>
    `<span class="schip ${on ? "" : "off"}">${esc(label)} ${esc(on ? t("served") : t("absent"))}</span>`;
  const chips = [
    `${org.length} ${t("tabOrganic")}`,
    ...(d.pages_fetched > 1 ? [t("sumPages", d.pages_fetched)] : []),
    ...(rel.length ? [`${rel.length} ${t("tabRelated")}`] : []),
    ...(paa.length ? [`${paa.length} ${t("tabPaa")}`] : []),
    ...(ai ? [ai.source === "ai_mode" ? "AI Mode" : t("tabAi")] : []),
    ...(lr.length ? [`${lr.length} ${t("sumLocal")}`] : []),
    ...(maps.length ? [`${maps.length} ${t("tabMaps")}`] : []),
    ...(solves.length ? [t("solvesLabel", solvedN)] : []),
    ...(dupes ? [t("dupes", dupes)] : []),
  ];
  const headChips = Object.keys(heads).length
    ? headChip(t("tabPaa"), !!heads.paa) +
      headChip("PASF", !!heads.pasf) +
      headChip(t("tabRelated"), !!heads.related)
    : "";
  $("summaryBar").innerHTML =
    chips.map((c) => `<span class="schip">${esc(c)}</span>`).join("") +
    headChips +
    (tm.web_fallback ? `<span class="schip warn">${esc(t("webFallback"))}</span>` : "");
  $("summaryBar").hidden = false;
  $("rawPre").textContent = JSON.stringify(d, null, 2);

  const errs = d.errors || [];
  const solve = solveSummary(d);
  if (!errs.length && org.length) {
    setStatus("ok", esc(t("stDone", org.length, paa.length, (d.related_searches || []).length)));
  } else if (org.length) {
    const codes = errs.map((e) => e.code).join(", ");
    setStatus("warn",
      `<div class="st-title">${t("stPartial", org.length, esc(codes))}</div>` +
      (solve ? `<div class="st-sub">${esc(solve)}</div>` : ""));
  } else {
    const first = shortError(errs[0] || { code: "unknown", message: "" });
    const extra = errs.length > 1 ? ` (+${errs.length - 1})` : "";
    const sub = [first.msg, solve].filter(Boolean).join(", ");
    setStatus("err",
      `<div class="st-title">${esc(t("stBlocked"))}: ${esc(errLabel(first.code))}${esc(extra)}</div>` +
      (sub ? `<div class="st-sub">${esc(sub)}</div>` : ""));
  }
}

/* --- events --- */

tabsEl.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  tabsEl.querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-selected", b === btn ? "true" : "false"));
  TAB_IDS.forEach((id) => { $(`panel-${id}`).hidden = id !== btn.dataset.tab; });
});

$("copyBtn").addEventListener("click", async () => {
  if (!lastData) return;
  await copyText(JSON.stringify(lastData, null, 2));
  $("copyBtn").textContent = t("copied");
  setTimeout(() => { $("copyBtn").textContent = t("copy"); }, 1500);
});

/* --- organic + AI panel actions (delegated) --- */

$("orgList").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const item = btn.closest(".org-item");
  const act = btn.dataset.act;
  if (act === "toggle" && item) {
    const snip = item.querySelector(".org-snip");
    const open = snip.classList.toggle("clamp");
    btn.textContent = open ? t("showMore") : t("showLess");
    return;
  }
  if (act === "open" && btn.dataset.link) {
    window.open(btn.dataset.link, "_blank", "noopener");
    return;
  }
  if (act === "copy-link" && btn.dataset.link) {
    if (await copyText(btn.dataset.link)) flashIconBtn(btn);
    return;
  }
  if (act === "copy-snip" && btn.dataset.snip) {
    if (await copyText(btn.dataset.snip)) flashIconBtn(btn);
  }
});

$("orgCopyAll").addEventListener("click", async (e) => {
  const org = (lastData && lastData.organic) || [];
  if (!org.length) return;
  const text = org.map((r) => `${r.position}. ${r.title}\n${r.link}${r.snippet ? `\n${r.snippet}` : ""}`).join("\n\n");
  if (await copyText(text)) {
    const b = e.currentTarget;
    const orig = b.textContent;
    b.textContent = t("copied");
    setTimeout(() => { b.textContent = orig; }, 1400);
  }
});

$("orgExpandAll").addEventListener("click", () => {
  const list = $("orgList");
  const collapsed = list.querySelectorAll(".org-snip.clamp");
  const expand = collapsed.length > 0;
  list.querySelectorAll(".org-snip").forEach((p) => p.classList.toggle("clamp", !expand));
  list.querySelectorAll('.more-btn[data-act="toggle"]').forEach((b) => {
    b.textContent = expand ? t("showLess") : t("showMore");
  });
  $("orgExpandAll").textContent = expand ? t("collapseAll") : t("expandAll");
});

$("aiBody").addEventListener("click", async (e) => {
  const btn = e.target.closest('button[data-act="copy-block"]');
  if (!btn) return;
  if (await copyText(btn.dataset.text || "")) flashIconBtn(btn);
});

$("aiCopyAll").addEventListener("click", async (e) => {
  const ai = lastData && lastData.ai_overview;
  const blocks = (ai && ai.text_blocks) || [];
  if (!blocks.length) return;
  if (await copyText(blocks.join("\n\n"))) {
    const b = e.currentTarget;
    const orig = b.textContent;
    b.textContent = t("copied");
    setTimeout(() => { b.textContent = orig; }, 1400);
  }
});

$("preset").addEventListener("change", (e) => applyPreset(e.target.value));

document.querySelectorAll("#langToggle button").forEach((b) =>
  b.addEventListener("click", () => {
    setLang(b.dataset.lang);
    relabelProgress();
    if (lastData) renderAll(lastData);
  }));

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (running) return;
  const params = new URLSearchParams({
    q: $("q").value.trim(), preset: $("preset").value,
    hl: $("hl").value.trim(), gl: $("gl").value.trim(),
    domain: $("domain").value.trim(),
    location: $("location").value.trim(),
    pages: $("pages").value || "3",
    mock: $("mock").checked ? "1" : "0",
    solve: $("solve").checked ? "1" : "0",
  });
  const submittedQuery = params.get("q");
  if (!submittedQuery) return;
  const isMock = params.get("mock") === "1";

  running = true;
  setFormDisabled(true);
  setStatus("", esc(t("stRunning", submittedQuery)));
  $("summaryBar").hidden = true;
  if (isMock) {
    $("progressCard").hidden = true;
  } else {
    $("progressList").innerHTML = "";
    $("progressCard").hidden = false;
  }

  try {
    let data;
    if (isMock) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 30000);
      const r = await fetch(`/api/scrape?${params}`, { signal: ctrl.signal });
      clearTimeout(timer);
      data = await r.json();
    } else {
      data = await runStreamed(params);
    }
    renderAll(data);
  } catch (err) {
    const msg = err && err.name === "AbortError" ? t("timeout") : String(err && err.message || err);
    setStatus("err", esc(t("stNetErr", msg)));
  } finally {
    running = false;
    setFormDisabled(false);
    $("q").value = submittedQuery;
    loadSession();
  }
});

setLang(uiLang);
loadPresets();
loadStatus();
setInterval(loadSession, 30000);
