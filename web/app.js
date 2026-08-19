/* BI TestPilot AI — SPA controller.
   Three screens, one job: upload -> stream progress over SSE -> show results. */

const $ = (id) => document.getElementById(id);
const api = (path, opts) => fetch(path, opts);

const state = {
  projectId: null,
  jobId: null,
  pbixFile: null,
  dataFile: null,
  dsMode: "file",     // "file" | "sql"
  dsReady: false,
  source: null,       // EventSource
  projectName: null,  // set when created through the project form
  projectEnv: null,
};

/* ── screens ─────────────────────────────────────────────── */
function show(n) {
  document.querySelectorAll(".screen").forEach((s, i) =>
    s.classList.toggle("is-visible", i === n - 1));
  document.querySelectorAll(".step").forEach((s) => {
    const step = Number(s.dataset.step);
    s.classList.toggle("is-active", step === n);
    s.classList.toggle("is-done", step < n);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* FastAPI reports a validation failure with `detail` as an array of objects,
   so passing it straight to Error() renders "[object Object]" and tells the
   user nothing. Flatten whatever shape arrived into a sentence. */
function errorText(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      if (typeof d === "string") return d;
      const where = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
      return (where ? where + ": " : "") + (d.msg || JSON.stringify(d));
    });
    return parts.join("; ") || fallback;
  }
  return detail.msg || detail.detail || fallback;
}

function setStatus(el, text, kind = "") {
  el.textContent = typeof text === "string" ? text : errorText(text, String(text));
  el.className = "status" + (kind ? " " + kind : "") +
                 (el.id === "setup-status" ? " center" : "");
}

/* ── file pickers (click + drag/drop) ────────────────────── */
function wireDrop(zoneId, inputId, nameId, onPick) {
  const zone = $(zoneId), input = $(inputId), name = $(nameId);
  const accept = (file) => {
    if (!file) return;
    name.textContent = `${file.name} · ${(file.size / 1048576).toFixed(1)} MB`;
    zone.classList.add("has-file");
    onPick(file);
  };
  input.addEventListener("change", () => accept(input.files[0]));
  ["dragenter", "dragover"].forEach((e) =>
    zone.addEventListener(e, (ev) => {
      ev.preventDefault(); zone.classList.add("is-over");
    }));
  ["dragleave", "drop"].forEach((e) =>
    zone.addEventListener(e, () => zone.classList.remove("is-over")));
  zone.addEventListener("drop", (ev) => {
    ev.preventDefault(); accept(ev.dataTransfer.files[0]);
  });
}

wireDrop("pbix-drop", "pbix-input", "pbix-name", (f) => {
  state.pbixFile = f; refreshAnalyze();
});
wireDrop("data-drop", "data-input", "data-name", (f) => {
  state.dataFile = f; state.dsReady = true; refreshAnalyze();
  setStatus($("ds-status"), "");
});

/* ── datasource tabs ─────────────────────────────────────── */
document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
    tab.classList.add("is-active");
    state.dsMode = tab.dataset.ds;
    $("ds-file").classList.toggle("is-visible", state.dsMode === "file");
    $("ds-sql").classList.toggle("is-visible", state.dsMode === "sql");
    state.dsReady = state.dsMode === "file" ? !!state.dataFile : false;
    setStatus($("ds-status"), "");
    refreshAnalyze();
  }));

$("sql-auth").addEventListener("change", (e) => {
  // Windows auth ignores username/password.
  $("sql-creds").style.display =
    e.target.value === "SQL Login" ? "grid" : "none";
});

function refreshAnalyze() {
  $("btn-analyze").disabled = !(state.pbixFile && state.dsReady);
}

/* ── LLM settings (machine-level, inherited by every run) ──
   The browser only ever knows provider + model. Endpoint, credentials and
   token budget are resolved on the server, so no secret exists in this file,
   in the DOM, or in localStorage. */

//: The last SAVED config. The badge reflects this, not the form, so unsaved
//  edits can never be mistaken for the settings a run will actually use.
let savedLlm = null;

function applyLlm(cfg) {
  savedLlm = cfg;
  refreshLlmBadge();
  if (!cfg.is_configured) $("llm-card").open = true;
}

function llmIsDirty() {
  if (!savedLlm) return false;
  return (
    $("llm-provider").value !== savedLlm.provider ||
    $("llm-model").value !== (savedLlm.model || "")
  );
}

function refreshLlmBadge() {
  const badge = $("llm-badge");
  if (llmIsDirty()) {
    badge.textContent = "Unsaved — click Save";
    badge.className = "badge dirty";
    return;
  }
  const cfg = savedLlm || {};
  badge.textContent = cfg.is_configured
    ? `${cfg.provider}${cfg.model ? " · " + cfg.model : ""}` : "Not configured";
  badge.className = "badge" + (cfg.is_configured ? " ok" : "");
}

["llm-provider", "llm-model"].forEach((id) =>
  $(id).addEventListener("change", () => { refreshLlmBadge(); refreshBudget(); }));

/* Today's token spend for the *selected* provider and model.

   Free to call — the server answers from its local ledger and never contacts
   the provider — so it is refreshed on every change rather than cached. The
   selection is passed explicitly because quota is granted per model: showing
   the saved model's headroom while another is selected would report tokens
   the next run will not have. */
async function refreshBudget() {
  const box = $("llm-budget");
  const provider = $("llm-provider").value;
  const model = $("llm-model").value;
  if (!provider || !model) { box.hidden = true; return; }
  try {
    const res = await api("/api/settings/llm/budget?provider="
      + encodeURIComponent(provider) + "&model=" + encodeURIComponent(model));
    const b = await res.json();
    if (!res.ok || !b.configured) { box.hidden = true; return; }
    renderBudget(b);
  } catch {
    // A budget we cannot read must never block configuring the model.
    box.hidden = true;
  }
}

function renderBudget(b) {
  const box = $("llm-budget");
  const n = (v) => Number(v || 0).toLocaleString();
  box.hidden = false;

  if (!b.enforced) {
    // No cap configured for this provider: report the spend, claim no limit.
    box.className = "budget";
    $("llm-budget-figure").textContent = `${n(b.used)} used today`;
    $("llm-budget-fill").style.width = "0%";
    $("llm-budget-note").textContent =
      `${b.calls || 0} call(s) · no daily cap configured for this provider`;
    return;
  }

  const pct = Math.min(100, Math.round((b.used / b.limit) * 100));
  // Banded rather than a gradient: the only decision this drives is whether to
  // start a run now or wait for the reset.
  box.className = "budget" + (b.remaining <= 0 ? " spent"
    : b.remaining < b.limit * 0.15 ? " low" : "");
  $("llm-budget-figure").textContent =
    `${n(b.remaining)} left of ${n(b.limit)}`;
  $("llm-budget-fill").style.width = pct + "%";
  const resets = (b.resets_at || "").slice(11, 16);
  $("llm-budget-note").textContent = b.remaining <= 0
    ? `Used up — analysis will not start until the budget resets at ${resets}.`
    : `${n(b.used)} used across ${b.calls || 0} call(s) · resets ${resets}`;
}

/* Load the models for a provider and populate the dropdown. Called on load and
   whenever the provider changes — there is no Fetch button. */
