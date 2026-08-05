"""Test Case Generator (Module 9).

The LLM generates enterprise-format test cases from the deterministic
AnalysisContext (scenario, steps, expected result, priority). Python then
**deterministically auto-populates** Actual Result, Status and Remarks by
cross-referencing each generated case against the validation findings — so the
verdict is grounded in Python's evidence, never the model's opinion.
"""

from __future__ import annotations

from src.core.constants import Priority, TestCaseKind, TestStatus
from src.core.exceptions import LLMResponseError
from src.core.logger import get_logger
from src.domain.models import (
    AnalysisContext,
    LLMSettings,
    TestCase,
    ValidationFinding,
)
from src.services.llm import create_client
from src.services.llm.base import LLMClient
from src.services.llm.json_utils import extract_json
from src.services.llm.prompt_builder import (
    TESTCASE_SYSTEM_PROMPT,
    build_testcase_user_prompt,
)
from src.storage.project_repository import ProjectRepository

_logger = get_logger()

_MAX_CASES = 40


class TestCaseService:
    """Generates, auto-populates and persists enterprise test cases."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def load(self, project) -> list[TestCase]:
        return self._repo.load_test_cases(project)

    # --- generation -------------------------------------------------------
    def generate(
        self,
        project,
        context: AnalysisContext,
        settings: LLMSettings | None = None,
        *,
        client: LLMClient | None = None,
    ) -> list[TestCase]:
        settings = settings or (self._repo.load_llm_settings(project) or LLMSettings())
        client = client or create_client(settings)

        response = client.complete(
            TESTCASE_SYSTEM_PROMPT, build_testcase_user_prompt(context)
        )
        raw_cases = self._parse_cases(response.content)
        cases = [self._to_test_case(rc) for rc in raw_cases[:_MAX_CASES]]

        self._autopopulate(cases, raw_cases, context.validations)
        self._repo.save_test_cases(project, cases)
        _logger.info("Generated %d test case(s) for %s", len(cases), project.id)
        return cases

    # --- parsing ----------------------------------------------------------
    # Keys that identify an object as a test case (used when salvaging).
    _CASE_KEYS = {"test_scenario", "expected_result", "test_steps", "module"}

    @staticmethod
    def _parse_cases(content: str) -> list[dict]:
        data = extract_json(content)
        if isinstance(data, dict):
            data = data.get("test_cases", data.get("testCases"))
        if isinstance(data, list):
            cases = [c for c in data if isinstance(c, dict)]
            if cases:
                return cases

        # Salvage path: recover complete test-case objects even from a truncated
        # or prose-wrapped response (common on small max_tokens / free tiers).
        salvaged = TestCaseService._salvage_cases(content)
        if salvaged:
            _logger.warning("Recovered %d test case(s) from a partial response.", len(salvaged))
            return salvaged

        raise LLMResponseError(
            "The LLM did not return usable test cases (likely truncated). Increase "
            "'Max tokens' in the LLM configuration, or try again."
        )

    @staticmethod
    def _salvage_cases(content: str) -> list[dict]:
        """Extract every complete JSON object from *content* and keep the ones
        that look like test cases — tolerating an unclosed trailing object."""
        import json

        objects: list[dict] = []
        stack: list[int] = []
        in_str = False
        esc = False
        for i, ch in enumerate(content or ""):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                stack.append(i)
            elif ch == "}" and stack:
                start = stack.pop()
                try:
                    obj = json.loads(content[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict) and (TestCaseService._CASE_KEYS & obj.keys()):
                    objects.append(obj)
        return objects

    @staticmethod
    def _coerce_kind(value: str) -> TestCaseKind:
        v = (value or "").strip().lower()
        return TestCaseKind.UNIT if v in ("unit", "developer", "dev") else TestCaseKind.QA

    @staticmethod
    def _coerce_priority(value: str) -> Priority:
        try:
            return Priority.from_value(value)
        except ValueError:
            return Priority.MEDIUM

    def _to_test_case(self, rc: dict) -> TestCase:
        return TestCase(
            kind=self._coerce_kind(rc.get("kind", "")),
            module=str(rc.get("module", "")).strip(),
            test_scenario=str(rc.get("test_scenario", "")).strip(),
            test_steps=str(rc.get("test_steps", "")).strip(),
            test_data=str(rc.get("test_data", "")).strip(),
            expected_result=str(rc.get("expected_result", "")).strip(),
            priority=self._coerce_priority(rc.get("priority", "")),
            status=TestStatus.NOT_EXECUTED,
        )

    # --- deterministic auto-population -----------------------------------
    def _autopopulate(
        self,
        cases: list[TestCase],
        raw_cases: list[dict],
        findings: list[ValidationFinding],
    ) -> None:
        for case, rc in zip(cases, raw_cases):
            matches = self._match_findings(rc, findings)
            if not matches:
                case.status = TestStatus.NOT_EXECUTED
                case.actual_result = ""
                case.remarks = (
                    "No matching deterministic finding; requires manual verification."
                )
                continue

            failing = [f for f in matches if not f.passed]
            if failing:
                case.status = TestStatus.FAIL
                case.actual_result = self._summarise(failing, actual=True)
                case.remarks = self._summarise(failing, actual=False)
            else:
                case.status = TestStatus.PASS
                case.actual_result = "Matches expected result."
                case.remarks = self._summarise(matches, actual=False)

    @staticmethod
    def _match_findings(rc: dict, findings: list[ValidationFinding]) -> list[ValidationFinding]:
        rule_id = str(rc.get("related_rule_id", "")).strip()
        entity = str(rc.get("related_entity", "")).strip().casefold()

        by_rule = [f for f in findings if rule_id and f.rule_id == rule_id]
        by_entity = [
            f for f in findings if entity and f.entity and entity in f.entity.casefold()
        ]

        if by_rule and by_entity:
            both = [f for f in by_rule if f in by_entity]
            return both or by_rule
        if by_rule:
            return by_rule
        return by_entity

    @staticmethod
    def _summarise(findings: list[ValidationFinding], *, actual: bool) -> str:
        parts = []
        for f in findings[:5]:
            if actual:
                parts.append(f.actual or f.description or f.title)
            else:
                parts.append(f"[{f.rule_id}] {f.description or f.title}")
        return " | ".join(p for p in parts if p)
