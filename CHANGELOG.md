# 更新日志 / Changelog

## [Unreleased] — V4.0.0

V4.0.0 尚未发布，`v4.0.0` 标签也尚未创建。本节汇总当前 Unreleased 工作树中已实现的工作。2026-08-13 已完成本地最终门禁：免缓存完整 pytest 为 **3658 passed、5 skipped**（330.80 秒；15 条 ASE/NumPy 上游弃用警告），全仓 Ruff、actionlint 和 23 个第一方 JavaScript 文件的 `node --check` 均通过；PyInstaller `--full` 单文件构建成功。最终 EXE 为 **113,586,017 bytes（108.32 MiB）**，SHA-256 为 `bcadc9046685c62cf1a9157d0ceba49b131190184dbe30073ce4189ec6817e2d`。

V4.0.0 is not released, and the `v4.0.0` tag has not been created. This section records implemented work in the current Unreleased tree. The final local gates ran on 2026-08-13: cache-free full pytest completed with **3658 passed and 5 skipped** (330.80 s; 15 upstream ASE/NumPy deprecation warnings); full-repository Ruff, actionlint, and `node --check` for 23 first-party JavaScript files passed; and the PyInstaller `--full` one-file build succeeded. The final EXE is **113,586,017 bytes (108.32 MiB)**, SHA-256 `bcadc9046685c62cf1a9157d0ceba49b131190184dbe30073ce4189ec6817e2d`.

2026-08-15 在隔离 worktree 对下述严格指纹/复用变更重新执行了免缓存完整 pytest：
**3693 passed、5 skipped**（226.23 秒；15 条 ASE/NumPy 上游弃用警告）；全仓 Ruff、
25 个第一方 JavaScript 文件的 `node --check`、变更 Python 文件的 `py_compile` 和
`git diff --check` 通过。本轮没有重建 PyInstaller EXE 或重跑 actionlint，因此上面的
2026-08-13 冻结产物哈希仍是历史产物证据，不能被本轮软件测试自动升级。

On 2026-08-15 the strict-fingerprint/reuse change below was revalidated in an isolated worktree:
cache-free full pytest completed with **3693 passed and 5 skipped** (226.23 s; 15 upstream ASE/NumPy
deprecation warnings); full-repository Ruff, `node --check` for 25 first-party JavaScript files,
`py_compile` for the changed Python files, and `git diff --check` passed. PyInstaller and actionlint
were not rerun, so the 2026-08-13 frozen binary hash above remains historical artifact evidence and
is not upgraded by this software-only validation.

2026-08-20 在隔离 worktree 完成严格复用 P1 加固后，全量 pytest 为 **3735 passed、5 skipped**
（199.15 秒；15 条 ASE/NumPy 上游弃用警告）；全仓 Ruff、25 个第一方 JavaScript 文件的
`node --check`、25 个变更 Python 文件的 `py_compile` 与 `git diff --check` 通过。未连接真实
集群执行 VASP，也未重建发行包，因此这些门只证明软件合同，不升级科学结论或冻结发行资格。

After the P1 strict-reuse hardening on 2026-08-20, the isolated worktree passed the full test suite:
**3735 passed and 5 skipped** in 199.15 s, with 15 upstream ASE/NumPy deprecation warnings. Full-repository
Ruff, `node --check` for 25 first-party JavaScript files, `py_compile` for 25 changed Python files, and
`git diff --check` also passed. No real-cluster VASP run or release rebuild was performed, so these gates
prove software contracts only and do not upgrade scientific or frozen-release qualification.

2026-08-21 继续闭合严格复用复核发现的 4 组 P1：source fingerprint 每次从同一权威快照重建；
输出哈希、解析和 VASP 版本使用同一 no-follow 文件实体；复用事务以持久目录 handle/file-id 锚定，
根目录漂移时恢复到 prepared；EOS、spin scan 等派生 builder 在最终输入落定后重签 recipe/closure，
且运行环境只允许服务端可信 profile 绑定。聚焦回归为 **182 passed、1 skipped**，builder/manifest/
submit 接缝为 **341 passed**；免缓存全量 pytest 为 **3748 passed、6 skipped**（332.37 秒；15 条
ASE/NumPy 上游弃用警告）。全仓 Ruff、全部第一方 JavaScript 的 `node --check`、变更 Python 文件的
`py_compile` 与 `git diff --check` 通过。未运行真实集群 VASP、未重建发行包，也未执行远端 CI。

