/* Admin screens: Run, Jobs, Repos, Skill & models, Access. Loaded by index.html only for admins.
   Every call goes to the admin API; the server enforces the role, this file only draws the screens. */
(() => {
"use strict";
const E = s => (s ?? "").toString().replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const Q = (s, r = document) => r.querySelector(s);
const QA = (s, r = document) => [...r.querySelectorAll(s)];
const main = () => document.getElementById("main");
const money = v => v == null ? "–" : "$" + (+v).toFixed(v < 1 ? 3 : 2);
const when = s => s ? s.replace("T", " ").slice(0, 16) : "–";
const bdg = (t, c) => `<span class="badge ${c || "s-na"}">${E(t)}</span>`;
const score = v => v == null ? "–" : (+v).toFixed(1);
const statusCls = {done: "s-good", running: "s-ok", queued: "s-na", failed: "s-bad", cancelled: "s-na"};
let timer = null;
const stopTimer = () => { if (timer) clearInterval(timer); timer = null; };

async function call(method, path, body) {
  const r = await fetch(location.origin + path, {method, credentials: "same-origin",
    headers: {"X-Requested-With": "audit", "Content-Type": "application/json"}, body: body === undefined ? undefined : JSON.stringify(body)});
  if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  let data = null; try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}
function toast(msg, bad) {
  const t = document.createElement("div"); t.className = "toast" + (bad ? " bad" : ""); t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

function show(name, arg) {
  stopTimer();
  document.body.classList.add("admin-view");
  QA("[data-admin]").forEach(b => b.classList.toggle("on", b.dataset.admin === name));
  main().innerHTML = `<div class="empty">Loading…</div>`;
  return views[name](arg).catch(e => { main().innerHTML = `<div class="card"><b>Could not load:</b> ${E(e.message)}</div>`; });
}

/* ================================================================== Run */
async function run() {
  const [st, gh, tracked] = await Promise.all([call("GET", "/api/settings"), call("GET", "/api/github/repos").catch(e => ({error: e.message, repos: []})), call("GET", "/api/repos")]);
  const sel = new Set(Admin.preset || []); Admin.preset = null;
  let filter = "", recentOnly = true;
  const months = () => Q("#r-period").value === "custom" ? null : +Q("#r-period").value;
  const sinceDate = () => { const m = months(); if (!m) return Q("#r-since").value || "0000-01-01"; const d = new Date(); d.setMonth(d.getMonth() - m); return d.toISOString().slice(0, 10); };
  main().innerHTML = `
  <div class="card"><h3>1 · Pick repositories ${gh.org ? `<span class="muted small">from ${E(gh.org)}</span>` : ""}</h3>
    ${gh.error ? `<div class="note bad">${E(gh.error)}. Set <code>GITHUB_TOKEN</code> and <code>GITHUB_ORG</code> on the server, or type repos below.</div>` : ""}
    <div class="row"><input id="r-filter" placeholder="Filter repos…" style="min-width:220px">
      <label class="chk"><input type="checkbox" id="r-recent" checked> only pushed in the period</label>
      <button id="r-all">Select shown</button><button id="r-none">Clear</button><button id="r-reload">Reload list</button>
      <input id="r-add" placeholder="owner/name" style="width:200px"><button id="r-addb">Add by name</button></div>
    <div id="r-list" class="scroll" style="max-height:300px;margin-top:10px"></div>
    <div class="small muted" id="r-count"></div></div>
  <div class="card"><h3>2 · Period and what to do</h3>
    <div class="grid3">
      <div class="field"><label>Period</label><select id="r-period"><option value="1">Last 1 month</option><option value="3" selected>Last 3 months</option><option value="6">Last 6 months</option><option value="12">Last 12 months</option><option value="custom">Custom…</option></select>
        <div id="r-custom" hidden class="row" style="margin-top:6px"><input type="date" id="r-since"> – <input type="date" id="r-until"></div></div>
      <div class="field"><label>Pull from GitHub</label><label class="chk"><input type="checkbox" id="r-collect" checked> collect PRs, commits, reviews (code only, no LLM)</label></div>
      <div class="field"><label>Scoring</label>
        <label class="chk"><input type="radio" name="r-score" value="llm" checked> LLM with our skill (uses your API key)</label>
        <label class="chk"><input type="radio" name="r-score" value="heuristic"> Rule-based estimate (free, no LLM)</label>
        <label class="chk"><input type="radio" name="r-score" value="none"> Don't score, collect only</label></div>
    </div>
    <div class="grid3" style="margin-top:8px">
      <div class="field"><label class="chk"><input type="checkbox" id="r-rh" checked> also re-score rule-based results with the LLM</label>
        <label class="chk"><input type="checkbox" id="r-ros"> re-score PRs scored with an older skill version</label>
        <label class="chk"><input type="checkbox" id="r-batch"> Batch mode: half price, results within hours</label></div>
      <div class="field"><label>Stop after N PRs</label><input id="r-maxprs" type="number" min="1" placeholder="no limit"></div>
      <div class="field"><label>Stop when spend reaches (USD)</label><input id="r-maxusd" type="number" min="0.01" step="0.01" placeholder="no cap"></div>
    </div>
    ${!st.anthropic_key ? `<div class="note bad" style="margin-top:8px"><b>ANTHROPIC_API_KEY is not set</b> on the server, so LLM scoring will fail. Use rule-based or set the key.</div>` : ""}
  </div>
  <div class="card"><h3>3 · Check, then start</h3>
    <div class="row"><button id="r-est">Estimate cost</button><button id="r-go" class="primary">Start run</button><span class="muted small" id="r-msg"></span></div>
    <div id="r-estout" style="margin-top:10px"></div></div>`;

  const shown = () => gh.repos.filter(r => !r.archived && (!filter || r.full_name.toLowerCase().includes(filter)) && (!recentOnly || (r.pushed_at || "").slice(0, 10) >= sinceDate()));
  const draw = () => {
    const rows = shown();
    const extra = [...sel].filter(n => !gh.repos.some(r => r.full_name === n));
    Q("#r-list").innerHTML = `<table><thead><tr><th></th><th>Repository</th><th>Last push</th><th>Collected PRs</th><th></th></tr></thead><tbody>` +
      [...extra.map(n => ({full_name: n, extra: 1})), ...rows].map(r => `<tr><td><input type="checkbox" data-n="${E(r.full_name)}" ${sel.has(r.full_name) ? "checked" : ""}></td>
        <td><b>${E(r.full_name)}</b> ${r.private ? bdg("private") : ""}</td><td class="nw">${E((r.pushed_at || "").slice(0, 10) || "–")}</td>
        <td class="n">${r.collected_prs ?? "–"}</td><td>${r.tracked ? bdg("tracked", "s-good") : ""}</td></tr>`).join("") + `</tbody></table>`;
    QA("#r-list input[data-n]").forEach(c => c.onchange = () => { c.checked ? sel.add(c.dataset.n) : sel.delete(c.dataset.n); count(); });
    count();
  };
  const count = () => { Q("#r-count").textContent = `${sel.size} selected · ${shown().length} shown${gh.repos.length ? ` of ${gh.repos.length}` : ""}`; };
  const params = () => {
    const p = {kind: "refresh", repos: [...sel], score: Q("input[name=r-score]:checked").value, collect: Q("#r-collect").checked,
               replace_heuristic: Q("#r-rh").checked, rescore_old_skill: Q("#r-ros").checked, batch: Q("#r-batch").checked};
    if (Q("#r-period").value === "custom") { p.since = Q("#r-since").value; p.until = Q("#r-until").value; } else p.months = +Q("#r-period").value;
    if (Q("#r-maxprs").value) p.max_prs = +Q("#r-maxprs").value;
    if (Q("#r-maxusd").value) p.max_usd = +Q("#r-maxusd").value;
    if (!p.repos.length) throw new Error("pick at least one repository");
    return p;
  };
  Q("#r-filter").oninput = e => { filter = e.target.value.toLowerCase(); draw(); };
  Q("#r-recent").onchange = e => { recentOnly = e.target.checked; draw(); };
  Q("#r-period").onchange = () => { Q("#r-custom").hidden = Q("#r-period").value !== "custom"; draw(); };
  Q("#r-all").onclick = () => { shown().forEach(r => sel.add(r.full_name)); draw(); };
  Q("#r-none").onclick = () => { sel.clear(); draw(); };
  Q("#r-reload").onclick = guard(async () => { Object.assign(gh, await call("GET", "/api/github/repos?refresh=1")); draw(); });
  Q("#r-addb").onclick = () => { const v = Q("#r-add").value.trim(); if (/^[\w.-]+\/[\w.-]+$/.test(v)) { sel.add(v); Q("#r-add").value = ""; draw(); } else toast("use owner/name", true); };
  Q("#r-est").onclick = guard(async () => {
    Q("#r-estout").innerHTML = `<span class="muted">Estimating…</span>`;
    const e = await call("POST", "/api/jobs", {...params(), estimate: true});
    const models = Object.entries(e.by_model);
    Q("#r-estout").innerHTML = `<div class="kpis"><div class="kpi"><div class="v">${e.prs}</div><div class="l">PRs to score (already collected)</div></div>
      <div class="kpi"><div class="v">${e.mode === "llm" ? "≈ " + money(e.usd_estimate) : "free"}</div><div class="l">${e.batch ? "batch price" : "estimated cost"}</div></div>
      <div class="kpi"><div class="v" style="font-size:15px">${E(e.skill_hash)}</div><div class="l">skill version (${E(e.skill_source)})</div></div></div>
      ${models.length ? `<table><thead><tr><th>Model</th><th class="n">PRs</th><th class="n">Estimate</th><th>Rules that chose it</th></tr></thead><tbody>${models.map(([m, d]) => `<tr><td>${E(m)}</td><td class="n">${d.prs}</td><td class="n">${money(d.usd)}</td><td class="small">${Object.entries(d.rules).map(([k, v]) => E(k) + " ×" + v).join(", ")}</td></tr>`).join("")}</tbody></table>` : ""}
      <div class="small muted">${E(e.note)} PRs that collecting finds later are not counted yet.</div>`;
  });
  Q("#r-go").onclick = guard(async () => {
    const p = params();
    if (p.score === "llm" && !p.max_usd && !confirm("No spend cap is set. Start the run anyway?")) return;
    const r = await call("POST", "/api/jobs", p);
    toast("Run started"); show("jobs", r.id);
  });
  draw();
}

/* ================================================================== Jobs */
async function jobs(openId) {
  const list = await call("GET", "/api/jobs");
  main().innerHTML = `<div class="card"><h3>Jobs</h3><div class="scroll"><table><thead><tr><th>#</th><th>What</th><th>Status</th><th>Progress</th><th class="n">Cost</th><th>By</th><th>Started</th></tr></thead>
    <tbody>${list.map(j => `<tr class="click" data-id="${j.id}"><td>${j.id}</td><td>${E(j.kind)} ${E(describe(j))}</td><td>${bdg(j.status, statusCls[j.status])}</td>
      <td>${j.progress_total ? `${j.progress_done}/${j.progress_total}` : "–"}</td><td class="n">${money(j.cost_usd)}</td><td>${E(j.created_by || "")}</td><td class="nw">${when(j.started_at || j.created_at)}</td></tr>`).join("") ||
      `<tr><td colspan="7" class="empty">No jobs yet. Start one from <b>Run</b>.</td></tr>`}</tbody></table></div></div><div id="jd"></div>`;
  QA("tr[data-id]").forEach(tr => tr.onclick = () => detail(+tr.dataset.id));
  const open = openId || (list.find(j => j.status === "running" || j.status === "queued") || {}).id;
  if (open) detail(open);
}
const describe = j => { const p = j.params || {}; return j.kind === "try" ? `${p.repo}#${p.number}` : `${(p.repos || []).length} repo(s), ${p.score || ""}${p.batch ? " batch" : ""}`; };

async function detail(id) {
  stopTimer();
  let wasLive = null;
  const draw = async () => {
    const j = await call("GET", "/api/jobs/" + id);
    const pct = j.progress_total ? Math.round(100 * j.progress_done / j.progress_total) : 0;
    const s = j.summary || {}, live = j.status === "running" || j.status === "queued";
    if (wasLive === true && !live) { show("jobs", id); return; }      // it just finished: redraw the list too
    wasLive = live;
    Q("#jd").innerHTML = `<div class="card"><div class="row" style="justify-content:space-between"><h3 style="margin:0">Job #${j.id} · ${E(j.kind)} ${bdg(j.status, statusCls[j.status])}</h3>
      ${live ? `<button id="j-cancel">Cancel</button>` : ""}</div>
      ${j.progress_total ? `<progress max="100" value="${pct}" style="width:100%;margin:10px 0"></progress><div class="small muted">${j.progress_done} of ${j.progress_total} PRs · ${(j.input_tokens || 0).toLocaleString()} tokens in, ${(j.output_tokens || 0).toLocaleString()} out · ${money(j.cost_usd)}</div>` : ""}
      ${j.error ? `<div class="note bad">${E(j.error)}</div>` : ""}
      <div class="small muted" style="margin:8px 0">${E(describe(j))} · by ${E(j.created_by || "?")} · ${when(j.created_at)}${j.finished_at ? " → " + when(j.finished_at) : ""}</div>
      ${j.kind === "try" ? tryResult(s) : refreshSummary(s)}
      <details ${live ? "open" : ""}><summary>Log (${j.events.length})</summary><pre class="log">${j.events.map(e => `${E(e.ts.slice(11, 19))} ${e.level === "info" ? "" : "[" + E(e.level) + "] "}${E(e.message)}`).join("\n")}</pre></details></div>`;
    const c = Q("#j-cancel"); if (c) c.onclick = guard(async () => { await call("POST", `/api/jobs/${id}/cancel`); toast("Cancel requested"); draw(); });
    if (!live) stopTimer();
  };
  await draw().catch(e => toast(e.message, true));
  timer = setInterval(() => draw().catch(() => {}), 2000);
}
function refreshSummary(s) {
  if (!s || !s.repos) return "";
  const col = Object.entries(s.collect || {});
  return `${s.stopped ? `<div class="note">${E(s.stopped)}</div>` : ""}
    <div class="kpis"><div class="kpi"><div class="v">${s.saved ?? 0}</div><div class="l">scored</div></div><div class="kpi"><div class="v">${(s.failed || []).length}</div><div class="l">failed</div></div>
    <div class="kpi"><div class="v">${(s.repos || []).length}</div><div class="l">repos</div></div></div>
    ${col.length ? `<table><thead><tr><th>Repo</th><th class="n">New</th><th class="n">Updated</th><th class="n">Unchanged</th><th class="n">Shared PRs</th></tr></thead><tbody>${col.map(([n, c]) => `<tr><td>${E(n)}</td><td class="n">${c.new}</td><td class="n">${c.updated}</td><td class="n">${c.unchanged}</td><td class="n">${c.shared_prs ?? ""}</td></tr>`).join("")}</tbody></table>` : ""}
    ${Object.entries(s.failed_repos || {}).map(([n, m]) => `<div class="note bad"><b>${E(n)}</b>: ${E(m)}</div>`).join("")}
    ${(s.failed || []).slice(0, 20).map(f => `<div class="note bad"><b>${E(f.pr)}</b>: ${E(f.error)}</div>`).join("")}`;
}
function tryResult(s) {
  if (!s || !s.results) return "";
  const rows = [...Object.entries(s.results).map(([k, r]) => [k === "draft" ? "Draft skill (unsaved)" : "Active skill", r]), ...(s.stored ? [["Stored score", {scores: {commit_msg: s.stored.commit_msg, code_quality: s.stored.code_quality, validation: s.stored.validation, review_response: s.stored.review_response}, overall: s.stored.overall, model: s.stored.model || s.stored.engine, skill_hash: s.stored.skill_hash}]] : [])];
  return `<table><thead><tr><th></th><th class="n">Commits</th><th class="n">Code</th><th class="n">Validation</th><th class="n">Review resp.</th><th class="n">Overall</th><th>Model</th><th>Skill</th><th class="n">Cost</th></tr></thead><tbody>
    ${rows.map(([l, r]) => r.error ? `<tr><td>${E(l)}</td><td colspan="8">${E(r.error)}</td></tr>` : `<tr><td><b>${E(l)}</b></td>${["commit_msg", "code_quality", "validation", "review_response"].map(k => `<td class="n">${score(r.scores[k])}</td>`).join("")}<td class="n"><b>${score(r.overall)}</b></td><td>${E(r.model || "")}</td><td><code>${E(r.skill_hash || "")}</code></td><td class="n">${money(r.cost_usd)}</td></tr>`).join("")}</tbody></table>
    ${Object.entries(s.results).filter(([, r]) => r.summary).map(([k, r]) => `<div class="card inner"><b>${k === "draft" ? "Draft" : "Active"} says:</b> ${E(r.summary)}
      <ul class="tp">${(r.talking_points || []).map(t => `<li>${E(t)}</li>`).join("")}</ul>
      <div class="small muted">${(r.code_issues || []).length} code issue(s): ${(r.code_issues || []).slice(0, 6).map(i => `${E(i.severity)} ${E(i.file)}${i.line ? ":" + i.line : ""}`).join(" · ")}</div></div>`).join("")}`;
}

/* ================================================================== Repos */
async function repos() {
  const [rows, st] = await Promise.all([call("GET", "/api/repos"), call("GET", "/api/settings")]);
  const sc = st.schedule || {};
  main().innerHTML = `<div class="card"><h3>Tracked repositories</h3>
    <p class="muted small">Tracked repos are what scheduled runs refresh, and the default when a run has no repos selected. Pick and add them from the <b>Run</b> screen, or type one here.</p>
    <div class="row"><input id="t-add" placeholder="owner/name" style="width:240px"><button id="t-addb">Track</button><button id="t-run" class="primary">Run for tracked repos…</button></div>
    <div class="scroll" style="margin-top:10px"><table><thead><tr><th>Repository</th><th class="n">PRs</th><th>Last collected</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr><td><b>${E(r.full_name)}</b> ${r.enabled ? "" : bdg("paused")}</td><td class="n">${r.prs}</td><td>${when(r.last_collected_at)}</td>
      <td class="nw"><button data-pause="${E(r.full_name)}" data-en="${r.enabled ? 0 : 1}">${r.enabled ? "Pause" : "Resume"}</button> <button data-rm="${E(r.full_name)}">Remove</button></td></tr>`).join("") || `<tr><td colspan="4" class="empty">Nothing tracked yet.</td></tr>`}</tbody></table></div></div>
    <div class="card"><h3>Scheduled refresh</h3><p class="muted small">Runs a collect + score on the tracked repos by itself. Leave it off to run only by hand.</p>
    <div class="row"><label class="chk"><input type="checkbox" id="s-on" ${sc.enabled ? "checked" : ""}> enabled</label>
      every <input id="s-every" type="number" min="1" value="${sc.every_hours || 24}" style="width:70px"> hours · last <input id="s-months" type="number" min="1" value="${sc.months || 1}" style="width:60px"> month(s) ·
      scoring <select id="s-score"><option value="llm" ${sc.score !== "heuristic" && sc.score !== "none" ? "selected" : ""}>LLM</option><option value="heuristic" ${sc.score === "heuristic" ? "selected" : ""}>rule-based</option><option value="none" ${sc.score === "none" ? "selected" : ""}>collect only</option></select>
      · spend cap $<input id="s-usd" type="number" min="0.01" step="0.01" value="${sc.max_usd || ""}" placeholder="none" style="width:80px"> <button id="s-save">Save</button></div></div>`;
  const post = guard(async (b) => { await call("POST", "/api/repos", b); show("repos"); });
  Q("#t-addb").onclick = () => post({full_name: Q("#t-add").value.trim()});
  QA("[data-pause]").forEach(b => b.onclick = () => post({full_name: b.dataset.pause, enabled: !!+b.dataset.en}));
  QA("[data-rm]").forEach(b => b.onclick = guard(async () => { if (confirm(`Stop tracking ${b.dataset.rm}? Collected data stays.`)) { await call("DELETE", "/api/repos/" + b.dataset.rm); show("repos"); } }));
  Q("#t-run").onclick = () => { Admin.preset = rows.filter(r => r.enabled).map(r => r.full_name); show("run"); };
  Q("#s-save").onclick = guard(async () => {
    await call("PUT", "/api/settings", {schedule: {enabled: Q("#s-on").checked, every_hours: +Q("#s-every").value, months: +Q("#s-months").value, score: Q("#s-score").value, max_usd: Q("#s-usd").value || null}});
    toast("Schedule saved");
  });
}

/* ================================================================== Skill & models */
async function skillView(tab) {
  tab = typeof tab === "string" ? tab : "routing";
  const [rt, sk] = await Promise.all([call("GET", "/api/routing"), call("GET", "/api/skill")]);
  let rules = JSON.parse(JSON.stringify(rt.rules));
  let draft = JSON.parse(JSON.stringify(sk.files)), cur = "SKILL.md", dirty = false, tried = null;
  const modelOpt = m => Object.entries(rt.models).map(([k, v]) => `<option value="${E(k)}" ${k === m ? "selected" : ""}>${E(k)} — ${E(v.label.replace(/ \(.*/, ""))} ($${v.input}/$${v.output} per M)</option>`).join("");
  const opts = (arr, v) => arr.map(x => `<option ${x === v ? "selected" : ""}>${E(x)}</option>`).join("");
  const shell = () => {
    main().innerHTML = `<div class="tabs"><button data-st="routing">Models & routing</button><button data-st="skill">Skill & agents</button><button data-st="try">Try on a PR</button></div><div id="sv"></div>`;
    QA("[data-st]").forEach(b => { b.classList.toggle("on", b.dataset.st === tab); b.onclick = () => { tab = b.dataset.st; shell(); }; });
    ({routing: routingTab, skill: skillTab, try: tryTab})[tab]();
  };

  function routingTab() {
    const tri = v => v === true ? "yes" : v === false ? "no" : "";
    Q("#sv").innerHTML = `<div class="card"><h3>Which model scores which PR</h3>
      <p class="muted small">Rules are checked top to bottom; the <b>first match wins</b>. The last rule must have no conditions: it is the default. Cheaper models for small or routine PRs, Opus for the risky ones.
      ${rt.custom ? bdg("customised", "s-ok") : bdg("defaults")}</p>
      <div id="rules"></div>
      <div class="row"><button id="rl-add">Add rule</button><button id="rl-save" class="primary">Save routing</button><button id="rl-reset">Reset to defaults</button></div></div>
      <div class="card"><h3>Test: which rule would a PR like this hit?</h3><div class="row">
        lines <input id="tt-lines" type="number" value="300" style="width:90px"> files <input id="tt-files" type="number" value="5" style="width:70px">
        <label class="chk"><input type="checkbox" id="tt-shared"> shared</label><label class="chk"><input type="checkbox" id="tt-trunc"> truncated diff</label>
        repo <input id="tt-repo" placeholder="owner/name" style="width:180px"> paths <input id="tt-paths" placeholder="src/auth/login.py, …" style="width:220px"><button id="tt-go">Test</button></div><div id="tt-out" class="small" style="margin-top:8px"></div></div>`;
    const drawRules = () => {
      Q("#rules").innerHTML = rules.map((r, i) => { const w = r.when || {}; return `<div class="card inner" data-i="${i}">
        <div class="row"><b>${i + 1}.</b><input data-f="name" value="${E(r.name)}" style="flex:1;min-width:180px"><button data-act="up" ${i === 0 ? "disabled" : ""}>↑</button><button data-act="down" ${i === rules.length - 1 ? "disabled" : ""}>↓</button><button data-act="del" ${rules.length === 1 ? "disabled" : ""}>Delete</button></div>
        <div class="grid4"><div class="field"><label>Lines ≥</label><input data-w="min_lines" type="number" value="${w.min_lines ?? ""}"></div><div class="field"><label>Lines ≤</label><input data-w="max_lines" type="number" value="${w.max_lines ?? ""}"></div>
          <div class="field"><label>Files ≥</label><input data-w="min_files" type="number" value="${w.min_files ?? ""}"></div><div class="field"><label>Files ≤</label><input data-w="max_files" type="number" value="${w.max_files ?? ""}"></div>
          <div class="field"><label>Shared PR</label><select data-w="shared"><option value="">any</option><option ${tri(w.shared) === "yes" ? "selected" : ""}>yes</option><option ${tri(w.shared) === "no" ? "selected" : ""}>no</option></select></div>
          <div class="field"><label>Diff truncated</label><select data-w="truncated"><option value="">any</option><option ${tri(w.truncated) === "yes" ? "selected" : ""}>yes</option><option ${tri(w.truncated) === "no" ? "selected" : ""}>no</option></select></div>
          <div class="field"><label>State</label><select data-w="state"><option value="">any</option>${opts(["merged", "open", "closed"], w.state)}</select></div>
          <div class="field"><label>Scored before by</label><select data-w="prior_engine"><option value="">any</option>${opts(["none", "heuristic", "claude-code", "anthropic-api"], w.prior_engine)}</select></div>
          <div class="field" style="grid-column:span 2"><label>Repos (comma separated, * allowed)</label><input data-w="repo" value="${E((w.repo || []).join(", "))}"></div>
          <div class="field" style="grid-column:span 2"><label>Changed path matches (regex)</label><input data-w="path_regex" value="${E(w.path_regex || "")}"></div></div>
        <div class="row" style="margin-top:8px"><b>→ score with</b>
          <select data-f="agent">${opts(rt.agents, r.agent)}</select><select data-f="model">${modelOpt(r.model)}</select> effort <select data-f="effort">${opts(rt.efforts, r.effort)}</select></div></div>`; }).join("");
      QA("[data-act]").forEach(b => b.onclick = () => { collect(); const i = +b.closest("[data-i]").dataset.i;
        if (b.dataset.act === "del") rules.splice(i, 1); else { const j = i + (b.dataset.act === "up" ? -1 : 1); [rules[i], rules[j]] = [rules[j], rules[i]]; } drawRules(); });
    };
    const collect = () => { rules = QA("#rules [data-i]").map(c => {
      const when = {}; QA("[data-w]", c).forEach(el => { const k = el.dataset.w, v = el.value.trim(); if (!v) return;
        when[k] = ["min_lines", "max_lines", "min_files", "max_files"].includes(k) ? +v : ["shared", "truncated"].includes(k) ? v === "yes" : k === "repo" ? v.split(",").map(x => x.trim()).filter(Boolean) : v; });
      const f = k => Q(`[data-f=${k}]`, c).value; return {name: f("name"), when, agent: f("agent"), model: f("model"), effort: f("effort")}; }); };
    Q("#rl-add").onclick = () => { collect(); rules.splice(rules.length - 1, 0, {name: "New rule", when: {min_lines: 300}, agent: "scorer", model: Object.keys(rt.models)[1] || Object.keys(rt.models)[0], effort: "high"}); drawRules(); };
    Q("#rl-save").onclick = guard(async () => { collect(); const r = await call("PUT", "/api/routing", {rules}); rules = r.rules; rt.custom = true; toast("Routing saved: used by the next run"); routingTab(); });
    Q("#rl-reset").onclick = guard(async () => { if (!confirm("Go back to the default routing rules?")) return; await call("POST", "/api/routing/reset"); rules = JSON.parse(JSON.stringify(rt.default_rules)); rt.custom = false; routingTab(); });
    Q("#tt-go").onclick = guard(async () => { const r = await call("POST", "/api/routing/test", {lines: +Q("#tt-lines").value, files: +Q("#tt-files").value, shared: Q("#tt-shared").checked, truncated: Q("#tt-trunc").checked, repo: Q("#tt-repo").value, paths: Q("#tt-paths").value.split(",").map(x => x.trim()).filter(Boolean)});
      Q("#tt-out").innerHTML = `Rule <b>${r.index + 1}. ${E(r.rule.name)}</b> → ${E(r.rule.agent)} on <b>${E(r.rule.model)}</b>, effort ${E(r.rule.effort)} <span class="muted">(saved rules only: save first to test edits)</span>`; });
    drawRules();
  }

  function skillTab() {
    const names = Object.keys(draft).sort((a, b) => a === "SKILL.md" ? -1 : b === "SKILL.md" ? 1 : a.localeCompare(b));
    if (!draft[cur]) cur = "SKILL.md";
    Q("#sv").innerHTML = `<div class="card"><div class="row" style="justify-content:space-between"><h3 style="margin:0">Scoring skill ${bdg(sk.hash, sk.source === "edited" ? "s-ok" : "s-good")} <span class="small muted">${sk.source === "edited" ? "edited version in use" : "repo default in use"}</span></h3>
      <span class="small muted">${dirty ? "unsaved changes" : ""}</span></div>
      <p class="muted small"><b>SKILL.md</b> is the procedure, <b>rubric.md</b> the standards, and each <b>agents/*.md</b> defines a scorer (front matter: mode <code>single</code> or <code>agentic</code>, effort, max_turns, max_diff_chars). Which agent and model run for a PR is chosen on <b>Models & routing</b>. Saving makes a new version; existing scores keep the version that made them.</p>
      <div class="split"><div class="files">${names.map(n => `<button data-f="${E(n)}" class="${n === cur ? "on" : ""}">${E(n)}</button>`).join("")}
        <button id="sk-addag">+ Add agent</button>${cur.startsWith("agents/") && cur !== "agents/scorer.md" ? `<button id="sk-delag" class="danger">Delete ${E(cur.split("/")[1])}</button>` : ""}</div>
        <div style="flex:1;min-width:0"><textarea id="sk-ed" class="ed" spellcheck="false">${E(draft[cur])}</textarea></div></div>
      <div class="row" style="margin-top:10px"><input id="sk-note" placeholder="What did you change? (optional)" style="flex:1;min-width:220px"><button id="sk-save" class="primary">Save as new version</button><button id="sk-reset">Back to repo version</button></div></div>
      <div class="card"><h3>Versions</h3><table><thead><tr><th>Version</th><th>Saved</th><th>By</th><th>Note</th><th></th></tr></thead><tbody>
        <tr><td><code>${E(sk.repo_hash)}</code> ${sk.hash === sk.repo_hash ? bdg("in use", "s-good") : ""}</td><td>repo files</td><td></td><td>shipped with the app</td><td><button data-load="${E(sk.repo_hash)}">Load in editor</button></td></tr>
        ${sk.versions.map(v => `<tr><td><code>${E(v.hash)}</code> ${v.hash === sk.hash ? bdg("in use", "s-good") : ""}</td><td class="nw">${when(v.created_at)}</td><td>${E(v.created_by || "")}</td><td>${E(v.note || "")}</td>
          <td class="nw"><button data-load="${E(v.hash)}">Load in editor</button> ${v.hash === sk.hash ? "" : `<button data-act="${E(v.hash)}">Make active</button>`}</td></tr>`).join("")}</tbody></table></div>`;
    Q("#sk-ed").oninput = e => { draft[cur] = e.target.value; dirty = true; };
    QA("[data-f]").forEach(b => b.onclick = () => { cur = b.dataset.f; skillTab(); });
    Q("#sk-addag").onclick = () => { const n = (prompt("Agent name (lowercase letters, digits, dashes):") || "").trim();
      if (!/^[a-z0-9][a-z0-9-]{0,40}$/.test(n)) { if (n) toast("use lowercase letters, digits and dashes", true); return; }
      draft[`agents/${n}.md`] = `---\nname: ${n}\ndescription: What this agent is for.\nmode: single\neffort: high\nmax_diff_chars: 600000\n---\nExtra instructions for this agent.\n`; cur = `agents/${n}.md`; dirty = true; skillTab(); };
    const del = Q("#sk-delag"); if (del) del.onclick = () => { if (confirm(`Remove ${cur} from the draft?`)) { delete draft[cur]; cur = "SKILL.md"; dirty = true; skillTab(); } };
    Q("#sk-save").onclick = guard(async () => { const r = await call("PUT", "/api/skill", {files: draft, note: Q("#sk-note").value}); toast(`Saved as ${r.hash}: used by the next run`); show("skill", "skill"); });
    Q("#sk-reset").onclick = guard(async () => { if (!confirm("Use the files that ship with the app again? Your saved versions stay in the list.")) return; await call("POST", "/api/skill/reset"); show("skill", "skill"); });
    QA("[data-load]").forEach(b => b.onclick = guard(async () => { const r = await call("GET", "/api/skill/version/" + b.dataset.load); draft = r.files; cur = "SKILL.md"; dirty = true; skillTab(); toast("Loaded into the editor (not saved yet)"); }));
    QA("[data-act]").forEach(b => b.onclick = guard(async () => { await call("POST", "/api/skill/activate", {hash: b.dataset.act}); show("skill", "skill"); }));
  }

  async function tryTab() {
    const meta = await call("GET", "/api/meta");
    Q("#sv").innerHTML = `<div class="card"><h3>Try the skill on one PR</h3><p class="muted small">Scores one already-collected PR and shows the result next to the stored score. Nothing is saved. Tick the box to use the text currently in the editor (unsaved) and compare it with the active version. This calls the API, so it costs a little.</p>
      <div class="row"><select id="tr-repo">${meta.repos.map(r => `<option>${E(r)}</option>`).join("")}</select> PR # <input id="tr-n" type="number" min="1" style="width:90px">
        <select id="tr-model"><option value="">model: by routing rules</option>${Object.keys(rt.models).map(m => `<option>${E(m)}</option>`).join("")}</select>
        <select id="tr-agent"><option value="">agent: by routing rules</option>${rt.agents.map(a => `<option>${E(a)}</option>`).join("")}</select>
        <select id="tr-eff">${opts(rt.efforts, "high")}</select></div>
      <label class="chk" style="margin-top:8px"><input type="checkbox" id="tr-draft" ${dirty ? "checked" : ""}> use the editor's unsaved text, and compare with the active version</label>
      <div class="row"><button id="tr-go" class="primary">Score it</button><span id="tr-msg" class="muted small"></span></div><div id="tr-out" style="margin-top:10px"></div></div>`;
    Q("#tr-go").onclick = guard(async () => {
      const body = {kind: "try", repo: Q("#tr-repo").value, number: +Q("#tr-n").value, compare_active: true};
      if (Q("#tr-model").value || Q("#tr-agent").value) { if (!(Q("#tr-model").value && Q("#tr-agent").value)) throw new Error("pick both a model and an agent, or leave both on routing"); body.route = {model: Q("#tr-model").value, agent: Q("#tr-agent").value, effort: Q("#tr-eff").value}; }
      if (Q("#tr-draft").checked) body.draft = draft;
      const {id} = await call("POST", "/api/jobs", body);
      Q("#tr-msg").textContent = "Scoring…";
      stopTimer();
      timer = setInterval(async () => { const j = await call("GET", "/api/jobs/" + id).catch(() => null); if (!j || j.status === "queued" || j.status === "running") return; stopTimer();
        Q("#tr-msg").textContent = ""; Q("#tr-out").innerHTML = j.error ? `<div class="note bad">${E(j.error)}</div>` : tryResult(j.summary); }, 1500);
    });
  }
  shell();
}

/* ================================================================== Access & settings */
async function access() {
  const [st, u] = await Promise.all([call("GET", "/api/settings"), call("GET", "/api/users")]);
  const flag = (ok, label, hint, yes = "set", no = "not set") => `<tr><td>${E(label)}</td><td>${ok ? bdg(yes, "s-good") : bdg(no, "s-bad")}</td><td class="small muted">${E(hint)}</td></tr>`;
  main().innerHTML = `<div class="card"><h3>Server configuration <span class="small muted">(read-only: keys are environment variables, never entered or shown here)</span></h3>
    ${st.problems.map(p => `<div class="note bad">${E(p)}</div>`).join("")}
    <table><tbody>${flag(st.github_token, "GITHUB_TOKEN", "read-only token used to collect PRs")}${flag(st.anthropic_key, "ANTHROPIC_API_KEY", "needed for LLM scoring")}${flag(st.sdk_installed, "anthropic Python package", "pip install -r requirements-llm.txt", "installed", "missing")}
    ${flag(st.org, "GITHUB_ORG", st.org || "default organisation for the repo picker and org-member check")}${flag(st.oauth, "GitHub sign-in (OAuth)", "GITHUB_OAUTH_CLIENT_ID / _SECRET / PUBLIC_URL")}${flag(st.viewer_password, "VIEWER_PASSWORD", "shared password for the office TV")}${flag(st.session_secret, "AUDIT_SESSION_SECRET", "keeps logins valid across restarts")}</tbody></table>
    <div class="small muted" style="margin-top:6px">Active skill <code>${E(st.skill_hash)}</code> (${E(st.skill_source)}) · ${st.concurrency} scoring call(s) at a time (AUDIT_CONCURRENCY) ${st.readonly ? " · server is read-only" : ""}</div></div>
  <div class="card"><h3>People and access</h3>
    <p class="muted small">A GitHub login is matched to a person by GitHub id, a verified email, or a row here. Roles: <b>admin</b> (everything), <b>viewer</b> (everyone's data, read-only), <b>self</b> (only their own data).
    Logins in ADMIN_USERS / VIEWER_USERS always work. Org members who are also collected people get <b>self</b> automatically. Add a row to give someone outside the org access, to map an email, or to switch someone off.</p>
    <div class="scroll"><table><thead><tr><th>GitHub login</th><th>GitHub id</th><th>Email</th><th>Name</th><th>Role</th><th>Person (data)</th><th>Active</th><th></th></tr></thead><tbody>
    ${u.app_users.map(r => `<tr><td>${E(r.gh_login || "")}</td><td>${r.gh_id ?? ""}</td><td>${E(r.email || "")}</td><td>${E(r.name || "")}</td><td>${bdg(r.role, r.role === "admin" ? "s-bad" : r.role === "viewer" ? "s-ok" : "s-na")}</td>
      <td>${E(r.uid ? (u.people.find(p => p.uid === r.uid) || {}).login || r.uid : "")}</td><td>${r.active ? "yes" : bdg("off", "s-bad")}</td>
      <td class="nw"><button data-edit="${r.id}">Edit</button> <button data-del="${r.id}">Delete</button></td></tr>`).join("") || `<tr><td colspan="8" class="empty">No rows yet.</td></tr>`}</tbody></table></div>
    <div class="card inner" style="margin-top:12px"><b id="u-title">Add a person</b><div class="grid4" style="margin-top:8px">
      <div class="field"><label>GitHub login</label><input id="u-login"></div><div class="field"><label>GitHub id (optional)</label><input id="u-ghid" type="number"></div>
      <div class="field"><label>Email (optional)</label><input id="u-email"></div><div class="field"><label>Name</label><input id="u-name"></div>
      <div class="field"><label>Role</label><select id="u-role"><option>self</option><option>viewer</option><option>admin</option></select></div>
      <div class="field" style="grid-column:span 2"><label>Person whose data they see (self)</label><select id="u-uid"><option value="">— match automatically —</option>${u.people.map(p => `<option value="${E(p.uid)}">${E(p.login || p.name || p.uid)}${p.name && p.login ? " (" + E(p.name) + ")" : ""}</option>`).join("")}</select></div>
      <div class="field"><label>Active</label><select id="u-active"><option value="1">yes</option><option value="0">no (blocked)</option></select></div></div>
      <div class="row"><button id="u-save" class="primary">Save</button><button id="u-clear">Clear form</button></div></div></div>`;
  let editId = null;
  const fill = r => { editId = r ? r.id : null; Q("#u-title").textContent = r ? "Edit person" : "Add a person"; Q("#u-login").value = r?.gh_login || ""; Q("#u-ghid").value = r?.gh_id ?? ""; Q("#u-email").value = r?.email || ""; Q("#u-name").value = r?.name || "";
    Q("#u-role").value = r?.role || "self"; Q("#u-uid").value = r?.uid || ""; Q("#u-active").value = r ? r.active : 1; };
  QA("[data-edit]").forEach(b => b.onclick = () => { fill(u.app_users.find(r => r.id === +b.dataset.edit)); scrollTo(0, document.body.scrollHeight); });
  QA("[data-del]").forEach(b => b.onclick = guard(async () => { if (confirm("Delete this row? They lose access unless env lists or org rules still apply.")) { await call("DELETE", "/api/users/" + b.dataset.del); show("access"); } }));
  Q("#u-clear").onclick = () => fill(null);
  Q("#u-save").onclick = guard(async () => { await call("POST", "/api/users", {id: editId, gh_login: Q("#u-login").value, gh_id: Q("#u-ghid").value, email: Q("#u-email").value, name: Q("#u-name").value, role: Q("#u-role").value, uid: Q("#u-uid").value, active: +Q("#u-active").value}); toast("Saved"); show("access"); });
}

const views = {run, jobs, repos, skill: skillView, access};
window.Admin = {show, stop: stopTimer, preset: null};
})();
