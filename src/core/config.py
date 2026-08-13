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
ENV_FILE = REPO_ROOT / ".env"


def _load_env_file() -> None:
    """Read ``.env`` into the environment, once, at import time.

    Keys belong in the environment rather than a JSON file that is easy to
    commit by accident. A ``.env`` at the repo root is the least-friction way
    to set them on a developer machine; real deployments set the variables
    directly. Existing environment variables always win, so a shell export
    overrides the file.
    """
    if not ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        _logger.info("python-dotenv not installed; .env not loaded.")
        return
    load_dotenv(ENV_FILE, override=False)
    _logger.info("Loaded environment from %s", ENV_FILE)


_load_env_file()


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
    #
    # The schema, the rules and the table map are re-sent on every round-trip
    # and dwarf the part that actually varies, so batch size is the main lever
    # on what a run costs: at 10 items a run spent 84% of each prompt re-saying
    # the same thing. Raise it further only while watching the batch-error
    # count — too many items in one reply and the JSON truncates mid-object.
    max_scenarios: int = 10
    max_items_per_call: int = 15

    # Client-side pacing against the provider's tokens-per-minute cap. A batch
    # costs ~5,400 tokens, so only two fit Groq's free-tier 12,000 TPM window;
    # sending nine at once got most of them rejected *and* still charged them
    # against the daily quota. 0 disables pacing (paid tiers, local models).
    llm_tokens_per_minute: int = 12000

    # The daily cap, which pacing cannot solve — waiting does not refill it.
    # Tracked per key in config/token_usage.json and checked before each call,
    # so a spent key stops the run cleanly instead of failing every remaining
    # batch with a 429. 0 = use the provider's known free-tier figure; set a
    # number here to override it for every provider.
    llm_tokens_per_day: int = 0
    # Refuse to start a run with less than this left, rather than burning the
    # remainder on a report that would be too incomplete to act on. 0 = start
    # whenever any budget at all remains.
    llm_min_tokens_to_start: int = 6000

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
