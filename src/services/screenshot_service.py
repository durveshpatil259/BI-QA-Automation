"""Screenshot processing service.

Deterministically extracts facts from uploaded dashboard screenshots — image
dimensions, format and file size, plus **optional** OCR text — and assembles a
:class:`VisualAnalysis`. This is Python-side work; the AI later *reasons* over
these facts (e.g. comparing on-screen numbers to metadata) but never extracts
them itself.

OCR is an optional enhancement: if the ``pytesseract`` package and the Tesseract
binary are available, detected text is captured; otherwise processing continues
with a clear note instead of failing.
"""

from __future__ import annotations

from pathlib import Path

from src.core.constants import SCREENSHOT_EXTENSIONS
from src.core.logger import get_logger
from src.domain.models import Project, Screenshot, VisualAnalysis
from src.storage import file_manager as fm
from src.storage.project_repository import ProjectRepository

_logger = get_logger()


class ScreenshotService:
    """Processes screenshots into deterministic :class:`VisualAnalysis` facts."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository
        self._ocr_checked = False
        self._ocr_available = False

    # --- OCR capability (checked once, lazily) ---------------------------
    def ocr_available(self) -> bool:
        if self._ocr_checked:
            return self._ocr_available
        self._ocr_checked = True
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401

            # Probe the Tesseract binary; missing binary raises at call time.
            pytesseract.get_tesseract_version()
            self._ocr_available = True
        except Exception as exc:  # noqa: BLE001 - package or binary missing
            _logger.info("OCR unavailable (%s); screenshots processed without text.", exc)
            self._ocr_available = False
        return self._ocr_available

    def has_screenshots(self, project: Project) -> bool:
        paths = self._repo.paths_for(project)
        return bool(fm.list_dir(paths.screenshots_dir, SCREENSHOT_EXTENSIONS))

    # --- processing -------------------------------------------------------
    def process(self, project: Project, *, run_ocr: bool = True) -> VisualAnalysis:
        """Process all screenshots for *project* and persist the result."""
        paths = self._repo.paths_for(project)
        files = fm.list_dir(paths.screenshots_dir, SCREENSHOT_EXTENSIONS)

        analysis = VisualAnalysis()
        if not files:
            analysis.warnings.append("No screenshots found to process.")
            self._repo.save_visual_analysis(project, analysis)
            return analysis

        use_ocr = run_ocr and self.ocr_available()
        if run_ocr and not use_ocr:
            analysis.warnings.append(
                "OCR engine not available; text was not extracted from screenshots. "
                "Install 'pytesseract' and the Tesseract binary to enable it."
            )

        for path in files:
            analysis.screenshots.append(self._process_one(path, use_ocr, analysis))

        analysis.total_screenshots = len(analysis.screenshots)
        self._repo.save_visual_analysis(project, analysis)
        _logger.info(
            "Processed %d screenshot(s) for project %s (ocr=%s)",
            analysis.total_screenshots, project.id, use_ocr,
        )
        return analysis

    def _process_one(self, path: Path, use_ocr: bool, analysis: VisualAnalysis) -> Screenshot:
        shot = Screenshot(file_name=path.name)
        try:
            shot.size_bytes = path.stat().st_size
        except OSError:
            shot.size_bytes = None

        try:
            from PIL import Image

            with Image.open(path) as img:
                shot.width, shot.height = img.size
                shot.format = img.format or path.suffix.lstrip(".").upper()
                if use_ocr:
                    shot.detected_text = self._ocr(img, path, analysis)
        except Exception as exc:  # noqa: BLE001 - unreadable/corrupt image
            shot.notes = f"Could not read image: {exc}"
            analysis.warnings.append(f"{path.name}: {shot.notes}")
        return shot

    @staticmethod
    def _ocr(img, path: Path, analysis: VisualAnalysis) -> str:
        try:
            import pytesseract

            text = pytesseract.image_to_string(img)
            return " ".join(text.split())  # collapse whitespace/newlines
        except Exception as exc:  # noqa: BLE001
            analysis.warnings.append(f"OCR failed for {path.name}: {exc}")
            return ""

    # --- load -------------------------------------------------------------
    def load(self, project: Project) -> VisualAnalysis | None:
        return self._repo.load_visual_analysis(project)
