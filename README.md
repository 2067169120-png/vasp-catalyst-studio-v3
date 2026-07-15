# VASP Catalyst Studio (vcstudio)

Lightweight desktop automation platform for VASP catalysis workflows:
**input generation → multi-cluster submission → failure diagnosis → bounded
recovery → ΔE / free-energy analysis → publication-ready reports** — packaged
as a single Windows EXE, with a deterministic zero-token core (LLM only in the
optional analysis layer) and fully offline operation (your data never leaves
your machine).

> 📂 Repository map: **[STRUCTURE.md](STRUCTURE.md)** ·
> 中文完整用法: **[使用说明.md](使用说明.md)** ·
> English guide: **[docs/user-guide-en.md](docs/user-guide-en.md)**

## Statement of need

Graduate-student catalysis screening lives between two worlds that existing
tools serve poorly: terminal pre/post-processors (VASPKIT, qvasp) leave
cluster orchestration to hand-written bash, while workflow infrastructures
(AiiDA, atomate2, pyiron) assume a server/database mindset and a Python-first
user. LLM-agent frameworks (VASPilot, AutoDFT) automate decisions but require
online inference in the loop. vcstudio targets the gap: a zero-install desktop
tool that drives double-hop PBS/Slurm clusters, refuses to guess (explicit
state machine, bounded self-healing, NEEDS_HUMAN stops), intercepts geometry
errors *before* they burn cluster time, and generates Methods paragraphs
guaranteed consistent with the actual INCAR/KPOINTS/POTCAR. See the full
[feature comparison with published tools](docs/comparison.md).

## Features

| Layer | Module | What it does |
|---|---|---|
| Generate | `vcstudio/generate/` | POSCAR + your INCAR → validated 4-file input set; completes only missing keys, **never overwrites yours**; `job.yaml` records sha256 provenance |
| Submit | `cluster/{schedulers,script_builder,submitter,connection}` | PBS/Slurm dialects (pure functions); preflight gates; dual-track scripts (auto / your template passed through verbatim) |
| Monitor + diagnose | `submitter.refresh_job` + `cluster/diagnose.py` | scheduler exit reason + output integrity + log signatures + convergence + energy sanity → 12+11 failure classes → 4 terminal states |
| Bounded recovery | `submitter.continue_from_contcar` | only for recoverable classes; CONTCAR validated; INCAR frozen; **max 3 rounds**; anything else → NEEDS_HUMAN |
| Analyze + report | `project/` + `external/` | ΔE gating (all-DONE before numbers), Li–S discharge path (ΔG/PDS/U_L), Origin/SVG dual chart engines, POV-Ray structure figures, bilingual LLM analysis (real INCAR injected, no fabricated citations) |
| Publication aids | `cluster/convergence.py`, `generate/structure_view.py`, `generate/methods_text.py`, `project/dosparse.py` | per-ionic-step convergence charts; 3D structure preview with molecule–slab clash interception; bilingual Methods + BibTeX from real inputs; total-DOS SVG from vasprun.xml |

Three invariants: ① methodology sovereignty (your INCAR/template is passed
through verbatim) ② deterministic core is zero-token ③ explicit state, never
silent (job.yaml state machine + audit history).

## Install & quickstart

**GUI (Windows)**: double-click `dist\VASP Catalyst Studio.exe`
(web UI default; `--legacy` starts the Tkinter fallback).

**Library + CLI**:

```bash
pip install -e .[dev]
vcs gen --poscar POSCAR --incar my.incar --calc-type slab -o results/job1/
```

**Try it offline in 5 minutes** (no cluster, no licensed POTCARs needed):
[examples/quickstart](examples/quickstart/README.md) — includes a fake demo
POTCAR library generator (real pseudopotentials are licensed material and are
never distributed with this repository).

**Tests**: `python -m pytest` — 424 tests, 1 skipped (optional OriginLab
smoke behind `VCS_ORIGIN_SMOKE=1`). CI runs the suite on
ubuntu/windows × Python 3.10/3.12.

## Scientific conventions

- `E_ads = E(slab+ads) − E(slab) − E(ref)`; negative = favorable adsorption
- `μ_Li = (E(Li₂S) − E(S₈)/8) / 2`; discharge path ΔG referenced to S8* = 0;
  `U_L = −max(ΔG/Δn_e)` (computational hydrogen electrode)
- All energies are DFT electronic energies (no ZPE/entropy) — stated
  explicitly in reports and in AI prompts
- Validation protocol (Montoya-style MAE vs independent references):
  [docs/validation.md](docs/validation.md)
- Credentials (cluster passwords / LLM keys) go to the Windows Credential
  Manager only — never to plain text

## Contributing & citing

See [CONTRIBUTING.md](CONTRIBUTING.md). If you use this software, please cite
via [CITATION.cff](CITATION.cff). Licensed under [MIT](LICENSE).

---

## 中文速览

轻量化 VASP 自动化桌面平台:生成输入 → 多集群提交 → 失败诊断 → 有界恢复 →
ΔE/自由能分析 → 出版级报告。Windows 单文件 EXE,确定性核心零 token,
全离线运行。

- **快速开始**:双击 `dist\VASP Catalyst Studio.exe`(四页:生成/吸附能项目/
  任务/集群);改完代码双击 `重新打包EXE.bat` 重打包
- **离线体验**:[examples/quickstart](examples/quickstart/README.md)
  (石墨烯示例+假赝势库,五分钟跑通全链)
- **完整用法**:[使用说明.md](使用说明.md);目录结构:[STRUCTURE.md](STRUCTURE.md)
- **测试**:`python -m pytest`(424 用例)
- **科学约定/发刊工具链**:同上英文节;竞品对比见
  [docs/comparison.md](docs/comparison.md),验证协议见
  [docs/validation.md](docs/validation.md)

```
vcstudio/       核心包(generate/cluster/project/external/gui/shared/cli)
tests/          424 测试   docs/superpowers/specs/  设计文档
config.yaml     本地配置(赝势库/分子库/理想窗口/llm 端点)
dist/           打包产物 EXE(gitignore)   results/  作业输出(不入 git)
```
