# Anonyma architecture

Conventions: code, identifiers, comments and docstrings in **English**; every
user-facing string (GUI, CLI output, `Issue.message`, reports, tags) in **French**
only. No translation layer.

## Pipeline

```
inputs ──collect_inputs──▶ (path, logical name)
   │  per file, in a ProcessPoolExecutor
   ▼
Extractor.extract ──▶ Document{segments, children} (+ FILENAME segment added by pipeline)
   ▼
Detector chain (registry.build_detectors, by profile) ──▶ Entity[]
   │  back in the main process, batch-wide
   ▼
resolver.resolve ─▶ resolver.propagate ─▶ resolver.resolve  ──▶ EntityCluster{} (BatchAnalysis)
   │  ── GUI review: ReviewDecisions (confirm / reject / retype / manual add) ──
   ▼
Replacer.assign ──▶ SubstitutionTable (stable for the whole batch; MappingStore in pseudonymization)
   │  per top-level file, in a ProcessPoolExecutor
   ▼
replacer.build_plan ─▶ Writer.write (children first, then container) ─▶ output file
   ▼
extract_file(output) ─▶ Verifier.verify ─▶ status ANONYMIZED / NEEDS_REVIEW / RESIDUE / FAILED
```

Public API (`anonyma/pipeline.py`): `Pipeline(config, store=None)`,
`.analyze(inputs, progress, cancel) -> BatchAnalysis`,
`.apply(analysis, output_dir, decisions, progress, cancel) -> BatchResult`,
`.run(...)`, `.close(analysis)`. `ProgressEvent`, `CancelToken`.
`report.BatchReport.from_result(result)` builds the exportable report.

## Contracts (`anonyma/models.py`)

* `Segment`: `id`, `text`, `locator` (opaque, writer-specific), `context`
  (`SegmentKind`, label, page, bbox), optional `char_boxes` for visual redaction.
  Offsets are `str` indices, `end` exclusive.
* `Document`: `source` (readable file, never modified), `name` (logical, `/`
  separators, sensitive), `format`, `segments`, `children`, `properties`
  (writer-private, picklable), `issues`, `status`.
* `Extractor`: `name`, `extensions` (`".ext" -> format`), optional `sniff(head)`,
  `extract(source, document, ctx)`. Must expose **all** text, metadata included.
  Containers call `ctx.budget.consume(...)` before materialising a child, then
  `ctx.extract_child(data, name, parent)` which appends the child to
  `parent.children` and never raises for child problems (except `ArchiveBombError`).
* `Writer`: `name`, `formats`, `output_extension`, `output_format`,
  `write(document, plan, destination, ctx) -> list[Issue]`. Writes a copy, applies
  `plan.for_segment(seg.id)` replacements, **purges metadata**. Container writers
  use `ctx.child_outputs[child.id]` (`path is None` ⇒ omit the child, never embed
  original bytes; `name` is the anonymized entry name). A writer with
  `directory_output = True` receives a folder path to create (e.g. PST restored as
  a tree of eml); the pipeline re-reads the folder for verification and, below the
  top level, packs it into a `.zip`.
* `Detector`: `name`, `min_profile`, `is_available(config)`,
  `detect(segments, document_id) -> list[Entity]`.
* `OcrEngine.recognize(PIL.Image) -> list[OcrLine]`, created by
  `ocr.create_engine(config)`, reached by extractors through `ctx.ocr`.
* `MappingStore`: `lookup`, `save`, `reverse`, `used_substitutes`.

Sensitive fields are `repr=False`. Never log values, file names or exception
messages that may contain them; use `logs.fingerprint(value)` if correlation is
needed. Issue messages are French and value-free.

## Plugins

Drop a module in `anonyma/extractors/`, `anonyma/writers/` or
`anonyma/detectors/` and decorate the class with `@register_extractor`,
`@register_writer` or `@register_detector`. Modules starting with `_` are helpers
and are not auto-imported. A plugin whose optional dependency is missing is
reported in `registry.unavailable` and its format becomes "non supporté".

## Portability and safety

* `paths.configure_environment()` (every entry point and worker) redirects temp,
  HF cache and XDG cache into the application folder; `netguard.install()` blocks
  every outbound connection except inside `netguard.allow_network()`.
* Temp files live in `data/tmp/<batch>/`, removed by `Pipeline.close` and
  `paths.clean_tmp()` at startup.
* Outputs that fail verification go to `<output>/_quarantaine/` and are never
  marked anonymized.

## Tests

`tests/test_contracts.py` runs every fixture of `tests/fixtures/contract/` through
the whole pipeline with the canary values of `tests/canary.py`; each new format
must add a fixture (synthetic, with canaries in body and metadata) and its
generator in `tests/fixtures/generators/`.
