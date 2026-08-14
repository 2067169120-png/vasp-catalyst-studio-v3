# VASP Catalyst Studio 4.0

VASP Catalyst Studio 4.0 是一个以项目为中心、证据驱动、可配置的材料与催化研究工作台。它把结构与输入准备、作业提交、状态监控、失败诊断、有界恢复、分析、出图和报告组织在同一个桌面应用中，同时把“文件生成成功”和“科学结论可发布”严格分开。

The deterministic core runs locally and over user-configured SSH connections. External LLM use is optional, disabled by default, and outside the numerical decision chain.

> 用户文档：[中文使用说明](使用说明.md) · [English user guide](docs/user-guide-en.md)
>
> 工程地图：[STRUCTURE.md](STRUCTURE.md) · 历史发布记录：[CHANGELOG.md](CHANGELOG.md)
>
> 科学边界：[验证说明](docs/validation.md) · [失败分类](docs/failure-taxonomy.md) · [报告状态与证据合同](docs/report-state-contract.md)

## 当前工作台

默认 Web 工作台有 7 个一级区域、32 个语义路由。实现上复用 11 个普通物理页面，并将 AI Assistant 作为 drawer 打开；“路由数”不等于“HTML 页面数”。

| 一级区域 | 当前用途 |
|---|---|
| Home | 全局概览、项目管线、活动与阻断项 |
| Project | 项目概览、成员、工作流、运行记录与项目活动 |
| Prepare | 结构、输入、批量准备、任务模板与 preflight |
| Run | 本地/远程作业台账、提交、监控、续算、下载 |
| Analyze | 吸附、热力学、电子结构、电荷、比较与自定义分析 |
| Publish | 图表、报告、SI、草稿包、revision 与导出 |
| Environment | 集群、本机运行器、依赖、数据路径、模板与设置 |

全局项目上下文贯穿 Prepare、Run、Analyze 和 Publish。深链接、未保存状态与当前工作模式均显式处理；界面切换不会把显示层状态写成科学事实。

### 已实现的 Web 工作流与边界

当前 Web 工作台已实现下列用户流程，并为后端合同或生产 JavaScript 状态机提供了聚焦自动化测试。这里的“已实现”只表示相应软件合同已落地，不表示当前工作树已经完成 root 全量回归、重建最终 EXE 或通过远端 CI，更不表示 diagnostic/blocked 数据通过了科学验证。

| 区域 | 已实现能力 | 必须保留的边界 |
|---|---|---|
| Project | Clone、Move、Adopt Copy；操作前预检、显式确认、注册表/台账更新和失败回滚 | Clone 铸造新项目身份；Move 保留原身份；Adopt Copy 默认重新铸造身份。目标已存在时不覆盖；部分回滚会留下明确恢复证据，不报告假成功 |
| Run / Jobs | Selection Tray、包含筛选后隐藏项的 Batch Review、统一 Operation Queue、提交/续算/取消幂等键 | 一次只运行一个互斥批操作；重复请求不会再次执行远程副作用；资源预测只读且不授权提交 |
| Analyze | capability cards；Task Results；DOS/PDOS、Bands、功函数、Bader、差分电荷、ELF 分布摘要；Property calculators | 只显示服务端解析器给出的数值、来源 hash 和分母。ELF 解析严格校验 ELFCAR 主网格并仅给分布统计，不据此宣称成键、盆、临界点或拓扑结论 |
| Analyze / Governance | 下一步计算建议与确认后的只读 draft intent | 只从绑定当前数据指纹、经人工科学复核的 ValidationResult 和冻结分析信号派生；不含命令，不授权或自动提交作业 |
| Home / Project · Research Explorer | 跨项目元素/化学式、facet、adsorbate、任务、状态、方法、证据、能量/能垒筛选；表格、histogram、scatter、元素周期表聚合和 live provenance | 索引只读派生且可重建，不是事实源；默认只保留一个方法兼容 cohort，散点不静默混方法；partial/stale/unavailable 时扣留科学 DTO；保存视图只写 filters/sort/axes，并受 authority_id + revision CAS 约束 |
| Publish | revision scientific diff、Evidence/Claim Graph、确定性 SI capsule | 每次从权威历史重新校验冻结 revision；缺失证据边显式标记。capsule 去除密钥/绝对路径、不覆盖已有文件；生成 diagnostic capsule 不等于科学 final |
| Home / Resume Center | 恢复当前会话的设置、导入流程和报告配置草稿入口 | 恢复的是有界草稿引用和权威持久状态，不把浏览器草稿提升为项目事实或已发布报告 |
| Environment / Jobs | 实验室策略模板和基于台账/manifest 的资源预测 | 都是建议；策略必须显式确认且不改写旧作业，预测缺证据时保持 unavailable/低置信度，二者都不能旁路提交确认和幂等门禁 |

