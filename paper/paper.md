---
title: 'VASP Catalyst Studio: a zero-install desktop platform for bounded,
  auditable VASP catalysis workflows on HPC clusters'
tags:
  - Python
  - density functional theory
  - VASP
  - high-throughput screening
  - catalysis
  - workflow automation
authors:
  - name: Yuhao Chen
    # AUTHOR-TODO:投稿前补填 ORCID(勿编造);拿到后取消下一行注释并填入真实 ID。
    # orcid: 0000-0000-0000-0000
    affiliation: 1
affiliations:
  # AUTHOR-TODO:投稿前补填单位(机构 / 城市 / 国家)。
  - name: <AUTHOR-TODO institution, city, country>
    index: 1
date: 2026-07-15
bibliography: paper.bib
---

<!-- AUTHOR-TODO:作者名已填 Yuhao Chen;ORCID 与单位仍为占位,投稿前必须补填真实
     信息(勿编造)。ORCID 见上方注释行。 -->

# Summary

VASP Catalyst Studio (`vcstudio`) is a lightweight desktop platform that
automates the full loop of a catalysis screening campaign with the Vienna
Ab initio Simulation Package (VASP) [@Kresse1996]: validated input generation
from a user's POSCAR and INCAR, submission to PBS/Slurm clusters over
double-hop SSH, monitoring with a 15-class job diagnosis backed by 15 VASP
internal-error signatures (`docs/failure-taxonomy.md`), bounded
CONTCAR-based recovery, adsorption-energy and Li--S discharge-path analysis,
and publication-ready reports (convergence charts, 3D structure previews with
molecule--slab clash interception, bilingual Methods paragraphs with BibTeX
generated from the *actual* input files, and total-DOS figures). The tool
ships as a single Windows executable requiring no installation, database or
server; the core is fully deterministic and offline, with an optional
LLM-based analysis layer kept strictly outside the decision loop. Provenance is
per-job manifest-level: every job directory carries a `job.yaml` recording
input hashes (sha256 of POSCAR/POTCAR), a nine-state lifecycle and an audit
history, so a result can be traced to the exact inputs that produced it. This
is deliberately lighter than a queryable full-provenance DAG (e.g. AiiDA); the
trade-off is discussed in `docs/comparison.md`.

# Statement of need

Existing VASP tooling clusters at two poles. Terminal pre/post-processors
such as VASPKIT [@Wang2021VASPKIT] and qvasp [@Yi2020qvasp] accelerate input
preparation and analysis but leave cluster orchestration to hand-written
shell scripts. Workflow infrastructures such as AiiDA [@Huber2020AiiDA],
atomate2 [@Ganose2025Atomate2] and pyiron [@Pyiron2024] provide robust
provenance and multi-code support, but assume a Python-first user operating a
daemon/database stack. GUI platforms exist (e.g. ALKEMIE [@Wang2021ALKEMIE]),
and recent LLM-agent frameworks such as VASPilot [@Liu2025VASPilot] automate
error recovery with online model inference in the loop.

For the graduate-student catalysis workflow — screening tens to hundreds of
adsorption systems on a shared university cluster — none of these fits: data
often may not leave the local machine, cluster time is too scarce for
avoidable resubmissions, and silent automated edits to a calculation's
methodology are unacceptable in work that must survive peer review.
`vcstudio` addresses this niche with three design invariants: (i)
*methodology sovereignty* — the user's INCAR keys are never overwritten, and
recovery never edits INCAR; (ii) a *deterministic, zero-token core* — LLMs
appear only in an optional analysis layer; (iii) *explicit state, never
silent* — recovery is bounded (three rounds), and anything outside the rule
set halts as NEEDS_HUMAN with evidence. To our knowledge, no published tool
packages this combination (see `docs/comparison.md`): it also intercepts
geometry errors before submission (covalent-radius clash scan of the
molecule--slab gap) and generates Methods paragraphs guaranteed consistent with
the actual INCAR/KPOINTS/POTCAR, including the subtle case where the GGA tag
overrides the POTCAR flavor.

# Functionality

- **Generate**: POSCAR + user INCAR → validated four-file input set;
  KPOINTS policy by calculation type; POTCAR concatenation from a licensed
  PAW library with ENMAX/ENCUT cross-checks; sha256 provenance in `job.yaml`.
- **Submit**: PBS and Slurm dialects as pure functions; jump-host SSH with
  host-key gating; preflight checks; user job-script templates passed through
  verbatim.
- **Monitor & diagnose**: scheduler terminal reasons, output integrity, log
  error signatures and convergence traces → four terminal states.
- **Bounded recovery**: CONTCAR continuation for recoverable classes only,
  INCAR frozen, maximum three rounds.
- **Analyze & report**: gated adsorption energies (all members DONE), Li--S
  discharge path (ΔG staircase, rate-determining step, limiting potential),
  OriginLab/SVG dual chart engines, POV-Ray structure figures, and a
  validation pipeline that computes MAE against independent reference data
  when such data are supplied (protocol in `docs/validation.md`, following
  @Montoya2017).

# Validation

The repository ships a validation protocol (`docs/validation.md`) and a tested
comparison pipeline (`vcstudio/project/benchmark.py`) that computes per-system
errors and the mean absolute error (MAE) of adsorption energies against
independently published reference values, following the methodology of
@Montoya2017. We report the protocol and pipeline here rather than a headline
MAE number: a like-for-like benchmark requires cluster runs whose functional,
dispersion correction and ENCUT are matched to the reference set, and those
runs are pending cluster access (see `docs/validation.md`). Once back-filled,
the pipeline emits a publication-ready comparison table for this section.
<!-- AUTHOR-TODO:集群数据回填后,把 benchmark.render_markdown 生成的对比表贴入
     此节,并声明泛函/色散修正/ENCUT 对齐口径。 -->
The test suite comprises 525 automated tests (2 skipped when OriginLab and
POV-Ray are absent) run in CI on Linux and Windows.

# Acknowledgements

<!-- AUTHOR-TODO:资助号/致谢。 -->

# References