On 2026-08-21 four additional strict-reuse P1 groups were closed: source fingerprints are rebuilt from
the same authoritative snapshot; output hashing, parsing, and VASP-version evidence share one no-follow
file entity; reuse transactions are anchored to persistent directory handles/file identities and roll back
to prepared on root drift; and derived builders such as EOS and spin scan rebind recipe/input closure only
after final input bytes exist, while execution environments remain server-bound from trusted profiles.
Focused tests passed with **182 passed and 1 skipped**, builder/manifest/submission seams passed with
**341 passed**, and cache-free full pytest completed with **3748 passed and 6 skipped** in 332.37 s, with
15 upstream ASE/NumPy deprecation warnings. Full-repository Ruff, all first-party JavaScript `node --check`,
changed-file `py_compile`, and `git diff --check` passed. No real-cluster VASP run, release rebuild, or remote
CI was performed.

2026-08-21 完成本轮研究工作台整合后的最终本地门禁：免缓存全量 pytest 为
**5191 passed、12 skipped**（670.53 秒；15 条 ASE/NumPy 上游弃用警告）；全仓 Ruff、
30 个第一方 JavaScript 文件的 `node --check`、`pip check` 与 `git diff --check` 通过。
本机未安装 actionlint，因此工作流静态检查仍由推送后的远端 CI 执行。`vcstudio-4.0.0`
wheel 构建成功（9,877,749 bytes，SHA-256
`44b769e7f7f3aeaaa10421e84b54d3592e45d7bae262ec3bf6fe9baf64686109`），并包含 52 个 Web
资源与 2 个 locale。基于目录整理提交 `9bb6a0d` 的 PyInstaller `--full` 单文件 EXE 为
**115,183,481 bytes（109.85 MiB）**，SHA-256
`2bf048797fac3299732730e7699e31424d169039668729d1acea1f81f471b34b`；冻结进程内 `full`
检查 6/6、`journey` 10/10 阶段均以退出码 0 完成，且网络尝试与集群操作均为 0。

On 2026-08-21 the integrated research-workbench tree completed its final local gates: cache-free full
pytest passed with **5191 passed and 12 skipped** in 670.53 seconds, with 15 upstream ASE/NumPy
deprecation warnings; full-repository Ruff, `node --check` for 30 first-party JavaScript files,
`pip check`, and `git diff --check` passed. Actionlint was not installed locally, so workflow linting
remains a hosted-CI gate after push. The `vcstudio-4.0.0` wheel built successfully (9,877,749 bytes,
SHA-256 `44b769e7f7f3aeaaa10421e84b54d3592e45d7bae262ec3bf6fe9baf64686109`) with 52 Web assets
and both locales. The PyInstaller `--full` one-file EXE built from repository-layout commit `9bb6a0d` is
**115,183,481 bytes (109.85 MiB)** with SHA-256
`2bf048797fac3299732730e7699e31424d169039668729d1acea1f81f471b34b`; inside that frozen
process, the 6/6 `full` checks and all 10/10 `journey` phases exited 0 with zero network attempts and
zero cluster operations.

同日完成仓库布局收口：原根目录 `_待处理归档/` 迁入 `docs/archive/legacy-process/`，Windows
打包与清理入口分别归入 `packaging/windows/` 和 `tools/windows/`；根目录只保留标准项目入口。
新增布局合同测试，防止平台脚本重新散落到根目录，并锁定清理入口不得删除 `dist/` 发布产物或
`results/` 研究输出。本地生成目录统一由 `.gitignore` 管理，不进入版本库。

The same-day repository-layout cleanup moved legacy process material under
`docs/archive/legacy-process/` and placed Windows packaging/cleanup entry points under their owning
`packaging/windows/` and `tools/windows/` directories. A repository-layout contract now prevents
platform scripts from drifting back to the root and ensures generated cleanup preserves both `dist/`
artifacts and `results/` research outputs.

### 报告、状态与项目工作区（Phase A–B）
- 建立报告与状态合同，明确稳定项目身份、服务端快照、门禁结论、产物状态和 revision
  之间的绑定关系；未通过门禁或缺少证据时保留诊断状态，不冒充最终发布结果。
- 引入项目工作区壳，统一项目上下文、路由、选择恢复和跨页面入口，使分析与报告围绕同一
  项目身份工作，而不是由各页面各自维护易漂移的副本。
