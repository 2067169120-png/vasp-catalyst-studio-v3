# C1 收敛过程可视化 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans,task-by-task 实现。Steps 用 `- [ ]` 跟踪。
> Spec:`docs/superpowers/specs/2026-07-15-c1-convergence-visualization-design.md`。

**Goal:** 作业详情画 E0/ΔE/|F|max vs 离子步(离线 ECharts,数据来自本地 OSZICAR/OUTCAR)。

**Architecture:** 纯逻辑 `cluster/convergence.py` → 薄 api `gui_web/api.py` → 离线前端 `assets/converge.js`(+ vendor/echarts.min.js)。不改方法学、不碰 diagnose/submitter 既有逻辑。

## Global Constraints(全任务)
- 每 commit 带 pathspec(树上可能有 WIP)。
- TDD:Python 先失败测试;前端 node --check + 真窗人工验收(开不了窗则台账标待验)。
- 全量 pytest 全绿(基线 **354 passed 1 skipped**)。
- 运行时零 CDN;ECharts 仅本地文件(构建期下载入库,记来源/版本/license)。
- UI 零 emoji、pill、中文文案、数据过 `VCS.esc`;密码只 keyring;方法学键绝不改;api 薄+注入。
- 分支 `feature/gui-exe-config`,不合 master;逐任务追加 `.superpowers/sdd/progress.md`。

---

### Task 1: 解析纯函数 `cluster/convergence.py`

**Files:** Create `vcstudio/cluster/convergence.py`;Test `tests/test_convergence.py`。

**Interfaces:**
- `parse_oszicar(text) -> list[dict]`:每项 `{step,E0,dE,scf_iters}`;`dE=|E0[i]-E0[i-1]|`,第 1 步 dE=None;按 `F=` 切块,E0 取 `E0=` 值,scf_iters=块内 SCF 行数(借 diagnose `_SCF_LINE_RE` 口径,但**复制常量到本模块,不 import diagnose 私有名**)。
- `parse_outcar_fmax(text) -> list[float]`:逐 `TOTAL-FORCE (eV/Angst)` 块 → `max sqrt(fx²+fy²+fz²)`;逐行流式;非力块行跳过。
- `convergence_series(oszicar_text, outcar_text=None) -> dict`:对齐两序列(较短截断,fmax 缺补 None),产 `{'steps','E0','dE','fmax','scf_iters','have_forces','notes'}`;空/垃圾输入 → 空序列+notes,绝不抛。

- [ ] Step 1: 先读 diagnose.py 的 `_SCF_LINE_RE`/F= 切块/`last_block_scf_iters` 抄准口径。
- [ ] Step 2: 失败测试——真实 VASP 片段夹具:①3+ 离子步 OSZICAR + 对应 OUTCAR 力块(手算 E0/dE/|F|max 断言);②缺 OUTCAR(have_forces=False);③单离子步;④空/垃圾(空序列+notes);⑤含坏行(跳过+notes 计数)。
- [ ] Step 3: 实现 → 全量 pytest 绿。
- [ ] Step 4: `git commit -m "feat(convergence): OSZICAR/OUTCAR -> per-ionic-step E0/dE/|F|max series parser" -- vcstudio/cluster/convergence.py tests/test_convergence.py`

### Task 2: api 处理器 `conv_series`

**Files:** Modify `vcstudio/gui_web/api.py`;Test `tests/test_web_api.py`(追加)。

**Interfaces:** `conv_series(job_dir) -> {'ok','series'|'error'}`:读本地 `job_dir/OSZICAR`(+`OUTCAR` 若在)→ `convergence_series`。缺 OSZICAR → `{'ok':False,'error':'该作业尚无本地 OSZICAR,请先拉取'}`。ctor 注入 `conv_mod`(默认 convergence)以离线测;全 try/except → `{'error'}`。

- [ ] Step 1: 失败测试(tmp job_dir 放真实 OSZICAR/OUTCAR → 断言 series 键齐;缺 OSZICAR → error;注入假 conv_mod 断言被调用)。
- [ ] Step 2: 实现 → 全量绿。
- [ ] Step 3: `git commit -m "feat(gui_web): conv_series api handler (local OSZICAR/OUTCAR -> series)" -- vcstudio/gui_web/api.py tests/test_web_api.py`

### Task 3: 离线 ECharts 供应商入库 + 打包带上

**Files:** Create `vcstudio/gui_web/assets/vendor/echarts.min.js`(构建期下载);Create `assets/vendor/README.md`(来源 URL/版本/Apache-2.0);Modify `assets/index.html`(本地 `<script>` 引入);Modify `packaging/build_exe.py`(assets 已整目录 `--add-data`,确认 vendor 一并带上,必要时补)。

- [ ] Step 1: 下载固定版本 echarts.min.js(记 SHA)落 `assets/vendor/`;写 vendor/README.md(来源+版本+license+SHA)。
- [ ] Step 2: index.html 加 `<script src="vendor/echarts.min.js"></script>`(在页面脚本前);确认 asset_dir 打包整目录已含 vendor(build_exe --add-data assets 递归);node --check 其余 js 不受影响;pytest 全绿(assets-only)。
- [ ] Step 3: `git commit -m "build(gui_web): vendor offline ECharts (Apache-2.0) + bundle in web exe" -- vcstudio/gui_web/assets/vendor packaging/build_exe.py vcstudio/gui_web/assets/index.html`

### Task 4: 前端 `converge.js` + jobs 接线

**Files:** Create `vcstudio/gui_web/assets/converge.js`;Modify `assets/index.html`(script tag);Modify `assets/jobs.js`(行内「收敛」按钮)。

**Interfaces:** `VCS.showConvergence(jobDir, name)` → `api.conv_series` → `VCS.modal` 内 ECharts:x=离子步;左轴 E0 折线,右轴 log(|dE|、|F|max);pill 图例;缺力/缺数据顶部黄条;resize 重算;destroy on close。jobs.js 每行操作区加「收敛」→ 调用。

- [ ] Step 1: 实现 converge.js(布局用现有卡片/modal 样式;容器固定高度;`echarts.init` + `setOption` + `dispose`)。
- [ ] Step 2: node --check(converge.js/jobs.js);pytest 全绿;真窗验收(拉回作业出图、缺力降级、断网不崩)——开不了窗则台账标「待真窗/VPN 验收」。
- [ ] Step 3: `git commit -m "feat(gui_web): convergence chart view (E0/dE/|F|max vs ionic step, offline ECharts)" -- vcstudio/gui_web/assets/converge.js vcstudio/gui_web/assets/jobs.js vcstudio/gui_web/assets/index.html`

### Task 5: 终审 + 修复轮

- [ ] review-package 范围 = C1 全部提交;重点:解析口径科学正确(E0/|F|max 手算核对)、api 异常纪律、ECharts 纯离线(无 CDN 引用)、modal dispose 无内存泄漏、缺数据降级文案。must-fix 修完复核 Ready。
- [ ] 台账记 C1 完成 + 遗留(conv_fetch 运行中拉取若未做则记为 C1.1 backlog)。
