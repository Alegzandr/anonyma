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
  (`SegmentKind`, label, page, bbox, `where`/`line` for reports only), optional
  `char_boxes` for visual redaction. Offsets are `str` indices, `end` exclusive.
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
  `paths.clean_tmp()` at startup. A batch folder is claimed (`paths.claim`: a locked
  `.verrou` file held until `Pipeline.close` or the end of the process), so the
  startup cleanup of another instance only removes folders whose owner is gone.
* Every output is written in the batch temp folder and only moved to the output
  folder (or to `<output>_quarantaine/`, a sibling,) once verified, under a name reserved by
  exclusive creation; any failure on the way deletes it. Outputs that fail
  verification are never marked anonymized.
* Worker processes set up logging like the main process (`logs.setup_logging(worker=True)`):
  nothing reaches the user's terminal. Pools are shut down by `pipeline.worker_pool`:
  pending jobs cancelled, idle workers given `POOL_EXIT_TIMEOUT` seconds, then terminated.
  The pool is sized by the profile the installed models serve (`registry.usable_profile`).
* Each batch records the files it placed in an output folder (`outputs.record`, in
  `data/sorties/`) so that `run --remplacer` deletes them, and only them, when unchanged.
* Errors shown to users: only `UserError` (and `StoreError`, `ModelError`) messages
  are displayed as is; any other exception is shown and logged by type only.

## Verification and statuses

The written output is re-extracted with the same extractors (so every page is
rendered and OCR'd, codes decoded, invisible text and metadata read) and checked by
`Verifier.verify` against every value detected in the batch (with derived, mixed-case,
hyphen-free and OCR-tolerant forms), every detector of the profile, and the original
names of the file and of its embedded items (each path component and its stem,
certain; their distinctive words, possible). Values kept by the organization policy
(`policy.keeps`) are not residues.

Identifiers are atomic (`anonyma/atomic.py`): no detection, propagated mention,
replacement or verification hit may start or end inside a GUID, a long hexadecimal
run or a base64/generated token; a span overlapping one is trimmed to its outside
part or dropped (enforced in `detect_tree`, `resolver.propagate`, `build_plan` and
the verifier, for every detector).

Structured data (`anonyma/structure.py`, `anonyma/extractors/_embedded.py`): JSON keys
and CSV headers that are structural names (`createdon`, `@odata.etag`, OData
annotations) are neither exposed nor rewritten, and the raw backstop never searches the
batch's structural names. A base64 value or `data:` URI holding a file (JSON, XML, CSV)
is extracted as a child like a mail attachment and written back re-encoded in the same
style, or as an empty value when the child is omitted. The raw backstop
(`anonyma/rawscan.py`) turns any base64 file of an output that no re-extracted item
accounts for into a certain `FICHIER_INCORPORE` residue.

| Outcome | Status | Output |
|---|---|---|
| certain residue | RESIDUE | `<output>_quarantaine/` |
| output not re-extractable | FAILED | `<output>_quarantaine/` |
| an image/page of the **output** could not be OCR'd (`VerificationResult.unread`, any `ocr` issue: OCR disabled, unavailable, failed) | NEEDS_REVIEW + `verify.incomplete` | `<output>_quarantaine/` (content never checked) |
| possible residue, uncertain entities, an image/page of the **input** not OCR'd (`analyse.ocr_incomplete`), or embedded items omitted from the container (`container.incomplete`) | NEEDS_REVIEW | output folder |
| container whose every embedded item was omitted and with no text of its own (`container.empty`) | FAILED | nothing written |
| none of the above | ANONYMIZED | output folder |

An issue is an OCR gap when one of the dot-separated parts of its code starts with
`ocr` (`ocr.failed`, `pdf.ocr_unavailable`).

The CLI derives its exit code from the batch verdict (`report.BatchReport.exit_code`,
computed from the outcomes of the top-level files): 1 when a file is in quarantine,
failed, unsupported or cancelled, 0 otherwise (anonymized or to review).

## File names

Names go through detection like any text: the pipeline adds a FILENAME segment to
every document (detectors read it with `_`, `+` and the extension dot as spaces), and
`propagate_names` searches every batch value in names, metadata, attributes and
headers with separators ignored (`Contrat_jean_DUPONT.pdf`, `DUPONT-Jean.pdf`).
With `Config.neutral_filenames`, outputs are named `document_0001.ext` (input order),
their folders `dossier_0001`, and embedded items `piece_jointe_0001.ext` (numbered per
container, inner folders dropped); in pseudonymization the correspondence is saved in
the encrypted store (`save_filename` / `filenames`, `anonyma reidentifier --noms`).
Reports, logs and CLI messages only show output names, never input paths.

## Tests

`tests/test_contracts.py` runs every fixture of `tests/fixtures/contract/` through
the whole pipeline with the canary values of `tests/canary.py`; each new format
must add a fixture (synthetic, with canaries in body and metadata) and its
generator in `tests/fixtures/generators/`.
