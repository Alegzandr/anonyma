"""GLiNER zero-shot NER on ONNX Runtime (profile STANDARD).

The model folder is located with ``ModelManager().path(spec.id)``; the detector
reports itself unavailable when the model (or onnxruntime/tokenizers) is missing.
The ONNX session is created lazily on the first call, once per process.
"""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Sequence
from pathlib import Path

from anonyma.config import Config
from anonyma.logs import get_logger
from anonyma.model_catalog import DEFAULT_GLINER, ModelKind, ModelSpec, get_spec
from anonyma.models import Detector, Entity, EntityType, Profile, Segment
from anonyma.registry import register_detector

log = get_logger("detectors.gliner")

#: Model label (English, as trained) -> entity type.
LABELS: dict[str, str] = {
    "person": EntityType.PERSON,
    "organization": EntityType.ORGANIZATION,
    "location": EntityType.LOCATION,
    "address": EntityType.ADDRESS,
    "job title": EntityType.JOB_TITLE,
    "phone number": EntityType.PHONE,
    "email address": EntityType.EMAIL,
    "date of birth": EntityType.DATE_OF_BIRTH,
    "username": EntityType.USERNAME,
    "iban": EntityType.IBAN,
    "social security number": EntityType.NIR,
    "credit card number": EntityType.CREDIT_CARD,
    "license plate number": EntityType.LICENSE_PLATE,
    "identity document number": EntityType.CASE_NUMBER,
    "ip address": EntityType.IP_ADDRESS,
}

_models: dict[tuple[str, str, int], object] = {}
_lock = threading.Lock()


def model_spec(config: Config) -> ModelSpec | None:
    spec = get_spec(config.gliner_model or DEFAULT_GLINER)
    return spec if spec is not None and spec.kind is ModelKind.GLINER else None


def model_dir(spec: ModelSpec) -> Path | None:
    from anonyma.model_manager import ModelManager

    path = ModelManager().path(spec.id)
    if path is None:
        return None
    onnx = spec.extra.get("onnx", "model.onnx")
    required = [onnx, "tokenizer.json"]
    return path if all((path / name).is_file() for name in required) else None


@register_detector
class GlinerDetector(Detector):
    name = "gliner"
    min_profile = Profile.STANDARD

    @classmethod
    def is_available(cls, config: Config) -> bool:
        if importlib.util.find_spec("onnxruntime") is None:
            return False
        if importlib.util.find_spec("tokenizers") is None:
            return False
        spec = model_spec(config)
        return spec is not None and model_dir(spec) is not None

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.labels: dict[str, str] = dict(LABELS)
        for label, entity_type in config.custom_labels.items():
            if label.strip():
                self.labels[label.strip()] = entity_type.strip().upper() or EntityType.BUSINESS_TERM
        self.threshold = float(getattr(config, "gliner_threshold", 0.3))

    def _model(self) -> object:
        from anonyma.gliner_onnx import GlinerOnnx, default_threads

        spec = model_spec(self.config)
        if spec is None:
            raise RuntimeError("GLiNER model not configured")
        path = model_dir(spec)
        if path is None:
            raise RuntimeError("GLiNER model files missing")
        threads = int(getattr(self.config, "gliner_threads", 0)) or default_threads(
            self.config.workers
        )
        key = (str(path), spec.extra.get("onnx", "model.onnx"), threads)
        with _lock:
            if key not in _models:
                _models[key] = GlinerOnnx(path, key[1], threads=threads)
            return _models[key]

    def detect(self, segments: Sequence[Segment], document_id: str) -> list[Entity]:
        from anonyma.gliner_onnx import GlinerOnnx

        todo = [s for s in segments if any(c.isalpha() for c in s.text)]
        if not todo:
            return []
        model = self._model()
        assert isinstance(model, GlinerOnnx)
        labels = list(self.labels)
        predictions = model.predict([s.text for s in todo], labels, threshold=self.threshold)
        found: list[Entity] = []
        for segment, spans in zip(todo, predictions, strict=True):
            for span in spans:
                entity_type = self.labels.get(span.label)
                if entity_type is None:
                    continue
                found.append(
                    Entity(
                        entity_type=entity_type,
                        value=segment.text[span.start : span.end],
                        document_id=document_id,
                        segment_id=segment.id,
                        start=span.start,
                        end=span.end,
                        score=round(span.score, 3),
                        detector=self.name,
                        attributes={"label": span.label},
                    )
                )
        return found


__all__ = ["LABELS", "GlinerDetector", "model_dir", "model_spec"]
