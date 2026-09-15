# 目录结构（VASP Catalyst Studio 4.0）

> 当前目录地图。产品和科学边界见 [README.md](README.md)，操作流程见
> [使用说明.md](使用说明.md)，报告状态合同见
> [docs/report-state-contract.md](docs/report-state-contract.md)，催化领域合同、研究配方与
> dry-run DAG 见 [docs/research-workbench-domain-recipes.md](docs/research-workbench-domain-recipes.md)。

## 顶层目录

| 路径 | 职责 |
|---|---|
| `vcstudio/` | 主 Python 包；计算准备、集群、项目、分析、报告与 GUI 适配都在此处 |
| `tests/` | 跨模块 pytest 行为、合同、跨平台与前端资产测试；不承诺与包目录一一对应 |
| `docs/` | 当前协议/指南、`planning/` 历史快照、`research/` 调研、`archive/` 历史过程材料与 `superpowers/` 设计记录 |
| `paper/` | 软件论文草稿与参考文献；真实 benchmark/作者信息未补齐前不是投稿成稿 |
| `examples/` | 可复现示例、配置和辅助演示脚本 |
| `packaging/` | PyInstaller 打包和 release 辅助逻辑；`windows/build-exe.cmd` 是本地一键入口 |
| `tools/` | 独立维护/转换/审计脚本；`windows/clean-generated.cmd` 只清可再生缓存 |
| `results/` | 默认作业/项目输出根；计算结果通常被 gitignore |
| `dist/` | 本地打包产物（gitignore，可能尚未存在） |
| `tmp/` | 本地临时/QA 文件（gitignore，不得作为发布证据） |

缓存目录（如 `__pycache__/`、`.pytest_cache/`、`.ruff_cache/`）是本地生成物，
不属于源码结构或发布产物。

## 顶层文件

| 文件 | 职责 |
|---|---|
| `README.md` | V4 当前产品总览、架构、能力和诚实边界 |
| `使用说明.md` | 中文操作手册；默认 Web 与 legacy Tk 分开说明 |
| `STRUCTURE.md` | 本文件 |
| `CHANGELOG.md` | 发布历史与当前 Unreleased 条目；历史测试数不代表当前分支 |
| `CITATION.cff` | 引用元数据；`date-released` 只在真实 release 日期已知时填写 |
| `pyproject.toml` | 包元数据、依赖、测试/lint 配置和 CLI 入口 |
| `config.example.yaml` | 配置模板；真实凭据、内网地址和用户配置不入库 |

Windows 辅助入口已归位到职责目录：打包使用
`packaging/windows/build-exe.cmd`，清理可再生缓存使用
`tools/windows/clean-generated.cmd`。根目录不再放置平台专用脚本。

## `vcstudio/` 包

| 子包 | 职责 |
|---|---|
| `generate/` | VASP 四件套、任务目录和派生输入；用户 INCAR 键不被静默覆盖 |
| `engines/` | VASP、CP2K、Gaussian、CASTEP 的引擎合同；能力深度不对称 |
| `cluster/` | SSH/PBS/Slurm、提交、监控、失败分类、有界恢复和本地运行器 |
| `project/` | 项目事实、吸附能/自由能、分析视图、Research Notebook、报告合同、revision/manifest 与偏好 |
| `campaign/` | 文件式 campaign task DAG、模板、三态验证门与审计账本 |
| `molbuild/` | 分子/结构构建和受限转换适配 |
| `external/` | 可选外部软件与渲染适配（Origin、POV-Ray、Multiwfn、VMD、LLM 等） |
| `shared/` | 配置、manifest、i18n、场景、凭据和跨层基础类型 |
| `gui_web/` | 默认 pywebview/Web 工作台：Python API bridge + 离线 HTML/CSS/JS 资产 |
| `gui/` | legacy Tkinter 四页 GUI；保留兼容，不是默认 Web 工作台 |
| `cli/` | `vcs` 命令入口（`gui`、`gen` 等子命令） |

`project/` 内的报告路径以 ReportSpec → ReportSnapshot → ValidationResult →
ReportModel/manifest 为核心；生成工件、科学资格和发布门禁是三个相关但不等价
的事实。`external/` 不是报告或科学状态的权威存储位置。

跨项目研究浏览器由 `project/research_explorer.py` 生成可重建内存索引、分页查询、服务端
聚合和独立的 live provenance；`project/research_views.py` 只保存受
`authority_id + revision` CAS 保护的 filters/sort/axes。Web 组件位于
`gui_web/assets/research-explorer.js` 与 `.css`，同时挂载在 Home/Project，不改变 32 个语义路由。
这些文件都不是项目、作业、验证或冻结报告的事实源。

## 默认 Web 信息架构

- 7 个一级区域：Home、Project、Prepare、Run、Analyze、Publish、Environment。
- 32 个语义路由，由 `gui_web/assets/workspace.js` 维护。
- 11 个普通 workspace 物理页面，加 1 个 AI Assistant 抽屉。
- 当前项目、模式、引擎、任务、阶段和同步状态由工作区上下文条统一表达。

固定“几页”的旧说法只适用于历史快照；legacy Tk 仍是 Generate、Adsorption
project、Jobs、Cluster 四个 tab。

## 入口

- **默认 Web GUI**：`vcs gui`、`python -m vcstudio.gui_web`，或默认 EXE。
- **legacy Tk GUI**：`vcs-gui` 或 `python -m vcstudio.gui`；打包时使用
  `--legacy`。
- **CLI 示例**：`vcs gen --poscar … --incar … -o results/job1/`。
- **测试**：`python -m pytest`；当前通过/跳过数以最新命令输出与 CI 为准。

CI 矩阵覆盖 Ubuntu/Windows × Python 3.10/3.11/3.12。外部计算软件、许可、
POTCAR/BASIS/POTENTIAL 数据、WebView2 和可选渲染器由用户或系统提供，不能从
单文件 EXE 的存在推断这些依赖已经具备。
