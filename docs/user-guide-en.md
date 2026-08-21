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

The legacy Tk UI is compatibility-only. It does not provide the current Web shell's Project lifecycle, Selection Tray, Resume Center, report insights, laboratory policies, or governed next-calculation recommendations.

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

The Home **Resume Center** lists resumable settings, imports, and report-configuration drafts from the current session. It stores bounded draft references only; opening one still makes the owning component reload authoritative server state. A browser draft does not become a project fact, job status, or published revision. Explicitly saved settings and published reports are already durable and do not depend on Resume Center.

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

Environment → Settings also provides **laboratory recommendation policies**. A template may propose method and resource values, with explicit overrides limited to approved fields; previewing it changes no job. Confirmation persists a revisioned, hashed selection, but the policy remains `recommendation_only`: it does not rewrite existing `job.yaml` files, modify generated inputs, or submit work. A concurrent policy update produces a revision conflict and requires the user to review and confirm again.

## 4. End-to-end workflow

### Create, clone, move, or adopt a project

Use Project to create or import a project, inspect its members, references, method fingerprint, and workflow stage. Project state is read from manifests and evidence, not inferred from a label on screen.

Project lifecycle identity rules are explicit:

| Operation | Identity and path semantics |
|---|---|
| Clone | Copies the current project to a new directory and mints a new project UUID/opaque ID and remote namespace; only project-local locators are rebased, while external references remain external |
| Move | Moves the current project while preserving its UUID/opaque ID; project-local locators are rebased and the registry atomically replaces the old location for the same identity |
| Adopt Copy | Claims an existing copied project folder; **Remint identity** is the default so two folders do not share one identity. Preserve is allowed only when explicitly selected and no identity/path conflict exists |

All three flows use a server-selected source/destination and a dry-run preflight. No copy, move, or registry write occurs before explicit confirmation. Apply rechecks whether the project, registry, or ledger changed after preflight, and never overwrites an existing destination. Registry or ledger failure rolls back files, `project.yaml`, and registration projections. If the filesystem prevents complete rollback, the UI reports partial rollback and retains recovery evidence instead of reporting false success. Clone, Move, and Adopt also publish their lifecycle to the shared Operation Queue.

Project also provides a **Research Notebook + Human Review Ledger**:

- Notes are categorized as problems, hypotheses, observations, interpretations, limitations, or next steps; decisions and human reviews are separate records.
- Every save appends a journal revision under revision+head+project CAS. An external machine-local head/sequence anchor detects a missing, truncated, or rolled-back journal. An edit uses `supersedes`; deletion appends a tombstone and preserves history.
- Links use opaque project/job/source/report-revision IDs. The server revalidates every link on read, displays current/stale/missing, and returns semantic navigation routes rather than filesystem paths. A missing or corrupt `job.yaml` is missing evidence, never a path-derived current fact.
- Attachment bytes enter project-local content-addressed storage through the native picker. Browser DTOs expose only name, size, media type, and hash. Notebook, attachment, anchor, and lock paths reject symlinks, junctions, and reparse points; blob hashes are rechecked on read and archive.
- A human locally enters their reviewer ID, display name, role, decision, requested changes, and optional signature attribution. This is self-attribution, not authentication or a cryptographic electronic signature.
- An AI proposal cannot be recorded as human review. Notebook review never changes `ValidationResult`, claims, final status, campaign accepted status, or `human_scientific_reviewed`.

Unsaved body text and attachment bytes never enter workspace localStorage. Resume Center stores only a safe marker containing the opaque project ID; it can return to the owning project but cannot reconstruct an unsaved body.

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

### Submit, batch-review, and monitor

Run → Jobs supports single and batch submission, multi-server refresh, result fetching, remote file management, the local runner, and supported derived calculations.

The nine-state `job.yaml` lifecycle records operational progress such as CREATED, SUBMITTED, and RUNNING. Submission is blocked before network access when the project, server, input, or capability contract is invalid.

After selecting multiple jobs, the **Selection Tray** lists the entire selection. Jobs hidden by current status, cluster, or project filters remain counted and are called out explicitly. Expand **Batch Review** before Submit, Fetch, Continue, Cancel, or Remove. The page acquires an exclusive operation lock before confirmation can race with a second click. Submission, continuation, and cancellation also use server idempotency keys: retrying the same payload replays the recorded result, while an in-flight duplicate remains busy and cannot repeat the remote side effect. Long operations from Jobs, Project lifecycle, Analysis, and Publish share the global **Operation Queue**, with confirming/running/succeeded/failed states.

Select **Check duplicate calculations**, or begin submission, to rebuild a bounded advisory index
from the server ledger, authoritative `job.yaml` manifests, and current file hashes. The panel shows
versioned fingerprint fields, exact matches, field-level differences for near matches, failed,
unconverged, or incomplete sources, savings estimates, and source-verification status. A near match
is never an equivalence claim. Missing Method Recipe semantics, VASP/build identity, or any required
input leaves a legacy job `incomplete / explicit_legacy`. Reuse is off by default. **Reference
existing result** revalidates the source and creates a fresh provenance node/link and decision without
inheriting accepted/final qualification; choosing to recalculate an exact match requires a retained
reason. The browser sends opaque job IDs and does not parse or fingerprint VASP inputs.

