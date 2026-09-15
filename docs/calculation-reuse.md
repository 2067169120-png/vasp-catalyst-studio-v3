# Strict scientific fingerprint and explainable reuse

This contract governs duplicate-calculation advice and explicit local result
reuse. It borrows the provenance idea that a reused calculation still creates
a new node, but it does not copy AiiDA code and it is not a database-backed
caching system.

## Independent facts

The following states must not be collapsed:

- a candidate appears in the rebuildable local index;
- two complete strict fingerprints are an `exact match`;
- a source result is complete, converged and still file-verifiable;
- a user explicitly chose to reference that source;
- a campaign task was validated or accepted;
- a report was qualified as final.

An exact fingerprint match is advisory evidence for the same versioned input
identity. It is not an inherited `accepted` or `final` qualification. A near
match only reports differences and is never described as equivalent.

## Versioned fingerprint

The schema is `vcstudio.scientific-fingerprint/v1`. Its semantic SHA-256 binds:

- the actual scaled cell in angstrom and ordered fractional coordinates;
- ordered element identities, Selective Dynamics flags and POSCAR tail data;
- the complete effective INCAR mapping, including constraints, charge, spin,
  Hubbard U, dispersion and solvent controls;
- every effective KPOINTS line;
- the irreversible full-content POTCAR SHA-256 plus irreversible TITEL hashes;
- engine, task type and calculation type;
- the Method Recipe authority's schema and canonical `semantic_sha256`;
- VASP version, build/compiler identity and the vcstudio creator version.

POSCAR structure normalization uses
`cell-angstrom+fractional-mod1-12significant/v1`: the established structure
parser applies VASP scale semantics and Cartesian-to-fractional conversion;
periodic fractional coordinates are wrapped to `[0, 1)`, signed zero is
removed, and physical numbers are represented with twelve significant digits.
Atom order is retained. This deliberately favours false negatives over unsafe
equivalence claims.

The strict fingerprint consumes, but never recreates, Method Recipe decisions:

```yaml
inputs:
  method_recipe:
    schema: vcstudio.method-recipe/v1
    semantic_sha256: <64 lowercase hex characters>
```

This nested field is the only authoritative recipe integration contract.
Legacy aliases are not promoted merely because they contain hash-shaped text.
A missing/invalid recipe, VASP/build identity, final input hash, input file,
stable manifest job identity or canonical component produces:

```text
status = incomplete
digest = null
recipe_status = explicit_legacy   # when the recipe binding is absent
```

Two incomplete records never produce an exact/equivalence conclusion. Existing
legacy jobs remain readable and submittable under their existing submission
rules, but are not silently upgraded to strict-reuse eligibility.

## Rebuildable bounded index

`CalculationReuseIndex` is rebuilt on each query from the ordinary jobs ledger,
the current authoritative `job.yaml` in each job directory and current file
hashes. It has a default capacity of 512 records. Selected targets are retained
inside that bounded window. The index reports its capacity, indexed and
observed denominators, truncation, `rebuildable=true` and
`authoritative=false`.

Deleting or rebuilding the index cannot change scientific truth. Every
advisory and every reuse decision rereads the manifests and files.

## Advisory classes

The Jobs Selection Tray displays server-produced facts only:

- strict fingerprint schema/status/digest and field summaries;
- exact matches;
- near matches with field-level differences and a non-equivalence warning;
- failed, unconverged and incomplete sources;
- source revalidation status;
- measured saved core-hours, requested upper bound, or `unknown`.

The browser sends opaque job IDs. It does not read files, canonicalize inputs,
hash content, compare fingerprints, decide convergence or calculate savings.
The public response recursively removes locators and credentials, and POTCAR
content is never returned.

## Explicit result reference

The default action remains recalculation. Reuse occurs only after the user
selects `Reference existing result`. The server then locks source and target in
canonical order and repeats all checks:

1. both strict fingerprints are complete and identical;
2. the target is an unsubmitted `CREATED` job with a stable manifest identity;
3. the source is `DONE`, has non-contradictory convergence/exit evidence, a
   clean OUTCAR footer or complete vasprun, finite energy and matching current
   OSZICAR energy;
4. OSZICAR and OUTCAR/vasprun are present in the source's authoritative result
   hash map and still match;
5. neither manifest, fingerprint nor result bundle changed between choice and
   commit.

The target receives its own versioned decision, provenance nodes and `reuses`
link. Only hash-bound result bytes and the narrow calculation evidence required
for deterministic revalidation are materialized. Source campaign acceptance,
report markers and final qualifications are not copied. The target's
`accepted_inherited` and `final_inherited` decision fields are always false.

Reuse uses a durable two-phase transaction:

```text
prepared decision/provenance in target job.yaml
  -> idempotent hash-checked result materialization
  -> succeeded decision + DONE state
```

If the process exits between files, a retry with the same decision ID accepts
only already-materialized bytes with the expected hashes and continues the
missing files. A completed replay revalidates both the current source and the
materialized target; it does not trust an old success record.

## Explicit recalculation

When a verified exact source exists, a user may still recalculate. The reason
is required and is durably stored in the target `reuse_decisions` with the
current strict fingerprint. The submission-time guard rereads that decision;
an input change invalidates it. The same decision ID is restart-idempotent and
cannot be rebound to a different reason or payload.

Both ordinary Jobs submission and project one-stop submission apply the same
pre-SSH guard. Project password and host-key retries retain one operation ID,
while per-job submission journals remain the cross-process/restart authority.

## Current evidence boundary

Automated tests prove canonicalization, hashes, fail-closed classifications,
TOCTOU checks, recovery records, path-free API projection and browser wiring.
They do not prove performance on a real cluster, VASP scientific correctness,
human acceptance, or publication finality. Real older jobs without the Method
Recipe and execution-environment bindings correctly remain `incomplete`.
