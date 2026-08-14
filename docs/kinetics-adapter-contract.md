# Microkinetic safe-adapter contract (phase 1)

## Authority and ownership

The microkinetic layer is a consumer, not a second reaction-data authority.
Reaction Map and Catalysis Model own their canonical DTOs and frozen
reaction/thermochemistry projections.  This layer accepts either a read-only
`Mapping` or an object implementing `KineticsInputProvider.kinetics_input()`.
It does not save a competing reaction-network object.

The adapter projection uses `vcstudio.kinetics-network/v1`.  Its
`source_projection` identifies the upstream schema, version, projection hash,
and evidence references.  `input_sha256` is the canonical JSON SHA-256 of the
exact adapter projection excluding only the self-declared `input_sha256` field.
Any change to species, steps, methods, conditions, assumptions, or provenance
therefore invalidates a previous preview and imported result.

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

## Preview and explicit confirmation

`preview_export()` performs no write and exposes no local executable path.  It
returns:

- the full input audit;
- the adapter ID/version;
- the user-configured tool basename/version/SHA-256/size, if a regular absolute
  file exists;
- planned artifact names, sizes, and SHA-256 values; and
- a preview token binding input, adapter, tool identity, and artifact hashes.

The tool is inspected by hashing the regular file only.  It is never imported
or executed.  Missing CatMAP leaves the solve capability `unavailable`, while
the audit and compatible input preview remain available.

`confirm_export()` requires `confirmed is True` and an exact preview-token
match.  It recomputes the bundle and writes only below:

```text
<project>/.vcstudio/kinetics/exports/<input_sha256>/
```

It never accepts an output filename or command from the browser.  Existing
different files are not overwritten.  The frozen bundle contains:

- `kinetics-input.json`
- `kinetics-audit.json`
- `energetics.tsv`
- `model.mkm`
- `process-contract.json`
- `manifest.json`

Every project-local directory component is resolved under the project root and
rejected if it is a symbolic link or Windows junction.  New bundles are built
in a sibling staging directory and published with one directory rename, so a
partially written bundle is never treated as confirmed.  Loading a confirmed
bundle rechecks the directory chain, manifest size, artifact names, sizes,
SHA-256 values, adapter identity, preview token, and diagnostic-only status.

When scientific evidence fails, only `kinetics-audit.json` is exportable.  The
process contract is explicitly non-executable: `execution_permitted=false`,
`shell=false`, `accepts_user_arguments=false`, and `auto_install=false`.  It
documents a fixed argv shape for an operator-controlled external runner but
does not invoke it.

## Result import

The only accepted result schema is `vcstudio.kinetics-result/v1`.  Import
requires exact matches for:

- input SHA-256;
- VASP Catalyst Studio adapter ID/version;
- user-installed tool SHA-256; and
- the complete unit map.

The normalized unit contract is K, bar, V, s^-1, fraction, dimensionless, and
eV as appropriate.  Unknown fields, non-finite numbers, paths/commands in
identifiers, out-of-range conditions, unknown species/steps/sites, coverage or
selectivity above one, and malformed convergence/sensitivity records are
rejected.

Imported values are server-normalized into
`vcstudio.kinetics-normalized-result/v1`.  A browser may render those values but
must not solve equations, recompute metrics, change units, or infer missing
results.

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
