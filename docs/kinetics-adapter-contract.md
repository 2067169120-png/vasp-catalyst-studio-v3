# Microkinetic safe-adapter contract (phase 1)

## Authority and ownership

The microkinetic layer is a consumer, not a second reaction-data authority.
Reaction Map and Catalysis Model own their canonical DTOs and frozen
reaction/thermochemistry projections.  This layer accepts either a read-only
`Mapping` or an object implementing `KineticsInputProvider.kinetics_input()`.
It does not save a competing reaction-network object.

The adapter projection uses `vcstudio.kinetics-network/v2`.  Its
`source_projection` identifies the upstream schema, version, projection hash,
and evidence references.  `input_sha256` is the canonical JSON SHA-256 of the
exact adapter projection excluding only the self-declared `input_sha256` field.
Any change to species, steps, methods, conditions, assumptions, or provenance
therefore invalidates a previous preview and imported result.

The three current data boundaries are
`vcstudio.kinetics-network/v2`, `vcstudio.kinetics-result/v2`, and
`vcstudio.kinetics-normalized-result/v2`.  A v1 payload at any of these
boundaries is not silently upgraded.

## Mandatory audit

`vcstudio.project.kinetics.audit_network()` fails closed and reports all issues
it can find.  It checks:

1. exact schema, unknown fields, safe identifiers, and declared input hash;
2. upstream projection schema/version/hash and bounded evidence references;
3. explicit mean-field and steady-state assumptions;
4. uniform-site and lateral-interaction boundaries;
5. an explicit mechanism-completeness claim and feed-to-target connectivity;
6. temperature, total pressure, concentration, and potential standard states;
7. bounded temperature/pressure/potential operating ranges and cross-checks;
8. element, charge, and site conservation through reactants, transition state,
   and products;
9. duplicate/reverse-duplicate steps, missing species, and unused species;
10. reaction energy, forward/reverse barriers, state-energy identities, and
    detailed balance;
11. method identity/compatibility and per-energy evidence/uncertainty; and
12. prefactor, BEP, scaling, and step-uncertainty provenance.

Gas species carry explicit partial pressures with provenance.  Their sum must
equal the declared total pressure.  Electrochemical projections must declare a
potential model, standard potential, reference electrode, and potential range.

Stationary-point roles are structural, not caller labels.  Each elementary
step has exactly one coefficient-one `transition_state`-phase species.  The
only additional transition-side entries permitted are neutral, composition-free
`surface` species used for explicit empty-site bookkeeping.  Every transition
state has exactly one frequency below -50 cm^-1; non-transition-state species
have none.  Negative frequencies from -50 cm^-1 through zero, including gas
modes, are treated as bounded numerical noise.  The 50 cm^-1 threshold is a
fixed server policy and cannot be relaxed by input.

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
surface states, ideal lateral interactions, and equal forward/reverse `s^-1`
prefactors (frozen into CatMAP's `prefactor_list`).  Electrochemical potential
dependence, liquid/solution species, non-Gibbs energy bases, asymmetric or
pressure/concentration-dependent prefactors, multisite species, and
parameterized lateral interactions remain unavailable until an equally strict
export mapping exists.  Such a projection retains its core audit and receives
a separate `CATMAP_PHASE1_CONTRACT_UNSUPPORTED` adapter audit; no model scaffold
is emitted.

Phase 1 also requires exactly one site type because the canonical input does
not yet carry evidence-bound site-population totals.  It never invents an
independent total of `1.0` for each of several site types.  The one explicit
empty-site species must have an exact frozen formation energy of zero eV,
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
