---
title: 'VASP Catalyst Studio: a project-centered, evidence-bound desktop
  workbench for catalysis calculations on HPC clusters'
tags:
  - Python
  - density functional theory
  - VASP
  - high-throughput screening
  - catalysis
  - workflow automation
authors:
  - name: Sheng Zhu
    affiliation: "1"
    email: 10234810401@stu.ecnu.edu.cn
affiliations:
  - name: School of Chemistry and Molecular Engineering, East China Normal University
    index: 1
date: 2026-07-15
bibliography: paper.bib
---

> **Draft metadata boundary.** The date above is the original manuscript date,
> not a V4 release date. Author name, affiliation and contact email are supplied.
> ORCID, funding and acknowledgement data remain unconfirmed and must be
> completed by the author before submission.

# Summary

VASP Catalyst Studio (`vcstudio`) is a Windows-first desktop research
workbench for project-centered, auditable density-functional-theory workflows.
Its deepest scientific path targets the Vienna *Ab initio* Simulation Package
(VASP) [@Kresse1996]: input generation from user-owned POSCAR and INCAR files,
PBS/Slurm submission over SSH/ProxyJump, monitoring with 18 job outcomes and
16 VASP internal-error signatures mapped to four target states, bounded
CONTCAR recovery, adsorption/free-energy analysis and evidence-bound report
publishing. CP2K, Gaussian and CASTEP expose bounded file-level adapters, but
their capability depth is asymmetric and the software does not assume
cross-engine numerical equivalence.

The V4 interface maintains a current project, mode, engine, task and stage
across preparation, running, analysis and publication routes. Per-job
`job.yaml` manifests record input hashes, a nine-state lifecycle and history;
file-backed campaign DAGs add task gates and ledgers; reports freeze a
ReportSpec, ReportSnapshot, ValidationResult and canonical model before a
revisioned manifest is published. This evidence chain is lighter than AiiDA's
queryable database provenance [@Huber2020AiiDA], but it makes the boundaries of
each local artifact explicit.

The deterministic generation, parsing, analysis and reporting core does not
require a cloud model. Network paths remain visible: remote cluster operations
require SSH, optional external-LLM analysis/paper extraction/chat defaults off,
and first-time DECIMER model acquisition may require a download. Model text is
never a numerical fact and cannot advance scientific or publication gates.

# Statement of need

Terminal pre/post-processors such as VASPKIT [@Wang2021VASPKIT] and qvasp
[@Yi2020qvasp] accelerate VASP preparation and analysis but generally leave
cluster orchestration to scripts. Workflow infrastructures such as AiiDA
[@Huber2020AiiDA], atomate2 [@Ganose2025Atomate2] and pyiron [@Pyiron2024]
provide stronger database-backed provenance and broader calculator support,
but assume a Python-first user and additional services. GUI platforms also
exist (for example ALKEMIE [@Wang2021ALKEMIE]), while recent LLM-agent
frameworks such as VASPilot [@Liu2025VASPilot] place online model inference
inside more of the operational loop.

For a researcher screening many adsorption systems on a shared cluster, three
requirements are easy to lose between those poles: methodology sovereignty,
bounded recovery and evidence that survives publication review. `vcstudio`
therefore keeps user INCAR keys immutable during generation/recovery, limits
automatic continuation to supported failure classes and three rounds, and
stops outside the rule set as `NEEDS_HUMAN`. It also intercepts molecule-slab
geometry clashes before submission and generates Methods/BibTeX text from the
frozen real inputs rather than from an unconstrained narrative.

The single-file Windows build removes the need for a separate Python
installation; it does **not** grant or bundle scientific software licenses,
POTCAR/BASIS/POTENTIAL data, every optional renderer, or system WebView2.

# Functionality

- **Project workspace:** seven top-level regions and semantic routes preserve
  project/engine/task/stage context, activity state, dirty guards and
  restart-safe preferences.
- **Prepare:** VASP four-file generation, calculation-specific KPOINTS,
  licensed PAW-library concatenation, ENMAX/ENCUT checks, structure tooling and
  preflight; bounded CP2K/Gaussian/CASTEP adapters expose only declared fields.
- **Run:** PBS/Slurm script models, SSH and host-key gates, local runner,
  monitoring, 18+16 failure taxonomy, output integrity and bounded recovery.
- **Analyze:** registry-driven adsorption, multi-project comparison,
  electronic/charge/task-result views, and Li--S free-energy paths. Raw DFT
  totals and uncorrected adsorption values use `E0`; ZPE/thermal/entropy
  corrections require explicit qualified frequency evidence, temperature,
  low-frequency policy, imaginary-mode gates and a correction fingerprint.
- **Publish:** figures plus revisioned ReportSpec → ReportSnapshot →
  ValidationResult → ReportModel/manifest bundles. Artifact availability,
  scientific qualification and publication eligibility remain separate.
- **Optional AI:** bounded result interpretation, paper method/data extraction
  and assistant chat behind an explicit external-data gate. AI output cannot
  write `accepted`, alter numerical facts or bypass budget/method/report gates.

# Report accessibility boundary

HTML emits language, metadata, heading/table/figure structure and alternative
text. DOCX emits core metadata, document/run language, Word heading/caption
styles, repeating table headers and image descriptions. Both remain
**conditional** on meaningful authored descriptions and browser/Word
Accessibility Checker plus human review. The current ReportLab PDF is visual
and searchable and records `/Lang` and metadata, but it is untagged, has no
structure tree or image alternative text, and explicitly reports
`tagged=false` and `pdf_ua=false`. Format generation is not a WCAG, scientific
review or PDF/UA certificate; the precise contract is documented in
`docs/report-state-contract.md`.

# Validation

The repository includes a fail-closed comparison pipeline
(`vcstudio/project/benchmark.py`) and a Li--S adsorption validation protocol
(`docs/validation.md`) following the comparison form of @Montoya2017. It can
report per-system errors, MAE, effective sample size, uncovered entries and
method differences when independently published reference values are supplied.
No headline vcstudio MAE is reported here: like-for-like V4 cluster runs and a
method-aligned, provenance-recorded reference table are still pending. A report
contract or unit-test pass cannot substitute for that scientific benchmark.

For software regression evidence, the V4 working tree was run locally on
Windows on 2026-08-11: **3336 tests passed and 5 environment- or
platform-specific tests were skipped**, with no failures. The maintained CI
matrix covers Ubuntu and Windows
on Python 3.10, 3.11 and 3.12. These counts are a dated snapshot; the latest
pytest/CI output is the authority for the current branch.

# Acknowledgements

Funding and acknowledgement text have not been supplied in the repository and
must be provided by the author before submission. No source of support is
inferred here.

# References
