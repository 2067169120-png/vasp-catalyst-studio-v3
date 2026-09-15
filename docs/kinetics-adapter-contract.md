# Microkinetic safe-adapter contract (phase 1)

## Authority and ownership

The microkinetic layer is a consumer, not a second reaction-data authority.
Reaction Map and Catalysis Model own their canonical DTOs and frozen
reaction/thermochemistry projections.  Raw mappings are not trusted at the
adapter boundary.  The layer accepts only a structural `KineticsInputProvider`
that supplies one exact frozen reaction projection, one exact immutable
`KineticsModelSpec`, the derived input, and opaque evidence bytes.  It does not
save a competing reaction-network object.

The adapter projection uses `vcstudio.kinetics-network/v3`.  Its
`source_projection` identifies the upstream schema, version, projection hash,
and evidence references.  Its `model_spec` binding identifies the exact
`vcstudio.kinetics-model-spec/v1` revision, semantic SHA-256, source-projection
SHA-256, and model-decision evidence.  `input_sha256` is the canonical JSON
SHA-256 of the exact adapter projection excluding only the self-declared
`input_sha256` field.  Any change to reaction facts, spec decisions, evidence
hashes, methods, or conditions therefore invalidates a previous preview and
imported result.

The three current data boundaries are
`vcstudio.kinetics-network/v3`, `vcstudio.kinetics-result/v2`, and
`vcstudio.kinetics-normalized-result/v2`.  A v1 payload at any of these
boundaries is not silently upgraded.

`KineticsModelSpec` is a strict, path-free and credential-free DTO.  It owns
only modelling decisions: the source authority/generation/revision binding;
rate-law policy; evidence-bound assumptions and feed reservoirs; target
products; per-step prefactors, BEP/scaling choices and uncertainty; explicit
site-population totals; and an optional explicit saddle selector.  Immutable
revisions use parent revision/current-hash CAS.  Confirmed creates and advances
are stored only below `<project>/.vcstudio/kinetics/model-spec/`; same-revision
same-byte writes replay, while stale source, authority, parent or current hash
conflicts fail closed.  A stable OS advisory lock makes competing processes a
single-winner CAS.  Before a write, the caller first obtains a complete
store snapshot sealing its persistent authority, generation, current heads,
immutable revision chain and exact store bytes.  `put()` requires that snapshot
seal and a trusted `KineticsSourceAuthority`.  Inside the same model-spec OS
lock it calls `authoritative_kinetics_source_snapshot()` twice: once before the
store CAS and again immediately before publication.  Both sealed snapshots
must be identical and must exactly match project ID, domain authority,
generation, network revision, source-projection hash and, when supplied,
network ID.  A missing validator, stale store snapshot, or source change writes
nothing.  Even an empty store persists its authority on the first snapshot, so
delete/recreate and rollback ABA cannot masquerade as the original store.  A
separate append-only private anchor WAL is never reconstructed from
`store.json`.  Initialization and every advance use `PREPARE → store fsync →
COMMIT`: the prepare record hash-binds a transient full pending store, while
each commit chains the authority, generation, exact store hash and immutable
revision-chain hash to the preceding commit.  A crash with only a prepare can
be reconciled forward from that pending payload.  Once the commit marker is
durable, restoring older store bytes is a rollback error even if the caller
also presents the formerly valid old snapshot.  A store with a missing or
mismatched anchor fails closed; legacy stores require an explicit trusted
migration rather than automatic anchor invention.

The formal Reaction integration starts from
`vcstudio.frozen-reaction-network/v2`.  `ValidatedFrozenReactionV2` accepts only
the exact self-hash, canonical-envelope authority, ready/microkinetics-ready
gates, complete server-validated frozen/condition identity, available edges
with no missing facts, and a separate hash-bound `ValidatedReactionSnapshot`
for project/domain/network/source identity.  The latter is required because the
frozen v2 DTO intentionally does not duplicate `project_id`.  A v1 frozen
network is migration/render input only and cannot enter this formal adapter.

