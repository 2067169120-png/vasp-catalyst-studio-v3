# Kinetics authoring core contract

`vcstudio.project.kinetics_authoring` is the server-only transaction boundary for
creating and activating `KineticsModelSpec` revisions. It does not read reaction
facts from a browser payload and it does not provide an execution API.

## Browser DTO

`vcstudio.kinetics-model-spec-draft/v1` has exact keys. Its required keys are `schema`,
`spec_id`, `mode`, `rate_law_policy`, `assumptions`, `feed_reservoirs`,
`target_product_ids`, `steps`, and `site_population_totals`.
`saddle_selector` is the only optional key.

Evidence is represented only by opaque `*_ref_id` or `evidence_ref_ids` values.
The draft cannot contain project roots, source bindings, revisions, hashes,
actors, credentials, or raw reaction facts. Empty or partial drafts are invalid;
the compiler supplies no scientific defaults.

## Injected server authorities

`AuthoringSourceAuthority.authoring_source_snapshot()` must perform a live read
and return an `AuthoringSourceSnapshot`. The sealed snapshot owns the project,
domain, network, projection, required membership/coverage sets, saddle
candidates, evidence catalogue, and a separate solver-readiness gate with
bounded opaque reason codes. Catalogue entries may bind an artifact SHA-256
directly. Otherwise an injected `EvidenceResolver(reference_id)` must return the
exact artifact bytes; the compiler hashes those bytes.

The model-spec axis comes from `KineticsModelSpecStore.snapshot()` plus the exact
current head for `(source.project_id, draft.spec_id)`. The compiler derives the
revision, parent, expected head hash, and source binding server-side.

### Default production prerequisite

The default desktop production composition intentionally installs no evidence
byte resolver. The repository has no authority that can safely turn an opaque
domain `EvidenceRef` into the immutable scientific artifact bytes required by
the compiler. In that composition, bootstrap must return
`status=missing_prerequisite` with
`reason=evidence_resolver_unavailable`; it must not report Kinetics authoring-
source or solver readiness. The UI must direct the user to import and bind the required
scientific evidence before preview/confirmation. A canonical domain envelope,
its JSON bytes, or its semantic hash is authority metadata, not the referenced
scientific artifact, and must never be substituted as resolver output.

## Minimal API wiring

The API adapter needs only these calls:

```python
service = KineticsAuthoringCoordinator(
    project_root,
    source_authority=source_authority,
    evidence_resolver=evidence_resolver,
)

preview = service.preview(browser_draft, intent_id=intent_id)
confirmation = KineticsAuthoringConfirmation.from_dict(browser_confirmation)
result = service.confirm(browser_draft, confirmation)
```

Before the first preview, bootstrap must initialize `KineticsModelSpecStore`
outside the preview request (call `snapshot()` once). Preview intentionally does
not initialize or write the model store. A browser confirmation echoes exactly:
`intent_id`, `draft_sha256`, `preview_sha256`, `source_snapshot_sha256`,
`store_snapshot_sha256`, `selector_snapshot_sha256`, and `confirmed=true`.
`KineticsAuthoringCoordinator.confirmation_from_preview()` is a server/test
convenience for constructing that shape.

`preview.to_dict()` is bounded and path-free. It exposes source/store/selector
CAS, the proposed spec identity/hash, `can_confirm`, `solver_ready`, bounded
`solver_readiness_reasons`, issues, and always `authorizes_execution=false`; it
does not expose the compiled spec object. `can_confirm` means the decision DTO
is contract-valid and may be saved. It is intentionally independent from
`solver_ready`: a diagnostic spec may be saved and selected while remaining
solver-blocked. Runtime execution must honor its own projection/audit gates and
must never infer execution authority from `can_confirm` or `confirmed` alone.

`confirm()` returns `committed`, `replayed`, `conflict`, `unavailable`, or
`needs_repreview`. Conflicts carry `AdoptAndStopConflict`, the latest available
CAS values, and `retry_automatically=false`. API code must not retry a conflict
automatically.

## Selector and durability

The v2 selector is fixed at `.vcstudio/kinetics/active-model-spec.json`. It is an
immutable, hash-sealed record with authority/revision CAS, exact project/spec
revision/hash, intent ID, transaction ID, and explicit confirmation. Its
independent append-only authority is fixed at
`.vcstudio/kinetics/.active-model-spec-anchor.wal`. Each bounded, hash-chained
transition stores the complete target v2 selector plus exact base/target
descriptors as a durable `PREPARE` followed by `COMMIT`. A valid v1 record is
readable for migration display but is never accepted as v2 write authority and
never initializes a v2 anchor.

All selector reads and authoring writes use `.authoring.lock`. Confirmation
durably writes `authoring-pending.json`, calls `KineticsModelSpecStore.put()`
with its mandatory two live source callbacks, re-reads the exact spec head,
appends and fsyncs selector-anchor `PREPARE`, atomically replaces and directory-
fsyncs the selector, appends and fsyncs selector-anchor `COMMIT`, verifies both
targets, appends the fixed `authoring-commit-receipt.json` ledger, then clears
pending state. Selector `COMMIT`, not replacement of `active-model-spec.json`,
is the selector commit point.

Recovery is fail closed:

- exact spec/selector base: record an abandoned intent, clear pending, require a
  new preview (and a new intent);
- target spec plus base selector: advance the exact pending selector;
- target spec plus target selector: add/verify the receipt and clear pending;
- any side outside its exact base/target: raise
  `KineticsAuthoringRecoveryError`.

An outstanding selector-anchor `PREPARE` is itself recovered forward: current
selector bytes must equal its exact base or its complete target. Base is
replaced by target and then `COMMIT` is appended; target receives only the
missing `COMMIT`. Any other state fails closed. Once a v2 selector exists,
missing, truncated-beyond-a-torn-final-append, tampered, replaced, or rolled-back
anchor/current authority is never adopted or rebased. Genesis is legal only
when both the v2 selector and anchor history are completely empty. A process
object pins the anchor file entity after first observation and rejects entity
replacement.

For fault tests, the coordinator calls the injected hook at `after_pending`,
`after_spec`, `after_selector_prepare`, `after_selector_replace`,
`after_selector`, and `after_receipt`.
