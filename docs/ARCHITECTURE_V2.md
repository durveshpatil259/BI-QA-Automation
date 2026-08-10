# BI TestPilot AI — V2 Architecture

**Goal:** two user actions (upload PBIX, configure datasource) → one **Analyze** click →
fully automatic pipeline → downloadable QA report.

---

## 0. Critical design decisions (read first)

These three decisions shape everything else. Two of them change the *meaning* of
"validation", so they need an explicit call before implementation starts.

### D1. Where do "dashboard values" come from once screenshots are removed?

The pipeline must "compare dashboard values with datasource values". A PBIX stores
**DAX formulas, not rendered numbers** — `Total Sales = SUM(Sales[Amount])` never
contains `$109.81M`. Screenshots were previously the only source of rendered values.

**RESOLVED — implemented via `pbixray`, verified against the real dashboard.**

`pbixray` (pure Python, PyPI) decompresses the binary VertiPaq model inside a native
`.pbix` and exposes not only the model definition but the **row data**. That means the
number a KPI card renders can be **recomputed in pandas from the file itself**:

| Measure | Computed from PBIX data | Dashboard showed |
|---|---|---|
| Total Sales | 109,809,274.20 → `$109.81M` | `$109.81M` ✅ |
| Total Profit | 12,551,366.25 → `$12.55M` | `$12.55M` ✅ |
| Profit Margin % | 0.114302 → `11.4%` | `11.4%` ✅ |
| Quantity Sold | 274,776 → `275K` | `275K` ✅ |
| Total Orders | 31,455 → `31K` | `31K` ✅ |
| Avg Order Value | 3,490.99 → `$3,491` | `$3,491` ✅ |

Implemented as **`PbixDataService`** (stage 5 below). No screenshot, no Power BI
Desktop, no live Analysis Services, no user interaction — and it runs headless in CI.

**Scope, stated honestly.** This is a pragmatic DAX subset, not a DAX engine:

* **Supported** — `SUM / AVERAGE / MIN / MAX / COUNT / COUNTROWS / DISTINCTCOUNT`,
  plus measures derived from other measures (`[A]-[B]`, `[A]+[B]`, `[A]*[B]`,
  `DIVIDE([A],[B])`), resolved iteratively so chains resolve.
* **Not supported** — `CALCULATE` with filter context, time intelligence
  (`SAMEPERIODLASTYEAR`, `DATEADD`), row-context iterators (`SUMX`, `AVERAGEX`).

On the reference model this evaluates **9 of 33 measures** — precisely the headline KPI
cards. Unsupported measures are **omitted, never guessed**, so validation degrades to
`executability` / `dax-consistency` for them.

### D2. Extraction: `pbixray`, not `pbi-tools`

The original plan used `pbi-tools` (a .NET CLI). Two findings killed that:

1. **`dotnet` is not installed** on the target machine — the .NET route needs a runtime
   install on top of the tool itself.
2. **⚠️ Name collision.** The PyPI package literally named **`pbi-tools`** is a
   *completely unrelated project* — a Power BI **REST API wrapper**
   (`import pbi`, alpha, by Sam Thomas). The PBIX-extracting `pbi-tools`
   (Mathias Thierbach) is **not on PyPI at all**. Adding `pbi-tools` to
   `requirements.txt` silently installs the wrong package. **Never do this.**

`pbixray` replaces it entirely, and is strictly better here: pure Python, a normal
`requirements.txt` entry, no subprocess, no .NET, no Power BI Desktop, cross-platform.

**Verified on the previously-unreadable native `.pbix`:**

```
before (stdlib parser):  tables 0,  measures 0,  relationships 0
after  (pbixray):        tables 7,  measures 33, relationships 8, pages 3, visuals 53
```

**Extraction strategy** — `BestPowerBIExtractor` picks per file type and falls back:

| File | Primary | Why |
|---|---|---|
| `.pbit` / `.pbip` / `.zip` | stdlib parser | text model **and** carries measure `formatString`, which pbixray does not expose |
| `.pbix` | `pbixray` | only reader for the binary VertiPaq model |

Whichever runs first, the other is the fallback; a result with **0 tables** triggers the
fallback rather than being accepted. `pbixray` covers only the data model, so the report
layout (pages, visuals, bookmarks) is still merged in from the ZIP's `Report/Layout`.

### D3. Python 3.13 is present, not 3.12

3.13 is a superset for our purposes; no code change needed. Pinning to 3.12 would mean
a second interpreter for no benefit. **Target 3.12+.**

---

## 1. Layered architecture

Dependencies point inward only. The Streamlit UI is replaced; **the domain, storage
and most services are reused unchanged**.

