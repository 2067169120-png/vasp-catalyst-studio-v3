# C1 收敛过程可视化 — 设计 (spec / 实施报告)

> 对齐 `2026-07-14-ui-redesign-pywebview-design.md` §C.1。作业详情画
> **E0 / ΔE / |F|max vs 离子步**;SCF 震荡与力收敛一眼可见。竞品几乎无 GUI 做好这一环。

## 目标与非目标

**目标(本期 C1)**
- 从**本地 job_dir** 的 `OSZICAR` + `OUTCAR` 解析出逐离子步序列,web GUI 用离线 ECharts 画曲线。
- 已拉回(DONE/UNCONVERGED/失败)作业立即可看;运行中作业提供**一键拉取最新** OSZICAR/OUTCAR 后重画。
- 数据不全(缺 OUTCAR → 无 |F|max;仅 SCF 无 F= → 单点)时降级显示,明说缺什么,绝不编造。

**非目标(明确不做)**
- 不做远程 OSZICAR 的**流式实时推送**(每秒 tail)。刷新 = 用户点「拉取最新」触发一次定向 fetch,复用既有拉取通道。
- 不解析 vasprun.xml(那是 C4 DOS/能带)。
- 不改任何方法学键、不碰提交/诊断既有逻辑。

## 数据来源与既有可复用件

- 拉回文件:`submitter.FETCH_FILES = ('CONTCAR','OSZICAR','OUTCAR')` 已把三者落到 `job_dir`(见 submitter.py:475)。DONE/失败作业本地即有全量 OSZICAR/OUTCAR。
- `diagnose.py` 已有 OSZICAR 尾部解析件可借鉴口径:`_SCF_LINE_RE`、按 `F=` 切离子步块、`last_block_scf_iters`(diagnose.py:165)。C1 需**全序列**解析,新写纯函数,不改 diagnose。
- 运行中作业:`submitter._live_check` 只 `tail -n 200 OSZICAR`(submitter.py:349),不足以画全程 → C1 的「拉取最新」走定向 fetch 取完整 OSZICAR/OUTCAR。

## 解析口径(科学正确性)

**OSZICAR → 逐离子步**
- 每个离子步在 OSZICAR 以 `... F= ...E0= ...  d E =...` 行收尾;其上是若干 SCF 电子步行(`DAV:`/`RMM:` 等,含电子能与 dE)。
- 每离子步产出:`{step:int, E0:float, dE:float(该离子步相对上一步的 |ΔE0|), scf_iters:int}`。
  - `E0` 取 F= 行的 `E0=` 值(自由能外推到 σ→0,与报告能量口径一致)。
  - `dE`(离子步间)= `|E0[i] − E0[i-1]|`,第 1 步为 None。首屏用 log 轴看收敛尾部。
  - `scf_iters` = 该离子步块内 SCF 行数(借 diagnose 口径)。

**OUTCAR → 逐离子步 |F|max**
- 每离子步一段 `POSITION          TOTAL-FORCE (eV/Angst)`,块内每原子一行 `x y z fx fy fz`。
- `|F|max = max_atom sqrt(fx²+fy²+fz²)`;逐段产出一个值,顺序对齐 OSZICAR 离子步。
- OUTCAR 可能很大 → **逐行流式**解析,只在力块内累加,不整体载入。
- 若含 selective dynamics,VASP 仍对全部原子打印力;不做冻结原子过滤(与 EDIFFG 判定一致,保守多显示)。

**对齐**:以离子步序号对齐 E0 与 |F|max;两者长度可能差 1(OUTCAR 末段可能未写全)→ 以较短者截断,`fmax` 缺位补 null,前端断线显示。

## 分层与接口

沿用三层(纯逻辑 → 薄 api → 离线前端)。