- 新增严格、版本化的催化领域 DTO 与五条内置研究配方；Prepare → Templates 可用 typed evidence
  和显式参数覆盖生成零副作用 DAG 预览及 semantic hash。配方 ready 不代表科学 validated/accepted，
  `job.yaml` 仍是作业事实源。Versioned catalysis DTOs and five built-in research recipes now provide
  typed-evidence, parameter-source-aware, side-effect-free DAG previews without granting execution authority.
- 催化 DTO 进一步分离 schema version 与不可变 object revision，并新增独立 create-only/CAS envelope
  store；ElementaryStep v3 以精确有理系数、phase、charge、逐类型 site stoichiometry 和非空 TS participant
  tuple 表达反应，并通过 authoritative resolver 分别检查 reactants/TS/products 的精确守恒。配方 UI 以 ID+version 复合身份隔离逆序响应，
  API 输入拒绝与输出脱敏统一使用全项目凭据分类器。

### Revisioned 工作台与适配器（Phase C–D）
- 新增 revisioned report workbench，将配置草稿、绑定预览、发布 revision、产物清单及
  HTML / DOCX / PDF 格式能力纳入同一流程，并保留诊断报告与最终报告的边界。
- 新增 analysis workbench、preferences 与 batch adapters；旧项目页、批次入口和分析入口
  通过适配器进入统一合同，不旁路现有科学门禁、格式能力检查或项目身份约束。
- 新增项目级本地优先 Research Notebook 与 Human Review Ledger：journal 采用项目身份绑定的外部
  head/sequence anchor、跨进程锁、revision+head+project CAS、完整状态机重放、链式 digest、
  append-only supersedes/tombstone；路径与 blob no-follow/reparse fail-closed。关联的 project/job/source/report revision
  在读取时重新校验并显示 current/stale/missing。正文和附件不进入 workspace localStorage；Resume
  Center 只保存 opaque marker。人工 reviewer attribution 是本地自声明，不是认证或密码学签名，
  且不会提升 ValidationResult、claims、final、accepted 或 `human_scientific_reviewed`。
  Project-local Research Notebook and Human Review Ledger records are append-only, revision/head/project CAS-bound,
  externally head-anchored, digest-chained, evidence/blob-revalidated, and excluded from workspace body storage.
  Human attribution remains self-asserted and
  cannot bypass or elevate the existing scientific/report gates.
- Publish → Export 新增本地 DOI-ready `vcs-archive`：从重新校验的 report revision 生成
  path-free dry-run、许可/风险/排除清单和 submission-readiness gaps，确认后原子写出绑定
  revision/manifest/plan hash 的确定性 ZIP；不会上传、申请 DOI 或把 archive integrity 冒充科学发布。

### 密度、键盘、双语与可访问性边界（Phase E）
- 提供舒展、标准、紧凑三档界面密度，补齐键盘导航、焦点可见性、动态 ARIA / title /
  placeholder 和空态；语言切换只重绘界面，不改变科学配置。
- 收口中英双语动态文案，英文模式不以中文作为静默 fallback；公式、文件名、原始科研数据
  和后端原始载荷仍按数据合同保留。
- 分离“可生成”与“可访问性”能力：HTML、DOCX、PDF 分别记录视觉、可搜索、语义结构、
  文档语言、元数据、图片替代文本、tagged / PDF-UA 和人工复核要求；未验证的能力不作过度声明。

### 项目身份、批处理与恢复 / Project identity, batch operations, and recovery
- Project 新增 Clone、Move 与 Adopt Copy 的服务端预检/确认流程。Clone 铸造新身份，Move 保留身份，
  Adopt Copy 默认重铸身份；只重定位项目内 locator，不覆盖已存在目标。注册表/台账失败触发回滚，
  无法完整回滚时保留 partial-rollback 恢复证据，不报告假成功。
  Project now provides server-preflighted, explicitly confirmed Clone, Move, and Adopt Copy flows. Clone
  mints a new identity, Move preserves identity, and Adopt Copy remints by default. Existing destinations
  are never overwritten; failed registry/ledger updates roll back or retain explicit partial-recovery evidence.
- Jobs 新增 Selection Tray 与 Batch Review，完整显示跨筛选选择和隐藏项；提交、续算、取消采用服务端
  幂等键，页面互斥锁阻止重复并发副作用。Jobs、Project、Analysis 与 Publish 的长操作汇入统一
  Operation Queue。Home 新增 Resume Center，只恢复有界草稿引用并重新读取权威状态。
  Jobs now includes a Selection Tray and Batch Review, including filter-hidden selections. Server
  idempotency and a UI mutex prevent duplicate remote effects. Long Project/Jobs/Analysis/Publish operations
  share one Operation Queue, while Resume Center restores bounded draft references without promoting them.
