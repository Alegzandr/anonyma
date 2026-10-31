"""Local LLM extraction with llama-cpp-python (optional ``llm`` extra).

The model answers with strict JSON constrained by a JSON-schema grammar, so it can
neither "think" (Qwen3 thinking mode is also disabled with ``/no_think``) nor drift
out of the format. The answer is validated again and every extracted text is
located back in the segment; anything that cannot be found verbatim is dropped.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anonyma.models import EntityType

#: Types the LLM may return.
LLM_TYPES: tuple[str, ...] = (
    EntityType.PERSON,
    EntityType.ORGANIZATION,
    EntityType.LOCATION,
    EntityType.ADDRESS,
    EntityType.JOB_TITLE,
    EntityType.INDIRECT,
    EntityType.PHONE,
    EntityType.EMAIL,
    EntityType.DATE_OF_BIRTH,
    EntityType.USERNAME,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entites": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "texte": {"type": "string", "minLength": 2, "maxLength": 200},
                    "type": {"type": "string", "enum": list(LLM_TYPES)},
                },
                "required": ["texte", "type"],
            },
        }
    },
    "required": ["entites"],
}

SYSTEM_PROMPT = (
    "Tu es un outil d'anonymisation. Tu repères, dans des textes professionnels en "
    "français, tout ce qui permet d'identifier une personne, directement ou "
    "indirectement. Tu réponds uniquement par un objet JSON."
)
USER_PROMPT = (
    "Liste les éléments identifiants du texte ci-dessous : noms de personnes, "
    "organisations, lieux, adresses, fonctions, et surtout les identifiants indirects "
    "(une description qui désigne une personne précise sans la nommer, par exemple "
    "« la DRH de l'agence de Nantes », « le seul médecin du village », « le fils du "
    "maire »). Recopie chaque élément exactement comme il est écrit dans le texte. "
    'Types possibles : {types}. Si rien n\'est identifiant, réponds {{"entites": []}}.\n\n'
    "Texte :\n<<<\n{text}\n>>>\n/no_think"
)


@dataclass(frozen=True, slots=True)
class LlmMention:
    text: str
    entity_type: str


def available() -> bool:
    return importlib.util.find_spec("llama_cpp") is not None


def parse_answer(raw: str) -> list[LlmMention]:
    """Validate the model answer against :data:`SCHEMA`; invalid items are dropped."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    items = data.get("entites") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[LlmMention] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text, entity_type = item.get("texte"), item.get("type")
        if not isinstance(text, str) or not isinstance(entity_type, str):
            continue
        text = text.strip()
        if entity_type in LLM_TYPES and 2 <= len(text) <= 200:
            out.append(LlmMention(text, entity_type))
    return out


class LlmRunner:
    """One llama.cpp model instance (thread-safe, CPU)."""

    def __init__(self, model_path: Path, *, n_ctx: int = 4096, threads: int = 0) -> None:
        from llama_cpp import Llama

        self.llm = Llama(
            model_path=str(model_path),
            n_ctx=n_ctx,
            n_threads=threads or max(1, (os.cpu_count() or 2) - 1),
            n_gpu_layers=0,
            verbose=False,
        )
        self._lock = threading.Lock()

    def extract(self, text: str, *, max_tokens: int = 768) -> list[LlmMention]:
        prompt = USER_PROMPT.format(types=", ".join(LLM_TYPES), text=text)
        with self._lock:
            response: Any = self.llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object", "schema": SCHEMA},
                temperature=0.0,
                max_tokens=max_tokens,
            )
        try:
            content = response["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return []
        return parse_answer(content)


def chunks(text: str, size: int = 1500, overlap: int = 200) -> Sequence[tuple[int, str]]:
    """Overlapping windows cut on whitespace."""
    if len(text) <= size:
        return [(0, text)]
    out: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind(" ", start + size // 2, end)
            end = cut if cut > start else end
        out.append((start, text[start:end]))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return out


__all__ = ["LLM_TYPES", "SCHEMA", "LlmMention", "LlmRunner", "available", "chunks", "parse_answer"]