**逻辑层(新)`vcstudio/cluster/convergence.py`——纯函数,零 IO,离线可测**
- `parse_oszicar(text: str) -> list[dict]` — 离子步序列(上口径)。
- `parse_outcar_fmax(text: str) -> list[float]` — 逐离子步 |F|max。
- `convergence_series(oszicar_text: str, outcar_text: str | None) -> dict`
  → `{'steps':[1..n], 'E0':[...], 'dE':[None,...], 'fmax':[...|null], 'scf_iters':[...], 'have_forces':bool, 'notes':[...]}`。
  异常/空输入 → 空序列 + notes 说明,绝不抛。

**api 层 `vcstudio/gui_web/api.py`——薄、注入、JSON-safe**
- `conv_series(job_dir: str) -> {'ok', 'series'|'error'}`:读本地 `job_dir/OSZICAR`(+`OUTCAR` 若在)→ `convergence_series` → 返回。缺 OSZICAR → `{'ok':False,'error':'该作业尚无本地 OSZICAR,请先拉取'}`。注入 `conv_mod`(默认 convergence)+ fs 读取以便离线测。
- `conv_fetch(name, password, trust_new, job_dir, remote_dir) -> {'ok','error','needs_trust'}`:对单作业定向拉取 OSZICAR/OUTCAR(复用 submitter 拉取件)。断网/认证失败给结构化 error 不崩。**MVP 可先只交付 `conv_series`(本地),`conv_fetch` 作 Task 内可选**。

**前端 `vcstudio/gui_web/assets/`——离线 ECharts**
- 供应商:`assets/vendor/echarts.min.js`(Apache-2.0)。**构建期下载落地并入库**(离线约束:运行时禁 CDN;构建期下载允许)。`index.html` 本地 `<script src>` 引入,打包 `--add-data` 带上。
- `converge.js`:`VCS.showConvergence(jobDir, name)` → `api.conv_series` → 在 `VCS.modal` 里初始化 ECharts:x=离子步;左轴能量(E0 折线)+ 右轴 log(|dE|、|F|max);pill 图例;缺数据/缺力时顶部黄条明示;窗口 resize 重算。
- `jobs.js`:每行操作区加「收敛」按钮 → `VCS.showConvergence(row.job_dir, row.name)`;运行中作业 modal 内提供「拉取最新」→ `conv_fetch` → 重画(若交付 conv_fetch)。

## 测试策略

- `parse_oszicar` / `parse_outcar_fmax` / `convergence_series`:pytest 用**真实 VASP 片段**夹具(多离子步 OSZICAR + 对应 OUTCAR 力块;及缺 OUTCAR、单步、空/垃圾输入的降级)。断言 E0/dE/|F|max 数值与手算一致。
- `conv_series`:注入假 conv_mod + tmp job_dir,happy + 缺文件 error。
- 前端:`node --check converge.js`;真窗人工验收(拉回作业出图、缺力降级、运行中拉取)——实现环境开不了窗则台账标「待真窗/VPN 验收」。
- 全量 pytest 保持全绿(基线 354 passed 1 skipped)。

## 离线 / 安全约束(全程)

- 运行时零 CDN;ECharts 仅本地文件。构建期一次性下载入库,记来源+版本+license。
- 密码只 keyring;`conv_fetch` 走既有凭据回退,不落 yaml。
- api 处理器薄、异常兜底、依赖注入;逻辑层零 IO。
- UI 零 emoji、pill 状态、中文文案、数据过 `VCS.esc`;方法学键绝不改。

## 风险与降级

| 风险 | 处理 |
|---|---|
| OUTCAR 很大解析慢 | 逐行流式,只读力块;单作业量级(NSW≤500)可接受 |
| 运行中 OSZICAR 半行/截断 | 解析逐行 try,跳过坏行,notes 记「N 行无法解析」 |
| 续算多轮 OSZICAR 追加 | 按出现顺序连续编号;跨轮 E0 断点由 dE 尖峰自然体现(不特殊处理,notes 提示) |
| ECharts 体积(~1MB)增大 exe | 可接受;仅 web 入口带,legacy 不带 |
