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

function setStatus(el, text, kind = "") {
  el.textContent = text;
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
  $(id).addEventListener("change", refreshLlmBadge));

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
    if (!res.ok) throw new Error(body.detail || "Could not load models");

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
    if (!res.ok) throw new Error(body.detail || "Request failed");
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
  if (!res.ok) throw new Error((await res.json()).detail || "Could not create project");
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
    if (!up.ok) throw new Error(upBody.detail || "Upload failed");
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
    if (!res.ok) throw new Error(job.detail || "Could not start analysis");

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
