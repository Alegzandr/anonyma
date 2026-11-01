# Product

<!-- impeccable:product-schema 1 -->

## Platform

desktop

Native desktop application (PySide6 / Qt Widgets, Fusion style) shipped as a portable
folder for Windows (primary), macOS (Apple Silicon) and Linux, plus a companion CLI
(`anonyma-cli`). Not a web product: no browser surface, no server.

## Users

Two populations, weighted equally, inside a single organisation:

- **Business teams** (HR, support, sales…): non-technical, anonymise documents occasionally,
  mostly through the graphical interface. They need the safe path to be the obvious one and
  must never have to understand detectors, profiles or models to get a correct result.
- **Technical / data profiles**: prepare datasets, script batches with `anonyma-cli`, tune
  profiles, lists and configuration. They expect predictable exit codes, JSON output and
  full control.

The GUI and the CLI matter equally; neither is a second-class surface.

## Product Purpose

Anonyma anonymises documents of every common format in bulk, entirely on the local machine:
it detects personal data (names, addresses, e-mails, phone numbers, IBAN, NIR…), replaces it
consistently across the whole batch, purges metadata, then re-reads every produced file to
check that nothing remains.

The anonymised output is used mainly to:

- **send documents to online AI services** (ChatGPT, Claude…) without exposing personal data;
- **build test or training data**: realistic corpora that contain no real personal data.

Success means a user can hand over an output folder with confidence: every file is either
verified clean, explicitly flagged for review, or quarantined, and nothing sensitive leaks
through a file the tool silently skipped.

## Positioning

- **Fully local, provably offline for documents**: outbound connections are blocked inside
  the process; the only network actions happen at startup: downloading the models this
  machine needs (Hugging Face, chosen by the application from the hardware) and the
  update check of the portable build (one GitHub API request). Neither carries any
  document or setting, and both can be switched off in Réglages (models are then
  installed on request, or imported from a folder offline). Both go through the proxy of
  the workstation (system settings, PAC script on Windows) or the one set in Réglages,
  and trust the system certificates, so they work on corporate PCs. No telemetry.
- **Verification is part of the product, not an option**: every output is re-read by
  independent means; a recovered original value sends the file to `<output>_quarantaine/` (a sibling of the output folder), never to
  the "Anonymisé" count.
- **Original format preserved**: each file comes back in its own format, folder tree and
  layout (msg stays msg, legacy Office via the bundled LibreOffice, PST as a folder of eml),
  with file and folder names anonymised too.
- **Batch-consistent decisions**: one entity, one substitute, across the whole batch; a
  decision taken in the preview applies to every occurrence at once.
- **Decides instead of asking**: the user is almost never asked anything. Uncertain values
  are masked, what the verification still finds is masked by a second pass, and the
  application chooses everything a user could not judge (profile, models, OCR, workers,
  thresholds). Settings are only what depends on the user's needs.

## Operating Context

- Input: files or folders dropped in; output: a separate folder (originals are never
  modified). Archives, mailboxes and attachments are processed recursively.
- Workflow in the GUI: **Traitement** → **Rapport**, "Anonymiser" never pauses; plus an
  **Aperçu** to consult (double-click a file) and **Réglages** (whose Application tab
  also shows the state of the automatic model setup).
- The preview is consultative and entity-centric ("Bérénice Lagarde — PERSONNE — 14
  occurrences dans 6 fichiers"), with side-by-side context (text, rendered PDF/image with
  boxed zones) and keyboard shortcuts (N / P, Maj+N, V, R, T, A). Corrections there are
  possible, never required.
- Workflow in the CLI: `run`, `analyser` (dry run), `formats`, `materiel`, `modeles`,
  `config`, `reidentifier`; exit codes 0 / 1 / 2 are a contract.
- Runs from a portable folder (local disk or USB key): config, logs, temp files and models
  live in `data/` and `models/` next to the executable. Machines may have no internet access
  (offline model import).
- Hardware varies widely; profiles `leger` / `standard` / `complet` adapt detection to RAM,
  CPU and GPU (DirectML on Windows, Core ML on macOS).

## Capabilities and Constraints

- Formats: Office (docx, xlsx, pptx, odt, ods, odp, rtf), legacy Office (doc, xls, ppt),
  PDF (text and scanned, real redaction), e-mail (eml, msg, mbox, pst), data/text (txt, csv,
  json, xml, html…), images with OCR (png, jpg, tiff, heic, webp…), archives (zip, 7z).
- Replacement modes: *Balises* (`[PERSONNE_001]`), *Réaliste* (plausible substitutes),
  *Masquage* (blackout). Regimes: *Anonymisation* (irreversible) or *Pseudonymisation*
  (encrypted correspondence table protected by a passphrase that is never stored).
- File statuses are fixed vocabulary: **Anonymisé**, **À revoir**, **Résidus** (quarantine),
  **Échec**, **Non pris en charge**.
- Recall over precision: one detection is enough to mask; uncertain entities are masked,
  never left visible, never handed to the user.
- Anything that cannot be anonymised is omitted and reported, never copied as is.
- Logs and reports contain no sensitive value and no original file name.
- No Qt native file dialogs (they write recent folders to the Windows registry, outside the
  portable folder). No external resource is ever loaded by the UI.
- **Internal use only**: AGPL/GPL components (PyMuPDF, extract-msg) forbid distribution to
  third parties. No public marketing site, store page or pricing exists or should be implied.
- UI text is French only, with no i18n layer; code and comments are English.

## Brand Commitments

- Name: **Anonyma**.
- Mark: a capital A whose crossbar is a redaction bar, on an accent tile
  (`anonyma/brand.py`, exported to `packaging/icons/` as svg, png, ico, icns). Shared by the
  GUI, the HTML report and the packaging icons.
- Voice: plain, precise French; states exactly what was done and what remains to check
  ("Anonymisé", "À revoir", "ne doivent pas être diffusés"). Never reassures beyond what was
  verified.

## Evidence on Hand

- Benchmark: synthetic corpus of 24 trap documents with measured recall and precision,
  `tests/benchmark/RESULTS.md` and `tests/benchmark/results/`.
- Security and format behaviour documented in `README.md`; architecture in
  `docs/ARCHITECTURE.md`; licences in `LICENSES.md`.
- No testimonials, customer names, certifications or third-party audits exist. Do not
  fabricate any.

## Product Principles

1. **Nothing leaves the machine, and the user can tell.** Locality is the core promise; any
   network action is explicit, visible and can be switched off (the startup update check
   is the only automatic one and sends nothing about the user's data).
2. **Never claim more than was verified.** Status, counts and wording reflect what the
   verifier actually checked; doubt is surfaced, not hidden.
3. **The safe default does the work.** A non-technical user who clicks "Anonymiser" with
   default settings gets a correct, verified result; expertise adds control, not safety.
4. **One decision, everywhere.** Decisions are made per entity and propagate across the
   batch; the user reviews meaning, not occurrences.
5. **Parity between GUI and CLI.** Every capability that matters is reachable from both, with
   the same vocabulary.

## Accessibility & Inclusion

- Light, dark and system themes (`anonyma/gui/theme.py`).
- Review is fully keyboard-driven; keyboard operation should extend to every workflow.
- Status must never rely on colour alone (users decide whether a file is safe to share).
- No formal standard (RGAA / WCAG level) has been set yet: open decision.
