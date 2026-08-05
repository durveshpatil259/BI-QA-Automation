# BI TestPilot AI

**Enterprise AI Platform for Business Intelligence QA Automation**

An AI-powered QA automation platform for Business Intelligence dashboards. It
automates developer testing, QA testing, data/visual/metadata validation,
regression testing, AI-assisted root-cause analysis and AI-generated test cases
across **Power BI, Tableau, Qlik and MicroStrategy**.

> **Core principle:** Python performs *all* deterministic work — parsing,
> datasource access, comparison and validation — and assembles a single
> **Analysis Context**. Only then is the selected LLM called, purely to reason
> and generate (summaries, root-cause analysis, recommendations, test cases).
> The LLM never reads a datasource, executes SQL, parses dashboards or performs
> comparisons.

## Tech stack

Python · Streamlit · pure-Python services · local project-folder storage ·
JSON configuration. **No** Docker/Kubernetes, cloud, authentication, RAG or
multi-agent in the MVP. Users bring their own LLM API keys.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

The app creates `config/app_config.json` and a `projects/` folder on first run.

## Architecture

Layered Clean Architecture — dependencies point inward only:

```
UI (Streamlit)  ->  Services  ->  Domain  <-  Storage
                                    ^
                              Core (config, constants, logging, exceptions)
```

| Layer | Location | Responsibility |
|-------|----------|----------------|
| Core | `src/core` | Constants/enums, config, logging, exceptions |
| Domain | `src/domain` | Pure dataclass models + JSON serialization |
| Storage | `src/storage` | Project-folder persistence (JSON + assets) |
| Services | `src/services` | Business logic & the deterministic→AI pipeline |
| UI | `src/ui` | Streamlit pages, components, theme, state |

### Project storage layout (per project)

```
projects/<name>__<id>/
    project.json
    Dashboard/              raw uploaded dashboard files
    Screenshots/            raw uploaded screenshots
    Metadata/               dashboard_metadata.json, analysis_context.json
    Reports/                <report-id>.json
    Logs/                   analysis.log
    Settings/               llm_settings.json
    Configuration/          datasource.json
    Generated Test Cases/   exported test cases
```

## Build roadmap (module-by-module) — MVP complete ✅

1. **Foundation** ✅ — structure, core, domain models, storage, runnable shell
2. **Project Manager** ✅ — create/open/edit/delete projects
3. **Dashboard & Screenshot Upload** ✅ — auto analysis-mode detection
4. **Datasource Configuration** ✅ — SQL Server & Excel, read-only connectors
5. **Metadata Extraction** ✅ — Power BI (PBIX/PBIT/PBIP/PBIR: TOM + TMDL + report layout); Tableau/Qlik/MicroStrategy pluggable
6. **Screenshot Processing** ✅ — image facts + optional OCR
7. **Comparison + Validation + Rule Engine** ✅ — deterministic; assembles the Analysis Context
8. **LLM Engine** ✅ — provider abstraction (Grok first; OpenAI/DeepSeek/Qwen pluggable)
9. **Test Case Generator** ✅ — LLM-authored, Python auto-populates Actual/Status/Remarks
10. **Report Generator, History, Settings** ✅ — combined report + HTML/CSV export

### Post-MVP extensions (pluggable, no architectural change)
- Tableau / Qlik / MicroStrategy metadata extractors
- Claude / Gemini / Llama LLM clients
- RAG and multi-agent orchestration
