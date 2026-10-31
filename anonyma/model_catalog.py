"""Catalogue of downloadable models.

Contract between the detection workstream (fills ``CATALOG``) and the
infrastructure workstream (``model_manager`` downloads/verifies what is listed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ModelKind(StrEnum):
    GLINER = "gliner"
    LLM = "llm"
    OCR = "ocr"


@dataclass(frozen=True, slots=True)
class ModelFile:
    #: Path inside the Hugging Face repository.
    filename: str
    #: Expected SHA-256 (hex). Empty only while the entry is being authored.
    sha256: str = ""
    size: int = 0


@dataclass(frozen=True, slots=True)
class ModelSpec:
    #: Stable local id, e.g. "gliner-multi-pii"; also the folder name in models/.
    id: str
    kind: ModelKind
    repo_id: str
    #: Pinned commit hash of the repository.
    revision: str
    files: tuple[ModelFile, ...]
    #: License declared on Hugging Face (SPDX-like, lower case), checked again at
    #: download time against the live repository metadata.
    license: str
    title: str  # French, shown in the GUI
    description: str = ""  # French
    #: Minimum RAM (bytes) recommended to run it.
    min_ram: int = 0
    extra: dict[str, str] = field(default_factory=dict)


GiB = 1024**3

#: Tokenizer/config files shared by both GLiNER precisions (filip-w/gliner-multi-pii-onnx,
#: an Apache-2.0 ONNX export of urchade/gliner_multi_pii-v1, mdeberta-v3-base backbone).
_GLINER_REPO = "filip-w/gliner-multi-pii-onnx"
_GLINER_REVISION = "6b33889739246bb5c88f0a0c78960bd1be606d2d"
_GLINER_COMMON = (
    ModelFile(
        "config.json", "90023a6b0863a66326bfcecf9e693e114ce2b7e42306bdd467476a48b6724e71", 580
    ),
    ModelFile(
        "gliner_config.json",
        "c1e16b011e25e85f89789a6ed377bf012b397b1c6366ae0817f007c7d2401d0b",
        2280,
    ),
    ModelFile(
        "tokenizer.json",
        "c5b8041501fcdee792b9b112dd592861f04391a24633a73fd9f05aaaf6e8eff1",
        16034824,
    ),
    ModelFile(
        "tokenizer_config.json",
        "c12b7b3bab6cd638f70b203daf1438bf7fedbeab4728a1e155728af9a5a55687",
        2928,
    ),
)

CATALOG: list[ModelSpec] = [
    ModelSpec(
        id="gliner-multi-pii",
        kind=ModelKind.GLINER,
        repo_id=_GLINER_REPO,
        revision=_GLINER_REVISION,
        files=(
            *_GLINER_COMMON,
            ModelFile(
                "model.onnx",
                "da317b3dafb6ea26c19bc8725d3c969f788a4669e52e080cf5f56a4e057581b5",
                1157108162,
            ),
        ),
        license="apache-2.0",
        title="GLiNER multilingue données personnelles",
        description=(
            "Reconnaissance d'entités (personnes, organisations, lieux, adresses, fonctions) "
            "multilingue, spécialisée données personnelles. Précision complète, la plus "
            "rapide sur processeur. Environ 1,2 Go."
        ),
        min_ram=6 * GiB,
        extra={"onnx": "model.onnx"},
    ),
    ModelSpec(
        id="gliner-multi-pii-fp16",
        kind=ModelKind.GLINER,
        repo_id=_GLINER_REPO,
        revision=_GLINER_REVISION,
        files=(
            *_GLINER_COMMON,
            ModelFile(
                "model_fp16.onnx",
                "42735064f92d90babc7ef7fc49e8520d985b08047d25843d97e854699b891139",
                579679779,
            ),
        ),
        license="apache-2.0",
        title="GLiNER multilingue données personnelles (compact)",
        description=(
            "Même modèle en demi-précision : deux fois plus petit (environ 580 Mo), "
            "résultats équivalents, un peu plus lent sur processeur."
        ),
        min_ram=4 * GiB,
        extra={"onnx": "model_fp16.onnx"},
    ),
    ModelSpec(
        id="qwen3-4b",
        kind=ModelKind.LLM,
        repo_id="Qwen/Qwen3-4B-GGUF",
        revision="bc640142c66e1fdd12af0bd68f40445458f3869b",
        files=(
            ModelFile(
                "Qwen3-4B-Q4_K_M.gguf",
                "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5",
                2497280256,
            ),
        ),
        license="apache-2.0",
        title="Qwen3 4B (Q4)",
        description=(
            "Modèle de langage local pour les identifiants indirects et les cas ambigus. "
            "Environ 2,5 Go, pour les machines à partir de 8 Go de mémoire."
        ),
        min_ram=8 * GiB,
        extra={"gguf": "Qwen3-4B-Q4_K_M.gguf"},
    ),
    ModelSpec(
        id="qwen3-8b",
        kind=ModelKind.LLM,
        repo_id="Qwen/Qwen3-8B-GGUF",
        revision="7c41481f57cb95916b40956ab2f0b139b296d974",
        files=(
            ModelFile(
                "Qwen3-8B-Q4_K_M.gguf",
                "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785",
                5027783488,
            ),
        ),
        license="apache-2.0",
        title="Qwen3 8B (Q4)",
        description=(
            "Modèle de langage local, plus précis. Environ 5 Go, pour les machines à partir "
            "de 16 Go de mémoire."
        ),
        min_ram=16 * GiB,
        extra={"gguf": "Qwen3-8B-Q4_K_M.gguf"},
    ),
    ModelSpec(
        id="qwen3-14b",
        kind=ModelKind.LLM,
        repo_id="Qwen/Qwen3-14B-GGUF",
        revision="530227a7d994db8eca5ab5ced2fb692b614357fd",
        files=(
            ModelFile(
                "Qwen3-14B-Q4_K_M.gguf",
                "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0",
                9001752960,
            ),
        ),
        license="apache-2.0",
        title="Qwen3 14B (Q4)",
        description=(
            "Modèle de langage local le plus précis. Environ 9 Go, pour les machines à partir "
            "de 24 Go de mémoire (lent sans carte graphique)."
        ),
        min_ram=24 * GiB,
        extra={"gguf": "Qwen3-14B-Q4_K_M.gguf"},
    ),
    ModelSpec(
        id="ocr-latin-ppocrv5",
        kind=ModelKind.OCR,
        repo_id="monkt/paddleocr-onnx",
        revision="7b02d0a30a07ba2b92ad1ff5a8941ae2c633de65",
        files=(
            ModelFile(
                "languages/latin/rec.onnx",
                "614ffc2d6d3902d360fad7f1b0dd455ee45e877069d14c4e51a99dc4ef144409",
                7862832,
            ),
            ModelFile(
                "languages/latin/dict.txt",
                "3c0a8a79b612653c25f765271714f71281e4e955962c153e272b7b8c1d2b13ff",
                1634,
            ),
        ),
        license="apache-2.0",
        title="OCR alphabet latin (accents français)",
        description=(
            "Modèle de reconnaissance PP-OCRv5 latin : lit correctement les accents "
            "français dans les images et les PDF numérisés. Environ 8 Mo. Facultatif : "
            "sans lui, l'OCR intégré fonctionne mais perd certains accents."
        ),
        min_ram=0,
    ),
]

#: Default GLiNER model when ``Config.gliner_model`` is empty.
DEFAULT_GLINER = "gliner-multi-pii"
#: LLMs from the most to the least capable (used when ``Config.llm_model`` is empty).
LLM_PREFERENCE = ("qwen3-14b", "qwen3-8b", "qwen3-4b")


def get_spec(model_id: str) -> ModelSpec | None:
    return next((m for m in CATALOG if m.id == model_id), None)