The deterministic path-free conversion is itself sealed as
`vcstudio.kinetics-adapted-reaction-source/v1`, but that transport hash is not a
second source authority.  Model specs and `kinetics-network/v3` bind the exact
original `frozen_network_sha256`; the adapter seal only proves that converted
species/steps were not changed afterward.  FluidState maps exactly as
`gas -> gas`, `liquid -> liquid`, and `aqueous -> solution`, retaining the
complete canonical `vcstudio.fluid-standard-state/v1` record.  AdsorbateState
remains a surface/adsorbate/transition-state fact based on its explicit
structural role; a gas is never inferred from an ID, formula, or AdsorbateState.
Exact participants, direct reaction/barrier values, method identity, condition
revision, and edge/domain evidence remain hash-bound.  Activities, targets,
prefactors, rate-law policy, site totals, empirical choices, and uncertainty
come only from the model spec.

Semantic object and edge hashes are never assumed to be artifact-byte hashes.
Before conversion, the server supplies an explicit evidence binding index from
each mandatory opaque domain resolver reference and scoped method/edge hash
reference to one `artifact_sha256`.  The exact reference set must match: a
missing or extra binding is rejected before any resolver call.  Every artifact
is then resolved once and checked by bytes.  The combined source/spec chain is
globally unique by reference-to-hash binding, limited to 256 entries before
resolution, and limited to 64 MiB of resolved bytes in total.

The default desktop production composition intentionally has no such resolver:
this repository does not contain an authority that can map opaque domain
evidence references to immutable scientific artifact bytes. Its stable public
state is therefore `missing_prerequisite` with reason
`evidence_resolver_unavailable`, and no Kinetics provider or solver-ready export
may be constructed. The UI must guide the user to import and bind the missing
scientific evidence. Canonical-envelope JSON bytes and semantic hashes describe
authority records; using them as substitute artifact bytes is forbidden.

`KineticsProjectionProvider` calls its reaction seam exactly once at
construction, snapshots the exact spec, and snapshots every evidence byte.
All later source/spec/input/evidence methods return detached copies from memory:
the provider never reopens a job, guesses a missing activity/prefactor/site
total, or persists another set of reaction facts.  A v2 model spec source
binding includes and checks project ID, domain authority/generation, network ID
and revision, and the original frozen-network hash.

## Mandatory audit

`vcstudio.project.kinetics.audit_network()` fails closed and reports all issues
it can find.  It checks:

1. exact schema, unknown fields, safe identifiers, and declared input hash;
2. upstream projection plus exact model-spec schema/revision/hash and evidence;
3. explicit rate-law policy, mean-field and steady-state assumptions;
4. uniform-site and lateral-interaction boundaries;
5. an explicit mechanism-completeness claim and feed-to-target connectivity;
6. temperature and phase-applicable pressure/concentration/potential standard
   states, including every FluidState's exact canonical standard-state ledger;
7. bounded phase-applicable temperature/pressure/potential operating ranges and
   frozen-condition cross-checks;
8. element, charge, and site conservation through reactants, transition state,
   and products;
9. duplicate/reverse-duplicate steps, missing species, and unused species;
10. reaction energy, forward/reverse barriers, state-energy identities, and
    detailed balance;
11. method identity/compatibility and per-energy evidence/uncertainty; and
12. prefactor, BEP, scaling, and step-uncertainty provenance; and
13. evidence-bound site-population totals covering every frozen site type.

Every gas, liquid, or solution species has a model-spec reservoir, including
zero-activity products.  Gas activity uses `bar`; liquid/solution activity uses
`mol/L`; dimensionless fluid activity is rejected.  Optional explicit surface
reservoirs use only `dimensionless` and never create a `species.activity`
record.  `feed_species` contains only reservoirs whose activity is strictly
positive.  Gas partial pressures must sum to the declared total pressure.
Electrochemical projections must declare a potential model, standard
potential, reference electrode, and potential range.

