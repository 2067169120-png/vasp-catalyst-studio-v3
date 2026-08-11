# VASP Catalyst Studio 4.0 — English User Guide

VASP Catalyst Studio 4.0 is a project-centered, evidence-driven, configurable research workbench for materials and catalysis workflows. It brings structure and input preparation, job execution, diagnosis, bounded recovery, analysis, figures, and reporting into one desktop application.

It does not treat automation as scientific approval. Missing evidence remains `unknown`, blocked, or explicitly assigned to human review.

> Chinese guide: [使用说明.md](../使用说明.md)
>
> Report contract: [report-state-contract.md](report-state-contract.md)
>
> Failure taxonomy: [failure-taxonomy.md](failure-taxonomy.md)
>
> Validation boundary: [validation.md](validation.md)

## 1. Install and launch

### Packaged Windows application

Launch:

```text
dist\VASP Catalyst Studio.exe
```

The packaged application opens the current Web workbench. It requires the Microsoft Edge WebView2 Evergreen Runtime.

### Source installation

```powershell
pip install -e ".[dev]"
vcs gui
```

The direct module entry point is:

```powershell
python -m vcstudio.gui_web
```

The launchers have different meanings:

| Command | Interface |
|---|---|
| `vcs gui` | Current default Web workbench |
| `python -m vcstudio.gui_web` | Direct entry point for the current Web workbench |
| `vcs gui --legacy` | Legacy four-tab Tkinter interface |
| `vcs-gui` | Still the legacy four-tab Tkinter entry point; it is not the default Web UI |

## 2. Current information architecture

The default Web interface has **7 top-level areas and 32 semantic routes**. Those routes reuse **11 ordinary physical workspace pages**; the AI Assistant opens as a drawer rather than another ordinary page.

| Area | Responsibilities |
|---|---|
| Home | Global overview, project pipeline, recent activity, and blockers |
| Project | Project overview, members, workflow, runs, and project activity |
| Prepare | Structures, inputs, batches, task templates, and preflight |
| Run | Local/remote jobs, submission, monitoring, continuation, and fetching |
| Analyze | Adsorption, thermodynamics, electronic structure, charge, comparisons, custom analyses |
| Publish | Figures, reports, SI, draft packs, revisions, and export |
| Environment | Clusters, local runner, dependencies, data paths, templates, and settings |

A global project context connects Prepare, Run, Analyze, and Publish. Deep links, moved projects, unsupported routes, and unsaved edits are handled explicitly; the interface does not silently substitute another project or turn display state into scientific state.

## 3. First-time configuration

### Licensed VASP data

Set the local POTCAR/PAW library root under Environment → Settings. Licensed pseudopotentials are not distributed by this repository or its releases.

For software-only evaluation, use the fake library in [examples/quickstart](../examples/quickstart/README.md). It is not suitable for scientific calculations.

### Cluster profile

Under Environment → Cluster:

1. configure host, port, user, remote root, and Slurm or PBS;
2. add ProxyJump only when required;
3. test the connection;
4. compare the returned host, algorithm, and SHA256 fingerprint character by character with administrator-provided evidence;
5. inspect the generated submission script;
6. store passwords only in the operating-system credential store.

Remote operations retain server ownership. A job cannot be continued, cancelled, or fetched through a different server profile.

### Workflow and engine

Settings selects the workflow mode, compute engine, and intended calculation type. This filters visible routes and supported tasks; it does not rewrite the facts of existing jobs.

VASP is the primary engine. CP2K, Gaussian, and CASTEP expose only their explicitly supported adapter capabilities.

## 4. End-to-end workflow

### Create or select a project

Use Project to create or import a project, inspect its members, references, method fingerprint, and workflow stage. Project state is read from manifests and evidence, not inferred from a label on screen.

### Prepare structures and inputs

Prepare includes:

- structure loading/editing, fixed-layer selection, and vacuum checks;
- molecule library, metal slabs, SAC candidate matrices, and spin families;
- the 23-type VASP TaskCatalog;
- NEB, batch preparation, and preflight;
- POSCAR/INCAR/KPOINTS/POTCAR generation.

The generator completes missing INCAR keys but does not overwrite keys supplied by the user. Generated jobs record input hashes, provenance, and a `job.yaml` manifest.

Minimal CLI generation:

```powershell
vcs gen --poscar POSCAR --incar my.incar --calc-type slab -o results/job1
```

### Submit and monitor

Run → Jobs supports single and batch submission, multi-server refresh, result fetching, remote file management, the local runner, and supported derived calculations.

The nine-state `job.yaml` lifecycle records operational progress such as CREATED, SUBMITTED, and RUNNING. Submission is blocked before network access when the project, server, input, or capability contract is invalid.

### Diagnose and recover

The current implementation has:

- **18 job-classification outcomes**;
- **16 VASP internal-error signatures**;
- reduction to four handling states.

| State | Meaning | Action |
|---|---|---|
| DONE | Converged and required outputs are complete | Eligible for the next validation stage |
| UNCONVERGED | Execution ended without satisfying convergence | Continuation is offered only for recoverable classes |
| FAILED | Failure has explicit evidence | Evidence and suggestions are shown; methodology is not auto-edited |
| NEEDS_HUMAN | Rules or evidence are insufficient | Processing stops for human review |

A permitted continuation validates CONTCAR, freezes INCAR, and is limited to three rounds. There are no unlimited silent retries.

### Analyze

Available analysis routes include adsorption, reaction thermodynamics, electronic structure, charge, NEB, comparisons, and custom properties. The current Multiwfn registry contains **14 analysis entries**, and the UI menu is derived from that registry.

If convergence, reference state, structure, method fingerprint, unit, or complete-output evidence is missing, the result remains blocked or `unknown`.

