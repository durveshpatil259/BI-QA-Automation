"""Pipeline stages and their failure policies.

Declaring the failure policy *per stage* (instead of ad-hoc try/except inside the
runner) means the behaviour is inspectable and testable: a failed SQL row must
become a FAIL result, while a missing semantic model must abort the run.
"""

from __future__ import annotations

from enum import Enum


class FailurePolicy(str, Enum):
    """What the runner does when a stage raises."""

    #: Abort the run — later stages cannot produce anything meaningful.
    FATAL = "fatal"
    #: Record a warning and continue with reduced capability.
    DEGRADE = "degrade"
    #: Skip only this stage; it is optional enrichment.
    SKIP = "skip"


class Stage(str, Enum):
    """Ordered pipeline stages. The value is the label shown in the UI."""

    EXTRACT_METADATA = "Extracting metadata"
    READ_SCHEMA = "Reading datasource schema"
    EVALUATE_DAX = "Evaluating DAX measures"
    BUILD_CONTEXT = "Building analysis context"
    LLM_ANALYSIS = "Analysing with AI"
    GENERATE_SQL = "Generating SQL"
    EXECUTE_SQL = "Executing SQL and comparing"
    GENERATE_TESTS = "Generating test cases"
    BUILD_REPORT = "Building report"


#: Execution order. This list *is* the pipeline definition.
STAGE_ORDER: list[Stage] = [
    Stage.EXTRACT_METADATA,
    Stage.READ_SCHEMA,
    Stage.EVALUATE_DAX,
    Stage.BUILD_CONTEXT,
    Stage.LLM_ANALYSIS,
    Stage.GENERATE_SQL,
    Stage.EXECUTE_SQL,
    Stage.GENERATE_TESTS,
    Stage.BUILD_REPORT,
]


#: Stages that spend LLM tokens. Declared here with the rest of the pipeline
#: definition so the runner can skip them all once the daily budget is gone,
#: instead of letting each one discover the same exhausted key.
AI_STAGES: frozenset[Stage] = frozenset({
    Stage.LLM_ANALYSIS,
    Stage.GENERATE_SQL,
    Stage.GENERATE_TESTS,
})


#: Failure policy per stage — see docs/ARCHITECTURE_V2.md §3.
STAGE_POLICY: dict[Stage, FailurePolicy] = {
    # Without a model there is nothing to validate at all.
    Stage.EXTRACT_METADATA: FailurePolicy.FATAL,
    # No datasource -> skip comparison, still produce model-only validation.
    Stage.READ_SCHEMA: FailurePolicy.DEGRADE,
    # DAX evaluation is the source of true dashboard values; without it the
    # comparison falls back to executability rather than exact matching.
    Stage.EVALUATE_DAX: FailurePolicy.DEGRADE,
    Stage.BUILD_CONTEXT: FailurePolicy.FATAL,
    # AI narrative is valuable but never blocks the deterministic report.
    Stage.LLM_ANALYSIS: FailurePolicy.SKIP,
    # No plan -> nothing to execute, but the deterministic context still stands.
    Stage.GENERATE_SQL: FailurePolicy.DEGRADE,
    Stage.EXECUTE_SQL: FailurePolicy.DEGRADE,
    Stage.GENERATE_TESTS: FailurePolicy.DEGRADE,
    Stage.BUILD_REPORT: FailurePolicy.FATAL,
}
