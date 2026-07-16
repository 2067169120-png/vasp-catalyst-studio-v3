# VASP Catalyst Studio (vcstudio)

Lightweight desktop automation platform for VASP catalysis workflows:
**input generation → multi-cluster submission → failure diagnosis → bounded
recovery → ΔE / free-energy analysis → publication-ready reports** — packaged
as a single Windows EXE, with a deterministic zero-token core (LLM only in the
optional analysis layer) and fully offline operation (your data never leaves
your machine).

> 📂 Repository map: **[STRUCTURE.md](STRUCTURE.md)** ·
> 中文完整用法: **[使用说明.md](使用说明.md)** ·
> English guide: **[docs/user-guide-en.md](docs/user-guide-en.md)** ·
> 开发进度/待办: **[docs/planning/progress-2026-07-16.md](docs/planning/progress-2026-07-16.md)**

## 软件流程图 · Workflow

全流程 **生成 → 提交 → 监控/诊断 → 有界恢复 → 分析/报告**,每个区块标注了负责的代码包:

![vcstudio 流程图](docs/vcstudio-flowchart.png)

### 代码区块图(按流程分块,方便按需求查代码)

| 流程区块 | 代码包 | 负责什么 | 关键文件 |
|---|---|---|---|
| ① 生成 Generate | `vcstudio/generate/` | POSCAR+用户INCAR → 校验补全四件套(缺项才补,不改用户键) | `job_builder.py` `incar_builder.py` `kpoints.py`(倒格矢) `potcar.py` `poscar.py` |
| ② 提交 Submit | `vcstudio/cluster/` | PBS/Slurm 双方言、preflight、SSH 上传+提交 | `schedulers.py` `script_builder.py` `submitter.py` `connection.py` |
| ③ 监控+诊断 Monitor | `vcstudio/cluster/` | 查队列判排队/运行/终态 → 取证 → 12+ 失败分类;**续算沉降护栏** | `submitter.refresh_job` `diagnose.py` `convergence.py` |
| ④ 有界恢复 Recovery | `vcstudio/cluster/` | CONTCAR/改参续算(≤3轮,INCAR 冻结),否则交人工 | `submitter.continue_from_contcar` `batch_ops.py` |
| ⑤ 分析+报告 Analyze | `vcstudio/project/` + `external/` | ΔE 门控、吸附能/ΔG台阶/d带中心/Bader、论文级出图、报告 | `adsorption.py` `freeenergy.py` `thermo.py` `dosparse.py` `bader.py` `charts.py` `external/native_charts.py` |
| 跨层基础 Shared | `vcstudio/shared/` | 配置、清单(job.yaml状态机)、凭据(keyring) | `config.py` `manifest.py` `secrets.py` |
| 界面 GUI | `vcstudio/gui_web/`(默认 Web)+ `gui/`(旧 tkinter) | pywebview 前端 + `api.py` 薄门面;五页:仪表盘/生成/项目/任务/集群 | `gui_web/api.py` `assets/*.js` |

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

## 2026-07-16 更新(本轮进展)

- **修复续算状态误识别**:续算重投后新作业尚未进调度器队列时,不再拿上一轮旧
  OUTCAR 误判为"已完成/需续算/SCF 震荡",而是保持 SUBMITTED 视作仍在排队
  (续算沉降护栏,PBS/Slurm 均覆盖,含回归测试)。
- **原生论文级出图引擎** `external/native_charts.py`:纯 matplotlib 达论文质量
  (serif/矢量 PDF/多面板/色盲安全色板),不依赖 Origin/POV-Ray。吸附能分组柱状图、
  数据矩阵表、ΔG 自由能台阶图、**多催化剂热图**、**火山图**(自动求 Sabatier 峰顶)、
  标度关系图。*(注:引擎已就绪,接入 GUI/报告与打包收录为下一步,见 progress 文档)*
- **结果分析增强**:PDOS 投影 + d 带中心、Bader 电荷解析、可选 ΔG 口径
  (默认 ZPE−TS 对齐文献 / 可切 ASE 严格式含振动内能项)、PDS/U_L 图数一致性。
- **输入正确性**:KPOINTS 改用倒格矢(修六方/hcp slab 欠采样)、项目内 ENCUT 强制
  统一(保吸附能 ΔE 各成员基组一致)、计算类型下拉(修 web 硬编码 slab)。
- **诊断扩展**:磁盘满/IO 错误、裸退出码 137 降级为疑墙钟(可续算,不再困死)、
  ZBRENT 扩展签名。
- **界面**:任务页按吸附能项目组归并折叠(整组进度)、dashboard 首页落地。
- **工程化**:config 脱敏(移除内网 IP)、版本号单一事实来源、依赖分组声明、
  CI 加 lint+覆盖率+打包冒烟。

全套 476+ 测试通过。下一步待办见 **[docs/planning/progress-2026-07-16.md](docs/planning/progress-2026-07-16.md)**。

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
config.example.yaml  配置模板(复制为 config.yaml 填写;config.yaml 已 gitignore)
dist/           打包产物 EXE(gitignore)   results/  作业输出(不入 git)
```