```
┌──────────────────────────────────────────────────────────┐
│  Frontend (3 screens, static SPA)                          │  web/
│  Upload → Progress (SSE) → Results                          │
└───────────────────────┬──────────────────────────────────┘
                         │ REST + Server-Sent Events
┌───────────────────────▼──────────────────────────────────┐
│  API layer (FastAPI)                                       │  api/
│  routers, request/response schemas, DI wiring               │
└───────────────────────┬──────────────────────────────────┘
                         │
┌───────────────────────▼──────────────────────────────────┐
│  Orchestration                                             │  pipeline/
│  PipelineRunner + JobManager (asyncio) + ProgressReporter   │
└───────────────────────┬──────────────────────────────────┘
                         │ calls, in fixed order
┌───────────────────────▼──────────────────────────────────┐
│  Services (single responsibility each)                      │  services/
│  extraction · semantic model · JSON builder · LLM analysis   │
│  SQL generation · SQL execution · validation · comparison    │
│  DAX evaluation · report generation                          │
└──────┬────────────────────────────────────┬──────────────┘
       │                                      │
┌──────▼─────────┐                    ┌──────▼──────────┐
│  Domain         │◄───────────────────│  Storage         │  domain/, storage/
│  (dataclasses)  │                    │  (project files) │
└──────┬──────────┘                    └──────────────────┘
       │
┌──────▼────────────────────────────────────────────────────┐
│  Core: config, logging, exceptions, constants                │  core/
└────────────────────────────────────────────────────────────┘
```

---

## 2. Folder structure

```
bi_testpilot/
├── main.py                          # uvicorn entrypoint
├── pyproject.toml
│
├── api/
│   ├── deps.py                      # DI providers (settings, services, JobManager)
│   ├── schemas.py                   # Pydantic request/response models
│   └── routers/
│       ├── projects.py              # POST /projects, upload endpoints
│       ├── analysis.py              # POST /analyze, GET /jobs/{id}/stream
│       └── reports.py               # GET /reports/{id}.{html|pdf|xlsx}
│
├── pipeline/
│   ├── runner.py                    # PipelineRunner.run()  ← the orchestrator
│   ├── stages.py                    # Stage enum + ordered stage registry
│   ├── context.py                   # PipelineContext (data passed between stages)
│   ├── jobs.py                      # JobManager: asyncio tasks, status, cancellation
│   └── progress.py                  # ProgressReporter -> asyncio.Queue -> SSE
│
├── services/
│   ├── extractors/
│   │   ├── power_bi/pbixray_extractor.py  # DONE: binary model via pbixray
│   │   ├── power_bi/extractor.py          # REUSED: .pbit/.pbip/layout parser
│   │   └── factory.py                     # BestPowerBIExtractor: picks + falls back
│   ├── semantic/
│   │   ├── model_parser.py          # extracted files -> SemanticModel
│   │   └── json_builder.py          # SemanticModel -> LLM-ready JSON
│   ├── pbix_data_service.py         # DONE: DAX -> true value via pbixray + pandas
│   ├── validation/dax_analyzer.py   # REUSED: consistency rules, format strings
│   ├── llm/                         # REUSED wholesale
│   │   ├── client.py, providers.py, factory.py, prompt_builder.py, json_utils.py
│   ├── analysis_service.py          # NEW: one LLM call -> plan + tests + docs
│   ├── sql_generation_service.py    # extracts/validates SQL from LLM output
│   ├── sql_execution_service.py     # REUSED core: read-only execution + timing
│   ├── validation_engine.py         # REUSED: verdict logic
│   ├── comparison_engine.py         # REUSED: value_parser + tolerance
│   ├── datasources/                 # REUSED: sql_server.py, excel.py, factory.py
│   └── reporting/
│       ├── html_report.py           # REUSED (Jinja2)
│       ├── pdf_report.py            # NEW (WeasyPrint or Playwright print-to-PDF)
│       └── excel_report.py          # NEW (openpyxl / pandas)
│
├── domain/models.py                 # REUSED (+ SemanticModel, JobStatus)
├── storage/project_repository.py    # REUSED
├── core/{config,logging,exceptions,constants}.py   # REUSED
│
└── web/                             # static SPA (no build step required)
    ├── index.html
    ├── app.js                       # fetch + EventSource
    └── styles.css
```

---

## 3. Execution flow

`PipelineRunner.run()` executes an **ordered, deterministic** list of stages. No agent
decides what runs next; the sequence is fixed code.

