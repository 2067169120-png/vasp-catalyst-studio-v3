# Reaction Workbench contract

Status: versioned integration contract for Reaction Map, Thermochemistry Ledger,
Condition Explorer, report publication, and downstream microkinetics consumers.

## Authority boundary

The Reaction Workbench is a derived, read-only projection. It does not own or
persist catalyst surfaces, adsorbate states, elementary steps, condition sets,
method fingerprints, or evidence references. There is no independent
canonical `TransitionState` domain type. A transition state is the complete
`ElementaryStep/v3.transition_state` participant side.

Those objects remain owned by the canonical catalysis-domain package and the
executed-job source of truth remains `job.yaml`. Integration uses the narrow
`ReactionDomainSource` Protocol or an equivalent typed `Mapping` projection.
The provider must return one explicit opaque `project_id`; a projection cannot
be silently rebound to another project.

The formal path accepts only `vcstudio.catalysis-domain-envelope/v2` and parses
every envelope with `catalysis_contracts.DomainEnvelope.from_dict`. This binds
object type, schema version, immutable revision identity, parent/CAS hash,
payload identity, and semantic hash to the canonical DTO implementation.

`vcstudio.reaction-domain-projection/v1`, legacy envelope/v1, and raw payloads
are an explicit migration/render-only path. Raw payloads are schema-closed and
server-rehashed; caller-supplied raw digests are ignored. Every such input is
`migration_only=true`, `canonical_envelope_authority=false`, and can never be
formal-report, frozen-network, or microkinetics ready. The workbench never
infers reaction objects from filenames, directories, list order, similar IDs,
legacy CHE presets, or browser values.

## Input projection

Schema: `vcstudio.reaction-domain-projection/v2`

Required top-level members:

- `project_id`: opaque workspace identity;
- `authority`: the exact domain-authority ID, generation, snapshot SHA-256,
  network revision ID, and network semantic SHA-256 pinned by the active
  project-local binding;
- `network`: one canonical `ReactionNetwork` envelope;
- `surfaces`: canonical `CatalystSurface` envelopes;
- `states`: exactly one canonical `AdsorbateState/v1` or `FluidState/v1`
  envelope for every globally opaque state ID referenced by the network;
- `steps`: canonical `ElementaryStep/v3` envelopes;
- `conditions`: canonical `ConditionSet` envelopes;
- `bindings`: non-persistent structure/evidence/thermochemistry projections,
  keyed by canonical opaque object ID;
- `applicability`: evidence-bound ranges for any adjustable condition.

The `bindings` projection is an adapter output, not a storage surface. It may
carry hashes and parsed numeric evidence required by this workbench, but this
package never writes it back to the project or canonical DTOs. The adapter must
project status/origin/evidence bindings for every surface, participant state
(including transition-side resolver states), step, condition, and network;
thermochemistry is required only where the requested ledger/kinetics needs it.

The authority mapping is strict and path-free. The workbench compares its
network revision/hash to the canonical network envelope; absent or stale
authority facts cannot become canonical-envelope authority.

Formal v2 validates network closure. Network surface/step/condition members
must exactly match their envelope sets; `ReactionNetwork.state_ids` must close
all reactant, transition-state, and product participants, and the projected
state-envelope set must match that closure. Every ID must resolve to exactly
one member of the `AdsorbateState | FluidState` union: zero matches, a
cross-type collision, or another object type fails closed.

`AdsorbateState/v1.geometric_site_id` authorizes exactly one site of that type:
its participant must use `phase="adsorbed"` and exact
`site_stoichiometry={geometric_site_id: 1}`. Empty, fractional, multi-site, or
non-unit declarations are unavailable even if all three sides would cancel.
An empty geometric-site ID does not allow a participant to create site
authority in reverse. A `FluidState` participant must use exactly its declared
`gas|liquid|aqueous` phase, equal charge, and an empty site map. Solid,
electron, and surface participants remain unsupported until a corresponding
canonical state DTO exists. Element, total charge, and each
`(surface_id, site_id)` are conserved exactly across reactants → transition
state → products. The domain authority carries `surface_id` in each
authoritative participant-state record and keys its own exact conservation by
the full pair; equal site names on different surfaces never cancel and make the
projection unavailable before any runtime adapter can accept it.