Stationary-point roles are structural, not caller labels.  Each elementary
step has exactly one coefficient-one `transition_state`-phase species.  The
only additional transition-side entries permitted are neutral, composition-free
`surface` species used for explicit empty-site bookkeeping.  Every transition
state has exactly one frequency below -50 cm^-1; non-transition-state species
have none.  For frozen v2, the authoritative Workbench gate has already made
that stationary-point qualification and the adapter records the gate rather
than inventing or duplicating omitted mode values.  Otherwise, negative
frequencies from -50 cm^-1 through zero, including gas modes, are treated as
bounded numerical noise.  The 50 cm^-1 threshold is a fixed server policy and
cannot be relaxed by input.

The audit has separate denominators for species, elementary steps, and checks.
`machine_pass=true` means only that the machine audit found no blocking issue.
It is not human review, experimental validation, or publication acceptance.

## CatMAP relationship and license

CatMAP is a separate GPL-3.0 program.  This repository does not copy, vendor,
bundle, import, or automatically install CatMAP.  The adapter is
independently-authored MIT-licensed VASP Catalyst Studio code that writes data
files compatible with CatMAP's documented TableParser/setup conventions.

- CatMAP project and license: <https://github.com/SUNCAT-Center/catmap>
- TableParser input format: <https://catmap.readthedocs.io/en/latest/tutorials/generating_an_input_file.html>
- Reaction/setup conventions: <https://catmap.readthedocs.io/en/latest/tutorials/creating_a_microkinetic_model.html>
- Output variables: <https://catmap.readthedocs.io/en/latest/topics/output_variables.html>
- Electrochemical requirements: <https://catmap.readthedocs.io/en/latest/topics/electrochemistry.html>

The table contains the documented required columns:
`surface_name`, `site_name`, `species_name`, `formation_energy`, `frequencies`,
and `reference`.  Because the input values are already frozen Gibbs free
energies, the generated setup uses `frozen_gas` and `frozen_adsorbate` to avoid
adding a second thermochemical correction.

