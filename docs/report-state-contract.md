# Report state and evidence contract

This document defines the report-publication boundary used by VASP Catalyst
Studio. It is intentionally stricter than a renderer-success flag: creating a
file does not prove a scientific conclusion.

## Independent status axes

Every report-facing API and UI must keep these axes separate:

- `artifact_status`: whether the requested files were generated, committed,
  hash-checked, and recorded;
- `scientific_status` / `report_kind`: the purpose of an artifact that actually
  exists (`diagnostic`, `draft`, or `final`), otherwise `null`;
- `scientific_qualification`: the highest evidence level actually validated.
- `publication_gate_status`: whether the current project is `pending`,
  `eligible`, `blocked`, or `unknown` for publication;
- `desired_report_kind`: the kind a new report would receive under the current
  gate, independent of any existing artifact's kind.

`report_done` is an activity event. It may be emitted only after a canonical
non-draft marker is current, but it must never be used by itself to infer
`final`. A draft may be a current, readable artifact without closing the
automatic publication stage.

Supported qualification levels, from lowest to highest, are:

1. `diagnostic`
2. `adsorption_result_verified`
3. `thermodynamic_path_verified`
4. `kinetic_evidence_verified`
5. `publication_package_verified`
6. `human_scientific_reviewed`

A non-final artifact may retain a verified lower-level qualification when a
higher publication gate is blocked. That qualification still requires a named
validator, a passing required/blocking check, an exact content digest, and a
bound contract chain. `human_scientific_reviewed` additionally requires an
auditable human approval record; a machine validator cannot assign it by name
alone.

## Immutable contract chain

The canonical chain is:

```text
ReportSpec --semantic SHA-256--> ReportSnapshot
ReportSnapshot --semantic SHA-256--> ValidationResult
ValidationResult --report_model_sha256--> normalized visible report content
```

The v1 schemas are:

- `vcstudio.report-spec/v1`
- `vcstudio.report-snapshot/v1`
- `vcstudio.report-validation/v1`

Contract objects are immutable after construction. Their semantic hashes use
canonical UTF-8 JSON, reject non-finite/non-JSON data, preserve array order,
and exclude administrative timestamps and navigation-only locators only at
schema-declared reference fields (for example, a source file path). Identically
named fields inside scientific payloads, evidence, claims, and extensions remain
part of the hash. Moving the same project or template therefore does not change
its scientific identity, while changing scope, values, site labels, trajectory
metadata, checks, claims, section order, or policy identity does.

`ValidationResult.status` must agree with its checks:

- `passed`: every applicable check passed;
- `passed_with_warnings`: no required/blocking check failed and at least one
  non-blocking warning exists;
- `blocked`: at least one required/blocking check did not pass;
- `unknown`: an evaluation is absent, or at least one check is unknown.

Only `pass` satisfies a required or blocking check for final publication.

## Visible content binding

`report_model_sha256` covers the exact normalized content rendered in every
format, including:

- report kind, scientific qualification, and claim ceiling;
- locale and section order;
- title, metadata, summary, findings, methods, limitations, and recommendations;
- candidate, adsorption, and comparison tables;
- comparison context;
- figure bytes and semantic figure metadata.

Machine-specific paths and administrative timestamps are excluded. The report
builder computes this digest before constructing `ValidationResult`; the
renderer recomputes it immediately before rendering. A value, statement,
section, or figure changed after validation is therefore rejected before any
new report file is committed. Marker reads also recompute the same visible
projection independently from the frozen model sidecar, so coherently changing
ordinary model/file hashes cannot replace the validation-bound content digest.

## Portable bundle and transactional publication

A bound bundle contains:

- the selected HTML, DOCX, and/or PDF artifacts;
- `<stem>.model.json`, the canonical normalized model projection whose raw-file
  SHA-256 is `model_sha256`;
- `<stem>.spec.json`;
- `<stem>.snapshot.json`;
- `<stem>.validation.json`;
- `<stem>.manifest.json`.

The manifest records the model sidecar hash and size, semantic contract hashes,
contract-sidecar byte hashes and sizes, artifact hashes and sizes, both model
digests, scientific state, qualification, input fingerprint, and revision
metadata.

Publication is manifest-last and serialized across threads and processes for a
given output directory and stem. Replacing a bundle backs up every destination,
publishes selected artifacts and sidecars, removes stale formats/sidecars that
are no longer selected, and commits the manifest last. A failure restores both
replaced and deleted files. If restoration itself is incomplete, the old
complete manifest is invalidated and recovery evidence is retained on disk.
Content-addressed figure assets may remain because they are immutable and
hash-named.