The Selection Tray's **read-only resource forecast** reconstructs evidence from the server ledger, manifests, and matching historical task records. It reports a core-hour point estimate and range, confidence, failure rate, and budget risk; batch budget checks use the high end of the interval. Sparse history or incomplete inputs remain low-confidence/unavailable rather than guessed. A forecast always has `recommendation_only=true` and `authorizes_submission=false`; it cannot bypass confirmation, host trust, or idempotency gates.

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

### Analyze, Task Results, and governed recommendations

Available analysis routes include adsorption, reaction thermodynamics, electronic structure, charge, NEB, comparisons, and custom properties. The current Multiwfn registry contains **14 analysis entries**, and the UI menu is derived from that registry.

If convergence, reference state, structure, method fingerprint, unit, or complete-output evidence is missing, the result remains blocked or `unknown`.

Start with the Analysis **capability cards**. The server assigns `available`, `missing_prerequisite`, `mode_mismatch`, `not_implemented`, or `unavailable`. A non-available card cannot run and offers one action tied to the actual missing condition. Numeric values, display precision, parser module/version, opaque source identity, manifest/file hashes, and denominators are finalized on the server; the browser does not recompute scientific values.

Current parser boundaries are:

- Electronic structure can parse DOS/PDOS, bands/band gaps, and work function from registered project members or manifest-linked descendants with `vasprun.xml`, `EIGENVAL`, `LOCPOT`, and `OUTCAR` evidence;
- Charge and wavefunction can parse Bader and charge-density-difference results from `ACF.dat` and `CHGDIFF.vasp`;
- **ELF has a read-only quantitative distribution parser.** The server validates the main `ELFCAR` grid, finite values, and physical ELF range, then reports grid shape, atom count, min/max/mean/std, deterministic quantiles, and a histogram. This is not a bond, basin, critical-point, or topology analysis;
- **Task Results** summarizes server-parseable registered tasks and their evidence. A nearby directory outside the project-member/manifest-descendant chain is not silently included;
- **Property calculators** currently cover surface energy, formation/binding energy, and VASPsol solvation energy from manifest-bound operands. Incomplete sources, missing parser identity/version, or calculation failure fail closed.

The **Next-calculation recommendations** card is a governed read-only aid. Recommendations are produced only when the current analysis data fingerprint is bound to a `human_scientific_reviewed`, final-allowing ValidationResult, and only from frozen signals such as missing evidence, near-degeneracy, or sensitivity. Confirmation creates a hashed, non-executable draft intent. It contains no shell command, `sbatch`, `qsub`, or submission bridge and never creates or submits a job automatically. Missing human review, a closed final gate, or a fingerprint mismatch leaves the card blocked/unavailable.

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

Publish → Versions also provides:

- **Scientific diff** — select two authoritative history revisions. The server revalidates each complete bundle and compares ReportSpec scope, snapshot/input hashes, validation checks/status, scientific qualification, table/model numbers, figures, and manifest/file hashes. A date difference alone is not a scientific difference;
- **Evidence/Claim Graph** — derives conclusion, table, figure, check, source, job, and file-hash nodes/edges only from frozen model/spec/snapshot/validation/claim records. Missing links are explicit; mutable live files are not used to fill gaps, and local paths are not returned to the browser;
- **SI capsule** — obtains a single-use opaque destination token and a frozen preview receipt before explicit confirmation, then writes a deterministic ZIP containing canonical input manifests, contracts, model/figure metadata, Methods, BibTeX, environment/version, validation records, a Research Notebook ledger/limitations snapshot, a capsule manifest, and `SHA256SUMS`. Secrets, absolute paths, attachment bodies, caches, and mutable live files are excluded; an existing ZIP is never overwritten;
- **DOI-ready local archive** — in Publish → Export, first select a current revision and run a path-free dry-run. Every candidate shows its logical role, size, SHA-256, license/attribution, sensitive-risk status, include/exclude decision, and reason, together with submission readiness and gaps. After explicit confirmation, the server revalidates the same revision, source manifest, plan, destination identity, and confirmation envelope, then uses a destination lock, same-directory staging file, `fsync`, self-verification, and atomic non-overwriting publication. The deterministic ZIP contains the frozen spec/snapshot/validation/model, actual report files and authoritative figures, input manifest, Methods, frozen environment and parser versions, provenance graph, README/CITATION, archive manifest, and `SHA256SUMS`. Raw POTCAR content, secrets, absolute paths, caches, temporary/mutable live files, and resources without complete redistribution rights remain excluded.

These tools accept only the current opaque project ID and a revision ID. A stale or tampered bundle, wrong project, or missing evidence becomes stale/blocked/unavailable. A successfully written archive proves the local ZIP, manifest, and checksums, not scientific validation; a diagnostic/blocked revision remains diagnostic/blocked rather than becoming final publication. This workflow does not upload the archive, request a DOI, or claim publication.