这些新工作台能力属于默认 Web 界面。`vcs gui --legacy` / `vcs-gui` 的 Tk 四页界面仅用于兼容，不提供当前 Web shell、Project lifecycle、Selection Tray、Resume Center、report insights 或治理建议合同。

## 启动

Windows 打包版可直接启动 `dist\VASP Catalyst Studio.exe`。从源码启动默认 Web 工作台：

```powershell
pip install -e ".[dev]"
vcs gui
# 等价的模块入口
python -m vcstudio.gui_web
```

入口边界：

- `vcs gui`：当前默认 Web 工作台；
- `python -m vcstudio.gui_web`：当前默认 Web 工作台的直接模块入口；
- `vcs gui --legacy`：legacy Tkinter；
- `vcs-gui`：仍指向 legacy Tkinter 四页界面，不是默认 Web 工作台。

最小 CLI 输入生成示例：

```powershell
vcs gen --poscar POSCAR --incar my.incar --calc-type slab -o results/job1
```

POTCAR/PAW 数据受许可证约束，本仓库不分发。无集群、无 VASP 时可使用 [离线快速示例](examples/quickstart/README.md) 和 [合成分析链示例](examples/offline_analysis/README.md) 验证结构与软件流程；合成示例不构成真实科学验证。

## 证据与状态模型

每个作业目录中的 `job.yaml` 是作业事实源。九态生命周期用于记录 CREATED、SUBMITTED、RUNNING 等运行过程；终态诊断是另一条轴，不能混为一谈。

当前诊断实现包含：

- 18 个 job-classification outcomes；
- 16 个 VASP internal-error signatures；
- 归并到 4 个处理状态：`DONE`、`UNCONVERGED`、`FAILED`、`NEEDS_HUMAN`。

诊断结合调度器终态、输出完整性、收敛证据和日志签名。只有明确可恢复的类别才能从已验证的 CONTCAR 续算，INCAR 保持冻结，最多 3 轮；其余情况停止并交给人工判断。

Campaign 控制面进一步分离 `completed → validated → accepted`。完成计算不等于验证通过，验证通过也不等于已被接受进入最终比较、图表或报告。

## 引擎与分析能力

VASP 是主引擎，覆盖完整的输入、提交、监控、诊断、派生、分析和报告链。CP2K、Gaussian 与 CASTEP 是有边界的文件级适配器，只对各自能力清单内的任务提供输入生成、登记、提交、状态处理和引擎原生解析。

这些适配器不承诺：

- 跨引擎方法或绝对能量等价；
- VASP ENCUT 与 CP2K/CASTEP 截断量的直接换算；
- 平面波/PAW 与 Gaussian 基组能量的直接比较；
- 缺少方法指纹、参考态或单位证据时仍给出可信比较。

Multiwfn 的当前注册表包含 14 个分析入口；UI 菜单以该注册表为单一事实源。VMD、POV-Ray、Origin、RDKit、Multiwfn 等外部工具均通过适配器调用，缺失时必须明确降级或阻止相应功能。

## 数值与自由能边界