async function loadModels(provider, preferred) {
  const sel = $("llm-model");
  sel.innerHTML = '<option value="">Loading…</option>';
  sel.disabled = true;
  try {
    const res = await api(
      `/api/settings/llm/providers/${encodeURIComponent(provider)}/models`);
    const body = await res.json();
    if (!res.ok) throw new Error(errorText(body.detail, "Could not load models"));

    const models = body.models || [];
    if (!models.length) {
      sel.innerHTML = '<option value="">No models available</option>';
      setStatus($("llm-status"),
        `Unable to load ${provider} models. Check the backend ${provider} configuration.`,
        "err");
      return;
    }
    sel.innerHTML = models
      .map((m) => `<option value="${m.id}">${m.label}</option>`).join("");
    // Keep the saved model when it is still offered; otherwise the default.
    const wanted = models.some((m) => m.id === preferred) ? preferred : body.default;
    sel.value = wanted || models[0].id;
    sel.disabled = false;
    setStatus($("llm-status"), body.notice || "");
  } catch (err) {
    sel.innerHTML = '<option value="">Unavailable</option>';
    setStatus($("llm-status"), err.message, "err");
  } finally {
    sel.disabled = false;
    refreshLlmBadge();
  }
}

async function loadLlm() {
  const [provRes, cfgRes] = await Promise.all([
    api("/api/settings/llm/providers"),
    api("/api/settings/llm"),
  ]);
  if (!provRes.ok || !cfgRes.ok) {
    setStatus($("llm-status"), "Could not load AI model configuration.", "err");
    return;
  }
  const provs = await provRes.json();
  const cfg = await cfgRes.json();

  // A provider with no server-side key is still selectable, but says so.
  $("llm-provider").innerHTML = (provs.providers || [])
    .map((p) => `<option value="${p.id}">${p.label}${p.configured ? "" : " (no key on server)"}</option>`)
    .join("");
  $("llm-provider").value = cfg.provider || provs.selected;
  await loadModels($("llm-provider").value, cfg.model);
  applyLlm(cfg);
  refreshBudget();
}

// Changing the provider always reloads that provider's models.
$("llm-provider").addEventListener("change", async () => {
  $("llm-model").innerHTML = "";
  await loadModels($("llm-provider").value, "");
});

function llmPayload() {
  return { provider: $("llm-provider").value, model: $("llm-model").value };
}

async function postLlm(path) {
  try {
    const res = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(llmPayload()),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(errorText(body.detail, "Request failed"));
    return body;
  } catch (err) {
    setStatus($("llm-status"), err.message, "err");
    return null;
  }
}

$("btn-llm-save").addEventListener("click", async () => {
  if (!$("llm-provider").value) {
    return setStatus($("llm-status"), "Select an LLM provider.", "err");
  }
  if (!$("llm-model").value) {
    return setStatus($("llm-status"), "Select a model.", "err");
  }
  setStatus($("llm-status"), "Saving…");
  const body = await postLlm("/api/settings/llm");
  if (!body) return;
  applyLlm(body);
  refreshBudget();
  setStatus($("llm-status"), "AI model configuration saved successfully.", "ok");
});

$("btn-llm-test").addEventListener("click", async () => {
  if (!$("llm-provider").value || !$("llm-model").value) {
    return setStatus($("llm-status"), "Select a provider and model first.", "err");
  }
  setStatus($("llm-status"), "Testing…");
  const body = await postLlm("/api/settings/llm/test");
  if (!body) return;
  // Testing does not persist anything — say so, or a successful test reads
  // as "configured" while runs still use the previously saved provider.
  refreshBudget();          // the test itself costs a few tokens
  const suffix = body.ok && llmIsDirty() ? " Now click Save to use it." : "";
  setStatus($("llm-status"),
    (body.ok ? "✓ " : "✗ ") + body.message + suffix,
    body.ok ? "ok" : "err");
});

/* ── project + uploads ───────────────────────────────────── */
async function ensureProject() {
  if (state.projectId) return state.projectId;
  const name = (state.pbixFile?.name || "Analysis").replace(/\.[^.]+$/, "");
  const res = await api("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // A unique suffix avoids 409 when re-analysing the same file.
    body: JSON.stringify({ name: `${name} ${new Date().toLocaleString()}` }),
  });
  if (!res.ok) throw new Error(errorText((await res.json()).detail, "Could not create project"));
  state.projectId = (await res.json()).id;
  return state.projectId;
}

function sqlPayload() {
  return {
    server: $("sql-server").value.trim(),
    database: $("sql-database").value.trim(),
    auth_mode: $("sql-auth").value,
    username: $("sql-user").value.trim(),
    password: $("sql-pass").value,
    driver: $("sql-driver").value,
  };
}

async function saveDatasource() {
  const pid = await ensureProject();
  if (state.dsMode === "file") {
    const form = new FormData();
    form.append("file", state.dataFile);
    const res = await api(`/api/projects/${pid}/datasource/file`, {
      method: "POST", body: form,
    });
    const body = await res.json();
    if (!res.ok || !body.ok) throw new Error(body.detail || body.message);
    return body.message;
  }
  const res = await api(`/api/projects/${pid}/datasource/sql`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sqlPayload()),
  });
  const body = await res.json();
  if (!res.ok || !body.ok) throw new Error(body.detail || body.message);
  return body.message;
}

$("btn-test").addEventListener("click", async () => {
  setStatus($("ds-status"), "Testing connection…");
  try {
    setStatus($("ds-status"), await saveDatasource(), "ok");
    state.dsReady = true;
  } catch (err) {
    setStatus($("ds-status"), err.message, "err");
    state.dsReady = false;
  }
  refreshAnalyze();
});