## 5. Engine boundaries

| Engine | Supported role |
|---|---|
| VASP | Primary workflow with the full input, cluster, diagnosis, derivation, analysis, and reporting chain |
| CP2K | Bounded file-level adapter for its declared tasks and native units |
| Gaussian | Bounded molecular/cluster file-level adapter |
| CASTEP | Bounded file-level adapter using CASTEP-native inputs, pseudopotentials, and parsers |

The adapters do **not** promise cross-engine equivalence. In particular, the workbench must not:

- convert VASP ENCUT into a CP2K GTH density-grid cutoff as if they were equivalent;
- compare VASP PAW and Gaussian basis-set absolute energies directly;
- compare VASP and CASTEP absolute energies without aligned pseudopotential, cutoff, reference, and method evidence;
- promote raw energy from a failed or unconverged calculation to a trusted result;
- infer missing engine-native units or method fingerprints.

## 6. Energy and free-energy conventions

- `E_ads = E(slab+ads) − E(slab) − E(reference)`.
- `E0` is an electronic energy; it is not automatically a finite-temperature free energy.
- ZPE, thermal, and entropy corrections enter ΔG only when they are explicitly present, applicable, validated, and recorded.
- Missing vibrational or thermochemical evidence is never guessed.
- Every report or figure must identify whether it uses E0, E0+ZPE, or a thermal/entropy-corrected convention.
- Cross-project comparisons require aligned species order, reference states, method fingerprints, and correction conventions.

Real-system replication MAE and manuscript submission/acceptance status remain `UNKNOWN`. Automated tests and synthetic demonstrations verify bounded software contracts, not scientific agreement with experiment or publication readiness.

## 7. Evidence and publication

The current publication chain is:

```text
ReportSpec → ReportSnapshot → ValidationResult → revision + manifest
```

ReportSpec fixes the requested configuration. ReportSnapshot freezes the inputs and evidence. ValidationResult records checks, warnings, blockers, and the claim ceiling. A revision and manifest bind artifacts and contract sidecars to hashes and sizes.

Two independent axes must remain separate:

1. **Artifact axis** — renderer availability, file generation, hash/size verification, and manifest registration;
2. **Scientific axis** — qualification, report purpose, claim ceiling, and publication-gate status.

A generated file is not proof of a final scientific conclusion. If project inputs change during rendering, files may be preserved for recovery but cannot become the current ready revision.

### Format and accessibility boundaries

| Format | Current boundary |
|---|---|
| HTML | Semantic structure, document language, and image-description foundation; accessibility is conditional and still requires browser, screen-reader, and human review |
| DOCX | Language, Heading/Caption styles, repeating table headers, and image-description foundation; accessibility is conditional and still requires Word Accessibility Checker and human review |
| PDF | Visual, searchable ReportLab output; untagged, no structure tree or image alt text, `tagged=false`, `pdf_ua=false`, accessibility status partial |

The current PDF must never be described as PDF/UA or as an accessible replacement for HTML/DOCX. See [report-state-contract.md](report-state-contract.md).

## 8. Network, privacy, and optional AI

| Capability | Boundary |
|---|---|
| Local generation, parsing, gates, hashes, and report model | Deterministic and local; no LLM call |
| SSH execution | Connects only to user-configured clusters through explicit host-key controls |
| External LLM | Disabled by default; requires explicit project-data transfer permission and a stored credential |
| DECIMER | Excluded from the default single-file Windows EXE; first model preparation may access the network when no local cache is available |
| Credentials | Cluster passwords and LLM keys use the system credential store, not ordinary configuration files |

An LLM may assist with interpretation, paper-method extraction, and conversation. It cannot change scientific job state, write `accepted`, bypass `completed → validated → accepted`, replace ValidationResult, or submit/cancel HPC jobs. “Stop response” stops only the current model response.

See [matclaw-integration.md](matclaw-integration.md) for the assistant boundary.

## 9. Troubleshooting

| Symptom | Resolution |
|---|---|
| Web UI fails to start | Install pywebview and Edge WebView2; inspect `startup-error.log` |
| `vcs-gui` shows only four tabs | Expected: it is the legacy Tk entry point. Use `vcs gui` |
| A route or task is hidden | Check project context, workflow mode, and engine capability |
| First SSH connection is blocked | Obtain and verify an administrator-provided SHA256 host fingerprint |
| Continuation is unavailable | Only recoverable classes with valid CONTCAR evidence and remaining round budget are eligible |
| Only some report formats were written | Check format availability and accessibility records; partial file generation does not complete publication |
| PDF is not PDF/UA | Expected for the current ReportLab renderer; use HTML/DOCX and complete human accessibility review |
| AI is unavailable | It is off by default; enable external transfer explicitly and configure a credential only if desired |
| DECIMER needs a download | The default EXE excludes DECIMER; an uncached model may require first-use network access |
| Analysis is unknown or blocked | Follow the listed missing-evidence items rather than inferring a value |

## 10. Tests, history, and citation

Run:

```powershell
python -m pytest
```

A full-suite evidence snapshot on **2026-08-11** recorded **3336 passed, 5 skipped**. This is date-bound evidence; the current source of truth is always the latest pytest output and CI.

CI covers Ubuntu and Windows on Python 3.10, 3.11, and 3.12.

[progress-2026-07-16.md](planning/progress-2026-07-16.md) is a historical snapshot, not the current 4.0 capability or test baseline. Release history and its date-bound counts remain in [CHANGELOG.md](../CHANGELOG.md) and should not be mechanically replaced with current numbers.

Cite via [CITATION.cff](../CITATION.cff), contribute via [CONTRIBUTING.md](../CONTRIBUTING.md), and see the [MIT license](../LICENSE).
