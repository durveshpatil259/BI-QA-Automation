"""Entrypoint for the BI TestPilot AI API.

    python main.py            # http://127.0.0.1:8000  (docs at /docs)
    uvicorn src.api.app:app --reload

The single entry point: serves both the JSON API and the SPA in ``web/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.api.app import app  # noqa: E402  (re-exported for `uvicorn main:app`)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.api.app:app", host="127.0.0.1", port=8000, reload=False)