/* ── analyze ─────────────────────────────────────────────── */
$("btn-analyze").addEventListener("click", async () => {
  $("btn-analyze").disabled = true;
  try {
    setStatus($("setup-status"), "Creating project…");
    const pid = await ensureProject();

    setStatus($("setup-status"), "Uploading dashboard file…");
    const form = new FormData();
    form.append("files", state.pbixFile);
    const up = await api(`/api/projects/${pid}/pbix`, { method: "POST", body: form });
    const upBody = await up.json();
    if (!up.ok) throw new Error(errorText(upBody.detail, "Upload failed"));
    if (upBody.rejected?.length) throw new Error(upBody.rejected[0].reason);

    setStatus($("setup-status"), "Configuring datasource…");
    await saveDatasource();

    setStatus($("setup-status"), "Starting analysis…");
    const res = await api(`/api/projects/${pid}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Tolerance is fixed at 1% — the backend default. No longer a UI field.
      body: JSON.stringify({ tolerance_pct: 1 }),
    });
    const job = await res.json();
    if (!res.ok) throw new Error(errorText(job.detail, "Could not start analysis"));

    state.jobId = job.job_id;
    startRun();
  } catch (err) {
    setStatus($("setup-status"), err.message, "err");
    $("btn-analyze").disabled = false;
  }
});

/* ── screen 2: live progress over SSE ────────────────────── */
const STAGES = [
  ["EXTRACT_METADATA", "Extracting metadata"],
  ["READ_SCHEMA", "Reading datasource schema"],
  ["EVALUATE_DAX", "Evaluating DAX measures"],
  ["BUILD_CONTEXT", "Building analysis context"],
  ["LLM_ANALYSIS", "Analysing with AI"],
  ["GENERATE_SQL", "Generating SQL"],
  ["EXECUTE_SQL", "Executing SQL and comparing"],
  ["GENERATE_TESTS", "Generating test cases"],
  ["BUILD_REPORT", "Building report"],
];
const ICONS = { running: "◐", done: "✓", skipped: "!", failed: "✕" };

function startRun() {
  show(2);
  $("run-title").textContent = "Running analysis…";
  $("btn-cancel").disabled = false;
  $("stages").innerHTML = STAGES.map(([id, label]) =>
    `<li id="st-${id}"><span class="ico">○</span><span>${label}</span>
     <span class="msg"></span></li>`).join("");

  state.source = new EventSource(`/api/jobs/${state.jobId}/stream`);

  state.source.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    if (e.state) return;                       // final snapshot frame
    const li = $(`st-${e.stage}`);
    if (li) {
      li.className = e.status;
      li.querySelector(".ico").textContent = ICONS[e.status] || "○";
      if (e.status !== "running") li.querySelector(".msg").textContent = e.message;
    }
    $("bar-fill").style.width = e.pct + "%";
    $("bar-pct").textContent = e.pct + "%";
    $("bar-elapsed").textContent = (e.elapsed_ms / 1000).toFixed(1) + "s";
  };

  state.source.addEventListener("done", (ev) => {
    state.source.close();
    finishRun(JSON.parse(ev.data));
  });

  state.source.onerror = () => {
    // Stream closed; fall back to a snapshot poll.
    state.source.close();
    api(`/api/jobs/${state.jobId}`).then((r) => r.json()).then(finishRun);
  };
}

$("btn-cancel").addEventListener("click", async () => {
  $("btn-cancel").disabled = true;
  $("run-title").textContent = "Cancelling…";
  await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
});

/* ── screen 3: results ───────────────────────────────────── */
function stopRun(title, icon, cls, detail) {
  $("run-title").textContent = title;
  if (detail) {
    $("stages").insertAdjacentHTML("beforeend",
      `<li class="${cls}"><span class="ico">${icon}</span><span>${detail}</span></li>`);
  }
  $("btn-cancel").textContent = "Back to setup";
  $("btn-cancel").disabled = false;
  $("btn-cancel").onclick = () => { show(1); $("btn-analyze").disabled = false; };
}

async function finishRun(job) {
  // A run is what actually moves the number, so refresh whatever the outcome:
  // a failed or cancelled run has usually already spent part of the budget.
  refreshBudget();
  if (job.state === "failed") {
    stopRun("Analysis failed", "✕", "failed", job.error);
    return;
  }
  // Without this a cancelled run fell through to the results screen and
  // rendered an all-zero scorecard, which reads as a failed analysis.
  if (job.state === "cancelled") {
    stopRun("Run cancelled", "○", "skipped", "Stopped at your request.");
    return;
  }

  const s = job.summary || {};
  $("m-tests").textContent = s.tests ?? 0;
  $("m-passed").textContent = s.passed ?? 0;
  $("m-failed").textContent = s.failed ?? 0;
  $("m-warnings").textContent = (job.warnings || []).length;
  $("m-time").textContent = ((job.elapsed_ms || 0) / 1000).toFixed(1) + "s";

  const warnBox = $("warn-box");
  warnBox.hidden = !(job.warnings || []).length;
  $("warn-list").innerHTML = (job.warnings || [])
    .map((w) => `<li>${escapeHtml(w)}</li>`).join("");

  const pid = state.projectId;
  $("dl-html").href = `/api/projects/${pid}/report.html`;
  $("dl-pdf").href = `/api/projects/${pid}/report.pdf`;
  $("dl-xlsx").href = `/api/projects/${pid}/report.xlsx`;

  await loadResults();
  show(3);
}

async function loadResults() {
  const res = await api(`/api/projects/${state.projectId}/results`);
  const data = await res.json();
  const rows = data.rows || [];
  $("results-empty").hidden = rows.length > 0;
  // A file datasource has no SQL to show; its proof is the sheet, operation
  // and filters. Relabel the column so the header matches what is in it.
  const anySql = rows.every((r) => {
    const e = (r.source_evidence || r.generated_sql || "").trim();
    return !e || e.toUpperCase().startsWith("SELECT");
  });
  $("evidence-head").textContent = anySql ? "Query used to fetch" : "How it was calculated";

  $("results-body").innerHTML = rows.map((r) => {
    const k = r.status === "Pass" ? "pass" : r.status === "Fail" ? "fail" : "other";
    // The adapter that ran sets source_evidence; a plan's generated_sql
    // survives even when a file datasource never executed it.
    const evidence = (r.source_evidence || "").trim() || r.generated_sql || "";
    return `<tr>
      <td>${escapeHtml(r.test_id)}</td>
      <td>${escapeHtml(r.kpi)}</td>
      <td>${escapeHtml(r.scenario || "—")}</td>
      <td>${escapeHtml(r.dashboard_value || "—")}</td>
      <td class="sql" title="${escapeHtml(evidence)}">${escapeHtml(evidence || "—")}</td>
      <td>${escapeHtml(r.database_value || "—")}</td>
      <td>${escapeHtml(r.difference || "—")}</td>
      <td>${escapeHtml(r.match_type || "—")}</td>
      <td>${r.execution_time_ms ?? "—"}</td>
      <td><span class="pill ${k}">${escapeHtml(r.status)}</span></td>
    </tr>`;
  }).join("");
}

$("btn-explain").addEventListener("click", async () => {
  const btn = $("btn-explain");
  btn.disabled = true;
  setStatus($("explain-status"), "Asking the AI to explain failures…");
  try {
    const res = await api(`/api/projects/${state.projectId}/explain`, { method: "POST" });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Explanation failed");
    const items = body.explained || [];
    setStatus($("explain-status"),
      items.length ? `${items.length} failure(s) explained.` : "No failures to explain.",
      items.length ? "ok" : "");
    items.forEach((i) => {
      const row = [...document.querySelectorAll("#results-body tr")]
        .find((tr) => tr.cells[0].textContent === i.test_id);
      if (row && !row.nextElementSibling?.classList.contains("rec-row")) {
        row.insertAdjacentHTML("afterend",
          `<tr class="rec-row"><td></td><td colspan="9" class="rec">
             💡 ${escapeHtml(i.recommendation)}</td></tr>`);
      }
    });
  } catch (err) {
    setStatus($("explain-status"), err.message, "err");
  }
  btn.disabled = false;
});

$("btn-restart").addEventListener("click", () => {
  Object.assign(state, {
    projectId: null, jobId: null, pbixFile: null, dataFile: null, dsReady: false,
  });
  document.querySelectorAll(".dropzone").forEach((z) => z.classList.remove("has-file"));
  $("pbix-name").textContent = "";
  $("data-name").textContent = "";
  $("pbix-input").value = "";
  $("data-input").value = "";
  setStatus($("setup-status"), "");
  setStatus($("ds-status"), "");
  setStatus($("explain-status"), "");
  refreshAnalyze();
  show(1);
});

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* Load machine-level LLM config on first paint. */
loadLlm();

/* ── router ──────────────────────────────────────────────────
   Added with the dashboard layer. Everything above this line is the original
   analyse flow and is deliberately untouched: show(n) still toggles the three
   .screen elements by index, which now sit inside #route-analyze. Routes use
   .route, so the two mechanisms never select each other's elements. */

const ROUTES = {
  "/overview": { el: "route-overview", title: "Dashboard Overview",
                 sub: "AI-powered Power BI QA and unit-test automation" },
  "/projects": { el: "route-projects", title: "Projects",
                 sub: "Every analysis, with its result counts" },
  "/reports":  { el: "route-reports", title: "Reports",
                 sub: "Stored reports, ready to download" },
  "/suites":   { el: "route-suites", title: "Test Suites",
                 sub: "Generated tests grouped by what they prove" },
  "/sql":      { el: "route-sql", title: "SQL Validations",
                 sub: "Every executed query, with both values and the difference" },
  "/unit":     { el: "route-unit", title: "Unit Tests",
                 sub: "Measure, calculated-column and relationship checks" },
  "/bugs":     { el: "route-bugs", title: "Visual Bugs",
                 sub: "Validations that did not match the source" },
  "/models":   { el: "route-models", title: "LLM Models",
                 sub: "Providers the backend can reach" },
  "/settings": { el: "route-settings", title: "Settings",
                 sub: "What the engine is configured to do" },
  "/activity": { el: "route-activity", title: "Activity Log",
                 sub: "Recent projects, runs and results" },
  "/help":     { el: "route-help", title: "Help",
                 sub: "How the pipeline works and what each result proves" },
  "/new":      { el: "route-new", title: "Create New Project",
                 sub: "Name the project and record where the dashboard lives" },
  "/analyze":  { el: "route-analyze", title: "New Analysis",
                 sub: "Upload a dashboard, choose a source, run the pipeline",
                 steps: true },
};

/* Sections whose pages are not built yet. Naming them individually — rather
   than showing one generic "coming soon" — keeps the navigation honest about
   what each will contain. */
const PENDING = {
};

function currentRoute() {
  const hash = (location.hash || "").replace(/^#/, "");
  return hash.startsWith("/") ? hash : "/overview";
}

function renderRoute() {
  const path = currentRoute();
  const known = ROUTES[path];
  const pending = PENDING[path];

  document.querySelectorAll(".route").forEach((r) => r.classList.remove("is-visible"));

  let title, sub;
  if (known) {
    $(known.el).classList.add("is-visible");
    ({ title, sub } = known);
  } else if (pending) {
    const [name, text] = pending;
    $("ph-title").textContent = name;
    $("ph-text").textContent = text + " Not built yet.";
    $("route-placeholder").classList.add("is-visible");
    title = name;
    sub = "This section is not built yet";
  } else {
    location.hash = "#/overview";
    return;
  }

  $("route-title").textContent = title;
  $("route-sub").textContent = sub;
  // The step pills describe the analyse flow only; showing them elsewhere
  // would imply a stage the page has nothing to do with.
  $("steps").hidden = !(known && known.steps);

  document.querySelectorAll(".nav-item").forEach((a) =>
    a.classList.toggle("is-active", a.getAttribute("href") === "#" + path));

  // Per-route refreshes live here rather than in separate hashchange
  // listeners: one function decides what a route shows, so nothing can go
  // stale when the route is rendered by any other means.
  if (typeof showProjectBar === "function" && path === "/analyze") showProjectBar();
  if (typeof loadOverview === "function" && path === "/overview") loadOverview();
  if (typeof loadProjectsPage === "function" && path === "/projects") loadProjectsPage("projects");
  if (typeof loadProjectsPage === "function" && path === "/reports") loadProjectsPage("reports");
  if (typeof loadResultView === "function" && VIEWS[path]) loadResultView(path);
  if (typeof loadModelsPage === "function" && path === "/models") loadModelsPage();
  if (typeof loadSettingsPage === "function" && path === "/settings") loadSettingsPage();
  if (typeof loadActivityPage === "function" && path === "/activity") loadActivityPage();

  window.scrollTo({ top: 0 });
}

window.addEventListener("hashchange", renderRoute);
/* The first render is deliberately NOT called here. renderRoute() dispatches
   into the page modules below, whose `const`/`let` bindings are still in their
   temporal dead zone at this point — calling it now throws before the page has
   drawn. It is invoked at the very end of this file instead. */

/* ── dashboard overview ──────────────────────────────────────
   Every figure comes from /api/dashboard/stats, which sums the per-run
   summaries. When no run has completed the endpoint says so and this renders
   an empty state rather than zeros dressed as results. */

const fmt = (n) => Number(n || 0).toLocaleString();

function renderStack(el, parts) {
  const total = parts.reduce((a, p) => a + p.n, 0);
  el.innerHTML = total
    ? parts.filter((p) => p.n > 0)
        .map((p) => `<span class="${p.cls}" style="width:${(100 * p.n / total).toFixed(2)}%"
                       title="${p.label}: ${fmt(p.n)}"></span>`).join("")
    : "";
  return total;
}

function renderLegend(el, parts, total) {
  el.innerHTML = parts.map((p) => `
    <li>
      <span class="dot" style="background:var(${p.varname})"></span>
      <span>${p.label}</span>
      <span class="n">${fmt(p.n)}</span>
      <span class="pct">${total ? (100 * p.n / total).toFixed(0) + "%" : "–"}</span>
    </li>`).join("");
}

function renderRecent(rows) {
  const empty = !rows.length;
  $("recent-empty").hidden = !empty;
  $("recent-wrap").hidden = empty;
  if (empty) return;
  $("recent-body").innerHTML = rows.map((r) => {
    const cls = r.failed > 0 ? "fail" : "pass";
    const label = r.failed > 0 ? "Issues" : "Passed";
    return `<tr>
      <td>${escapeHtml(r.name || "Untitled")}</td>
      <td>${fmt(r.tests)}</td>
      <td>${fmt(r.passed)}</td>
      <td>${fmt(r.failed)}</td>
      <td>${fmt(r.issues)}</td>
      <td>${fmt(r.tokens)}</td>
      <td>${r.processing_time}</td>
      <td><span class="pill ${cls}">${label}</span></td>
      <td><a class="btn ghost sm" href="#/analyze">Open</a></td>
    </tr>`;
  }).join("");
}

async function loadOverview() {
  let d;
  try {
    const res = await api("/api/dashboard/stats");
    if (!res.ok) throw new Error("stats unavailable");
    d = await res.json();
  } catch {
    // A dashboard that cannot load its figures must not show stale or
    // invented ones.
    $("recent-empty").hidden = false;
    $("recent-empty").textContent = "Could not load dashboard statistics.";
    $("recent-wrap").hidden = true;
    return;
  }

  $("k-projects").textContent = fmt(d.projects_analyzed);
  $("k-passed").textContent = fmt(d.tests_passed);
  $("k-issues").textContent = fmt(d.issues_found);
  $("k-cases").textContent = fmt(d.test_cases_generated);
  $("k-time").textContent = d.avg_processing_time || "--";

  const ts = d.test_summary || {};
  const parts = [
    { label: "Passed",  n: ts.passed || 0,  cls: "s-pass", varname: "--pass" },
    { label: "Failed",  n: ts.failed || 0,  cls: "s-fail", varname: "--fail" },
    { label: "Warning", n: ts.warning || 0, cls: "s-warn", varname: "--warn" },
    { label: "Skipped", n: ts.skipped || 0, cls: "s-skip", varname: "--line" },
  ];
  const total = renderStack($("stack"), parts);
  renderLegend($("legend"), parts, total);
  $("summary-empty").hidden = !!total;
  $("summary-body").hidden = !total;

  renderRecent(d.recent_projects || []);
  renderTrend(d.trend || []);
  renderIssues(d);

  // Only shown once there is selection data to show; older runs predate it.
  const o = d.optimization || {};
  const hasOpt = (o.candidates || 0) > 0;
  $("opt-card").hidden = !hasOpt;
  if (hasOpt) {
    $("opt-card").querySelector(".hint").textContent =
      `Tests are chosen, not enumerated. Across ${fmt(o.runs)} run(s) that `
      + "recorded selection, candidates were generated deterministically and "
      + "only the subset proving something new was kept.";
    $("opt-kpis").innerHTML = [
      ["Candidates generated", fmt(o.candidates)],
      ["Selected", fmt(o.selected)],
      ["Duplicates removed", fmt(o.duplicates_removed)],
      ["Low-value skipped", fmt(o.low_value_skipped)],
      ["Computed without AI", fmt(o.compiled_without_llm)],
      ["AI calls", fmt(o.llm_calls)],
    ].map(([l, n]) => `<div class="kpi"><span class="kpi-l">${l}</span>
                        <span class="kpi-n">${n}</span></div>`).join("");
  }
}

/* renderRoute() calls loadOverview() when the overview is shown, so a run
   completed in this session is reflected without a reload. */

/* ── create new project ──────────────────────────────────────
   Creates the project record up front and hands the id to the existing setup
   flow. ensureProject() already returns early when state.projectId is set, so
   that flow needs no change: arriving via this page pre-creates, and going
   straight to #/analyze still creates lazily exactly as before. */

function showProjectBar() {
  const bar = $("project-bar");
  const named = state.projectName;
  bar.hidden = !named;
  if (!named) return;
  $("pb-name").textContent = named;
  const env = $("pb-env");
  env.hidden = !state.projectEnv;
  env.textContent = state.projectEnv || "";
}

async function createProject() {
  const name = $("np-name").value.trim();
  if (!name) {
    $("np-name").focus();
    return setStatus($("np-status"), "Give the project a name.", "err");
  }
  const btn = $("np-create");
  btn.disabled = true;
  setStatus($("np-status"), "Creating…");
  try {
    const res = await api("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        description: $("np-desc").value.trim(),
        bi_platform: $("np-platform").value,
        environment: $("np-env").value,
      }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(errorText(body.detail, "Could not create the project"));

    state.projectId = body.id;
    state.projectName = body.name;
    state.projectEnv = body.environment || "";
    setStatus($("np-status"), "");
    location.hash = "#/analyze";
  } catch (err) {
    // A duplicate name is the common case and is worth saying plainly.
    setStatus($("np-status"), err.message, "err");
  } finally {
    btn.disabled = false;
  }
}

$("np-create").addEventListener("click", createProject);
$("np-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") createProject();
});

/* renderRoute() keeps the project bar in step with the route. */

/* ── projects and reports ────────────────────────────────────
   Both render from /api/dashboard/projects. Filtering is client-side over one
   fetch: the list is small, and re-querying on every keystroke would be slower
   and no more accurate. */

let projectRows = [];

const when = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? "–" : d.toLocaleString(undefined,
    { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
};

const cell = (v) => (v === null || v === undefined ? '<span class="muted">–</span>' : fmt(v));

async function fetchProjects() {
  try {
    const res = await api("/api/dashboard/projects");
    if (!res.ok) throw new Error("unavailable");
    projectRows = (await res.json()).projects || [];
    return true;
  } catch {
    projectRows = [];
    return false;
  }
}

function projectStatus(r) {
  if (!r.analysed) return { cls: "", label: "Never run" };
  if ((r.failed || 0) > 0) return { cls: "fail", label: r.failed + " failing" };
  return { cls: "pass", label: "All passed" };
}

function matchesProjectFilters(r) {
  const q = $("pr-search").value.trim().toLowerCase();
  if (q && !(r.name || "").toLowerCase().includes(q)) return false;
  const env = $("pr-env").value;
  if (env && r.environment !== env) return false;
  switch ($("pr-status").value) {
    case "analysed": return r.analysed;
    case "never":    return !r.analysed;
    case "failed":   return r.analysed && (r.failed || 0) > 0;
    case "clean":    return r.analysed && (r.failed || 0) === 0;
    default:         return true;
  }
}

function renderProjects() {
  const rows = projectRows.filter(matchesProjectFilters);
  $("pr-count").textContent = rows.length === projectRows.length
    ? String(projectRows.length)
    : rows.length + " of " + projectRows.length;

  const empty = !rows.length;
  $("pr-wrap").hidden = empty;
  $("pr-empty").hidden = !empty;
  if (empty) {
    $("pr-empty").innerHTML = projectRows.length
      ? "No project matches those filters."
      : 'No projects yet. <a href="#/new">Create your first project.</a>';
    return;
  }

  $("pr-body").innerHTML = rows.map((r) => {
    const st = projectStatus(r);
    const desc = r.description
      ? '<div class="muted">' + escapeHtml(r.description) + "</div>" : "";
    const env = r.environment
      ? '<span class="pill">' + escapeHtml(r.environment) + "</span>"
      : '<span class="muted">–</span>';
    const openBtn = r.analysed
      ? '<button class="btn ghost" data-open="' + r.project_id + '">Results</button>' : "";
    const reportBtn = r.has_report
      ? '<a class="btn ghost" target="_blank" rel="noopener" href="/api/projects/'
        + r.project_id + '/report.html">Report</a>' : "";
    return "<tr>"
      + "<td>" + escapeHtml(r.name || "Untitled") + desc + "</td>"
      + "<td>" + env + "</td>"
      + "<td>" + when(r.updated_at) + "</td>"
      + "<td>" + cell(r.tests) + "</td><td>" + cell(r.passed) + "</td>"
      + "<td>" + cell(r.failed) + "</td>"
      + "<td>" + r.processing_time + "</td>"
      + '<td><span class="pill ' + st.cls + '">' + st.label + "</span></td>"
      + '<td><div class="row-actions">' + openBtn + reportBtn
      + '<button class="btn ghost danger" data-del="' + r.project_id + '">Delete</button>'
      + "</div></td></tr>";
  }).join("");
}

function renderReports() {
  const q = $("rp-search").value.trim().toLowerCase();
  const rows = projectRows.filter((r) => r.has_report
    && (!q || (r.name || "").toLowerCase().includes(q)));
  $("rp-count").textContent = String(rows.length);

  const empty = !rows.length;
  $("rp-wrap").hidden = empty;
  $("rp-empty").hidden = !empty;
  if (empty) {
    $("rp-empty").innerHTML = q
      ? "No report matches that search."
      : 'No reports yet. <a href="#/new">Run an analysis</a> to generate one.';
    return;
  }

  $("rp-body").innerHTML = rows.map((r) => {
    const base = "/api/projects/" + r.project_id;
    return "<tr>"
      + "<td>" + escapeHtml(r.name || "Untitled") + "</td>"
      + "<td>" + when(r.updated_at) + "</td>"
      + "<td>" + cell(r.tests) + "</td><td>" + cell(r.passed) + "</td>"
      + "<td>" + cell(r.failed) + "</td><td>" + cell(r.issues) + "</td>"
      + '<td><div class="row-actions">'
      + '<a class="btn ghost" target="_blank" rel="noopener" href="' + base + '/report.html">HTML</a>'
      + '<a class="btn ghost" href="' + base + '/report.pdf">PDF</a>'
      + '<a class="btn ghost" href="' + base + '/report.xlsx">Excel</a>'
      + "</div></td></tr>";
  }).join("");
}

/* Open a past project's results by reusing the existing results screen: it
   reads state.projectId, so pointing that at the chosen project and calling
   the same loader avoids a second implementation that could disagree with it. */
async function openProjectResults(projectId) {
  const row = projectRows.find((r) => r.project_id === projectId);
  state.projectId = projectId;
  state.projectName = row ? row.name : null;
  state.projectEnv = row ? row.environment : null;
  location.hash = "#/analyze";
  try {
    await loadResults();
    show(3);
  } catch {
    setStatus($("setup-status"), "Could not load results for that project.", "err");
    show(1);
  }
}

async function deleteProject(projectId) {
  const row = projectRows.find((r) => r.project_id === projectId);
  const name = row ? row.name : projectId;
  if (!window.confirm('Delete "' + name + '" and all of its results? This cannot be undone.'))
    return;
  const res = await api("/api/projects/" + projectId, { method: "DELETE" });
  if (!res.ok && res.status !== 204) {
    window.alert("Could not delete that project.");
    return;
  }
  await fetchProjects();
  renderProjects();
  renderReports();
}

/* One delegated listener per table, so re-rendering rows never leaves stale
   handlers behind. */
["pr-body", "rp-body"].forEach((id) => $(id).addEventListener("click", (e) => {
  const open = e.target.closest("[data-open]");
  if (open) return openProjectResults(open.dataset.open);
  const del = e.target.closest("[data-del]");
  if (del) return deleteProject(del.dataset.del);
}));

["pr-search", "pr-status", "pr-env"].forEach((id) =>
  $(id).addEventListener("input", renderProjects));
$("rp-search").addEventListener("input", renderReports);

async function loadProjectsPage(which) {
  const ok = await fetchProjects();
  if (!ok) {
    const box = which === "reports" ? $("rp-empty") : $("pr-empty");
    box.hidden = false;
    box.textContent = "Could not load projects.";
    (which === "reports" ? $("rp-wrap") : $("pr-wrap")).hidden = true;
    return;
  }
  // Environment options come from the data, so the filter can only ever offer
  // values that actually exist.
  const envs = [...new Set(projectRows.map((r) => r.environment).filter(Boolean))].sort();
  const sel = $("pr-env");
  const keep = sel.value;
  sel.innerHTML = '<option value="">Any environment</option>'
    + envs.map((e) => '<option value="' + escapeHtml(e) + '">' + escapeHtml(e) + "</option>").join("");
  sel.value = envs.includes(keep) ? keep : "";
  if (which === "reports") renderReports(); else renderProjects();
}

/* ── result views ────────────────────────────────────────────
   Test suites, SQL validations, unit tests and visual bugs are four readings of
   one project's stored results, fetched once per project and filtered in the
   browser. The project picker is shared across all four so switching view keeps
   the subject: a test id means nothing without knowing which run produced it. */

const tvCache = new Map();      // project_id -> payload
let tvProject = null;

const VIEWS = {
  "/suites": { pick: "tv-project-suites", render: () => renderSuites() },
  "/sql":    { pick: "tv-project-sql",    render: () => renderSql() },
  "/unit":   { pick: "tv-project-unit",   render: () => renderUnit() },
  "/bugs":   { pick: "tv-project-bugs",   render: () => renderBugs() },
};

function tvEmpty(routeId, message) {
  const box = $(routeId).querySelector(".tv-empty");
  const table = $(routeId).querySelector(".table-wrap");
  const show = Boolean(message);
  box.hidden = !show;
  table.hidden = show;
  if (show) box.innerHTML = message;
}

async function tvLoad(projectId) {
  if (tvCache.has(projectId)) return tvCache.get(projectId);
  const res = await api("/api/dashboard/projects/" + projectId + "/tests");
  if (!res.ok) throw new Error("Could not load results for that project");
  const payload = await res.json();
  tvCache.set(projectId, payload);
  return payload;
}

function currentPayload() {
  return tvProject ? tvCache.get(tvProject) : null;
}

/* ── suites ─────────────────────────────────────────────── */
function renderSuites() {
  const d = currentPayload();
  if (!d) return;
  if (!d.suites.length) return tvEmpty("route-suites", "No test cases were generated for this project.");
  tvEmpty("route-suites", "");
  $("su-body").innerHTML = d.suites.map((s) => {
    const executed = s.passed + s.failed + s.warning;
    // Denominator is what automation could decide, not everything generated.
    // Measuring against the total makes a manual checklist look like a
    // coverage failure, which is a different thing entirely.
    const auto = s.automatable !== undefined ? s.automatable : s.total;
    const pct = auto ? Math.round(100 * executed / auto) : null;
    const w = (n) => (executed ? (100 * n / executed).toFixed(1) : 0) + "%";
    return "<tr>"
      + "<td><b>" + escapeHtml(s.suite) + "</b></td>"
      + "<td>" + fmt(s.total) + "</td>"
      + '<td class="ok-n">' + fmt(s.passed) + "</td>"
      + "<td>" + (s.failed ? '<b style="color:var(--fail)">' + fmt(s.failed) + "</b>" : "0") + "</td>"
      + "<td>" + fmt(s.warning) + "</td>"
      + '<td><span class="muted">' + fmt(s.not_executed) + "</span></td>"
      + '<td><span class="muted">' + fmt(s.manual || 0) + "</span></td>"
      + '<td><div class="cov">'
      + (pct === null
          ? '<span class="muted">manual only</span>'
          : '<div class="bar-mini">'
            + '<span style="width:' + w(s.passed) + ';background:var(--pass)"></span>'
            + '<span style="width:' + w(s.failed) + ';background:var(--fail)"></span>'
            + '<span style="width:' + w(s.warning) + ';background:var(--warn)"></span>'
            + "</div><span>" + pct + "%</span>")
      + "</div></td>"
      + "</tr>";
  }).join("");
}

/* ── SQL validations ────────────────────────────────────── */
function renderSql() {
  const d = currentPayload();
  if (!d) return;
  const q = $("sq-search").value.trim().toLowerCase();
  const want = $("sq-status").value;
  const rows = d.sql_validations.filter((r) => {
    if (q && !(r.kpi || "").toLowerCase().includes(q)) return false;
    if (want && !(r.status || "").toLowerCase().startsWith(want)) return false;
    return true;
  });
  $("sq-count").textContent = rows.length === d.sql_validations.length
    ? String(rows.length) : rows.length + " of " + d.sql_validations.length;
  if (!d.sql_validations.length)
    return tvEmpty("route-sql", "No SQL validations ran for this project.");
  if (!rows.length) return tvEmpty("route-sql", "No validation matches those filters.");
  tvEmpty("route-sql", "");
  $("sq-body").innerHTML = rows.map((r) => {
    const cls = (r.status || "").toLowerCase().startsWith("pass") ? "pass"
      : (r.status || "").toLowerCase().startsWith("warn") ? "warn" : "fail";
    const ms = r.execution_time_ms === null || r.execution_time_ms === undefined
      ? '<span class="muted">–</span>' : Number(r.execution_time_ms).toFixed(0);
    return "<tr>"
      + "<td>" + escapeHtml(r.test_id) + "</td>"
      + "<td>" + escapeHtml(r.kpi) + "</td>"
      + "<td>" + escapeHtml(r.scenario) + "</td>"
      + '<td class="q">' + escapeHtml((r.query || "").slice(0, 300)) + "</td>"
      + "<td>" + escapeHtml(r.dashboard_value) + "</td>"
      + "<td>" + escapeHtml(r.database_value || "—") + "</td>"
      + "<td>" + escapeHtml(r.difference || "—") + "</td>"
      + "<td>" + ms + "</td>"
      + '<td><span class="pill ' + cls + '">' + escapeHtml(r.status) + "</span></td>"
      + "</tr>";
  }).join("");
}

/* ── unit tests ─────────────────────────────────────────── */
function renderUnit() {
  const d = currentPayload();
  if (!d) return;
  const q = $("ut-search").value.trim().toLowerCase();
  const rows = d.unit_tests.filter((r) =>
    !q || (r.module || "").toLowerCase().includes(q));
  $("ut-count").textContent = rows.length === d.unit_tests.length
    ? String(rows.length) : rows.length + " of " + d.unit_tests.length;
  if (!d.unit_tests.length)
    return tvEmpty("route-unit", "No developer unit tests were generated for this project.");
  if (!rows.length) return tvEmpty("route-unit", "No unit test matches that search.");
  tvEmpty("route-unit", "");
  $("ut-body").innerHTML = rows.map((r) => {
    const s = (r.status || "").toLowerCase();
    const cls = s.startsWith("pass") ? "pass" : s.startsWith("fail") ? "fail"
      : s.startsWith("warn") ? "warn" : "";
    // A verdict without its evidence is just an assertion. The remark says why
    // the check decided as it did, and the actual value is what it read.
    const finding = [r.actual, r.remarks].filter(Boolean).join(" — ");
    return "<tr>"
      + "<td>" + escapeHtml(r.test_case_id) + "</td>"
      + "<td>" + escapeHtml(r.module) + "</td>"
      + "<td>" + escapeHtml((r.scenario || "").slice(0, 90)) + "</td>"
      + "<td>" + escapeHtml((r.expected || "").slice(0, 80)) + "</td>"
      + '<td class="muted">' + escapeHtml(finding.slice(0, 130)) + "</td>"
      + "<td>" + escapeHtml(r.priority) + "</td>"
      + '<td><span class="pill ' + cls + '">' + escapeHtml(r.status) + "</span></td>"
      + "</tr>";
  }).join("");
}

/* ── visual bugs ────────────────────────────────────────── */
function renderBugs() {
  const d = currentPayload();
  if (!d) return;
  const sev = $("vb-sev").value;
  const rows = d.visual_bugs.filter((b) => !sev || b.severity === sev);
  $("vb-count").textContent = rows.length === d.visual_bugs.length
    ? String(rows.length) : rows.length + " of " + d.visual_bugs.length;
  if (!d.visual_bugs.length)
    return tvEmpty("route-bugs",
      "Nothing failed for this project — every validation matched the source.");
  if (!rows.length) return tvEmpty("route-bugs", "No issue at that severity.");
  tvEmpty("route-bugs", "");
  $("vb-body").innerHTML = rows.map((b) => "<tr>"
    + "<td>" + escapeHtml(b.test_id) + "</td>"
    + "<td>" + escapeHtml(b.kpi) + "</td>"
    + "<td>" + escapeHtml(b.scenario) + "</td>"
    + "<td>" + escapeHtml(b.issue) + "</td>"
    + "<td>" + escapeHtml(b.dashboard_value || "—") + "</td>"
    + "<td>" + escapeHtml(b.database_value || "—") + "</td>"
    + '<td><span class="sev ' + b.severity + '">' + b.severity + "</span></td>"
    + "</tr>").join("");
}

/* ── shared project picker ──────────────────────────────── */
function fillPickers(analysed) {
  const options = analysed.map((r) =>
    '<option value="' + r.project_id + '">' + escapeHtml(r.name || "Untitled")
    + "</option>").join("");
  document.querySelectorAll(".proj-pick").forEach((sel) => {
    sel.innerHTML = options;
    if (tvProject) sel.value = tvProject;
  });
}

async function loadResultView(path) {
  const view = VIEWS[path];
  if (!view) return;
  const routeId = $(view.pick).closest(".route").id;

  if (!projectRows.length) await fetchProjects();
  const analysed = projectRows.filter((r) => r.analysed);
  if (!analysed.length) {
    return tvEmpty(routeId,
      'No analysed project yet. <a href="#/new">Run an analysis</a> to populate this view.');
  }
  if (!tvProject || !analysed.some((r) => r.project_id === tvProject)) {
    tvProject = analysed[0].project_id;          // most recently updated
  }
  fillPickers(analysed);

  try {
    await tvLoad(tvProject);
  } catch (err) {
    return tvEmpty(routeId, escapeHtml(err.message));
  }
  view.render();
}

document.querySelectorAll(".proj-pick").forEach((sel) =>
  sel.addEventListener("change", async () => {
    tvProject = sel.value;
    await loadResultView(currentRoute());
  }));

["sq-search", "sq-status"].forEach((id) => $(id).addEventListener("input", renderSql));
$("ut-search").addEventListener("input", renderUnit);
$("vb-sev").addEventListener("input", renderBugs);


/* ── configuration pages ─────────────────────────────────────
   LLM models, effective settings and activity. All read-only: this application
   has no user management and no login, so nothing here edits machine config —
   it reports what the backend is actually configured to do. */

let settingsCache = null;

async function loadSettingsData() {
  if (settingsCache) return settingsCache;
  const res = await api("/api/dashboard/settings");
  if (!res.ok) throw new Error("Could not read settings");
  settingsCache = await res.json();
  return settingsCache;
}

/* ── LLM models ─────────────────────────────────────────── */
async function loadModelsPage() {
  let d;
  try { d = await loadSettingsData(); }
  catch { $("lm-body").innerHTML = '<tr><td colspan="6">Could not read provider configuration.</td></tr>'; return; }

  const active = (d.llm || {}).provider;
  const activeModel = (d.llm || {}).model;
  $("lm-body").innerHTML = (d.providers || []).map((p) => {
    const isActive = p.provider === active;
    const cred = p.configured
      ? '<span class="pill pass">Configured</span>'
      : '<span class="pill">No key on server</span>';
    const cap = p.tokens_per_day
      ? fmt(p.tokens_per_day) + " tokens"
      : '<span class="muted">not enforced</span>';
    const model = isActive && activeModel ? activeModel : p.default_model;
    return "<tr>"
      + "<td><b>" + escapeHtml(p.provider) + "</b>"
      + (isActive ? ' <span class="pill pass">In use</span>' : "") + "</td>"
      + "<td>" + escapeHtml(model) + "</td>"
      + "<td>" + fmt(p.models) + "</td>"
      + "<td>" + cred + "</td>"
      + "<td>" + cap + "</td>"
      + '<td><span class="muted">' + escapeHtml(p.env_var) + "</span></td>"
      + "</tr>";
  }).join("");
}

/* ── settings ───────────────────────────────────────────── */
const SETTING_LABELS = {
  tolerance_pct: "Comparison tolerance (%)",
  provider: "Provider", model: "Model", temperature: "Temperature",
  max_tokens: "Output token ceiling", tokens_per_minute: "Tokens per minute",
  tokens_per_day: "Tokens per day", min_tokens_to_start: "Minimum to start a run",
  compile_before_llm: "Compile measures before calling the model",
  max_scenarios: "Maximum slicer scenarios",
  max_items_per_call: "Queries requested per call",
  values_per_slicer: "Values validated per slicer",
  max_high_tests_per_subject: "High-priority tests per subject",
  max_medium_tests_per_subject: "Medium-priority tests per subject",
  max_low_tests_per_subject: "Low-priority tests per subject",
  send_sample_values_to_llm: "Send sample column values to the model",
};

function kvRows(obj) {
  return Object.entries(obj)
    .filter(([k]) => k !== "note")
    .map(([k, v]) => {
      let shown;
      if (v === true) shown = '<span class="pill pass">On</span>';
      else if (v === false) shown = '<span class="pill">Off</span>';
      else if (v === null || v === undefined) shown = '<span class="muted">not set</span>';
      else shown = escapeHtml(String(v));
      return '<div class="kv-row"><span class="k">'
        + escapeHtml(SETTING_LABELS[k] || k) + '</span><span class="v">'
        + shown + "</span></div>";
    }).join("");
}

function group(title, obj, note) {
  return '<div class="card setting-group"><h2>' + escapeHtml(title) + "</h2>"
    + (note ? '<p class="hint">' + escapeHtml(note) + "</p>" : "")
    + '<div class="kv">' + kvRows(obj) + "</div></div>";
}

async function loadSettingsPage() {
  let d;
  try { d = await loadSettingsData(); }
  catch { $("st-body").innerHTML = '<div class="card"><p class="hint">Could not read settings.</p></div>'; return; }

  const chips = (items) => '<div class="chips">'
    + items.map((i) => '<span class="chip">' + escapeHtml(i) + "</span>").join("")
    + "</div>";

  $("st-body").innerHTML =
      group("Validation", d.validation, d.validation.note)
    + group("AI model", d.llm,
        "Credentials and endpoints are resolved on the server and are not shown here.")
    + group("Workload", d.workload,
        "How much work one run does. Lower these for a slower model or a tighter token budget.")
    + group("Data privacy", d.privacy, d.privacy.note)
    + '<div class="card setting-group"><h2>Supported inputs</h2>'
      + '<div class="kv"><div class="kv-row"><span class="k">Dashboard files</span>'
      + "<span>" + chips(d.supported.dashboard_files) + "</span></div>"
      + '<div class="kv-row"><span class="k">Datasources</span>'
      + "<span>" + chips(d.supported.datasources) + "</span></div></div></div>";
}

/* ── activity ───────────────────────────────────────────── */
const FEED_ICON = { created: "＋", completed: "✓", generated: "▤", failed: "✕" };

async function loadActivityPage() {
  let d;
  try {
    const res = await api("/api/dashboard/activity?limit=80");
    if (!res.ok) throw new Error("unavailable");
    d = await res.json();
  } catch {
    $("ac-empty").hidden = false;
    $("ac-empty").textContent = "Could not load activity.";
    $("ac-body").innerHTML = "";
    return;
  }
  const events = d.events || [];
  $("ac-count").textContent = events.length < d.total
    ? events.length + " of " + d.total : String(d.total);
  $("ac-empty").hidden = events.length > 0;
  $("ac-body").innerHTML = events.map((e) => '<li class="' + (e.severity || "ok") + '">'
    + '<span class="ico">' + (FEED_ICON[e.kind] || "•") + "</span>"
    + "<span>" + escapeHtml(e.text)
    + '<span class="who"> · ' + escapeHtml(e.project || "") + "</span></span>"
    + "<time>" + when(e.at) + "</time></li>").join("");
}


/* ── dashboard charts ────────────────────────────────────────
   Inline SVG rather than a charting library: the page has no build step and
   loads no external script, and two series over fourteen points does not need
   one. Everything drawn comes from completed runs. */

const CHART = { w: 640, h: 170, padL: 34, padR: 10, padT: 12, padB: 26 };

function linePath(values, max, n) {
  const { w, h, padL, padR, padT, padB } = CHART;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;
  const step = n > 1 ? innerW / (n - 1) : 0;
  return values.map((v, i) => {
    const x = padL + i * step;
    const y = padT + innerH - (max ? (v / max) * innerH : 0);
    return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
  }).join(" ");
}

function renderTrend(points) {
  const box = $("trend-card");
  // One run is a dot, not a trend. Saying so is more useful than drawing a
  // flat line and calling it history.
  if (!points || points.length < 2) {
    $("trend-body").innerHTML =
      '<p class="empty">A trend needs at least two completed runs. '
      + (points && points.length ? "There is one so far." : "There are none yet.")
      + "</p>";
    return;
  }
  const { w, h, padL, padR, padT, padB } = CHART;
  const totals = points.map((p) => p.total || 0);
  const passed = points.map((p) => p.passed || 0);
  const max = Math.max(...totals, 1);
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;
  const step = points.length > 1 ? innerW / (points.length - 1) : 0;

  // Horizontal guides at 0, half and max, labelled — an unlabelled grid is
  // decoration, not information.
  const guides = [0, 0.5, 1].map((f) => {
    const y = padT + innerH - f * innerH;
    return '<line x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' + (w - padR)
      + '" y2="' + y.toFixed(1) + '" class="grid"/>'
      + '<text x="' + (padL - 6) + '" y="' + (y + 3.5).toFixed(1)
      + '" class="axis" text-anchor="end">' + Math.round(f * max) + "</text>";
  }).join("");

  const area = "M" + padL + " " + (padT + innerH)
    + " " + linePath(totals, max, points.length).slice(1)
    + " L" + (padL + (points.length - 1) * step).toFixed(1)
    + " " + (padT + innerH) + " Z";

  const dots = points.map((p, i) => {
    const x = padL + i * step;
    const y = padT + innerH - (p.passed / max) * innerH;
    return '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="2.6" '
      + 'class="dot-pass"><title>' + escapeHtml(p.project || "") + " — "
      + p.passed + " of " + p.total + " passed</title></circle>";
  }).join("");

  const first = when(points[0].at), last = when(points[points.length - 1].at);
  $("trend-body").innerHTML =
    '<svg viewBox="0 0 ' + w + " " + h + '" class="chart" role="img" '
    + 'aria-label="Tests executed and passed across the last '
    + points.length + ' runs">'
    + guides
    + '<path d="' + area + '" class="area-total"/>'
    + '<path d="' + linePath(totals, max, points.length) + '" class="line-total"/>'
    + '<path d="' + linePath(passed, max, points.length) + '" class="line-pass"/>'
    + dots
    + "</svg>"
    + '<div class="chart-foot"><span>' + first + "</span>"
    + '<ul class="legend-inline">'
    + '<li><span class="dot" style="background:var(--ink-2)"></span>Tests run</li>'
    + '<li><span class="dot" style="background:var(--pass)"></span>Passed</li>'
    + "</ul><span>" + last + "</span></div>";
}

function renderIssues(d) {
  const dist = d.issue_distribution || {};
  const total = (dist.high || 0) + (dist.medium || 0) + (dist.low || 0);
  if (!dist.runs) {
    // No run has trustworthy severity yet. Showing zeros would read as "no
    // problems found", which is a different claim from "not measured".
    $("issues-body").innerHTML =
      '<p class="empty">Severity is recorded per run. It will appear here '
      + "after the next analysis.</p>";
    return;
  }
  if (!total) {
    // Measured, and clean. Worth saying outright.
    $("issues-body").innerHTML =
      '<p class="empty">No issues across ' + fmt(dist.runs)
      + " run(s) — every validation matched the source.</p>";
    return;
  }
  const parts = [
    { label: "High", n: dist.high || 0, v: "--fail" },
    { label: "Medium", n: dist.medium || 0, v: "--warn" },
    { label: "Low", n: dist.low || 0, v: "--ink-2" },
  ];
  $("issues-body").innerHTML =
    '<div class="stack">'
    + parts.filter((p) => p.n).map((p) =>
        '<span style="width:' + (100 * p.n / total).toFixed(2) + "%;background:var("
        + p.v + ')" title="' + p.label + ": " + p.n + '"></span>').join("")
    + "</div><ul class=\"legend\">"
    + parts.map((p) => "<li>"
        + '<span class="dot" style="background:var(' + p.v + ')"></span>'
        + "<span>" + p.label + "</span>"
        + '<span class="n">' + fmt(p.n) + "</span>"
        + '<span class="pct">' + (100 * p.n / total).toFixed(0) + "%</span></li>").join("")
    + "</ul><p class=\"hint\">Across " + fmt(dist.runs) + " run(s) that recorded severity.</p>";
}

/* ── boot ────────────────────────────────────────────────────
   Everything the router dispatches to is defined by this point, so the first
   render is safe here and nowhere earlier. */
renderRoute();