## Canonical project marker

Manual project export, automatic close-out, chat `/report`, result import, and
legacy Tk project-report entry points all delegate to the same bundle service.
The service writes one `autopilot_report` marker only after:

1. every requested format exists;
2. the renderer reports success and the expected scientific kind;
3. the canonical model sidecar reproduces `model_sha256` and all contract
   sidecars reproduce their declared semantic hashes;
4. the complete contract chain validates;
5. the manifest agrees with the files, contracts, state, qualification, input,
   and model digests;
6. the project is reloaded and its pre-render artifact/scientific fingerprints
   still match.

If inputs change during rendering, files may remain as
`generated_unrecorded`, but no ready marker is written and no `report_done`
event is emitted. The operation response preserves those files and their actual
rendered kind for recovery while remaining `ok=false`; callers must not treat
them as current until marker persistence succeeds. Marker persistence merges
into the latest project data under a lock; it must not overwrite unrelated
concurrent project edits.

New markers use the path-independent `scientific_fingerprint` for freshness.
The older path-sensitive `input_fingerprint` is retained only for compatibility.
Historical markers may be read fail-closed, but new final markers require the
full sidecar/manifest chain.

## Format capabilities

The UI asks the backend for two independent per-format capability axes before
generation:

- `formats.<format>.available` means that the renderer and its required assets
  are available. It controls whether the format can be selected.
- `accessibility.<format>` describes properties the renderer can encode. It
  never changes renderer availability and must not be inferred from a file
  having been created.

HTML is always renderable; DOCX and PDF are enabled only when their real
renderer imports are available and packaged font assets can actually be parsed.
Merely finding a font-shaped path is insufficient. At least one format is
required. Missing one requested format makes the operation incomplete and
prevents marker persistence; successful generation of another format does not
silently satisfy the request. Non-interactive import, chat, and automatic
close-out paths request HTML plus only those optional formats that the same
render-availability probe has positively confirmed.

Accessibility records use fail-closed states. `conditional` means the renderer
encodes the listed foundations but author-supplied content and human review are
still required. `partial` means a known accessibility requirement is absent.
`unsupported` is reserved for a known unavailable accessibility mode, and
`unknown` means the backend did not provide enough evidence. The manifest stores
artifact-specific accessibility records, including the number of figures whose
source descriptions failed the narrow meaningful-alt baseline. Empty text and
labels such as `Figure 1` or `图 1` do not satisfy that baseline.

Current format boundaries are:

- HTML emits a document `lang`, metadata, headings, tables, figure/caption
  structure, and `img alt`. Its status is conditional when every figure has a
  non-generic description and partial otherwise. Browser/screen-reader review
  remains required for reading order, text quality, contrast, reflow, and
  keyboard behavior.
- DOCX emits core metadata and document language, applies `w:lang` to document
  defaults and report styles, preserves Word Heading/Caption styles and
  repeating table-header rows, and writes each image description to
  `wp:docPr/@descr` with a useful title. Its status is conditional when every
  figure has a non-generic description and partial otherwise. A structural OOXML
  test can verify those fields, but Word Accessibility Checker and human review
  must still confirm alt-text quality, reading order, table usability, color,
  and pagination.
- The ReportLab PDF is visual and searchable and records `/Lang` plus document
  metadata. It is nevertheless untagged: it has no structure tree and does not
  encode image alternative text. Its accessibility status therefore remains
  `partial`, with `tagged=false` and `pdf_ua=false`, even when generation
  succeeds. The application must never describe this artifact as PDF/UA or as
  an accessible substitute for the HTML or DOCX.

Automated tests may prove the presence of declared HTML elements/attributes,
OOXML language/style/header/alt fields, PDF text, `/Lang`, metadata, and the
absence of `/StructTreeRoot`. They cannot honestly certify WCAG conformance,
scientific adequacy of alternative text, screen-reader experience, usable
reading order, color-blind distinguishability, or PDF/UA conformance. Those
claims require suitable assistive-technology and human review; PDF/UA also
requires a tagged-PDF-capable renderer and a standards validator, which the
current ReportLab path does not provide.

## Legacy boundary

`report_full.generate_project_report()` remains a diagnostic-only low-level
legacy renderer for every requested status, including the historical `auto` and
`final` values. Current Web and Tk project-report entry points must not call it
as a second publication authority. Legacy read compatibility does not authorize
writing new unbound final markers or visually final unbound artifacts.
