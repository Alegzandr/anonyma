"""OCR engine factory (RapidOCR / ONNX).

Phase 0 stub, owned by the mails/archives/images workstream. Contract:
``create_engine(config)`` returns an ``models.OcrEngine`` or None when OCR is
disabled or unavailable.
"""

from __future__ import annotations

from anonyma.config import Config
from anonyma.models import OcrEngine


def create_engine(config: Config) -> OcrEngine | None:
    return None
