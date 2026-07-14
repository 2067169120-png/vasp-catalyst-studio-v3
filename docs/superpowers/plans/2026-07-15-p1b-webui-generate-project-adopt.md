# P1b — 生成页/项目页迁移 + 认领改造 + 入口切换 + 仓库整理 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把剩余两页(生成/吸附能项目)迁到 web GUI,认领流程改为一键零手填,--web 成为默认打包入口;收尾做仓库文件分块整理。

**Architecture:** 沿用 P1a 三层:batch_ops/纯逻辑(vcstudio.gui.logic、generate.job_builder、project.adsorption 均为 UI 无关纯函数,直接复用)→ `gui_web/api.py` 薄处理器(依赖注入、JSON-safe、异常兜底)→ `assets/*.js` 页面模块(复用 VCS.* 组件)。tkinter GUI 保持可用直到入口切换任务,之后以 --legacy 保留。

**Tech Stack:** 同 P1a(pywebview、原生 JS、pytest)。

## Global Constraints(全任务生效,与 P1a 相同)

- 每个 commit 必须带 pathspec(工作树可能有用户 WIP)。
- TDD:Python 处理器先失败测试;前端以 node --check + 真窗人工验收。
- 全量 pytest 全绿(基线 314 passed 1 skipped)。
- UI 零 emoji;状态 pill;中文文案;所有数据插值过 `VCS.esc`。
- 密码绝不落 yaml(只 keyring);方法学键绝不改;api 处理器薄、逻辑可注入离线测。
- 分支 feature/gui-exe-config,不合 master。
- 进度逐任务追加 .superpowers/sdd/progress.md。

---

### Task 1: Api 扩展 —— 生成页处理器

**Files:**
- Modify: `vcstudio/gui_web/api.py`
- Test: `tests/test_web_api.py`(追加)

**Interfaces:**
- Consumes(全部已存在,镜像 `vcstudio/gui/generate_tab.py` 的调用面,实现前先读它):
  `vcstudio.gui.logic` 的预览纯函数(generate_tab 顶部 import 的那组)、
  `vcstudio.generate.job_builder.build_job_dir(poscar_path, incar, out_dir, ...)`、
  `vcstudio.shared.config.load_config/set_potcar_lib_root/get_ui_state/set_ui_state`、
  `vcstudio.shared.manifest`(generate_tab._write_manifest 的落档逻辑)+ `cluster.ledger.register`。
- Produces(JS 契约):
  - `gen_preview(poscar_path, incar_path) -> {'ok', 'summary'|'error'}`(summary=预览文本/结构化 dict,镜像 _refresh_preview 喂给界面的内容)
  - `gen_state() -> {'poscar','incar','out_dir','lib_root'}`(get_ui_state 回填)
  - `gen_run(poscar_path, incar_path, out_dir, lib_root) -> {'ok','job_dir','warnings':[],'error'}`(镜像 _on_run→build_job_dir→_write_manifest→ledger.register 全链,persist ui_state/lib_root)
  - `pick_file(kind) -> {'path'}` / `pick_dir() -> {'path'}`:用 pywebview 的 `webview.windows[0].create_file_dialog`(webview 延迟 import,测试注入 dialog_fn)——web 页面没有原生文件选择,这是唯一新增的 webview 依赖点

- [ ] Step 1: 先读 generate_tab.py 与 gui/logic.py,把 _refresh_preview/_on_run/_write_manifest 的实际函数名与参数抄准
- [ ] Step 2: 失败测试(每个方法 happy+error;pick_file 注入假 dialog_fn 断言不真开窗)
- [ ] Step 3: 实现(全部走 try/except → {'error'} 口径;新 ctor 注入参数 config_mod/job_builder_mod/dialog_fn)
- [ ] Step 4: 全量 pytest 绿
- [ ] Step 5: `git commit -m "feat(gui_web): generate-page api handlers" -- vcstudio/gui_web/api.py tests/test_web_api.py`

### Task 2: 生成页前端 generate.js

**Files:**
- Create: `vcstudio/gui_web/assets/generate.js`
- Modify: `assets/index.html`(generate section 替换空态 + script tag)

**Interfaces:** Consumes Task 1 全部方法 + `VCS.*`;镜像 generate_tab 的 UI 语义:POSCAR/INCAR/输出目录三行(文本框+「浏览」按钮走 pick_file/pick_dir)、赝势库根目录行、即时预览面板(选完即调 gen_preview)、「生成四件套」主按钮 → gen_run → warnings 逐条 log(warn 级)+ 成功后提示去任务页。

- [ ] Step 1: 实现(布局用现有卡片/表单样式;预览面板 `<pre class="mono">`)
- [ ] Step 2: node --check;pytest 全绿(assets-only);真窗验收:回填上次路径、预览渲染、生成一个真作业目录并出现在任务页
- [ ] Step 3: `git commit -m "feat(gui_web): generate page" -- vcstudio/gui_web/assets`

### Task 3: Api 扩展 —— 项目页处理器

**Files:**
- Modify: `vcstudio/gui_web/api.py`
- Test: `tests/test_web_api.py`(追加)

