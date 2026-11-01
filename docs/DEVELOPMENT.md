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

To use Anonyma from sources without the development tools, install with
`uv sync --no-dev` and keep passing `--no-dev` to every command: a plain `uv run`
syncs the default groups again and reinstalls the dev tools.

```sh
uv sync --no-dev
uv run --no-dev anonyma run dossier -o sortie
uv run --no-dev python -m anonyma.app
```

From sources, `data/` and `models/` are created at the project root. The `ANONYMA_HOME`
environment variable selects another application folder. Without a LibreOffice on the
machine, `.doc` and `.ppt` are unsupported and `.xls` is written as `.xlsx`.

## Checks, build, benchmark

```sh
uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest
uv run pytest -m "not slow"                          # quick loop
ANONYMA_TEST_MODELS=<models-dir> uv run --extra llm pytest -m models   # real models
uv run python packaging/build.py                     # full portable folder + smoke tests + zip
uv run python packaging/build.py --sans-libreoffice  # light archive, no LibreOffice
uv run python packaging/gen_licenses.py              # regenerate LICENSES.md
uv run python -m tests.benchmark.measure             # benchmark: measured recall and precision
```

Tests marked `models` (real GLiNER and Qwen3 models, several minutes) run only when
`ANONYMA_TEST_MODELS` names a folder of installed models (one sub-folder per model, as in
`models/`); the repository's own `models/` is never used by the tests, and `--extra llm`
brings llama-cpp-python for the Qwen3 ones. Every other test runs with fakes and no model.
A test stuck for 5 minutes has the stacks of its threads dumped (`faulthandler_timeout`),
and every CI job has a `timeout-minutes`.

The benchmark (`tests/benchmark/`) anonymises a synthetic corpus of 24 trap documents and
re-reads the outputs by independent means; method and results in
[tests/benchmark/RESULTS.md](../tests/benchmark/RESULTS.md).

## Bundled LibreOffice

The portable build downloads a pinned LibreOffice (SHA-256 checked) at **build time**
(`packaging/libreoffice_bundle.py`) and copies it into `libreoffice/`; the application never
downloads it. On Windows, spelling dictionaries and UI languages other than French and
English are stripped. On Windows and Linux, the parts headless conversions never load are
removed too (`UNUSED_PARTS`: icon themes but one, bundled Python and Java, gallery,
templates, AutoText, Poppler PDF import); the build's smoke test converts doc, xls and ppt
with what is left.

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
- **Archive size**: `packaging/anonyma.spec` drops what the hooks collect but the app never
  loads (`UNUSED_FILES`: Mesa software OpenGL, OpenCV's FFmpeg back end, Pillow's AVIF codec,
  MuPDF headers, the Qt network stack, Qt translations other than French). No dependency
  should pull spaCy or another NLP stack in: the rule-based recognizers are plain Python.
- **Threads of the ONNX models** (`anonyma/threads.py`): in a worker pool, GLiNER and the OCR
  each get the logical CPUs divided by the pool size; a process running alone keeps ONNX
  Runtime's default. Letting every worker start one thread per core made OCR batches
  about 1.7 times slower.
- **No native Qt file dialogs**: they remember recent folders in the Windows registry,
  outside the portable folder.
- **Network guard**: every outbound connection is blocked at process level, except inside
  a user-triggered model download and the startup update check (`netguard.allow_network()`).
- **Self-update** (`anonyma/updater.py`, portable build only): `releases/latest` of the
  GitHub repository, archive `Anonyma-<version>-<os>-<arch>[-sans-libreoffice].zip`
  checked against the asset's `sha256` digest from the API, unpacked into `data/maj/`
  (not `data/tmp/`, emptied after each batch). A PowerShell or `sh` script waits for the
  application to exit, swaps every top-level entry except `data/` and `models/` (previous
  ones kept as `*.old-maj` and restored on failure), then relaunches it. On Windows it is
  started with `CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB`, never `DETACHED_PROCESS`
  (PowerShell 5.1 without a console exits without running its script); elsewhere in a
  new session. The application waits for the script's first state write before quitting
  (failure shown at once); the script logs its steps, value-free, to
  `data/logs/mise-a-jour.log` and its outcome to `data/maj-etat.txt`, which the next
  start reports to the user. Keep the archive
  names and their single `Anonyma/` top folder stable: older versions rely on them.
- **Two-level verification, then a corrective pass**: what the verification finds is
  masked batch-wide and the affected files are rewritten with the same substitutes. What
  remains then is graded: a recovered original value, or a reliable deterministic
  detector, quarantines the file; a signal from a statistical detector alone (GLiNER, LLM)
  marks it « À revoir », since these models react to the new context created by the tags.
- **PST restored as a folder of eml**: no free tool rewrites a PST reliably.
