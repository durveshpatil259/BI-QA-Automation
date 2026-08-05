"""Application-wide constants and enumerations.

These enums are the shared vocabulary of the whole system. Every layer
(domain, services, storage, UI) speaks in terms of the types defined here so
that there is a single source of truth for the concepts the product reasons
about: BI platforms, analysis modes, datasource kinds, LLM providers, and the
various status/severity scales.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String-valued enum that serializes to its value.

    Python 3.11+ ships ``enum.StrEnum`` but we define our own tiny variant so
    behaviour is identical and explicit across interpreter versions: members
    compare equal to their string value and JSON-serialize cleanly.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)

    @classmethod
    def from_value(cls, value: str) -> "StrEnum":
        """Case-insensitive lookup by value; raises ValueError if unknown."""
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().lower()
        for member in cls:
            if member.value.lower() == normalized:
                return member
        raise ValueError(f"{value!r} is not a valid {cls.__name__}")

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class BIPlatform(StrEnum):
    """Business Intelligence platforms the product can analyse."""

    POWER_BI = "Power BI"
    TABLEAU = "Tableau"
    QLIK = "Qlik"
    MICROSTRATEGY = "MicroStrategy"


class AnalysisMode(StrEnum):
    """How an analysis run is executed, auto-determined from uploaded assets.

    * METADATA  — only a dashboard file was uploaded.
    * VISUAL    — only screenshots were uploaded.
    * COMPLETE  — both a dashboard file and screenshots were uploaded.
    """

    METADATA = "Metadata Analysis"
    VISUAL = "Visual Analysis"
    COMPLETE = "Complete QA Analysis"


class DatasourceType(StrEnum):
    """Supported datasource kinds for deterministic data validation."""

    SQL_SERVER = "SQL Server"
    EXCEL = "Excel"


class SqlAuthMode(StrEnum):
    """Authentication strategy for SQL Server connections."""

    SQL_LOGIN = "SQL Login"          # username + password
    WINDOWS = "Windows Authentication"  # trusted connection


class LLMProvider(StrEnum):
    """LLM providers. Grok is implemented first; the abstraction layer allows
    the rest to be added without architectural change. Users bring their own
    API keys for every provider.
    """

    GROK = "Grok"
    CLAUDE = "Claude"
    OPENAI = "OpenAI"
    GEMINI = "Gemini"
    DEEPSEEK = "DeepSeek"
    LLAMA = "Llama"
    QWEN = "Qwen"


class AnalysisStatus(StrEnum):
    """Lifecycle of an analysis run."""

    NOT_STARTED = "Not Started"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"


class TestStatus(StrEnum):
    """Result status for a generated test case after validation."""

    PASS = "Pass"
    FAIL = "Fail"
    BLOCKED = "Blocked"
    NOT_EXECUTED = "Not Executed"


class TestCaseKind(StrEnum):
    """Category of generated test case."""

    UNIT = "Developer Unit Test"
    QA = "QA Test"


class Priority(StrEnum):
    """Severity/priority scale used for test cases and recommendations."""

    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class Severity(StrEnum):
    """Severity of a validation finding."""

    INFO = "Info"
    WARNING = "Warning"
    ERROR = "Error"
    CRITICAL = "Critical"


# --- Project folder layout -------------------------------------------------
# Every project on disk uses exactly this sub-folder structure. Centralised
# here so storage, services and UI never hard-code folder names.

class ProjectFolder(StrEnum):
    DASHBOARD = "Dashboard"
    SCREENSHOTS = "Screenshots"
    METADATA = "Metadata"
    REPORTS = "Reports"
    LOGS = "Logs"
    SETTINGS = "Settings"
    CONFIGURATION = "Configuration"
    TEST_CASES = "Generated Test Cases"


# File names used inside a project (kept together for consistency).
PROJECT_FILE = "project.json"                 # <root>/project.json
DATASOURCE_FILE = "datasource.json"           # Configuration/datasource.json
LLM_SETTINGS_FILE = "llm_settings.json"       # Settings/llm_settings.json
ANALYSIS_CONTEXT_FILE = "analysis_context.json"  # Metadata/analysis_context.json
METADATA_FILE = "dashboard_metadata.json"     # Metadata/dashboard_metadata.json
VISUAL_ANALYSIS_FILE = "visual_analysis.json"  # Metadata/visual_analysis.json
AI_REASONING_FILE = "ai_reasoning.json"       # Reports/ai_reasoning.json
TEST_CASES_FILE = "test_cases.json"           # Generated Test Cases/test_cases.json

# Recognised dashboard file extensions per platform (lower-case, no dot list).
DASHBOARD_EXTENSIONS: dict[BIPlatform, tuple[str, ...]] = {
    BIPlatform.POWER_BI: (".pbix", ".pbit", ".pbip", ".pbir", ".zip"),
    BIPlatform.TABLEAU: (".twb", ".twbx"),
    BIPlatform.QLIK: (".qvf", ".qvw"),
    BIPlatform.MICROSTRATEGY: (".mstr", ".json"),
}

SCREENSHOT_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".bmp", ".gif")

# Application identity
APP_NAME = "BI TestPilot AI"
APP_TAGLINE = "Enterprise AI Platform for Business Intelligence QA Automation"
