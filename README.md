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
| ③ 监控+诊断 Monitor | `vcstudio/cluster/` | 查队列判排队/运行/终态 → 取证 → 15 类作业分类 + 15 条 VASP 错误签名([失败分类表](docs/failure-taxonomy.md));**续算沉降护栏** | `submitter.refresh_job` `diagnose.py` `convergence.py` |
| ④ 有界恢复 Recovery | `vcstudio/cluster/` | CONTCAR/改参续算(≤3轮,INCAR 冻结),否则交人工 | `submitter.continue_from_contcar` `batch_ops.py` |
| ⑤ 分析+报告 Analyze | `vcstudio/project/` + `external/` | ΔE 门控、吸附能/ΔG台阶/d带中心/Bader、论文级出图、报告 | `adsorption.py` `freeenergy.py` `thermo.py` `dosparse.py` `bader.py` `charts.py` `external/native_charts.py` |
| ⑥ 结构生成 StructGen (v3.1) | `vcstudio/generate/` | SAC 模板库(六类配位×金属矩阵)、LiPS 分子库、吸附位点枚举与摆放、参考态注册 | `sac_builder.py` `molecules.py` `sites.py` `project/references.py` |
| ⑦ 派生计算 Derived (v3.1) | `vcstudio/generate/` + `project/` | 弛豫→频率(ZPE/熵)/电子结构静态(PDOS/Bader/差分电荷)一键派生;多自旋并跑;虚频质量闸 | `freq_builder.py` `estatic.py` `spin_scan.py` `chgdiff.py` |
| ⑧ 通用 CHE 引擎 (v3.1) | `vcstudio/project/` | 反应网络预设(Li-S 16e/缔合解离/ORR/HER/OER/CO2RR),U_eq/U_L/η,多电位台阶 | `reactions.py` `freeenergy.free_energy_path` |
| ⑨ campaign 控制面 (v3.1) | `vcstudio/campaign/` | 文件式任务 DAG:三态(completed/validated/accepted)、三门禁(提交/验收/报告)、方法指纹、决策账本、机时预算闸 | `schema.py` `states.py` `gates.py` `fingerprint.py` `ledger.py` `budget.py` `derive.py` |
| 跨层基础 Shared | `vcstudio/shared/` | 配置、清单(job.yaml状态机)、凭据(keyring) | `config.py` `manifest.py` `secrets.py` |
| 界面 GUI | `vcstudio/gui_web/`(默认 Web)+ `gui/`(旧 tkinter) | pywebview 前端 + `api.py` 薄门面;六页:仪表盘/生成/项目/作业/集群/设置 | `gui_web/api.py` `assets/*.js` |

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
| Monitor + diagnose | `submitter.refresh_job` + `cluster/diagnose.py` | scheduler exit reason + output integrity + log signatures + convergence + energy sanity → 15 job-classification outcomes + 15 VASP internal-error signatures → 4 terminal states ([taxonomy](docs/failure-taxonomy.md)) |
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
  标度关系图。**已接入 GUI**:项目页「论文级出图」卡片,勾选图类型一键生成
  (单项目:柱状图/表/台阶图;多项目对比:热图/标度关系/火山图),生成后自动打开
  图目录。打包默认收录 matplotlib(`--no-charts` 可关)。
- **结果分析增强**:PDOS 投影 + d 带中心、Bader 电荷解析、可选 ΔG 口径
  (默认 ZPE−TS 对齐文献 / 可切 ASE 严格式含振动内能项)、PDS/U_L 图数一致性。
- **输入正确性**:KPOINTS 改用倒格矢(修六方/hcp slab 欠采样)、项目内 ENCUT 强制
  统一(保吸附能 ΔE 各成员基组一致)、计算类型下拉(修 web 硬编码 slab)。
- **诊断扩展**:磁盘满/IO 错误、裸退出码 137 降级为疑墙钟(可续算,不再困死)、
  ZBRENT 扩展签名。
- **界面**:任务页按吸附能项目组归并折叠(整组进度)、dashboard 首页落地。
- **工程化**:config 脱敏(移除内网 IP)、版本号单一事实来源、依赖分组声明、
  CI 加 lint+覆盖率+打包冒烟。

全套 525 测试通过(2 项在无 OriginLab/POV-Ray 时跳过)。下一步待办见 **[docs/planning/progress-2026-07-16.md](docs/planning/progress-2026-07-16.md)**。

## 2026-07-17 更新:v3.1 Phase A —— DFT 全通量管线地基(引擎层)

依据 **[v3.1 总体方案](docs/planning/v3.1-总体方案.md)**(68 个调研 Agent 合成,验收基准=完整复现一篇 SAC 锂硫论文的 32 图 6 表,设计宪法:科学正确 > 超越对标 > 论文级出图 > 大通量 > 一平台),本轮落地 Phase A 引擎层(+363 测试):

- **campaign 文件式控制面** `vcstudio/campaign/`:任务 DAG(YAML/JSONL 即事实源,零数据库)、
  **completed/validated/accepted 三态分离**(只有 accepted 进图表报告;AI/外部无权直写 accepted)、
  三门禁(提交门=机时硬上限+单点先行;验收门=方法指纹一致性;报告门=图表溯源完备)、
  方法指纹对象(一次 ΔE 比较所有能量必须同指纹→可复现包"一致性证书")、
  append-only 决策/事件账本(写入前拦密钥)、机时预算账本、单机锁(过期只报不抢占)
