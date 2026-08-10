"""Global application configuration.

The app stores a single JSON config file at ``config/app_config.json`` holding
machine-wide settings that are not tied to any one project — most importantly
the root folder under which all projects are stored on disk.

Design notes
------------
* Configuration is a plain dataclass; loading/saving is explicit and
  side-effect free apart from touching the config file.
* Paths are resolved relative to the repository root so the app runs the same
  regardless of the current working directory Streamlit is launched from.
* No secrets live here. LLM API keys are stored per-project (or per-machine
  settings) and never committed to the global config with defaults.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.core.constants import LLMProvider
from src.core.logger import get_logger

_logger = get_logger()

# Repository root = two levels up from this file (src/core/config.py -> repo).
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
APP_CONFIG_FILE = CONFIG_DIR / "app_config.json"
DEFAULT_PROJECTS_DIR = REPO_ROOT / "projects"


@dataclass
class AppConfig:
    """Machine-wide application settings persisted to ``app_config.json``."""

    projects_root: str = str(DEFAULT_PROJECTS_DIR)
    default_llm_provider: str = LLMProvider.GROK.value
    theme: str = "light"

    # Machine-level LLM defaults. A project inherits these unless it has its
    # own saved settings — which is what lets the SPA configure the LLM once
    # instead of per generated project.
    default_llm_model: str = ""
    default_llm_base_url: str = ""
    default_llm_temperature: float = 0.2
    default_llm_max_tokens: int = 2048

    # SECURITY: when False (the default) the LLM receives table and column
    # NAMES only — never column contents. Sample values improve literal
    # accuracy but are real production data leaving your network, so enabling
    # this is an explicit, informed choice.
    send_sample_values_to_llm: bool = False

    # Workload size. Defaults suit a hosted model; lower both when running a
    # local LLM on CPU, where every generated token costs real seconds.
    #   max_scenarios      - filter combinations validated (1 = no slicer split)
    #   max_items_per_call - queries requested per LLM round-trip
    max_scenarios: int = 10
    max_items_per_call: int = 10
    # Optional machine-level default API keys per provider. Per-project
    # settings always take precedence over these.
    default_api_keys: dict[str, str] = field(default_factory=dict)

    # --- Derived helpers ---------------------------------------------------
    @property
    def projects_root_path(self) -> Path:
        return Path(self.projects_root)

    def ensure_projects_root(self) -> Path:
        """Create the projects root folder if missing and return it."""
        path = self.projects_root_path
        path.mkdir(parents=True, exist_ok=True)
        return path


def load_config() -> AppConfig:
    """Load the global config, creating it with defaults on first run."""
    if not APP_CONFIG_FILE.exists():
        config = AppConfig()
        save_config(config)
        _logger.info("Created default application config at %s", APP_CONFIG_FILE)
        return config

    try:
        raw = json.loads(APP_CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _logger.error("Failed to read app config (%s); falling back to defaults.", exc)
        return AppConfig()

    # Tolerate unknown/missing keys so config survives version upgrades.
    known = {f: raw.get(f, getattr(AppConfig(), f)) for f in AppConfig().__dict__}
    return AppConfig(**known)


def save_config(config: AppConfig) -> None:
    """Persist the global config to disk (pretty-printed JSON)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    APP_CONFIG_FILE.write_text(
        json.dumps(asdict(config), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _logger.debug("Saved application config to %s", APP_CONFIG_FILE)
