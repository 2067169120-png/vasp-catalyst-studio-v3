# 计算材料与催化工作台对标及产品路线（2026-08）

本文记录 VASP Catalyst Studio 4.0 的外部产品对标与第一方路线选择。它不是第三方项目的功能复刻清单，也不构成科学验证；所有新增能力仍须遵守本仓库的 `job.yaml` 事实源、方法一致性、ValidationResult、accepted rung 和报告双轴门禁。

## 1. 产品定位

VASP Catalyst Studio 的差异化方向应保持为：

- VASP-first，但允许有边界的文件级多引擎适配；
- 面向表面、吸附、反应路径和催化研究对象，而不只是管理计算目录；
- 桌面端、本地优先、易于使用；
- 每个结论都能回到结构、参数、作业、解析器、验证和报告 revision；
- 自动化可以节省操作，但不能替代方法选择、科学复核或发布审批。

不建议把 AiiDA、NOMAD、CatMAP 或其他平台整体嵌入本应用。它们更适合作为设计参照、数据交换目标或用户自装的可选适配层。

## 2. 同类项目提供的关键范式

| 项目 | 值得借鉴的范式 | 对本工作台的启发 |
|---|---|---|
| [AiiDA](https://aiida.readthedocs.io/projects/aiida-core/en/stable/topics/provenance/concepts.html) / [AiiDAlab](https://www.aiidalab.net/) | 区分数据溯源和逻辑溯源；可恢复工作流；浏览器应用与 App Registry | 同时回答“结果怎样生成”和“为什么执行这一步”；配方必须版本化，旧运行不能随模板升级漂移 |
| [Materials Cloud](https://www.materialscloud.org/) | Work / Discover / Explore / Archive 分层；完整 provenance 浏览；可复用和 DOI-ready 数据 | 把计算、分析、教学与发布分区；SI capsule 可继续演进为本地可审计 archive |
| [NOMAD](https://nomad-lab.eu/prod/v1/docs/explanation/processing.html) / [NOMAD Catalysis](https://fairmat-nfdi.github.io/nomad-catalysis-plugin/index.html) | 解析—归一化—索引流水线；结构化 ELN；催化样品/反应领域实体；可保存仪表板 | 从“目录集合”提升为可查询的 Catalyst / Surface / Adsorbate / ReactionStep / Evidence 对象 |
| [atomate2](https://materialsproject.github.io/atomate2/user/index.html) / jobflow | Maker、Job、Flow、标准任务文档、参数 powerup 和大规模运行 | 建立版本化 Method Recipe 和执行前 DAG；保存最终展开参数，不只保存模板名 |
| [ASE](https://wiki.fysik.dtu.dk/ase/) | 表面/吸附构建、轨迹播放器、NEB、振动和多种热化学模型 | 通用位点枚举、NEB 诊断条、结构—能量—力同步播放器、显式热化学假设 |
| [Materials Project API](https://docs.materialsproject.org/downloading-data/using-the-api/getting-started) / OPTIMADE | 结构和性质检索、标准 ID、任务级 VASP 数据、provenance | 增加来源明确的参考结构入口；外部能量必须保留方法和数据库版本，默认不得与本地能量混算 |
| [Catalysis-Hub](https://docs.catalysis-hub.org/en/latest/tutorials/index.html) | 按反应物、产物、表面组成和 facet 检索反应能、能垒、结构及论文 | 建立只读外部参考浏览器；保存 GraphQL 查询、DOI、许可、获取时间和原始快照 |
| [CatMAP](https://catmap.readthedocs.io/en/latest/) | 反应网络、自由能图、覆盖度、TOF、火山图和敏感性分析 | 在完整自由能/能垒证据之后增加微观动力学实验室；明确 steady-state mean-field 适用范围 |
| [FAIR Chemistry](https://fair-chem.github.io/models-1/) | 催化预训练模型与大规模吸附/NEB 数据集 | 只作为可选预筛选层；模型、checkpoint、训练域和 OOD 风险必须可见，候选仍须一致方法 DFT 确认 |

## 3. 当前覆盖与主要缺口

当前 4.0 已覆盖项目身份、23 类 VASP 任务、提交与恢复、7 类分析、报告 revision、scientific diff、Evidence/Claim Graph、SI capsule、Operation Queue 和 Resume Center。最需要补充的不是更多孤立按钮，而是以下连续用户旅程：

```text
参考结构 / bulk
  → 表面与 termination
  → 位点、取向与覆盖度
  → 吸附态与基元反应
  → NEB / 频率 / 热化学
  → 反应网络与条件扫描
  → 方法一致性、敏感性与验证
  → 可追溯图表、报告和发布包
```

### 计算前第一公里

- 缺少来源可追溯的 Materials Project / OPTIMADE 结构入口；
- 通用表面、termination 和吸附位点枚举尚未产品化；
- 用户仍常需自带完整 INCAR；缺少“值—来源—理由—风险—覆盖”的方法向导；
- Campaign 模板数量有限，且缺少通用 DAG dry-run 和最终参数差异预览。

### 分析与诊断

- NEB、ENCUT/k/vacuum/thickness 收敛扫描和 AIMD 尚未成为完整的一等分析视图；
- 缺少结构—轨迹—能量—最大力同步播放器；
- 缺少项目级研究搜索、方法兼容筛选和散点仪表板；
- 报告有冻结 Evidence Graph，但 live project 尚无完整的数据/逻辑双层 provenance 图。

### 催化领域能力

- 缺少正式、版本化的 CatalystSurface、AdsorbateState、ElementaryStep、ConditionSet 和 ReactionNetwork 合同；
- 缺少 Reaction Map、热化学逐项账本和条件浏览器；
- 微观动力学、表面稳定性、Pourbaix/Wulff 与外部反应数据库仍是后续能力。

## 4. 分阶段路线

### Now：第一批可验证增强

1. **催化对象模型与研究配方画廊**
   - 版本化实体、typed evidence refs、observed/imported/inferred provenance；
   - 吸附能、位点筛选、NEB、热化学和收敛扫描配方；
   - 只读 DAG 预览、缺失前置和完整参数 hash。
2. **VASP Method Recipe / INCAR 向导**
   - preview → explicit confirm → write；
   - 每个键展示来源、理由、风险和用户覆盖；
   - 不猜 U、磁态或方法映射，不覆盖已有键，不自动提交。
3. **Analysis Completion Pack**
   - NEB image 能量/力/收敛/路径质量；
   - 收敛扫描原始点、显式阈值和敏感性；
   - AIMD 温度、能量漂移和采样长度诊断；
   - 浏览器只渲染服务端权威结果。

### Next：形成连续催化研究链

1. Structure Source Hub 与通用表面/位点/取向/覆盖度枚举；
2. Reaction Map 与结构—轨迹—曲线诊断播放器；
3. 跨项目搜索、方法兼容过滤和可保存研究视图；
4. Catalysis-Hub 只读参考浏览器；
5. DOI-ready 本地 `.vcs-archive`，含 dry-run、许可证和敏感信息扫描。

### Later：独立验证后再开放

1. CatMAP 进程适配器和微观动力学实验室；
2. chemical-potential、Pourbaix、Wulff 和覆盖度相图；
3. 可解释的严格科学指纹缓存；
4. 用户自装的 FAIR-Chem/UMA 预筛选适配器；
5. 多用户空间、审阅角色、冻结发布和第三方插件 SDK。

## 5. 科学与许可边界

- 几何位点不等于活性位点；缺陷、磁性、溶剂、覆盖度和重构会破坏简单对称等价。
- NEB 只描述给定端点和初始带附近的路径，不证明完整机理；替代路径与 TS 频率仍需独立证据。
- 热化学模型、标准态、温度、压力、低频处理和虚频规则必须显式，并提供敏感性结果。
- XC、赝势、U、色散、k 点、参考态、标准态、覆盖度或溶剂不一致时，默认禁止混算。
- CatMAP 属于 steady-state mean-field 模型；机制缺失、BEP/scaling、prefactor 和数值收敛可主导输出。
- Pourbaix/Wulff 属于平衡热力学，不能证明运行条件下的动力学相、重构或亚稳态。
- ML 结果只能排序候选，不能进入 `accepted` 或最终报告，除非有一致方法 DFT 和人工科学复核。
- POTCAR 不得进入公开 archive。外部数据必须保留 attribution、许可、DOI/来源 ID 和获取时间。
- ASE（LGPL）与 pymatgen（MIT）可在正式依赖评审后复用；CatMAP/CatHub 客户端为 GPL，优先做进程/数据接口并进行许可复核，不直接复制或打包其代码；FAIR-Chem 权重不得绕过用户许可和模型访问条件。

## 6. 每项功能的共同验收合同

每项新能力都必须同时满足：

1. 输入、方法、解析器、单位、分母、源文件 hash 和版本可追溯；
2. `missing_prerequisite`、`not_implemented`、`unavailable` 是正常状态，不得伪造空结果；
3. 浏览器不重算科学数值，不持久化本地路径或密钥；
4. 预览不产生文件或远程副作用；写入、提交和取消继续走显式确认与幂等合同；
5. 生成 artifact 不提升 scientific status；`validated`、`accepted` 和 final report 仍需各自门禁；
6. Python 合同、API、真实生产 JavaScript/Fake DOM、i18n、键盘和窄屏回归同时通过；
7. 真实科学 benchmark 与独立人工复核在完成前保持 `UNKNOWN`。

## 7. 参考资料

- [AiiDA provenance concepts](https://aiida.readthedocs.io/projects/aiida-core/en/stable/topics/provenance/concepts.html)
- [AiiDA caching and reproducible reuse](https://aiida.readthedocs.io/projects/aiida-core/en/stable/howto/run_codes.html#how-to-save-compute-time-with-caching)
- [AiiDAlab](https://www.aiidalab.net/)
- [Materials Cloud](https://www.materialscloud.org/)
- [NOMAD processing](https://nomad-lab.eu/prod/v1/docs/explanation/processing.html)
- [NOMAD Catalysis Plugin](https://fairmat-nfdi.github.io/nomad-catalysis-plugin/index.html)
- [atomate2 VASP workflows](https://materialsproject.github.io/atomate2/user/codes/vasp.html)
- [ASE surfaces](https://wiki.fysik.dtu.dk/ase/ase/build/surface.html)
- [ASE thermochemistry](https://wiki.fysik.dtu.dk/ase/ase/thermochemistry/thermochemistry.html)
- [Materials Project API](https://docs.materialsproject.org/downloading-data/using-the-api/getting-started)
- [Catalysis-Hub tutorials](https://docs.catalysis-hub.org/en/latest/tutorials/index.html)
- [CatMAP tutorials](https://catmap.readthedocs.io/en/latest/tutorials/index.html)
- [FAIR Chemistry pretrained models](https://fair-chem.github.io/models-1/)

