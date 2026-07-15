# C3 计算方法段自动生成 — 实现计划

> Spec:`docs/superpowers/specs/2026-07-15-c3-methods-generation-design.md`。
> 全局约束同 C1/C2:窄 pathspec、TDD、全量 pytest 全绿(基线 389 passed 1 skipped)、
> api 薄+注入、UI 零 emoji 中文文案、方法学键只读、分支 feature/gui-exe-config 不合 master。

### Task 1: 纯模块 `generate/methods_text.py`
- `parse_potcar_titels` / `parse_kpoints_scheme` / `extract_facts` / `render_zh|en|bibtex`(映射表见 spec)。
- [ ] 失败测试(tests/test_methods_text.py:RPBE+D3 全事实、无 GGA→PBE、缺件降级、BibTeX 按需)→ 实现 → 全量绿 → `git commit -- vcstudio/generate/methods_text.py tests/test_methods_text.py`

### Task 2: api `methods_text(job_dir)`
- 读三文件(INCAR 缺→error;KPOINTS/POTCAR 缺→warnings 降级);注入 `methods_mod`。
- [ ] 失败测试 → 实现 → 全量绿 → `git commit -- vcstudio/gui_web/api.py tests/test_web_api.py`

### Task 3: 前端 jobs.js「方法」+ methods.js modal
- 三段 pre(中文/English/BibTeX)+ 各自复制按钮(clipboard 失败降级选中);真浏览器 harness 验收后删。
- [ ] node --check + pytest 全绿 → `git commit -- vcstudio/gui_web/assets/methods.js vcstudio/gui_web/assets/jobs.js vcstudio/gui_web/assets/index.html vcstudio/gui_web/assets/app.css`

### Task 4: 终审 + 修复轮
- [ ] 子代理审 C3 提交(重点:GGA 覆盖 POTCAR 味的措辞、映射表正确性、BibTeX 条目准确、缺件降级不编造);must-fix 修完复核;台账记完成。
