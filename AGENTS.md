# AGENTS.md

Guidance for coding agents working on Anonyma. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for the pipeline and the plugin contracts, [PRODUCT.md](PRODUCT.md) for users and positioning,
[README.md](README.md) for user-facing behaviour (formats, CLI, exit codes) and
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for sources, build, CI and releases. Keep the
README user-only: developer material goes to `docs/`.

## What this is

Anonyma anonymises documents of every common format in bulk, **entirely offline**: extract
text → detect personal data → resolve entities batch-wide → replace consistently → write the
file back **in its original format** → re-read the output and verify nothing remains. A
desktop GUI (PySide6) and a CLI (`anonyma-cli`, Typer) share the same `Pipeline`. Python 3.12
only, managed with `uv`.

## Commands

```sh
uv sync                                   # install (dev group included)
uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest
uv run pytest -m "not slow"               # quick loop (skips OCR, LLM, benchmark tests)
uv run pytest tests/test_pipeline.py -k name
uv run anonyma --help                     # CLI from sources
uv run python -m anonyma.app              # GUI from sources
uv run python -m tests.benchmark.measure  # recall/precision on the synthetic corpus
uv run python packaging/build.py          # portable build + smoke tests + zip
```

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, `mypy` (strict) and
`pytest` on Windows, macOS and Linux, then the portable build. All four checks must pass
before you consider a change done. On Windows, keep pytest's base temp short
(`--basetemp=C:\t\pt`) when LibreOffice tests run: it fails on long profile paths.

## Hard rules

- **Language**: code, identifiers, comments, docstrings, commit messages in **English**.
  Every user-facing string (GUI, CLI output, `Issue.message`, reports, tags such as
  `[PERSONNE_001]`, CLI option names) in **French**, with correct accents. No i18n layer.
- **Never leak sensitive data**: no original value, file name or exception message that
  may contain one in logs, reports or `Issue` messages. Sensitive model fields are
  `repr=False`; use `logs.fingerprint(value)` for correlation.
- **Offline**: `netguard` blocks outbound connections; only a user-triggered model download
  runs inside `netguard.allow_network()`. Do not add network calls, telemetry or new
  downloads at runtime.
- **Original format is the contract**: each output keeps its input format, folder tree and
  layout (msg stays msg, legacy Office via bundled LibreOffice, PST as a folder of eml,
  file and folder names anonymised). Changing an output format is a regression.
- **Fail closed**: something that cannot be anonymised or verified is omitted and reported,
  never copied as is; a verified residue sends the file to `<output>_quarantaine/` (a sibling of the output folder). Containers must
  never embed original child bytes (`child_outputs[id].path is None` ⇒ omit).
- **Recall over precision**: a single detection masks; uncertain entities are masked *and*
  sent to review, never left visible.
- **Originals are read-only**; temp files only under `data/tmp/` via `paths`.
- **Licensing**: the app is internal-use only (AGPL/GPL deps). Keep `pymupdf<2` and
  `opencv-python-headless`; reject non-commercial models or data. Regenerate `LICENSES.md`
  with `packaging/gen_licenses.py` when dependencies change.

## Layout

| Path | Role |
|---|---|
| `anonyma/pipeline.py` | `Pipeline.analyze` / `.apply` / `.run`, worker processes, verification |
| `anonyma/models.py` | contracts: `Segment`, `Document`, `Entity`, `Extractor`, `Writer`, `Detector`… |
| `anonyma/extractors/`, `anonyma/writers/`, `anonyma/detectors/` | plugins, auto-registered via `@register_*`; modules starting with `_` are helpers |
| `anonyma/resolver.py`, `replacer.py`, `substitutes.py` | batch-wide clustering/propagation, substitution plan, realistic substitutes |
| `anonyma/verifier.py` | re-reads outputs: certain residue → quarantine, statistical signal → « À revoir » |
| `anonyma/store.py` | encrypted mapping table (pseudonymisation) |
| `anonyma/config.py`, `paths.py`, `netguard.py`, `logs.py` | settings, portable folders, network guard, value-free logging |
| `anonyma/gui/` | Qt Widgets app; French enum labels in `gui/labels.py`, styling in `gui/theme.py` |
| `anonyma/data/` | compact lexicons built offline by `packaging/build_detection_data.py` |
| `packaging/` | PyInstaller spec, portable build, LibreOffice bundling, icons, licences |
| `tests/` | unit, contract, GUI (`tests/gui/`, pytest-qt) and benchmark tests |

## Adding things

- **New format**: an extractor (must expose *all* text, metadata included) and a writer
  (applies the plan, purges metadata), plus a synthetic fixture in `tests/fixtures/contract/`
  carrying the canaries of `tests/canary.py` in body **and** metadata, with its generator in
  `tests/fixtures/generators/`. `tests/test_contracts.py` then runs it end to end.
- **Detection change**: add a focused test, then check the benchmark
  (`tests/benchmark/`, results in `tests/benchmark/RESULTS.md`) for recall regressions.
- **Optional dependency**: a plugin whose import fails must degrade into
  `registry.unavailable` ("non supporté"), not crash.

## Tests

- `tests/conftest.py` gives every test its own `ANONYMA_HOME`; use the `canary_config`
  fixture (light profile, inline workers) for pipeline tests.
- Markers: `slow` (OCR, models, benchmark) and `models` (needs downloaded models; skipped
  when absent). Tests must use synthetic data only, never real personal data.
- GUI tests run headless (`QT_QPA_PLATFORM=offscreen`).

## Style

- Ruff (line length 100, rules in `pyproject.toml`) and mypy `strict`; type everything,
  no blanket `# type: ignore`.
- `from __future__ import annotations` at the top of modules.
- Commit subjects as `Area: what changed` (e.g. `PDF: object strings always rewritten`),
  with a short body explaining why.
- Don't commit scratch files (probe PDFs, screenshots) or `data/` / `models/`.