- `E_ads = E(slab+ads) − E(slab) − E(reference)`；负值表示该参考口径下吸附更有利。
- `E0` 表示电子能。缺少校正证据时，系统不得把 E0 描述成完整自由能。
- ZPE、热校正和熵项只有在显式提供、通过适用性检查并写入证据模型时才进入 ΔG；系统不会猜测缺失项。
- 报告和图表必须注明采用的是 E0、E0+ZPE，还是包含热/熵校正的口径。
- 不同结构、参考态、计算方法、赝势、k 点、单位或引擎之间的比较需要相应一致性证据。

真实 MAE、真实文献复现完成度和投稿/接收状态目前保持 `UNKNOWN`，不得从自动化测试、合成示例或文件生成成功推断。验证计划见 [docs/validation.md](docs/validation.md)。

## 跨项目 Research Explorer

Home 与 Project 复用同一个 Research Explorer 组件，不新增第 33 个语义路由。后端从项目注册表、
`project.yaml`、每个成员的 `job.yaml`、manifest 结果与 validation 记录构建内存索引；索引可随时
重建，不会回写上述文件，也不会替代它们成为第二事实源。

- 支持元素/化学式、facet、adsorbate、任务、状态、方法指纹、证据等级以及能量/能垒范围筛选；
- 服务端完成稳定排序、分页 cursor、单位/缺失值/样本分母、histogram bins、scatter 坐标和
  periodic-table 聚合，浏览器只渲染 DTO；
- 默认只选择一个方法兼容 cohort。用户显式关闭兼容过滤后可以查看混合表格，但多方法散点会
  `blocked_mixed_or_unverified_methods`，不会把不同或未验证的方法静默画在一起；
- 索引 freshness、注册项目分母、成功/失败来源数均公开；`partial`、`stale`、`unavailable` 时
  fail closed，扣留科学表格与聚合，直到成功重建；
- drill-down 只使用 opaque project/job/source ID。live provenance 将 input、job、repair/resume、
  parser、analysis、validation、report 分成 data/logical 两层，并标注
  `observed/imported/inferred/missing`；Publish 的 frozen Evidence Graph 仍是单独的 revision 工具；
- 保存的 research view 仅含有界名称、filters、sort、axes，通过 `authority_id + revision` CAS
  更新；不保存路径、密钥、结果行或任意正文。

## 报告合同

当前报告链为：

```text
ReportSpec → ReportSnapshot → ValidationResult → revision + manifest
```

语义 SHA-256 把规格、冻结快照、验证结果与最终可见模型绑定。报告采用两条独立状态轴：

1. **Artifact axis**：格式是否可生成、文件是否写入、hash/size 是否核对、manifest 是否登记；
2. **Scientific axis**：证据级别、claim ceiling、publication gate 与报告用途是否满足。

文件生成成功不证明科学结论为 final。输入在渲染期间变化时，产物可以保留为未登记文件，但不能写 ready marker 或冒充当前 revision。

Publish → Versions 还提供三种只读/导出工具：

- **Scientific diff**：选择两个权威 history revision，分别重新校验 bundle，再比较 ReportSpec scope、输入/快照 hash、validation 状态与 checks、科学资格、模型数值、图表以及文件/manifest hash；不会用日期差异冒充科学差异；
- **Evidence/Claim Graph**：只从冻结 spec、snapshot、validation、model 和 claim 记录构建 conclusion/table/figure/check/source/job/file-hash 关系；缺边明确显示为 missing，不读取可变 live 文件，也不公开本地路径；
- **SI capsule**：通过服务端目录选择生成确定性 ZIP，包含规范化合同、输入 manifest、模型/图表元数据、Methods、BibTeX、环境/版本、validation 记录、capsule manifest 和 `SHA256SUMS`；排除秘密、绝对路径、缓存和可变 live 文件，且绝不覆盖同名文件。

格式边界：

- HTML：具备语义结构、语言和图像描述基础；可访问性为 conditional，仍需浏览器、屏幕阅读器和人工复核；
- DOCX：具备语言、Heading/Caption、重复表头和图片描述基础；可访问性为 conditional，仍需 Word Accessibility Checker 与人工复核；
- ReportLab PDF：可视、可搜索，但未标记，无结构树和图片替代文本；始终 `tagged=false`、`pdf_ua=false`，不能称为 PDF/UA 或 HTML/DOCX 的无障碍替代品。

