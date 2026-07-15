# VASP Catalyst Studio — English User Guide

Condensed English companion to the full Chinese manual (`使用说明.md`).
Covers the end-to-end workflow: generate → submit → monitor → diagnose →
analyze → report.

## 1. The mental model

- Every job lives in its own directory whose **`job.yaml` is the single
  source of truth**: a 9-state machine (CREATED → SUBMITTED → RUNNING →
  DONE / UNCONVERGED / FAILED / NEEDS_HUMAN …) with a full audit history and
  sha256 provenance of the inputs.
- The **deterministic core never calls an LLM**. The optional AI analysis
  layer (`project/ai_analysis.py`) is the only place a model is invoked, and
  it receives your *real* INCAR — it cannot invent methodology.
- **Methodology sovereignty**: the platform completes only missing INCAR keys
  (e.g. MAGMOM) and never rewrites keys you set. Recovery never edits INCAR.

## 2. GUI workflow (web UI, default)

Launch `dist\VASP Catalyst Studio.exe` (or `vcs-gui`). Four pages:

1. **Generate** — pick POSCAR + your INCAR, calc type
   (molecule/slab/bulk decides the KPOINTS policy), output directory →
   4-file input set + `job.yaml`. A **3D preview** button renders the
   structure (offline 3Dmol.js) and annotates the shortest molecule–slab
   distance — geometry clashes are caught *before* submission.
2. **Adsorption project** — batch-create a slab + N adsorbate-configuration
   job set sharing one INCAR; ΔE is gated until every member is DONE.
3. **Jobs** — submit, refresh status, continue-from-CONTCAR (bounded, max 3
   rounds), fetch results, per-job actions: **Convergence** (E0/ΔE/|F|max per
   ionic step), **Structure** (CONTCAR-first 3D view), **Methods** (bilingual
   Methods paragraph + BibTeX generated from the job's real
   INCAR/KPOINTS/POTCAR), **DOS** (publication SVG from vasprun.xml).
4. **Cluster** — connection profiles (`clusters.yaml`), jump-host (double-hop
   SSH) support, resource parameters, job-script preview. Passwords are
   stored in the Windows Credential Manager, never in files.

## 3. CLI

```bash
vcs gen --poscar POSCAR --incar my.incar --calc-type slab -o results/job1/
# optional: --kpoints "5 5 1"   --lib-root <PAW library root>   --no-validate
```

Offline demo (fake POTCAR library, graphene cell):
see `examples/quickstart/README.md`.

## 4. Failure diagnosis and bounded recovery

`cluster/diagnose.py` classifies failures from scheduler exit reasons, output
integrity, log error signatures (cross-checked against Custodian's catalog)
and convergence traces into 12 job-level + 11 VASP-internal classes, reduced
to four terminal states:

| State | Meaning | What the platform does |
|---|---|---|
| DONE | converged, outputs complete | pulls results, renders figures |
| UNCONVERGED | ran out of ionic steps etc. | offers CONTCAR continuation (INCAR frozen, max 3 rounds) |
| FAILED | crash with known signature | labels the evidence; suggests, never auto-edits |
| NEEDS_HUMAN | rules don't cover it | stops and says so — no silent retries |

## 5. Analysis & reports

- **ΔE table**: only when all members are DONE; missing members are named.
- **Li–S discharge path**: ΔG staircase, rate-determining step highlighted,
  limiting potential U_L annotated (CHE convention).
- **Charts**: OriginLab engine when available (editable `.opju` archived),
  automatic SVG fallback otherwise. POV-Ray ball-and-stick structure figures.
- **AI chapter** (optional): bilingual interpretation with explicit caveats;
  requires an API key in the credential store.
- All energies are electronic (E0) without ZPE/entropy — stated in every
  report and prompt.

## 6. Validation & citing

The quantitative validation protocol (MAE against independent published
references, following Montoya & Persson's methodology) lives in
`docs/validation.md`; the feature comparison against published tools in
`docs/comparison.md`. Cite via `CITATION.cff`; contribute via
`CONTRIBUTING.md`.