### Format and accessibility boundaries

| Format | Current boundary |
|---|---|
| HTML | Semantic structure, document language, and image-description foundation; accessibility is conditional and still requires browser, screen-reader, and human review |
| DOCX | Language, Heading/Caption styles, repeating table headers, and image-description foundation; accessibility is conditional and still requires Word Accessibility Checker and human review |
| PDF | Visual, searchable ReportLab output; untagged, no structure tree or image alt text, `tagged=false`, `pdf_ua=false`, accessibility status partial |

The current PDF must never be described as PDF/UA or as an accessible replacement for HTML/DOCX. See [report-state-contract.md](report-state-contract.md) and [research-notebook-contract.md](research-notebook-contract.md).

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
| The ELF card is unavailable | Check that the job is DONE, ELFCAR is complete, and method evidence is verifiable. The parser reports only an ELF distribution summary, not bonding or topology conclusions |
| Next-calculation recommendations are unavailable | They require a human-scientific-reviewed, final-allowing ValidationResult bound to the current data fingerprint; the gate is not bypassed |
| Clone/Move reports partial rollback | Do not repeat the mutation blindly. Preserve the recovery evidence and inspect the original location, destination, registry, and ledger before choosing recovery |

## 10. Tests, history, and citation

Run:

```powershell
python -m pytest
```

The final local gates were actually run on 2026-08-13: cache-free full `python -m pytest` completed with **3658 passed and 5 skipped**, plus **15 upstream ASE/NumPy deprecation warnings**, in 330.80 s; full-repository Ruff and actionlint passed; and `node --check` passed for 23 first-party JavaScript files. The PyInstaller `--full` one-file build succeeded. The final EXE is **113,586,017 bytes (108.32 MiB)** with SHA-256 `bcadc9046685c62cf1a9157d0ceba49b131190184dbe30073ce4189ec6817e2d`.

The strict scientific-fingerprint/reuse change was revalidated in an isolated worktree on
2026-08-15. Cache-free full pytest completed with **3693 passed and 5 skipped** in 226.23 seconds,
with 15 upstream ASE/NumPy deprecation warnings. Full-repository Ruff, `node --check` for 25
first-party JavaScript files, `py_compile` for the changed Python files, and `git diff --check`
passed. This run did not rebuild the executable, rerun actionlint, or execute a real cluster/VASP
job; the frozen binary hash above remains evidence only for the 2026-08-13 artifact.

On 2026-08-21 the integrated tree completed the final local gates again: cache-free full pytest passed
with **5189 passed and 12 skipped** in 368.58 seconds, with 15 upstream warnings; full-repository Ruff,
`node --check` for 28 first-party JavaScript files, `pip check`, and `git diff --check` passed. Actionlint
was not installed locally and remains a hosted-CI gate. The `vcstudio-4.0.0` wheel contains 52 Web assets
and both locales. The PyInstaller `--full` EXE built from cross-platform closure commit `9758829` is
**115,182,610 bytes (109.85 MiB)** with SHA-256
`414c1091766f74d13a5e68858bef60b2c3049d07a27111709b7ef8bd0bd45a7d`.

Inside that 2026-08-21 frozen EXE, both `full` and `journey` healthchecks exited 0 and each reported
`ok=true` and `frozen=true`. The `full` profile passed 6/6 checks; `journey` completed all 10/10 phases,
recorded 0 network attempts and 0 cluster operations, and passed the restart-persistence check after service
reconstruction. Project lifecycle, the Jobs state machine, Analysis source/capability handling, report insights,
Resume Center, resource forecasting, laboratory policies, and next-calculation governance also retain focused
Python and/or executable-Node production-script regressions; the frozen journey preserves honest
`blocked`/`diagnostic` scientific states.

These are local software gates, not a substitute for GitHub-hosted remote CI; the exact pushed commit must still pass the hosted matrix. The `v4.0.0` tag has not been created and the changelog remains Unreleased; the 12 skips are not functionality passes. No real remote-cluster work or real scientific/experimental validation was performed, so automated checks, the offline journey, and the EXE artifact do not establish scientific validity or release readiness.

CI covers Ubuntu and Windows on Python 3.10, 3.11, and 3.12. Windows package-smoke is configured to build the real single-file EXE and run both `full` and `journey` inside that final binary. Journey uses an isolated HOME/config, real Api/ReportService, minimal offline jobs, a real Analysis preview, a diagnostic HTML report, and service reconstruction followed by persistence checks.

[progress-2026-07-16.md](planning/progress-2026-07-16.md) is a historical snapshot, not the current 4.0 capability or test baseline. Release history and its date-bound counts remain in [CHANGELOG.md](../CHANGELOG.md) and should not be mechanically replaced with current numbers.

Cite via [CITATION.cff](../CITATION.cff), contribute via [CONTRIBUTING.md](../CONTRIBUTING.md), and see the [MIT license](../LICENSE).
