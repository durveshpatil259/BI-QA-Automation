"""Configuration and activity, read-only.

Two things this must never do: expose a credential, and invent an event.

Credentials live in the environment or the machine config and are reported here
only as "configured" or not — never their value, not even truncated. Activity is
*derived* from what the projects on disk actually record, so the log cannot
disagree with the results it describes; there is no separate event store to
drift out of step with them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import Container, container
from src.core.constants import DASHBOARD_EXTENSIONS, DatasourceType
from src.core.logger import get_logger

_logger = get_logger()

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/settings")
def effective_settings(c: Container = Depends(container)):
    """What the engine is currently configured to do.

    Every value here is safe to display. Anything secret is represented by a
    boolean, and the response is assembled field by field rather than dumping
    the config object — a dump would leak whatever gets added to it later.
    """
    from src.core.config import load_config
    from src.services.llm import provider_registry as registry

    config = load_config()

    extensions = sorted({
        ext for exts in DASHBOARD_EXTENSIONS.values() for ext in exts
    })

    providers = []
    for provider, spec in registry.PROVIDERS.items():
        providers.append({
            "provider": provider.value,
            "default_model": spec.default_model,
            "models": len(spec.known_models),
            "env_var": spec.env_var,
            # Whether a key can be found, never the key.
            "configured": bool(registry.resolve_api_key(provider, "")),
            "tokens_per_day": registry.tokens_per_day_for(provider,
                                                          spec.default_model),
        })

    return {
        "validation": {
            "tolerance_pct": 1.0,
            "note": "A comparison passes when the difference is within this "
                    "percentage. Fixed at 1% and applied by Python, never by "
                    "the model.",
        },
        "llm": {
            "provider": config.default_llm_provider,
            "model": config.default_llm_model,
            "temperature": config.default_llm_temperature,
            "max_tokens": config.default_llm_max_tokens,
            "tokens_per_minute": config.llm_tokens_per_minute,
            "tokens_per_day": config.llm_tokens_per_day or None,
            "min_tokens_to_start": config.llm_min_tokens_to_start,
            "compile_before_llm": config.compile_before_llm,
        },
        "workload": {
            "max_scenarios": config.max_scenarios,
            "max_items_per_call": config.max_items_per_call,
            "values_per_slicer": config.values_per_slicer,
            "max_high_tests_per_subject": config.max_high_tests_per_subject,
            "max_medium_tests_per_subject": config.max_medium_tests_per_subject,
            "max_low_tests_per_subject": config.max_low_tests_per_subject,
        },
        "privacy": {
            "send_sample_values_to_llm": config.send_sample_values_to_llm,
            "note": "When off, the model receives table and column names only "
                    "— never column contents.",
        },
        "supported": {
            "dashboard_files": extensions,
            "datasources": [t.value for t in DatasourceType],
        },
        "providers": providers,
    }


@router.get("/activity")
def activity(limit: int = 60, c: Container = Depends(container)):
    """Recent events, derived from the projects themselves.

    Deliberately not a separate log. An event store written alongside the
    results can disagree with them — claiming an analysis completed for a
    project whose results are absent. Reading the projects means the log is
    always consistent with what is actually on disk, at the cost of only
    reporting events the artifacts still evidence.
    """
    events = []
    for project in c.project_service.list_projects():
        events.append({
            "at": project.created_at.isoformat(),
            "kind": "created", "severity": "ok",
            "project_id": project.id,
            "project": project.name,
            "text": "Project created"
                    + (f" for {project.environment}" if project.environment else ""),
        })

        summary = c.repository.load_run_summary(project)
        if summary is not None:
            failed = summary.tests_failed
            at = (summary.generated_at.isoformat()
                  if hasattr(summary.generated_at, "isoformat")
                  else str(summary.generated_at))
            # The run completing and its tests passing are different facts. A
            # run that finished and found 7 problems succeeded at its job;
            # labelling it "failed" would report the tool as broken rather than
            # the dashboard. Severity carries the findings instead.
            events.append({
                "at": at,
                "kind": "completed",
                "severity": "warn" if failed else "ok",
                "project_id": project.id,
                "project": project.name,
                "text": (f"Analysis completed — {summary.tests_total} test(s), "
                         f"{summary.tests_passed} passed"
                         + (f", {failed} failed" if failed else "")),
            })
            if summary.test_cases:
                # Only claim a reduction where the candidate count was recorded.
                # Older runs have none, and "825 from 825" reads as a selection
                # that removed nothing rather than one that was never measured.
                detail = (f"{summary.test_cases} test case(s) selected from "
                          f"{summary.candidates} candidate(s)"
                          if summary.candidates
                          else f"{summary.test_cases} test case(s) generated")
                events.append({
                    "at": at, "kind": "generated", "severity": "ok",
                    "project_id": project.id, "project": project.name,
                    "text": detail,
                })
        elif str(project.status).casefold().startswith("fail"):
            events.append({
                "at": project.updated_at.isoformat(),
                "kind": "failed", "severity": "error",
                "project_id": project.id,
                "project": project.name,
                "text": "Analysis did not complete",
            })

    events.sort(key=lambda e: e["at"], reverse=True)
    return {"events": events[:max(1, min(limit, 300))], "total": len(events)}
