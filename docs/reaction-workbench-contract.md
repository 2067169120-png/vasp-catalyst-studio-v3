# Reaction Workbench contract

Status: versioned integration contract for Reaction Map, Thermochemistry Ledger,
Condition Explorer, report publication, and downstream microkinetics consumers.

## Authority boundary

The Reaction Workbench is a derived, read-only projection. It does not own or
persist catalyst surfaces, adsorbate states, transition states, elementary
steps, condition sets, method fingerprints, or evidence references.

Those objects remain owned by the canonical catalysis-domain package and the
executed-job source of truth remains `job.yaml`. Integration uses the narrow
`ReactionDomainSource` Protocol or an equivalent typed `Mapping` projection.
The provider must return one explicit opaque `project_id`; a projection cannot
be silently rebound to another project.

The workbench validates canonical envelope object identity and semantic hashes
before deriving public DTOs. It never infers reaction objects from filenames,
directories, legacy CHE presets, or browser values.

## Input projection

Schema: `vcstudio.reaction-domain-projection/v1`

Required top-level members:

- `project_id`: opaque workspace identity;
- `network`: canonical `ReactionNetwork` envelope or validated payload;
- `surfaces`: canonical `CatalystSurface` envelopes/payloads;
- `states`: canonical `AdsorbateState` envelopes/payloads;
- `transition_states`: explicit transition-state envelopes/payloads;
- `steps`: canonical `ElementaryStep` envelopes/payloads;
- `conditions`: canonical `ConditionSet` envelopes/payloads;
- `bindings`: non-persistent structure/evidence/thermochemistry projections,
  keyed by canonical opaque object ID;
- `applicability`: evidence-bound ranges for any adjustable condition.

The `bindings` projection is an adapter output, not a storage surface. It may
carry hashes and parsed numeric evidence required by this workbench, but this
package never writes it back to the project or canonical DTOs.

## Stable output schemas

### `vcstudio.reaction-graph/v1`

Contains opaque nodes and elementary-step edges with:

- canonical object and projection hashes;
- structure, method, and evidence hashes;
- explicit reactant/product/TS IDs and unit stoichiometric coefficients;
- canonical condition-set ID/hash;
- separate thermodynamic and kinetic readiness;
- forward/reverse reaction and activation free energies only when compatible;
- explicit `missing_nodes` and `missing_edges`;
- `mechanism_complete=false` unless a future independent mechanism-completeness
  authority establishes otherwise.

`artifact_status=available` requires every bound edge to be both
thermodynamically and kinetically ready. Rendering never changes
`scientific_status`.

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
- TS frequency/mode qualification.

No missing term is replaced by zero. Every participating term requires an
explicit model and evidence hash. Arithmetic is denied when method, reference
state, standard state, canonical condition set, T/P/pH/potential, coverage,
component models, solvent model, coverage model, or low-frequency treatment is
missing or different.

Supported model vocabulary is explicit and does not import or copy CatMAP.

### `vcstudio.condition-derived-revision/v1`

Condition Explorer accepts only bounded `T`, `P`, `pH`, electrode potential,
and coverage parameters. The server produces a deterministic revision bound to:

- the canonical source-projection hash;
- the condition-request hash;
- evidence-bound applicability ranges;
- per-parameter response evidence;
- an evidence-bound joint/additive model when multiple parameters change.

The revision includes derived node and edge values. It always declares
`source_evidence_mutated=false`. Missing applicability, missing response
evidence, unsupported interactions, or out-of-range values are unavailable.

### `vcstudio.frozen-reaction-network/v1`

This is the only supported handoff to the Microkinetics package. It freezes:

- source projection, graph, and condition-revision hashes;
- explicit reactants, products, TS, and stoichiometry;
- condition-set and method/evidence hashes;
- condition-derived reaction free energies;
- forward and reverse activation free energies;
- independent thermodynamic and kinetic statuses;
- missing reasons and `microkinetics_ready`.

Downstream code must reject `readiness=blocked`, must not infer missing barriers
or stoichiometry, and must not treat this DTO as a rate-law or reactor-model
selection. `authorizes_execution` is always false.

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