`FluidState/v1` contains an exact plain elemental formula, non-null bounded
charge and multiplicity, and a strict `FluidStandardState/v1`. Gas states may
declare only 1 bar (100000 Pa) or 1 atm (101325 Pa); liquid and aqueous states
may declare only 1 molar (1.0 mol/L). It carries no surface/site, activity, or
energy fields. Fluid graph nodes retain `entity_type="fluid_state"`, phase,
formula, charge, multiplicity, and standard-state identity.

### Step-scoped saddle binding

The graph/ledger saddle needed by the UI is a derived node, never a canonical
domain object. It is generated per step from the full transition side and has a
hash-derived opaque node ID. A usable saddle requires an explicit
`bindings[step_id].saddle` object containing both:

- `step_semantic_sha256`, equal to the canonical step envelope hash;
- `transition_side_sha256`, equal to the canonical JSON hash of the complete
  `step.transition_state` participant array.

The saddle binding may then supply its own label, structure/evidence hashes,
scientific status, origin, and thermochemistry/frequency evidence. Missing
binding, structure, frequency/mode evidence, or hash mismatch cannot be
repaired by selecting the first participant, matching a similar ID, or using
array order. Missing data blocks kinetics; a hash mismatch rejects the input.

## Stable output schemas

### `vcstudio.reaction-graph/v1`

Contains opaque nodes and elementary-step edges with:

- canonical object and projection hashes;
- structure, method, and evidence hashes;
- explicit reactant/product participants, the complete transition participant
  side (including exact rational coefficient, phase, charge, and per-site
  stoichiometry), and the step-scoped derived saddle node ID;
- element and total-charge conservation plus exact rational occupancy maps
  keyed by `(surface_id, site_id)`; changing `top` to `bridge` is not treated
  as conserved merely because the total occupied-site count is unchanged;
- separately bound edge and NEB method/reference/evidence/endpoint/TS hashes;
- canonical condition-set ID/hash;
- separate thermodynamic and kinetic readiness;
- forward/reverse reaction and activation free energies only when compatible;
- explicit `missing_nodes` and `missing_edges`;
- `mechanism_complete=false` unless a future independent mechanism-completeness
  authority establishes otherwise.

`artifact_status=available` requires every bound edge to be both
thermodynamically and kinetically ready. Rendering never changes
`scientific_status`.

Microkinetics readiness also has an explicit binding-status gate. A declared
`unknown`, `unavailable`, or `blocked` status on any bound state/authoritative
transition participant, derived saddle, elementary step, condition set, or
reaction network denies `kinetic_ready` and
`microkinetics_ready`, even when hashes and origins are otherwise complete.

### `vcstudio.thermochemistry-ledger/v1`

Each entity row shows, independently:

- `E0`;
- `ZPE`;
- thermal `Delta H`;
- `-T Delta S`;
- standard-state correction and explicit standard state;
- temperature and pressure;
- component models;
- final `Delta G`;
- every term's evidence hash;
- original low frequencies, treatment rule, cutoff, reason, and sensitivity;
- original signed imaginary frequencies plus interpreted magnitudes;
- TS frequency/mode qualification under a server-fixed, hash-bound 50 cm-1
  noise policy (100 cm-1 scientific ceiling); input data cannot select a
  threshold that makes its own spectrum pass.

The declared threshold must be the exact canonical JSON number `50`; all
classification uses the server constant `50.0`, never the caller-provided
storage value. In particular, the `-50 cm-1` boundary cannot be flipped by an
approximately equal threshold.

No missing term is replaced by zero. Every participating term requires an
explicit role-compatible model, `eV` unit, provenance, and evidence hash.
Temperature/pressure and standard-state definitions are bounded and
dimension-checked, ZPE is non-negative, and unknown fields are rejected.
Arithmetic is denied when method, reference
state, standard state, canonical condition set, T/P/pH/potential, coverage,
component models, solvent model, coverage model, or low-frequency treatment is
missing or different.

Low-frequency compatibility compares only the shared policy signature
(`model`, `rule`, `cutoff`, and policy-evidence hash). Original spectra,
entity-specific treatment evidence/reasons, and sensitivities remain frozen in
separate treatment hashes and are not required to be identical across species.

Supported model vocabulary is explicit and does not import or copy CatMAP.

### `vcstudio.condition-derived-revision/v1`

Condition Explorer accepts only bounded `T`, `P`, `pH`, electrode potential,
and coverage parameters. The server produces a deterministic revision bound to:

- the canonical source-projection hash;
- the condition-request hash;
- evidence-bound applicability ranges;
- per-parameter response evidence;
- an evidence-bound joint/additive model when multiple parameters change.