- Jobs 新增版本化 strict scientific fingerprint 和提交前重复计算提示。只有规范化结构、完整
  INCAR/KPOINTS、POTCAR 内容身份、Method Recipe 语义哈希、任务/版本和 VASP 构建环境均完整时，
  才可能显示 exact match；near match 只列差异且不声明等价。索引从权威 manifest/文件重建、容量
  有界且不是事实源。复用默认关闭；显式引用会新建 provenance/decision，重验并物化 hash-bound
  结果，支持 prepared→succeeded 崩溃恢复，且从不继承 accepted/final。旧 Recipe 缺失作业保持
  `incomplete / explicit_legacy`。Jobs 与 Project 一键提交共用联网前门禁和幂等操作标识。
  Jobs now provides a versioned strict scientific fingerprint and pre-submission duplicate advisory. Exact
  matches require complete canonical inputs, recipe semantics, task/version, and VASP build evidence; near
  matches report differences only. The bounded index is rebuildable and non-authoritative. Reuse remains
  opt-in, creates fresh provenance and a durable decision, revalidates hash-bound results, recovers from a
  prepared transaction, and never inherits accepted/final qualification. Legacy recipe-less jobs remain
  explicitly incomplete, and both Jobs and Project submission apply the same pre-network guard.
- 严格复用门进一步绑定 VASP 实际输入闭包（包括 restart/VDW/ICONST/ML/KPOINTS_OPT 与 NEB 全 image）、
  当前输出重新解析的 task-aware 收敛证据、registry/manifest 双重项目身份和物化事务 CAS。浏览器提示继续
  使用有界非权威索引，而提交放行只接受完整权威扫描；任何超限、篡改、未知身份或显式失败证据均失败闭合。
  The strict reuse gate now binds VASP's actual input closure, task-aware convergence reparsed from current
  hash-bound outputs, cross-checked project identity, and materialization CAS. Browser advice remains bounded
  and non-authoritative; submission relies on a complete authoritative scan and fails closed on limits,
  tampering, unknown identity, or explicit contradictory result evidence.

### 分析、资源与治理 / Analysis, resources, and governance
- Home 与 Project 新增共享的跨项目 Research Explorer：从 registry/project/job/manifest/validation
  派生可重建只读索引，支持方法兼容默认过滤、稳定分页、服务端表格/histogram/scatter/元素周期表
  DTO、opaque ID drill-down 和 data/logical live provenance。保存视图只含 filters/sort/axes，并以
  authority_id + revision CAS 更新；partial/stale/unavailable 时 fail closed，且 frozen report graph
  保持独立。没有引入数据库、远程副作用或 accepted/publication gate 旁路。
- Analysis capability cards 以服务端证据区分 available、missing prerequisite、mode mismatch、
  not implemented 与 unavailable。Task Results、DOS/PDOS、Bands/带隙、功函数、Bader、差分电荷和
  Property calculators 使用真实 parser、来源 hash、opaque ID 与分母；浏览器不复算数值。
  Capability cards expose server-owned availability and evidence. Task Results, DOS/PDOS, bands/gaps,
  work function, Bader, charge-density difference, and property calculators use real parsers, opaque sources,
  hashes, and denominators rather than browser-computed values.
- **ELF 分布摘要解析已接入**：严格校验 ELFCAR 主网格、有限数和物理范围，并输出确定性统计、
  分位点、直方图与来源哈希；它不宣称成键、盆、临界点或拓扑结论。The read-only ELF parser
  reports a validated distribution summary only, never a bonding or topology conclusion.
- 新增只读资源预测和实验室建议策略。预测缺历史/输入时保持低置信度或 unavailable，不授权提交；
  策略必须显式确认、带 revision/hash，且不改写旧作业。治理后的下一步计算建议只从绑定当前数据指纹、
  经人工科学复核且允许 final 的 ValidationResult 生成；确认只产生不含命令、绝不自动提交的 draft intent。
  Read-only resource forecasts and laboratory recommendation policies never authorize submission or rewrite
  existing jobs. Governed next-calculation recommendations require a human-reviewed, final-allowing validation
  bound to the current fingerprint; confirmation creates a non-executable draft intent only.

### 报告洞察与可复现性 / Report insights and reproducibility
- Publish → Versions 新增 scientific diff：从权威 history 选择两个 revision，分别重新校验 bundle，
  比较 scope、输入/快照 hash、validation、科学资格、模型/表格数值、图表及 manifest/file hash，
  不以日期差异代替科学差异。Scientific diff revalidates both authoritative revisions and compares frozen
  scientific content and hashes, never dates alone.
