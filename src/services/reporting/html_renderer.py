"""Render an :class:`AnalysisReport` to a self-contained HTML document.

Uses Jinja2 with a fully inline stylesheet so the exported file opens anywhere
with no external assets. All dynamic values are auto-escaped by Jinja2.
"""

from __future__ import annotations

from src.core.constants import APP_NAME, APP_TAGLINE
from src.domain.models import AnalysisReport

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ report.project_name }} — QA Report</title>
<style>
  :root { --line:#e5e7eb; --muted:#6b7280; --ink:#111827; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         color: var(--ink); margin: 0; padding: 0 24px 64px; line-height: 1.5; }
  .wrap { max-width: 1000px; margin: 0 auto; }
  header { padding: 28px 0 12px; border-bottom: 2px solid var(--ink); margin-bottom: 20px; }
  header h1 { margin: 0; font-size: 1.5rem; }
  header .tag { color: var(--muted); font-size: .85rem; }
  .meta { display: flex; flex-wrap: wrap; gap: 8px 24px; margin: 12px 0 4px; font-size: .85rem; }
  .meta b { color: var(--muted); font-weight: 600; }
  h2 { font-size: 1.15rem; margin: 28px 0 8px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
  .cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 8px 0; }
  .card { border: 1px solid var(--line); border-radius: 10px; padding: 10px 16px; min-width: 110px; }
  .card .n { font-size: 1.4rem; font-weight: 700; }
  .card .l { color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; }
  table { border-collapse: collapse; width: 100%; font-size: .82rem; margin: 8px 0; }
  th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
  th { background: #f9fafb; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: .72rem; font-weight: 600; }
  .s-pass { background:#dcfce7; color:#166534; } .s-fail { background:#fee2e2; color:#991b1b; }
  .s-warn { background:#fef3c7; color:#92400e; } .s-info { background:#dbeafe; color:#1e40af; }
  .s-gray { background:#f3f4f6; color:#374151; }
  .muted { color: var(--muted); }
  ul { margin: 6px 0; padding-left: 20px; }
  .pre { white-space: pre-wrap; }
  footer { margin-top: 40px; padding-top: 12px; border-top: 1px solid var(--line);
           color: var(--muted); font-size: .78rem; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{{ report.project_name }} — QA Report</h1>
    <div class="tag">{{ app_name }} · {{ app_tagline }}</div>
    <div class="meta">
      <span><b>Platform</b> {{ report.platform }}</span>
      <span><b>Mode</b> {{ report.analysis_mode }}</span>
      <span><b>Status</b> {{ report.status }}</span>
      <span><b>Generated</b> {{ report.created_at.strftime('%Y-%m-%d %H:%M') }}</span>
      {% if report.llm_provider %}<span><b>AI</b> {{ report.llm_provider }} ({{ report.llm_model }})</span>{% endif %}
      <span><b>Report ID</b> {{ report.id }}</span>
    </div>
  </header>

  <h2>Validation summary</h2>
  <div class="cards">
    <div class="card"><div class="n">{{ vs.total }}</div><div class="l">Checks</div></div>
    <div class="card"><div class="n">{{ vs.passed }}</div><div class="l">Passed</div></div>
    <div class="card"><div class="n">{{ vs.failed }}</div><div class="l">Failed</div></div>
    <div class="card"><div class="n">{{ vs.critical }}</div><div class="l">Critical</div></div>
    <div class="card"><div class="n">{{ report.test_cases|length }}</div><div class="l">Test cases</div></div>
  </div>

  {% set opt = tu.optimization or {} %}
  {% if opt and opt.candidate_tests %}
  <h2>Test selection</h2>
  <p class="muted">
    The suite is chosen, not enumerated. Every candidate below was generated
    deterministically from the dashboard; what reaches this report is the
    subset that proves something no other test already proves.
  </p>
  <div class="cards">
    <div class="card"><div class="n">{{ '{:,}'.format(opt.candidate_tests) }}</div><div class="l">Candidates generated</div></div>
    <div class="card"><div class="n">{{ '{:,}'.format(opt.selected_tests) }}</div><div class="l">Selected</div></div>
    <div class="card"><div class="n">{{ '{:,}'.format(opt.duplicates_removed) }}</div><div class="l">Duplicates removed</div></div>
    <div class="card"><div class="n">{{ '{:,}'.format(opt.low_value_skipped) }}</div><div class="l">Low-value skipped</div></div>
  </div>
  {% if opt.by_priority %}
  <p class="muted">
    Selected by priority —
    {% for name, count in opt.by_priority.items() %}
      {{ name }}: <b>{{ count }}</b>{{ ", " if not loop.last }}
    {% endfor %}.
    High-priority data validation is never trimmed; the caps apply only to the
    restatements around it.
  </p>
  {% endif %}

  <h2>How much of this needed AI</h2>
  <div class="cards">
    <div class="card"><div class="n">{{ '{:,}'.format(opt.compiled_without_llm) }}</div><div class="l">Computed by Python</div></div>
    <div class="card"><div class="n">{{ '{:,}'.format(opt.plan_items) }}</div><div class="l">Validations in plan</div></div>
    <div class="card"><div class="n">{{ opt.llm_calls }}</div><div class="l">LLM calls</div></div>
    <div class="card"><div class="n">{{ '%.0f'|format(opt.compiled_pct) }}%</div><div class="l">Compiled, not generated</div></div>
  </div>
  <p class="muted">
    A measure whose DAX Python can express is compiled directly to SQL and
    never sent to a model — it is both cheaper and more trustworthy, because it
    derives from what the dashboard computes rather than a restatement of it.
    Every PASS/FAIL below was decided in Python by comparing numbers, never by
    asking a model whether two values agree.
  </p>
  {% endif %}

  {% if tu and tu.total_tokens %}
  <h2>AI token usage</h2>
  <div class="cards">
    <div class="card"><div class="n">{{ '{:,}'.format(tu.total_tokens) }}</div><div class="l">Total tokens</div></div>
    <div class="card"><div class="n">{{ '{:,}'.format(tu.prompt_tokens) }}</div><div class="l">Prompt</div></div>
    <div class="card"><div class="n">{{ '{:,}'.format(tu.completion_tokens) }}</div><div class="l">Completion</div></div>
    <div class="card"><div class="n">{{ tu.total_calls }}</div><div class="l">API calls</div></div>
  </div>
  <table>
    <thead><tr><th>Stage</th><th>Calls</th><th>Prompt</th><th>Completion</th><th>Total</th><th>Share</th></tr></thead>
    <tbody>
      {% for s in tu.by_stage %}
      <tr>
        <td>{{ s.stage }}</td>
        <td>{{ s.calls }}</td>
        <td>{{ '{:,}'.format(s.prompt_tokens) }}</td>
        <td>{{ '{:,}'.format(s.completion_tokens) }}</td>
        <td><b>{{ '{:,}'.format(s.total_tokens) }}</b></td>
        <td>{{ '%.0f'|format(100 * s.total_tokens / tu.total_tokens) }}%</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <p class="muted">
    Providers meter prompt + reserved output tokens against both per-minute and
    per-day quotas{% if tu.models %} · model{{ 's' if tu.models|length > 1 }}:
    {{ tu.models|join(', ') }}{% endif %}.
  </p>
  {% endif %}

  <h2>Executive summary</h2>
  <div class="pre">{{ report.executive_summary or 'Not generated.' }}</div>

  <h2>Root cause analysis</h2>
  <div class="pre">{{ report.root_cause_analysis or 'Not generated.' }}</div>

  <h2>Recommendations</h2>
  {% if report.recommendations %}
  <ol>{% for r in report.recommendations %}<li>{{ r }}</li>{% endfor %}</ol>
  {% else %}<p class="muted">None.</p>{% endif %}

  <h2>Validation findings</h2>
  {% if findings %}
  <table>
    <tr><th>Rule</th><th>Severity</th><th>Category</th><th>Finding</th><th>Entity</th><th>Detail</th></tr>
    {% for f in findings %}
    <tr>
      <td>{{ f.rule_id }}</td>
      <td><span class="pill {{ sev_class(f) }}">{{ f.severity }}</span></td>
      <td>{{ f.category }}</td>
      <td>{{ f.title }}</td>
      <td>{{ f.entity }}</td>
      <td>{{ f.description }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}<p class="muted">No findings.</p>{% endif %}

  {% if report.sql_validations %}
  <h2>Data validation (dashboard vs database)</h2>
  <div class="cards">
    <div class="card"><div class="n">{{ dvs.get('total', 0) }}</div><div class="l">Tests</div></div>
    <div class="card"><div class="n">{{ dvs.get('passed', 0) }}</div><div class="l">Pass</div></div>
    <div class="card"><div class="n">{{ dvs.get('failed', 0) }}</div><div class="l">Fail</div></div>
    <div class="card"><div class="n">{{ dvs.get('errors', 0) }}</div><div class="l">Errors</div></div>
  </div>
  <table>
    <tr><th>Test ID</th><th>KPI</th><th>Dashboard</th><th>{{ evidence_label }}</th>
        <th>Database</th><th>Difference</th><th>Time (ms)</th><th>Status</th></tr>
    {% for r in report.sql_validations %}
    <tr>
      <td>{{ r.test_id }}</td><td>{{ r.kpi_name }}</td><td>{{ r.dashboard_value }}</td>
      <td class="pre">{{ r.source_evidence or r.generated_sql }}</td><td>{{ r.database_value }}</td>
      <td>{{ r.difference }}</td><td>{{ r.execution_time_ms }}</td>
      <td><span class="pill {{ dv_status_class(r) }}">{{ r.status }}</span></td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}

  {% if report.comparisons %}
  <h2>Datasource comparison</h2>
  <table>
    <tr><th>Check</th><th>Dashboard</th><th>Datasource</th><th>Match</th><th>Difference</th></tr>
    {% for c in report.comparisons %}
    <tr>
      <td>{{ c.label }}</td><td>{{ c.dashboard_value }}</td><td>{{ c.datasource_value }}</td>
      <td><span class="pill {{ 's-pass' if c.matched else 's-fail' }}">{{ '✓' if c.matched else '✗' }}</span></td>
      <td>{{ c.difference }}</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}

  <h2>Test cases ({{ report.test_cases|length }})</h2>
  {% if report.test_cases %}
  <table>
    <tr>
      <th>ID</th><th>Kind</th><th>Module</th><th>Scenario</th><th>Steps</th><th>Test Data</th>
      <th>Expected</th><th>Actual</th><th>Status</th><th>Priority</th><th>Remarks</th>
    </tr>
    {% for t in report.test_cases %}
    <tr>
      <td>{{ t.test_case_id }}</td><td>{{ t.kind }}</td><td>{{ t.module }}</td>
      <td>{{ t.test_scenario }}</td><td class="pre">{{ t.test_steps }}</td><td>{{ t.test_data }}</td>
      <td>{{ t.expected_result }}</td><td>{{ t.actual_result }}</td>
      <td><span class="pill {{ status_class(t) }}">{{ t.status }}</span></td>
      <td>{{ t.priority }}</td><td>{{ t.remarks }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}<p class="muted">No test cases generated.</p>{% endif %}

  <footer>Generated by {{ app_name }}. Deterministic analysis by Python; narrative and
  test-case authoring by the configured LLM; verdicts auto-populated from validation evidence.</footer>
</div>
</body>
</html>
"""


def _sev_class(finding) -> str:
    from src.core.constants import Severity

    return {
        Severity.CRITICAL: "s-fail", Severity.ERROR: "s-fail",
        Severity.WARNING: "s-warn", Severity.INFO: "s-info",
    }.get(finding.severity, "s-gray")


def _status_class(case) -> str:
    from src.core.constants import TestStatus

    return {
        TestStatus.PASS: "s-pass", TestStatus.FAIL: "s-fail",
        TestStatus.BLOCKED: "s-warn", TestStatus.NOT_EXECUTED: "s-gray",
    }.get(case.status, "s-gray")


def render_html(report: AnalysisReport) -> str:
    from jinja2 import Environment

    env = Environment(autoescape=True)
    template = env.from_string(_TEMPLATE)
    # Show failing findings first, then the rest.
    findings = sorted(report.findings, key=lambda f: (f.passed, f.rule_id))
    return template.render(
        report=report,
        vs=report.validation_summary or {"total": 0, "passed": 0, "failed": 0, "critical": 0},
        dvs=report.data_validation_summary or {},
        tu=report.token_usage or {},
        evidence_label=(
            "Generated SQL"
            if all(
                (v.source_evidence or v.generated_sql or "").strip().upper().startswith("SELECT")
                for v in report.sql_validations if (v.source_evidence or v.generated_sql)
            )
            else "How it was calculated"
        ),
        findings=findings,
        sev_class=_sev_class,
        status_class=_status_class,
        dv_status_class=_status_class,
        app_name=APP_NAME,
        app_tagline=APP_TAGLINE,
    )