Applicability ranges, the response envelope, each parameter model, and the
joint model have typed schemas, evidence hashes, and origins. Their weakest
origin caps the derived status; a non-observed dependency blocks derived
readiness. Response base conditions must exactly match both the ledger and its
canonical condition-set digest, and both base and target must fall inside the
evidence-bound applicability range.

Condition-set, ledger, and response-base values are normalized to the same
canonical numeric representation and compared exactly. No absolute tolerance
is used, so `1e-12 Pa` and `2e-12 Pa` remain scientifically distinct.

The revision includes derived node and edge values. It always declares
`source_evidence_mutated=false`. Missing applicability, missing response
evidence, unsupported interactions, or out-of-range values are unavailable.

### `vcstudio.frozen-reaction-network/v2`

This is the only supported handoff to the Microkinetics package. It freezes:

- `version="2"` in addition to the versioned schema name;
- the pinned domain-authority and network-revision/hash identity facts;
- a minimal state catalog retaining each state's real object type, revision,
  semantic hash, phase, formula, charge, multiplicity, and either
  surface/unit-site authority or fluid standard state;
- source projection, graph, and condition-revision hashes;
- explicit reactants, products, the complete transition side, the derived
  saddle identity, and exact rational stoichiometry/phase/charge/site fields;
- explicit conservation and edge/NEB compatibility results;
- condition-set and method/evidence hashes;
- condition-derived reaction free energies;
- observed NEB electronic barriers (`Delta E‡`) separately from
  thermochemistry-derived forward/reverse free-energy barriers (`Delta G‡`);
- independent thermodynamic and kinetic statuses;
- hash-bound evidence references for the canonical step revision, domain
  evidence resolver handles, edge evidence/compatibility, saddle structure,
  saddle evidence, and saddle binding;
- missing reasons and `microkinetics_ready`.

The frozen `scientific_status` is the lowest qualification across the reaction
graph, thermochemistry ledger, and condition-derived revision. It is not copied
from the graph alone; readiness remains a separate fail-closed gate.

Downstream code must reject `readiness=blocked`, must not infer missing barriers
or stoichiometry, and must not treat this DTO as a rate-law or reactor-model
selection. `authorizes_execution` is always false.
Evidence references are opaque resolver handles plus hashes; the workbench
never fabricates evidence bytes. Missing resolver handles or any required hash
are explicit edge blockers in the frozen network.
Only a fully canonical-envelope projection with an observed provenance chain
can be `microkinetics_ready`; imported/inferred origins cap status and cannot be
promoted by a binding label.
Frozen v1 is not a formal input to this contract. Version 2 adds identity and
state-catalog facts only; it still does not invent species activities,
prefactors, rate laws, or other kinetics choices.
Observed NEB barrier values are public only when edge/NEB method, reference,
endpoint/TS structure, evidence hashes, and the observed provenance chain are
all compatible. Otherwise graph, derived revision, report, and frozen-network
DTOs expose `null`/`unavailable`, while retaining non-numeric missing reasons.
Edge and NEB reference hashes must additionally equal every participant and TS
ledger reference; agreeing only with each other is insufficient.

### `vcstudio.reaction-report-binding/v1`

Provides two server-finalized tables, a reaction-map figure binding, limitations,
and the condition-revision hash. The report workbench reloads the canonical
projection server-side and places the graph, ledger, derived revision, frozen
network, and binding into `ReportSnapshot.sources/payload/evidence`.

The visible reaction tables and server-rendered map figure enter the same
`ReportModel` content hash, `ValidationResult`, manifest, revision, history, and
CAS chain as existing report content. A successful render does not raise the
scientific qualification. Missing topology or TS evidence remains a warning or
blocker according to the existing report gate; it never becomes a supported
complete-mechanism claim.

## Scientific limitations

- No CatMAP code, defaults, reference states, or solver behavior is embedded.
- Different computational methods, reference states, standard states,
  conditions, coverage definitions, or solvent models are not mixed by default.
- A frequency- and mode-supported first-order saddle point increases only the
  TS kinetic-readiness field. It does not prove pathway uniqueness or mechanism
  completeness.
- Local condition-response models are used only inside their evidence-bound
  applicability ranges. Multi-parameter additivity requires its own evidence
  hash.
- The frozen network supplies energetics and identities, not rate constants,
  lateral-interaction models, transport, reactor physics, or uncertainty
  validation.