- 新增 Evidence/Claim Graph，只从冻结 model/spec/snapshot/validation/claim 记录建立 conclusion/table/
  figure/check/source/job/file-hash 关系，缺边显式标记且不泄漏本地路径。
  Evidence/Claim Graph derives only from frozen records, marks missing links, and omits local paths.
- 新增确定性 SI capsule：包含规范化合同、输入 manifest、模型/图表元数据、Methods、BibTeX、环境/版本、
  validation、capsule manifest 与 `SHA256SUMS`；排除密钥、绝对路径、缓存和 mutable live files，
  使用有 TTL/上限的单次目录 token，并以 no-overwrite 写入。Diagnostic/blocked capsule 仍不是 scientific final。
  Deterministic SI capsules are redacted, self-checksummed, destination-token bounded, and no-overwrite.
  Export success does not upgrade a diagnostic/blocked revision to scientific final.

### 冻结发行门 / Frozen release gate
- Windows package-smoke 现配置为构建真实最终 EXE，并在冻结进程内强制运行 `full` 与 `journey`。
  Journey 隔离 HOME/config、禁用网络、运行真实 Api/ReportService、离线作业、Analysis preview 和
  diagnostic HTML 报告，然后重建服务并验证项目/作业/report history/status 持久化。
  Windows package-smoke is configured to run both `full` and `journey` inside the real frozen executable.
  The journey is offline and isolated, exercises the real bridge and diagnostic report path, then verifies
  persistence after service reconstruction.

### 验证
- 上述新增能力已执行聚焦 Python 合同测试和/或真实 Node 生产 IIFE 回归；最终冻结 EXE 中的 `full` 与
  `journey` 都以退出码 0 完成，并各自报告 `ok=true`、`frozen=true`。`journey` 完成 10/10 阶段、网络尝试
  为 0、集群操作为 0，且服务重建后的重启持久化检查通过；它仍保持 Analysis `unverified/blocked`、report
  `diagnostic/blocked`，没有伪造科学验证。
  The features above have focused Python and/or executable-Node production-script coverage. In the final frozen EXE,
  both `full` and `journey` exited 0 and reported `ok=true` and `frozen=true`; journey completed 10/10 phases with
  0 network attempts, 0 cluster operations, and passed restart persistence after service reconstruction, while
  preserving honest Analysis `unverified/blocked` and report `diagnostic/blocked` scientific states.
- 推送后的具体提交仍须通过 GitHub-hosted 远端 CI，不能由本地门禁替代；5 个 skip 不等于功能通过。未执行真实远程集群
  作业或真实科学/实验验证，因此 Unreleased 不能据此描述为科学有效或正式 release-ready。
  The exact pushed commit must still pass GitHub-hosted CI, which is not replaced by local gates; the 5 skips are
  not functionality passes. No real remote-cluster work or scientific/experimental validation was performed, so
  Unreleased must not be described as scientifically validated or formally release-ready.

## v3.3.0 — 2026-07-18 · 对齐 starpivot 体验四缺口:实时曲线 / 贴图识别 / 实耗核时 / VMD 场景补齐

2109 项测试。对照 starpivot-DFT 全部页面逐项对齐后落地四件:

### 实时能量曲线(作业页「实时」按钮,对齐其「任务监控」tab)
- 本地 OSZICAR 优先,无则经 SSH 读远端(manifest.remote_dir);echarts E0-离子步曲线,
  10/30/60 秒轮询或手动;排队中/未提交诚实说明,密码/主机信任走既有口径

### 剪贴板贴图识别(对齐「粘贴图片」)
- 结构建模页直接 **Ctrl+V** 粘贴分子结构截图即识别(DECIMER),另有「粘贴图片」按钮;
  剪贴板位图 → base64 直传 → 临时 PNG 落盘可复查

### 实耗核时统计(概览第五卡「近 30 天实耗核时」)
- 新 `cluster/usage.py`:状态时间戳(RUNNING→终态)× 提交核数(提交时记录 nodes×ppn);
  在跑作业实时累计;缺核数/缺时间戳的作业单列原因、绝不编数;与预算估算口径并列展示