**Interfaces:**
- Consumes(镜像 `vcstudio/gui/project_tab.py`,实现前先读):`vcstudio.project.adsorption`(list_projects/load_project/创建+批量生成入口/delta_e_rows/CSV 导出)、`report_full.generate_project_report`、`report_full._member_dirs`。
- Produces:
  - `proj_list() -> {'projects': [{'path','name','n_members'}]}`
  - `proj_create(name, slab_path, config_paths, gas_path, out_root) -> {'ok','project'|'error','advisories':[]}`(镜像 _on_generate)
  - `proj_delta(path) -> {'ok','rows','missing'|'error'}`(ΔE 门控语义原样:缺成员明说)
  - `proj_export_csv(path, save_to) -> {'ok','file'|'error'}`
  - `proj_report(path, save_to) -> {'ok','file'|'error'}`(同步调用;耗时长在 js 侧提示等待)
  - 文件选择复用 Task 1 的 pick_file/pick_dir

- [ ] Step 1: 读 project_tab.py + adsorption.py 抄准函数名/参数
- [ ] Step 2: 失败测试 → 实现 → 全量绿
- [ ] Step 3: `git commit -m "feat(gui_web): project-page api handlers" -- vcstudio/gui_web/api.py tests/test_web_api.py`

### Task 4: 项目页前端 project.js

**Files:**
- Create: `vcstudio/gui_web/assets/project.js`
- Modify: `assets/index.html`(project section + script tag)

**Interfaces:** Consumes Task 3 方法 + `VCS.*`。UI:项目下拉 + 新建区(slab/组态多选/气相参考/输出根)→ 生成;ΔE 表(pill 状态 + 缺员黄条明示)、导出 CSV、生成完整报告(用 VCS.modal 转圈提示,完成后 log 文件路径)。

- [ ] Step 1: 实现;Step 2: node --check + pytest + 真窗验收(读出现有项目、ΔE 门控文案正确);Step 3: `git commit -m "feat(gui_web): project page" -- vcstudio/gui_web/assets`

### Task 5: 认领改造(用户核心诉求:零手填)

**Files:**
- Modify: `vcstudio/gui_web/api.py`、`tests/test_web_api.py`、`vcstudio/gui_web/assets/jobs.js`、`vcstudio/shared/config.py`(若无通用 kv 可复用 ui_state)

**Interfaces:**
- `adopt_root_get/set(path)`:认领本地根目录配置(默认 `%USERPROFILE%\vcstudio_jobs`),存 config ui_state。
- `adopt_all(name, password, trust_new) -> {'results':[[job_id, ok, msg]], 'needs_trust', 'message'}`:queue_detail → 对每个未纳管作业:workdir 为空则逐个 `submitter.query_workdir`(同一连接内;给 batch_ops 加 `adopt_scan(prof, pw, trust_new, known_ids, local_root)`,一次连接完成 明细+补目录+落 adopt_external_job,本地目录 = local_root/<作业名或job_id>,os.makedirs)。已纳管跳过;查不到 workdir 的作业标 needs_input 返回,前端对这几个走旧的单个认领弹窗。
- jobs.js 队列 modal 改造:①单个认领弹窗的远程目录输入框——空时自动调 query_workdir 预填;②modal 顶部加「一键认领全部未纳入(n)」按钮 → adopt_all → 逐条 log + reload;③本地目录输入框预填 adopt_root/<作业名>,可改。

- [ ] Step 1: 失败测试(adopt_scan 假件:queue_detail 3 作业其中 1 已纳管 1 无 workdir → 断言 adopt 调用集合/needs_input/目录创建)
- [ ] Step 2: 实现 batch_ops.adopt_scan + api.adopt_all/adopt_root_* → 全量绿
- [ ] Step 3: jobs.js 接线;node --check;真窗验收(断网时按钮给出连接失败日志而非崩)
- [ ] Step 4: `git commit -m "feat(gui_web): one-click adopt-all with auto workdir + local-root config" -- vcstudio/cluster/batch_ops.py vcstudio/gui_web/api.py vcstudio/gui_web/assets/jobs.js vcstudio/shared/config.py tests/test_web_api.py tests/test_batch_ops.py`

### Task 6: 入口切换 + 双 exe 重打包

**Files:** Modify `packaging/build_exe.py`
- 无参 = web 入口(名 'VASP Catalyst Studio',带 assets/collect-all webview);`--legacy` = 旧 tkinter(名 'VASP Catalyst Studio Legacy');删除 --web 开关(或留作无参别名)。
- [ ] pytest 全绿 → 两次打包(无参 + --legacy)→ 双 exe 各启动 15s 存活验证 → `git commit -- packaging/build_exe.py`

### Task 7: 全分支终审 + 修复轮

- [ ] review-package 范围 = P1b 全部提交;终审重点:api 新方法异常纪律、pick_file 的 webview 耦合是否被注入隔离、adopt_scan 单连接语义、两 exe 入口互不污染;must-fix 修完复核 Ready 才进 Task 8。

### Task 8: 仓库文件整理(阶段二,用户要求)

- 新建 `_待处理归档/` + 其内 README.md 清单(每行:原路径|为何判定过程性|建议)。移入:继续开发-交接总结.md、.superpowers/sdd/ 的 task-*-brief/report + review-*.diff(progress.md 留原位)、_回收待删/ 内容、其他根目录散件(拿不准就归档不删)。
- 保护线:vcstudio/、results/、dist/、tests/、docs/ 不动;git mv 移动已跟踪文件;移完全量 pytest;根目录补 STRUCTURE.md(每个顶层目录一行)。
- [ ] `git commit -m "chore: quarantine process files into _待处理归档/ + add STRUCTURE.md" -- <逐个列出>`