详细合同见 [docs/report-state-contract.md](docs/report-state-contract.md)。

## 网络、隐私与可选 AI

| 能力 | 默认边界 |
|---|---|
| 本地生成、解析、门禁、hash、报告模型 | 确定性、本地运行，不调用 LLM |
| SSH 提交与监控 | 仅连接用户明确配置的集群；known_hosts/指纹门控 |
| External LLM | 默认关闭；只有用户显式开启项目数据外发并配置凭据后才可调用 |
| DECIMER | 默认 Windows 单文件 EXE 不包含；本地没有缓存模型时，首次模型准备可能联网 |
| 密钥 | 集群密码和 LLM key 使用系统凭据库，不写入普通配置文件 |

LLM 可用于结果解读、论文方法段抽取和对话辅助。它不能修改作业科学状态、绕过 `completed/validated/accepted`、写入 accepted、替代 ValidationResult、提交或取消 HPC 作业。对话中的“停止回复”只停止当前模型响应。

## 测试与当前证据

```powershell
python -m pytest
```

2026-08-13 的本地最终门禁已实际运行：免缓存完整 `python -m pytest` 为 **3658 passed、5 skipped**，330.80 秒内出现 **15 条 ASE/NumPy 上游弃用警告**；全仓 Ruff、actionlint 与 23 个第一方 JavaScript 文件的 `node --check` 均通过。PyInstaller `--full` 单文件构建成功，最终 EXE 为 **113,586,017 bytes（108.32 MiB）**，SHA-256 为 `bcadc9046685c62cf1a9157d0ceba49b131190184dbe30073ce4189ec6817e2d`。

该最终冻结 EXE 内的 `full` 和 `journey` healthcheck 都以退出码 0 完成，并各自报告 `ok=true`、`frozen=true`。`journey` 完成 10/10 个阶段，网络尝试为 0、集群操作为 0，且服务重建后的重启持久化检查通过。上述新增工作流也保留聚焦 Python 合同测试和/或真实 Node 生产 IIFE 回归；冻结 journey 保持 `blocked`/`diagnostic` 科学状态的诚实边界。

这些是本地软件门禁，不替代 GitHub-hosted 远端 CI；推送后的具体提交仍必须通过远端矩阵。`v4.0.0` 标签尚未创建，CHANGELOG 仍保持 Unreleased；5 个 skip 不等于对应功能通过。也未执行真实远程集群作业或真实科学/实验验证，不能从这些自动化检查、离线 journey 或 EXE 产物推断科学有效性或发布就绪。

CI 矩阵覆盖 Ubuntu/Windows × Python 3.10、3.11、3.12。Windows package-smoke 会构建真实 EXE，并要求最终二进制的 `full` 与 `journey` 两个 profile 都成功后才上传 EXE 和 JSON 健康检查证据。

## 仓库地图

```text
vcstudio/
  generate/       结构、VASP 输入、TaskCatalog 与派生作业
  cluster/        SSH、调度器、台账、诊断与有界恢复
  project/        项目分析、证据模型、图表与报告
  campaign/       DAG、三态、门禁、指纹、账本与预算
  engines/        VASP 主引擎与受限 CP2K/Gaussian/CASTEP 适配器
  external/       Multiwfn、VMD、Origin、POV-Ray 等适配器
  molbuild/       分子输入、RDKit/DECIMER 边界
  gui_web/        当前默认 Web 工作台
  gui/            legacy Tkinter 四页界面
  shared/         manifest、配置、凭据与 i18n
tests/             自动化测试
docs/              合同、验证、设计与历史记录
```

## 历史说明

[2026-07-16 progress](docs/planning/progress-2026-07-16.md) 是历史快照，不是当前 4.0 能力表或测试基线。README 不再重复逐版本日志；历史功能、当时的测试数字和发布日期保留在 [CHANGELOG.md](CHANGELOG.md)，不应机械替换为当前数字。

## 贡献与引用

参见 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [CITATION.cff](CITATION.cff)。许可证见 [MIT LICENSE](LICENSE)。
