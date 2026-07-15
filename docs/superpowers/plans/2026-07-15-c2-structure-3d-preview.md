# C2 结构 3D 预览 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans,task-by-task 实现。Steps 用 `- [ ]` 跟踪。
> Spec:`docs/superpowers/specs/2026-07-15-c2-structure-3d-preview-design.md`。

**Goal:** POSCAR/CONTCAR 离线 3D 预览(3Dmol.js)+ 分子-衬底最短距离自动标注(撞车拦截)。

**Architecture:** 纯逻辑 `generate/structure_view.py`(复用 poscar.py 既有件)→ 薄 api `struct_view` → 前端 `structure.js` + vendor 3Dmol。全程同 C1 三层惯例。

## Global Constraints(同 C1)
- 每 commit 带 pathspec;TDD 先失败测试;全量 pytest 全绿(基线 368 passed 1 skipped)。
- 运行时零 CDN;vendor 记来源/版本/license/SHA。
- UI 零 emoji、中文文案、`VCS.esc` 全插值;api 薄+注入;方法学键绝不改。
- 分支 feature/gui-exe-config,不合 master;逐任务追加 progress.md。

---

### Task 1: 解析+间隙分析纯模块 `generate/structure_view.py`

**Files:** Create `vcstudio/generate/structure_view.py`;Test `tests/test_structure_view.py`。

**Interfaces(spec 定稿):**
- `parse_positions(content) -> {'elements','coords','cell'}`(笛卡尔 Å;Direct×晶格 / Cartesian×scale;负 scale=体积语义;Selective 行可选;行数不足 raise ValueError)
- `analyze_gap(elements, coords, cell) -> dict`(z 最大间隙 ≥1.8Å 分离;垂直间隙+3D 最近对(衬底 ±1 面内像);阈值 GAP_WARN=2.0/GAP_CRASH=1.5;分离失败兜底全局 <1.2Å 重叠检查)
- `structure_view(content) -> {'xyz','natoms','formula','gap','notes'}`

- [ ] Step 1: 失败测试(Direct+Selective / Cartesian / 负 scale / 行数不足 / 垃圾;间隙:3.0 ok、1.9 warn、1.24 crash 粘连兜底、跨胞边周期像、纯 slab)
- [ ] Step 2: 实现 → 全量绿
- [ ] Step 3: `git commit -- vcstudio/generate/structure_view.py tests/test_structure_view.py`

### Task 2: api `struct_view`

**Files:** Modify `vcstudio/gui_web/api.py`;Test `tests/test_web_api.py`(追加)。
- `struct_view(path, filename=None)`:filename='AUTO' → 依次 CONTCAR/POSCAR(返回 'used');普通 filename → join;None → path 即文件。缺文件/ValueError → 结构化 error。ctor 注入 `sview_mod`。
- [ ] 失败测试(happy / AUTO 回退 / 缺文件 / 注入 / 异常兜底)→ 实现 → 全量绿 → `git commit -- vcstudio/gui_web/api.py tests/test_web_api.py`

### Task 3: vendor 3Dmol.js 入库

**Files:** Create `assets/vendor/3Dmol-min.js`;Modify `assets/vendor/README.md`(追加行)、`assets/index.html`(script)。
- [ ] 下载固定版本(jsdelivr npm 3dmol,BSD-3-Clause),node --check 验证、记 SHA 入 README;index.html vendor script(echarts 之后);pytest 全绿;`git commit -- vcstudio/gui_web/assets/vendor vcstudio/gui_web/assets/index.html`

### Task 4: 前端 structure.js + 两处入口接线

**Files:** Create `assets/structure.js`;Modify `assets/generate.js`(POSCAR 行「3D」)、`assets/jobs.js`(行内「结构」)、`assets/index.html`(script)、`assets/app.css`(容器高)。
- `VCS.showStructure(path, filename, title)`:modal-wide + 3Dmol 球棍;信息条(化学式/原子数/间隙 level 着色);最近对黄虚线+距离标签;WebGL 失败降级文案;close 清理 viewer;natoms>5000 仅 stick。
- [ ] 实现;node --check;真浏览器 harness 验收(canvas 渲染、crash 红警条、pair 标注、dispose)后删 harness;pytest 全绿;`git commit -- vcstudio/gui_web/assets/structure.js vcstudio/gui_web/assets/generate.js vcstudio/gui_web/assets/jobs.js vcstudio/gui_web/assets/index.html vcstudio/gui_web/assets/app.css`

### Task 5: 终审 + 修复轮

- [ ] 子代理审 C2 全部提交(排除 vendored js):解析科学正确(分数→笛卡尔、负 scale、周期像)、间隙判定阈值语义、api 异常纪律、零 CDN、viewer 清理、XSS。must-fix 修完复核 Ready;台账记完成+遗留。
