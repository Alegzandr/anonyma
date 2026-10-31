# Developing Anonyma

User documentation lives in the [README](../README.md); the pipeline and plugin contracts
in [ARCHITECTURE.md](ARCHITECTURE.md). Coding-agent guidance: [AGENTS.md](../AGENTS.md).

## Running from sources

Prerequisite: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 if needed).

```sh
git clone <repository> anonyma && cd anonyma
uv sync
uv run anonyma --help        # command line
uv run python -m anonyma.app # graphical interface
```

From sources, `data/` and `models/` are created at the project root. The `ANONYMA_HOME`
environment variable selects another application folder. Without a LibreOffice on the
machine, `.doc` and `.ppt` are unsupported and `.xls` is written as `.xlsx`.

## Checks, build, benchmark

```sh
uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest
uv run pytest -m "not slow"                          # quick loop
uv run python packaging/build.py                     # full portable folder + smoke tests + zip
uv run python packaging/build.py --sans-libreoffice  # light archive, no LibreOffice
uv run python packaging/gen_licenses.py              # regenerate LICENSES.md
uv run python -m tests.benchmark.measure             # benchmark: measured recall and precision
```

The benchmark (`tests/benchmark/`) anonymises a synthetic corpus of 24 trap documents and
re-reads the outputs by independent means; method and results in
[tests/benchmark/RESULTS.md](../tests/benchmark/RESULTS.md).

## Bundled LibreOffice

The portable build downloads a pinned LibreOffice (SHA-256 checked) at **build time**
(`packaging/libreoffice_bundle.py`) and copies it into `libreoffice/`; the application never
downloads it. On Windows, spelling dictionaries and UI languages other than French and
English are stripped.

## Continuous integration and releases

`.github/workflows/ci.yml` runs static analysis and the tests on Windows, macOS and Linux
(with LibreOffice installed on the runners; the `.doc`/`.ppt` fixtures are generated when
absent), then builds the full portable archive of each system, LibreOffice included. The
build starts the GUI offscreen and anonymises, with the frozen executable, a batch of
fixtures (docx, xlsx, pdf, eml, msg, zip, png with OCR, doc, xls, ppt) that must all come
out « Anonymisé » with their original extension.

Pushing a `v*` tag publishes a GitHub release with the three archives once tests and
builds pass. The tag must match `__version__` (and `pyproject.toml`), otherwise publishing
fails. A tag with a suffix (`v1.2.0-rc1`) is published as a pre-release.

## Technical choices

- **PyMuPDF pinned to 1.x**: 2.0 ships code first published under a non-commercial licence.
- **OpenCV headless**: the full build bundles Qt and clashes with PySide6.
- **No native Qt file dialogs**: they remember recent folders in the Windows registry,
  outside the portable folder.
- **Network guard**: every outbound connection is blocked at process level, except inside
  a user-triggered model download (`netguard.allow_network()`).
- **Two-level verification**: a recovered original value, or a reliable deterministic
  detector, quarantines the file; a signal from a statistical detector alone (GLiNER, LLM)
  marks it « À revoir », since these models react to the new context created by the tags.
- **PST restored as a folder of eml**: no free tool rewrites a PST reliably.