### 可视化场景补齐(对齐其 IGMH / Fukui / AIM tab)
- **IGMH**:δg 等值面按 sign(λ2)ρ 着色(iso=0.01,±0.05 惯用色阶)
- **Fukui/CDFT**:f+/f−/f0/双描述符 cube ± 双相等值面
- **AIM 标注**:结构 CPK + CPprop.txt 临界点标注(BCP 橙/RCP 绿/CCP 紫,标签=编号:ρ);
  「查询 BCP」升级为结构化表(ρ / V(r) / Espinosa 键能估算,弱相互作用口径显式注明)
- HOMO⇄LUMO 一键切换重导 cube;等值面 iso 留空=场景默认(修正 NCI/IRI 被 0.001 覆盖的旧疾)


## v3.2.2 — 2026-07-18 · 金属 slab 建模(层厚收敛接通)+ 波函数四项架构归位

2083 项测试。交接 Backlog #2/#3 落地:

### 金属 slab 建模 + 层厚收敛可再生入口(Backlog #2)
- 新增 `generate/metal_slab.py`:fcc(111)/(100)/(110)、bcc(100)/(110)、hcp(0001)
  六种低指数面确定性 N 层 slab(纯 numpy;层距/堆垛经 ASE 全对距离谱交叉验证逐值一致;
  fcc ABC 与 hcp ABAB 用显式偏移序列区分)
- 结构建模页新增「金属 slab 建模」卡:元素/晶面联动晶格常数初猜回填(实验值,发表口径
  须同泛函 EOS/晶胞优化——始终显式提醒)、hcp 才显 c、冻结底层、INCAR 可选
- 作业 job.yaml 记可再生配方(inputs.recipe)→ **conv_thickness 层厚收敛真接通**:
  从金属 slab 作业一键派生即按配方再生不同层数系列(此前只能诚实报错);
  SAC 石墨烯作业给「单层二维基底无层厚概念」针对性说明,0 作业绝不假成功

### 波函数四项落回引擎注册表(Backlog #3,架构归位)
- ELF-LOL 截面/ADCH 电荷/性质汇总/Fukui-CDFT 自 api 层 `_EXTRA_ANALYSES`+run_script
  临时方案迁入 `multiwfn_driver.ANALYSES`(菜单流逐字节一致,测试锁定零漂移)
- 菜单与执行单一事实源=引擎注册表;`wavefn_run_extra` 保留为兼容入口(按引擎 run()
  执行);附带收益:远程波函数分析(走 ANALYSES)自动获得这四项
- 旧引擎缺项 → 菜单诚实少列 + 按项「引擎待扩展」说明


## v3.2.1 — 2026-07-18 · UI 精简重设计 + 死卡接通(安慰剂清零)

2051 项测试。用户反馈"排版乱/太长/选项墙"→紧凑重排;QA 排查出的 9 个接线断裂全修。

### 界面精简(参照 starpivot-DFT 紧凑排版)
- 分区手风琴:每卡片可折叠,每页默认只展开最常用分区,展开态记忆;页面高度大降
  (结构建模 2569→954px,设置 2297→914px,生成 1596→900px,全部首屏可见)
- 长选项墙→多选下拉:SAC 金属/模板/吸附质、波函数分析项改"已选 N 项"下拉+pill 摘要
- 密度压缩(padding/间距/字号);大段说明→(?) tooltip
- **VASP 优先**:②生成输入顶部引擎下拉默认 VASP,23 种计算类型五分类 optgroup 下拉
- **去吸附能中心主义**:④结果分析页"任意 DFT 计算的结果解析与出图",分析类型下拉切换;
  定位为通用 DFT 平台(吸附能只是诸多计算类型之一)

### 死卡接通(QA 排查:全库无假数据,9 处接线断裂已修)
- **NEB 过渡态**:接通 derive_neb(始末态插值+多image),生成页 NEB 面板(此前后端真实但 GUI 不可达)
- **形成能/结合能**:④加计算器卡(选 SAC+基底作业→Eb/Ef/σ 稳定性)
- **VASPsol 隐式溶剂化**:生成页高级区勾选+介电常数→写 INCAR
- **差分电荷**:修正为拆 AB/A/B 三静态+compute_chgdiff 面平均(此前只给单 CHGCAR,名不副实)
- **收敛扫描·层厚**:诚实化(不再假成功返回 0 作业,如实透出"需从建 slab 流程发起")
- **波函数四项**(ELF-LOL/ADCH/性质汇总/Fukui-CDFT):补 multiwfn_driver.run_script 真接通,降级归因修正
- 任务卡片按 作业生成/结果计算器/INCAR顾问 加徽标区分


## v3.2.0 — 2026-07-18 · 全 DFT 计算平台 + 一键出图 + AI 数据闭环