```
POST /analyze  ──►  JobManager.submit()  ──►  asyncio.Task
                                                   │
        ┌──────────────────────────────────────────┘
        ▼
 1  EXTRACT_METADATA     pbi-tools extract (subprocess)   [Python]
 2  PARSE_SEMANTIC_MODEL  extracted files -> SemanticModel [Python]
 3  BUILD_JSON            SemanticModel -> compact JSON     [Python]
 4  READ_SCHEMA           datasource tables/cols/PK/FK      [Python]
 5  EVALUATE_DAX          measures -> true values (SSAS)    [Python]   ← replaces screenshots
 6  LLM_ANALYSIS          one call: understand + generate   [LLM]
                            • business rules
                            • SQL per KPI and per visual
                            • QA test cases
                            • documentation
 7  VALIDATE_SQL          read-only guard on every query    [Python]
 8  EXECUTE_SQL           run all queries, capture timing   [Python]
 9  COMPARE               dashboard vs datasource + verdict [Python]
10  BUILD_REPORT          HTML + PDF + Excel                [Python]
11  (on demand) EXPLAIN   AI explanation of failures        [LLM]
```

Each stage: emits `ProgressEvent(stage, status, message, pct)` → queue → SSE → UI.

**Failure policy per stage** (declared in the stage registry, not ad hoc):

| Stage | On failure |
|---|---|
| 1–3 | **Fatal** — no model, nothing to validate |
| 4 | Fatal if datasource configured; skip if none |
| 5 | **Degrade** — no true values; comparison falls back to executability |
| 6 | Fatal (nothing to execute) |
| 7 | Per-item skip (mark item invalid, continue) |
| 8–9 | Per-item error (recorded as a FAIL row, run continues) |
| 10 | Fatal |
| 11 | Optional — never blocks the report |

### PipelineContext

A single mutable object threaded through stages — avoids re-reading from disk and makes
stages independently testable:

```python
@dataclass
class PipelineContext:
    project: Project
    datasource: DatasourceConfig | None
    extracted_dir: Path | None = None
    semantic_model: SemanticModel | None = None
    model_json: dict | None = None
    db_schema: DbSchema | None = None
    dax_values: dict[str, str] = field(default_factory=dict)
    llm_output: LLMAnalysis | None = None
    validation_plan: ValidationPlan | None = None
    results: DataValidationRun | None = None
    report: AnalysisReport | None = None
    warnings: list[str] = field(default_factory=list)
```

---

## 4. API design

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/projects` | Create project → `{project_id}` |
| `POST` | `/api/projects/{id}/pbix` | Upload PBIX (multipart) |
| `POST` | `/api/projects/{id}/datasource` | Configure SQL Server, or upload Excel/CSV |
| `POST` | `/api/projects/{id}/datasource/test` | Connection test (fast feedback pre-Analyze) |
| `POST` | `/api/projects/{id}/analyze` | Start pipeline → `{job_id}` (**202 Accepted**) |
| `GET` | `/api/jobs/{job_id}` | Job snapshot (status, stage, pct, counts) |
| `GET` | `/api/jobs/{job_id}/stream` | **SSE** live progress |
| `POST` | `/api/jobs/{job_id}/cancel` | Cooperative cancellation |
| `GET` | `/api/projects/{id}/results` | Summary + per-test rows |
| `GET` | `/api/projects/{id}/report.html\|.pdf\|.xlsx` | Download |
| `POST` | `/api/projects/{id}/explain` | AI failure explanations |

**Progress event shape (SSE `data:` payload)**

```json
{ "job_id":"job_7f3a", "stage":"EXECUTE_SQL", "index":8, "total":10,
  "status":"running", "pct":72,
  "message":"Executing SQL 14/22 — Sales by Category",
  "elapsed_ms":18432 }
```

**Why SSE over WebSockets:** progress is one-directional server→client. SSE is plain
HTTP, auto-reconnects, needs no extra protocol handling, and works through corporate
proxies. Cancellation goes over a normal `POST`.

---

## 5. Job queue (asyncio for MVP)

```python
class JobManager:
    def __init__(self, max_concurrent: int = 2):
        self._jobs: dict[str, Job] = {}
        self._sem = asyncio.Semaphore(max_concurrent)

    async def submit(self, runner, ctx) -> str:      # returns job_id immediately
    async def stream(self, job_id) -> AsyncIterator[ProgressEvent]:
    async def cancel(self, job_id) -> bool:
```

- **Blocking work** (`pbi-tools` subprocess, pyodbc queries, DAX evaluation) runs via
  `asyncio.to_thread(...)` so the event loop never stalls.
- **Cancellation is cooperative**: the runner checks `ctx.cancelled` between stages and
  between SQL executions.
- **Bounded concurrency** (`max_concurrent=2`) — LLM rate limits and Power BI Desktop's
  single-instance model make unbounded parallelism actively harmful.
- **Swap path to Celery/Redis:** `JobManager` is an interface; only `submit` and
  `stream` change. `PipelineRunner` is untouched because it already receives a
  `ProgressReporter` rather than writing to a queue directly.

---

## 6. LLM strategy — one call, not many

The prompt requires the LLM to do 8 things (understand pages, visuals, DAX,
relationships, Power Query, filters; infer rules; generate SQL, tests, docs). Doing
that as 8 calls multiplies latency, cost and failure modes.

**Design: one structured call** with the semantic-model JSON, returning one JSON
document with named sections (`business_rules`, `validation_plan`, `test_cases`,
`documentation`). Python then validates and splits it.

- **Groq + `qwen/qwen3-32b`** (or `deepseek-r1-distill-llama-70b`) via the existing
  OpenAI-compatible client — **no new dependency, no SDK**.
- **Token control:** the JSON builder (stage 3) is where cost is won or lost. It must
  emit a *compact* model (names, types, DAX, relationships, visual bindings) — not raw
  extracted files. Budget ~6–10k input tokens for a 7-table model.
- **Chunking rule:** if the compact JSON exceeds the model's context, split **by report
  page**, never mid-model — each call still sees the full table/measure list.
- **Robustness:** reuse the existing salvage parser (recovers complete objects from
  truncated JSON) — already proven on Groq free-tier truncation.

---

## 7. Migration strategy

The existing codebase is ~80% reusable. This is a **UI + orchestration replacement**,
not a rewrite. Each phase leaves the app working.

| Phase | Work | Risk | Streamlit still works? |
|---|---|---|---|
| **P0** | Add `docs/`, pin deps, install `pbi-tools`, verify `pbi-tools extract` on a native `.pbix` | Low | ✅ |
| **P1** ✅ | `pipeline/` (Runner, JobManager, Context, Progress) wrapping existing services — **done** | Low | ✅ |
| **P2** | Add `api/` + FastAPI app. Endpoints call `PipelineRunner`. Run on `:8000` alongside Streamlit. | Low | ✅ |
| **P3** | Build `web/` 3-screen SPA against the API. | Medium | ✅ |
| **P4** ✅ | `PbixRayExtractor` + `PbixDataService` — **done**, verified on the real `.pbix` | Resolved — pure Python, no external tool | ✅ |
| **P5** | Add PDF + Excel report writers. | Low | ✅ |
| **P6** | Delete `src/ui/`, `vision_service.py`, `screenshot_service.py`; drop screenshot fields from models + storage. | Medium | ❌ (intentional) |
| **P7** | Restructure folders to §2 layout; delete dead code; enforce DI. | Medium | ❌ |

**Do P4 before P6.** Removing screenshots before DAX evaluation works would leave a
window with no source of dashboard values at all.

### Reuse map

| Keep unchanged | Refactor | Delete |
|---|---|---|
| `domain/models.py` (minus screenshot fields) | `metadata_service` → stage | `ui/` (all Streamlit) |
| `storage/`, `core/` | `analysis_service` → single LLM call | `vision_service.py` |
| `services/llm/*` | `sql_validation_engine` → split exec vs compare | `screenshot_service.py` |
| `services/datasources/*` | `test_expansion_service` → stage | `ui/pages/*` |
| `services/validation/*` (value_parser, sql_guard, join_inference, dax_analyzer) | | |
| `services/extractors/power_bi/*` → fallback | | |
| `services/reporting/html_renderer.py` | | |

---

## 8. Cross-cutting

**Logging** — structured, one `job_id` per line, so a run is greppable:
`2026-08-06 12:00:00 | INFO | job=job_7f3a | stage=EXECUTE_SQL | msg=... | elapsed_ms=18432`

**Exceptions** — existing typed hierarchy is retained. FastAPI maps them:
`ValidationError → 400`, `ProjectNotFoundError → 404`, `LLMProviderError → 502`,
`PipelineError → 500`. Every response carries `{error, detail, stage, job_id}`.

**DI** — services are constructed once in `api/deps.py` and injected via
`Depends(...)`. `PipelineRunner` receives them through its constructor, so every stage
is unit-testable with fakes (exactly how the current test suite already works).

**Security** — the read-only SQL guard applies to **every** LLM-generated query before
execution; uploads are extension- and size-checked; `pbi-tools` runs with an explicit
timeout and never with shell interpolation of user input.

---

## 9. Open decisions for you

1. ~~D1 — DAX evaluation~~ **Approved and implemented** via `pbixray` + pandas.
2. ~~PDF engine~~ **WeasyPrint** (approved).
3. ~~CSV datasource~~ **In scope** (approved) — new connector alongside Excel/SQL Server.
4. ~~Multi-user~~ **Single-user local** (approved). No auth or per-user isolation.