The phase-1 setup generator is intentionally narrower than all features that a
scientifically valid canonical projection may describe.  It emits only frozen
Gibbs energies, thermal/non-electrochemical conditions, single-site explicit
surface states, ideal activities/lateral interactions, explicit reversible
detailed-balance policy, a mean-field steady-state reactor, and equal
forward/reverse `s^-1` prefactors (frozen into CatMAP's `prefactor_list`).  All
of those choices must be present in the model spec; the adapter has no fallback
rate law.  Electrochemical potential
dependence, liquid/solution species, non-Gibbs energy bases, asymmetric or
pressure/concentration-dependent prefactors, multisite species, and
parameterized lateral interactions remain unavailable until an equally strict
export mapping exists.  Such a projection retains its core audit and receives
a separate `CATMAP_PHASE1_CONTRACT_UNSUPPORTED` adapter audit; no model scaffold
is emitted.

Phase 1 also requires exactly one independent site type.  Its CatMAP
`species_definitions` total is copied from the evidence-bound
`site_population_totals` record; it is never invented as `1.0`.  The one
explicit empty-site species must have an exact frozen formation energy of zero eV,
because CatMAP represents that energy through its site balance and omits the
empty-site row from `energetics.tsv`.  A nonzero empty-site energy is rejected;
the adapter does not silently rebase the remaining states.

## Preview and explicit confirmation

`preview_export()` performs no write and exposes no local executable path.  It
returns:

- the full input audit;
- the adapter ID/version;
- the user-configured tool basename/SHA-256/size, if a regular absolute file
  exists, plus its version when a nonempty safe version was explicitly supplied;
- planned artifact names, sizes, and SHA-256 values; and
- a preview token binding input, adapter, tool identity, and artifact hashes.

The tool is inspected by hashing the regular file only.  It is never imported
or executed.  Missing CatMAP or an unknown/unsafe tool version leaves the solve
capability `unavailable`, while the audit and compatible input preview remain
available.  Model confirmation always binds the explicitly supplied safe tool
version as well as its file hash.

`confirm_export()` requires `confirmed is True` and an exact preview-token
match.  It recomputes the bundle and writes only below:

```text
<project>/.vcstudio/kinetics/exports/<input_sha256>/
```

It never accepts an output filename or command from the browser.  Existing
different files are not overwritten.  The frozen bundle contains:

- `kinetics-input.json`
- `kinetics-audit.json`
- `catmap-name-map.json`
- `catmap-gas-contract.json`
- `catmap-descriptors.json`
- `energetics.tsv`
- `model.mkm`
- `model-scan.mkm`
- `process-contract.json`
- `manifest.json`

Every project-local directory component is resolved under the project root and
rejected if it is a symbolic link or Windows junction.  New bundles are built
in a sibling staging directory and published with one directory rename, so a
partially written bundle is never treated as confirmed.  Loading a confirmed
bundle first performs no-follow `lstat` checks and accepts only regular,
non-reparse entries.  The directory must contain exactly `manifest.json` plus
the uniquely named artifacts declared by that manifest: no undeclared file or
subdirectory is accepted.  The manifest is limited to 1 MiB; a bundle may
declare at most 64 artifacts, each at most 20 MiB and at most 64 MiB combined.
Both declared and actual byte totals are bounded before artifact content is
read.  The loader then rechecks the current file entities, sizes and SHA-256
values, directory entries and directory-chain entities, adapter identity,
preview token, and diagnostic-only status.  The writer enforces the same
artifact count and byte limits before confirmation, so it cannot publish a
bundle its loader must reject.

The authored project root and every directory used by one operation are
identity-pinned and revalidated; project-root symlinks, junctions and reparse
points are rejected before `.vcstudio` is created.  Model selection uses an OS
selection lock.  Under that lock confirmation first recovers any interrupted
reservation, validates the caller's exact selection revision/current token,
checks per-input retention quotas, and persists a reservation.  Only then does
it stage and directory-fsync the atomically published bundle and revalidate its
exact integrity.  Each input has a fixed independent append-only
`.selection-anchor.wal`.  Confirmation appends and fsyncs a hash-chained
`PREPARE` binding the exact reservation hash and complete base/target selection,
atomically replaces and directory-fsyncs `current.json`, then appends and fsyncs
`COMMIT`.  Anchor `COMMIT`, not replacement of `current.json`, is the selection
commit point.  Only after `COMMIT` may the reservation be removed.

A stale CAS loser exits before creating its token directory.  Failure before
anchor `PREPARE` removes only that transaction's staged/new bundle and
reservation.  Once anchor preparation is attempted, exception cleanup never
deletes the final bundle or reservation.  The next locked confirmation requires
the exact reservation transaction/ownership/integrity, the exact prepared base
or target `current.json`, and the exact immutable bundle.  It deterministically
replaces base with target when needed, appends the missing `COMMIT`, and removes
the reservation; every other state fails closed.  A complete v1 `current.json`
without its anchor is not migration or genesis authority, and restoring older
pointer bytes below a later anchor is rejected.  Torn final WAL appends may be
truncated to the last complete record under the lock; complete records are never
rewritten.  The anchor entity is pinned for the operation.  The reservation
records a random transaction ID, its deterministic stage name, whether the final
bundle existed before the transaction, and an exact bundle-content seal.  A
pre-existing bundle is never recovery cleanup.  Each input retains at most 32
bundles and 512 MiB of bundle bytes; the anchor itself has independent byte and
record limits.

When scientific evidence fails, only `kinetics-audit.json` is exportable.  The
process contract is explicitly non-executable: `execution_permitted=false`,
`shell=false`, `accepts_user_arguments=false`, and `auto_install=false`.  It
documents a fixed argv shape for an operator-controlled external runner but
does not invoke it.

## Result import

The only accepted result schema is `vcstudio.kinetics-result/v2`.  Import
requires exact matches for:

- input SHA-256;
- VASP Catalyst Studio adapter ID/version;
- user-installed tool version and SHA-256; and
- the complete unit map.

The normalized unit contract is K, bar, V, s^-1, fraction, dimensionless, and
eV as appropriate.  Unknown fields, non-finite numbers, paths/commands in
identifiers, out-of-range conditions, unknown species/steps/sites, coverage or
selectivity above one, and malformed convergence/sensitivity records are
rejected.  A phase-1 coverage record may identify only a frozen surface or
adsorbate species, and its `site_type` must belong to that exact species rather
than merely existing somewhere else in the network.

Imported values are server-normalized into
`vcstudio.kinetics-normalized-result/v2`.  Every DRC and DSC record retains its
`target_species_id` and `step_id`; every reaction-order record retains its
`target_species_id` and perturbed `species_id`.  Target identifiers must name a
frozen `target_products` entry, while step and perturbed-species identifiers
must remain inside the frozen network.  TOF, selectivity, and apparent
activation-energy species are also restricted to frozen target products;
reaction-order perturbations are restricted to gas species listed in the
frozen feed reservoir.  Free-energy diagram state IDs must name frozen species
(including transition states) or the explicit `reactants` and `products`
aggregate labels; arbitrary safe identifiers are not promoted to authoritative
states.  A browser may render those values but
must not solve equations, recompute metrics, change units, or infer missing
results.

Every imported condition point must be complete.  TOF, selectivity, and
apparent activation energy contain exactly one record per target product; DRC
and DSC cover the full target-product × elementary-step Cartesian product; and
reaction order covers the full target-product × gas-feed Cartesian product.
Coverage has at least one record for every frozen site type and is unique by
`(species_id, site_type)`.  A free-energy diagram contains at least two unique
allowed `state_id` values.  All other metric identities are likewise unique by
their declared axis or composite axes.  Empty arrays, partial Cartesian
products, and duplicate identities are rejected rather than imputed.  Each
normalized point carries server-derived completeness denominators with required,
observed, unique, and duplicate record counts for audit and display.

Validated raw and normalized results are stored immutably below
`<project>/.vcstudio/kinetics/results/<input_sha256>/<result_sha256>/`.  A small
atomic `latest.json` pointer selects the current result for that exact input.
Every load revalidates the directory chain, raw-result hash, strict result
schema, unit map, input/adapter/tool hashes, and receipt hash.  Changing the
configured CatMAP tool after confirmation makes the old result unavailable;
the Dashboard keeps the confirmed and currently configured tool identities
separate and requires a new preview/confirmation cycle.

## Kinetic Dashboard boundary

`kinetic-dashboard` reuses the registry-driven analysis workbench.  Its browser
request may select only bounded display precision.  Reaction facts, methods,
conditions, barriers, prefactors, BEP/scaling parameters, uncertainty, audit
outcomes, and result values come only from the server-owned frozen projection,
confirmed adapter manifest, and revalidated result receipt.

Python formats every displayed condition and value for TOF, coverage,
selectivity, DRC, DSC, reaction order, apparent activation energy, free-energy
diagram, convergence, and sensitivity.  The browser preserves the server
order and renders only `display` strings plus server-projected units.  It has no
solver, unit conversion, rate expression, sensitivity perturbation, or
free-energy inference.  Missing CatMAP, a failed audit, a changed tool, a hash
mismatch, an unconverged result, or missing sensitivity evidence leaves the
numeric Dashboard unavailable while keeping the audit and export preview
accessible.

The public bridge accepts opaque project IDs only.  Export preview accepts no
destination; confirmation accepts only the exact preview SHA-256 plus an
explicit boolean confirmation; result import accepts a bounded JSON object,
not a path, executable, command, argv, or reaction-network payload.

## Scientific and publication limitations

Phase 1 is a steady-state mean-field model with uniform site populations.
Coverage-dependent lateral interactions are either explicitly neglected or
the input is unavailable; parameterized interactions are not silently reduced
to the ideal model.  Multisite species must be represented through explicit
site-state bookkeeping or remain unsupported.  A `claimed_complete` mechanism
is still an upstream assertion, not proof that no elementary step is missing.

All imported results remain `diagnostic`, including converged results.
Unconverged points or unavailable sensitivity evidence make the dashboard
unavailable while retaining the diagnostic rows for inspection.  No kinetics
artifact may directly enter `accepted`, `final`, or a publication-ready report.
Human scientific review and, where applicable, experimental validation remain
independent gates.