2009 项测试。三大升级:

### 全 DFT 计算类型目录(②生成输入)
23 种计算类型五分类一站选用:结构优化/晶胞优化/**状态方程 EOS(BM3 体弹模量)**/静态/
**收敛扫描器(ENCUT·k·真空·层厚,自动判收敛点)**/DOS·PDOS/**能带结构(Setyawan-Curtarolo
高对称路径,带隙直接间接判定)**/Bader/差分电荷/**ELF**/**功函数(LOCPOT 面平均+真空平台)**/
频率 ZPE/AIMD/NEB/**Dimer 过渡态(VTST)**/**表面能**/形成能结合能/吸附能项目/多自旋/
**VASPsol 溶剂化**/**DFT+U 值库(Materials Project 惯用值+来源,库内无值不编造)**。
每类=派生器+解析器+出图闭环;④结果分析页按 task_type 自动解析出图。

### 一键出图管线(提交后零点击到投稿级整图)
- 自动驾驶终点升级:全 DONE → 场景感知整套图(锂硫=柱状+台阶+火山+PDOS+差分电荷)
  → **多面板拼版**(a/b/c/d 标号,期刊宽度 DPI≥300)→ 图表 manifest 溯源 → 通知
- **计算活动模板**:「SAC 锂硫筛选(张洪毅式)」等 3 模板一键建全链 DAG
  (弛豫→静态→频率→分析),campaign 自动推进逐阶段派生提交
- 期刊风格预设(nature/acs/prb);显式溶剂化复合物组装(Li2S3+2DOL+DME)

### AI 助手数据闭环
- **论文数据表抽取**:表格数值→结构化对照集(每行页码线索,值域校验,绝不臆造)
- **复现自动对照**:计算完成 → MAE/RMSE/最差三项对照表 → 一键写入 validation.md
- **材料变体推荐**(确定性规则):母版→同族金属/配位/掺杂矩阵,排除论文已算,预算分批
- **论文草稿骨架**:Methods 全自动+图表全编号+Results 数据句(每句锚定真实数值带溯源锚)
  +讨论结论 [作者补充] 占位;自动句/占位句计数诚实呈现;md/docx

### starpivot 细节对齐
侧栏依赖状态+**一键下载/修复依赖**(后台 pip 带进度);概览核时四卡(近30天作业/核时/
剩余核时/监控状态);波函数分析分组菜单扩到 **16 种**(补 ELF/LOL 截面图/ADCH 电荷/
性质汇总/Fukui-CDFT 双描述符);可视化补 Fukui·ELF-LOL·NCI/IRI 散点图·output 面积分布
·服务器端渲染;分子建模输入原子坐标/切换氢/二面角测量;生成页自定义关键词+可编辑预览。


## v3.1.1 — 2026-07-18 · 分子计算全流程(对齐并超越 starpivot-DFT)

新增分子这条腿,与周期性催化主线并列。1704 项测试。

### 分子建模链(①结构建模·分子建模分区)
- 图片识别:DECIMER 图→SMILES(可编辑)+RDKit 2D 键线式渲染+复制(可选依赖,未装给指引)
- SMILES→3D:RDKit ETKDGv3+MMFF94/UFF(力场可选,seed 可复现)→直接载入 3D 编辑器
- 编辑器增强:球棍/仅键/线框/空间填充样式、±X/±Y/±Z/等距视角、保存图片、键长键角测量、原子列表表格、分子式/电子数属性、全选/删除选中
- 外部编辑器联动:GaussView/Avogadro 导出打开→改完检测导入(mtime 轮询)
- 保存 xyz/mol/pdb

### Gaussian 输入完整化(②生成输入)
- 任务九种:Opt/Freq/Opt+Freq/单点/TD 激发态/IRC/柔性扫描(ModRedundant)/NMR/过渡态优化(TS+Freq 验证)
- 泛函/基组常用下拉+自定义;色散 D3/D3BJ;溶剂 SMD/PCM/CPCM+常用溶剂+Generic 自定义 eps/epsinf
- %nprocshared/%mem/%chk;混合基组 Gen/GenECP:**元素周期表点选配基组**(1-86,ECP 勾选)
- 预览/复制剪贴板/导出/提交联动;附加段顺序守恒(坐标→ModRedundant→基组→ECP→SCRF)

### 提交与运行(③)
- 任意 .gjf/.com/.inp/.cell 多文件快速批量提交(自动识别引擎入台账)
- 作业列表:集群/状态筛选+排序+勾选批量取消(qdel/scancel 回写)
- **本机运行**:分子/快速作业本地软件直跑(跨平台运行器,日志尾随,可停止)
- 文件管理:远端目录浏览+文件下载

### 波函数分析与可视化(⑤新页,对齐 starpivot 七件套并超越)
- Multiwfn 驱动:ESP 极值/ESP 面/**ALIE**/HOMO-LUMO/NCI-RDG/IGMH/**IRI**/AIM 八种分析,本机或远程(实验性);缺 Multiwfn 给可复制命令流
- VMD 批渲染:ESP 着色表面(极值点标注球)/ALIE 表面/轨道/NCI/IRI/结构 CPK,Tachyon 高清,缺 VMD 给 tcl
- 外部工具路径统一设置+探测