- **频率生成端 + 虚频质量闸** `generate/freq_builder.py` + `thermo.classify_imaginary`:
  从完成弛豫一键派生 IBRION=5 频率作业(只放开吸附质+近邻,派生改动逐条留痕);
  虚频四象限分类(噪声/坏极小点/合法TS/非法TS),不可用者绝不静默进 ΔG——
  纯 ΔE 升级为**论文级 ΔG** 的最后一块拼图
- **多自旋并跑 + 磁矩守卫** `project/spin_scan.py`:非磁/低自旋/高自旋初猜族并行弛豫取基态
  (自旋误判可致 ΔE 偏差 >0.5 eV),末态磁矩审计(塌零/翻转告警),电子熵超标守卫
- **电子结构分析链**:vasprun **partial DOS 流式解析** + 自旋分辨 **d 带中心**(积分窗口显式)
  + d-p 杂化重叠;**Bader 全链**(AECCAR 合参考→bader 调用→ΔQ+守恒校验,未装则降级说明);
  **差分电荷工作流**(吸附态自动拆 AB/A/B 冻结几何三单点→网格代数→VESTA 可读 CHGDIFF.vasp
  +面平均 Δρ(z));`generate/estatic.py` 从弛豫一键派生 PDOS/Bader/差分电荷静态作业;
  出版级 `pdos_plot`(自旋镜像+εd 标线)与 `charge_profile_plot`
- **SAC 结构生成入口** `generate/{sac_builder,molecules,sites}.py` + `project/references.py`:
  石墨烯超胞+六类配位模板(MN4/MN3/MP1N3/MS1N3/MB1N3/MN4+B)×任意金属矩阵批量建模;
  15 种分子库(S8/Li2Sn/LiS 开壳/DOL/DME/参考小分子)+大盒装箱;SAC 语义位点枚举
  +吸附质双端启发摆放+取向采样(过近拒绝);参考态注册缓存(口径指纹守卫)
  +Eb/Ecoh 稳定性红绿灯
- **通用 CHE/ΔG 台阶引擎** `project/reactions.py` + `freeenergy.free_energy_path`:
  Li-S 硬编码抽象为"物种链+电子数+参比电对"通用引擎,8 组预设
  (Li-S 16e/论文缔合×2/解离/ORR/HER/OER/CO2RR),逐步质量-电荷守恒校验,
  U_eq/U_L/η,多电位台阶(U=0/U_eq/U_L 三线),U_eq 与实验区间对照告警
- **术语统一**(中文文案审查落地):组态→构型(58 处)、任务→作业、需人工统一、
  限制电位步口径说明等,为 i18n 中英双语铺路

933 测试通过(2 跳过)。GUI 接线(campaign 视图/频率与电子结构按钮/候选矩阵页)为 Phase A 收尾项,见总体方案 Phase A/B/C 路线。

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

**Verify the analysis chain offline** (diagnose → gated ΔE → chart → HTML
report, no VASP or cluster): [examples/offline_analysis](examples/offline_analysis/README.md)
— `python examples/offline_analysis/run_demo.py` drives a synthetic completed
job set end-to-end so a reviewer can confirm the analysis half of the pipeline.

**Tests**: `python -m pytest` — 933 tests, 2 skipped (optional OriginLab smoke
behind `VCS_ORIGIN_SMOKE=1`, and a POV-Ray real-render smoke). Parser
cross-checks against ASE run when `ase` is installed (in the `dev` extra).
CI runs the suite on ubuntu/windows × Python 3.10/3.12.

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

- **快速开始**:双击 `dist\VASP Catalyst Studio.exe`(默认 Web,五页:仪表盘/生成/
  吸附能项目/任务/集群;命令行 `vcs gui`,`--legacy` 用旧 tkinter);
  改完代码双击 `重新打包EXE.bat` 重打包
- **离线体验**:[examples/quickstart](examples/quickstart/README.md)
  (石墨烯示例+假赝势库,五分钟跑通全链);
  [examples/offline_analysis](examples/offline_analysis/README.md)
  (合成的已完成作业 → 诊断/ΔE/出图/报告,审稿人无 VASP/集群即可验证分析链路)
- **完整用法**:[使用说明.md](使用说明.md);目录结构:[STRUCTURE.md](STRUCTURE.md)
- **测试**:`python -m pytest`(933 用例,2 项在无 OriginLab/POV-Ray 时跳过)
- **科学约定/发刊工具链**:同上英文节;竞品对比见
  [docs/comparison.md](docs/comparison.md),验证协议见
  [docs/validation.md](docs/validation.md)

```
vcstudio/       核心包(generate/cluster/project/external/gui/shared/cli)
tests/          933 测试   docs/superpowers/specs/  设计文档
config.example.yaml  配置模板(复制为 config.yaml 填写;config.yaml 已 gitignore)
dist/           打包产物 EXE(gitignore)   results/  作业输出(不入 git)
```