### 周期性侧补全
- AIMD 输入派生(NVT Nose-Hoover/NVE,温度/步长/步数,继承源 INCAR 留痕)+OSZICAR MD 能量-温度解析
- 结果分析页 DONE 作业一键派生 AIMD


## v3.1.0 — 2026-07-17 · DFT 全通量管线

从"吸附能工具"升级为"催化计算桌面自动驾驶平台"。1401 项测试。
设计宪法：科学正确 > 产出超越对标 > 论文级出图 > 大通量 > 一平台完成。
总体方案与 68 Agent 调研合成见 `docs/planning/v3.1-总体方案.md`。

### 界面（starpivot 式工作流重构）
- 编号工作流侧栏：概览 → ①结构建模 → ②生成输入 → ③提交计算 → ④结果分析 → ⑤论文出图 → ⑥AI 助手（+集群/设置）
- 三主题（经典深邃/学术浅色/深空监控）、元素徽章、晶格水印、研究场景系统（按研究方向裁剪界面）、中英双语基础设施（设置页切换）
- 结构查看/编辑器：3Dmol 交互（选中/移动/删除/改元素/撤销）、固定底层、真空检查、导出 POSCAR
- 图表预设画廊：12 种论文图型缩略卡片，点选→选数据源→参数→一键出版级出图
- AI 助手页：论文→可编辑规格表（逐格出处）→计划（矩阵规模+机时预估）→实例化（单点先行+机时闸+三态门禁约束下的全自动开关）

### 引擎层
- campaign 文件式控制面：任务 DAG、completed/validated/accepted 三态、三门禁（提交/验收/报告）、方法指纹、决策账本、机时预算、单机锁
- SAC 建模：石墨烯超胞 + 六类配位模板（MN4/MN3/MP1N3/MS1N3/MB1N3/MN4+B）× 金属矩阵；15 种分子库；位点枚举与吸附质摆放；参考态注册与 Eb/Ecoh 稳定性
- 派生计算：弛豫 → 频率(ZPE/熵,虚频四象限闸)/电子结构静态(PDOS/Bader/差分电荷) 一键派生；多自旋并跑+磁矩守卫
- 电子结构：partial DOS 流式解析+自旋分辨 d 带中心、Bader 全链、差分电荷(AB/A/B)、COHP/LOBSTER(lobsterin 生成+解析+spilling 闸)
- CI-NEB：初末态插值→多 image 作业→逐 image 收敛监控→能垒提取+MEP 出图
- 通用 CHE/ΔG 引擎：8 组反应预设(Li-S 16e/缔合解离/ORR/HER/OER/CO2RR)、U_eq/U_L/η、多电位台阶、守恒校验
- 筛选引擎:描述符统一提取+火山图自动构造(双支拟合)
- 多引擎适配层：CalcSpec IR + VASP/CP2K/Gaussian/CASTEP(Materials Studio) 文件级后端、跨引擎不等价清单、参考态一致性先行闸（软件本体用户自备，不捆绑）
- 收尾流水线：Methods 自动成段(引文兜底)、SI 装订机、三线表工厂、口径一致性稽核——只组织真实数据,绝不编造

### 其他
- 术语统一（构型/作业/需人工/静默等 15 项）为 i18n 铺路
- v3.0 基线（2026-07-16）：自动驾驶管线、失败诊断自愈、设置页、原生论文级出图引擎等见 README 历史更新节

## v3.0 — 2026-07-16
自动驾驶管线 / 六角色评审整改 / 原生 matplotlib 出图引擎 / 设置页 / 三主题 / 科学防错包 / 发刊材料包。525→933 测试。

## v2.0 — 2026-07 前
生成→提交→监控诊断→有界恢复→ΔE/Li-S 分析→报告 主干。
