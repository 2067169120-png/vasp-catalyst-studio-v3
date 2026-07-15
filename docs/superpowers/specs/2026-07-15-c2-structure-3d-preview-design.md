# C2 结构 3D 预览 — 设计 (spec / 实施报告)

> 对齐 `2026-07-14-ui-redesign-pywebview-design.md` §C.2。提交前/拉回后直接查看
> POSCAR/CONTCAR(3Dmol.js 离线),**自动标注分子-衬底最短距离**——把 S8 撞车类
> 问题(历史真实事故:模板 S8 与骨架 S 距 1.24/1.396 Å,秒发散)拦在提交前。

## 目标与非目标

**目标(本期 C2)**
- 本地 POSCAR/CONTCAR → 3D 球棍模型(3Dmol.js,WebGL,离线打包)。
- 自动做**分子-衬底分离 + 最短距离分析**:垂直间隙 + 3D 最近原子对(含面内周期像),
  < 阈值给撞车红警(历史标定:目标间隙 ~3.0 Å,1.24 Å 即撞车)。
- 入口两处:生成页 POSCAR 行「3D」按钮(提交前拦截);任务页行内「结构」按钮
  (优先 CONTCAR,无则 POSCAR——拉回后看弛豫结果)。
- 解析不出/纯分子/纯 slab 等情况降级明说,绝不编造。

**非目标(明确不做)**
- 不做超胞/重复显示、不画晶格盒、不做轨迹动画(NSW 逐步回放)——backlog。
- 不做结构编辑(拖原子/改坐标)。
- 不做远程文件拉取(复用任务页既有拉回;本期只读本地)。

## 数据与科学口径

**POSCAR 解析(新纯模块 `generate/structure_view.py`,复用 `generate/poscar.py` 既有件)**
- 复用 `parse_poscar_species`(物种/计数)与 `read_cell_vectors`(3×3 晶格,已乘缩放因子)。
- 新增坐标解析:第 8/9 行起(Selective dynamics 行可选,大小写不敏感,S/D/C/K 首字母判定);
  `Direct/Fractional` → 分数坐标×晶格;`Cartesian` → ×缩放因子。
  负缩放因子按 VASP 语义 = 目标体积,scale = (|v|/det)^(1/3)。
  坐标行取前 3 列(selective 的 T/F 忽略);行数不足计数 → 结构化 error。
- 输出 XYZ 文本喂 3Dmol(走它最成熟的 xyz 解析路径,避开其 vasp 解析器的坑),
  同一次解析同时产间隙分析——单源。

**分子-衬底分离(与历史实践一致:骨架/吸附物按 z 分离)**
- 全原子按 z 排序,找**最大相邻 z 间隙**;间隙 ≥ 1.8 Å 且两侧原子数均 ≥1 → 上方=分子、下方=衬底。
  (1.8 Å < 目标 3.0 Å 但 > 常见成键距,能同时接住"已撞到 1.2 Å"的病例吗?不能——
  撞车时 z 间隙消失。所以撞车判定**不依赖分离成功**,见下。)
- 分离成功 → 报:垂直间隙(分子最低 z − 衬底最高 z)、3D 最近原子对距离
  (衬底原子扩 ±1 面内周期像,防分子跨胞边时漏判)、分子化学式。
- 分离失败(纯分子/纯 slab/已撞车粘连)→ `separated: False` + 说明,并**兜底报全局
  最近非成键异常**:全结构最近原子对 < 1.2 Å(任何体系都不该有)→ 红警"疑似原子重叠"。
- 阈值(历史事故标定,写成模块常量):`GAP_TARGET=3.0`(建议)、`GAP_WARN=2.0`(黄警)、
  `GAP_CRASH=1.5`(红警,S8 撞车 1.24/1.396 均落此档)。

## 分层与接口

**逻辑层(新)`vcstudio/generate/structure_view.py`——纯函数,零 IO**
- `parse_positions(content) -> {'elements':[...], 'coords':[[x,y,z]...], 'cell':3x3}`(笛卡尔 Å)
- `analyze_gap(elements, coords, cell) -> dict`:
  `{'separated':bool, 'vertical_gap':float|None, 'min_dist':float|None,
    'pair':{'i','j','elem_i','elem_j'}|None, 'mol_formula':str|None,
    'n_mol':int, 'n_slab':int, 'level':'ok'|'warn'|'crash'|None, 'notes':[...]}`
- `structure_view(content) -> dict`:`{'xyz':str, 'natoms':int, 'formula':str,
  'gap':<analyze_gap 结果>, 'notes':[...]}`;解析失败 → raise ValueError(api 层兜)。

**api 层 `gui_web/api.py`**
- `struct_view(path, filename=None) -> {'ok','view'|'error'}`:filename 给了就
  os.path.join(path, filename)(任务页传 job_dir + 'CONTCAR'/'POSCAR',路径拼接留后端,
  JS 不碰 os.sep);读文件 → structure_view;缺文件/解析失败结构化 error。
  ctor 注入 `sview_mod`。CONTCAR 优先逻辑放前端(两次探测)还是后端?——后端:
  `filename='AUTO'` 时依次试 CONTCAR、POSCAR,返回实际用的 `used` 字段,单次调用。

**前端**
- 供应商:`assets/vendor/3Dmol-min.js`(BSD-3-Clause,构建期下载入库,README 记
  来源/版本/license/SHA,同 ECharts 惯例)。
- `structure.js`:`VCS.showStructure(path, filename, title)` → `struct_view` →
  `.modal-wide` 内 3Dmol viewer(球棍:sphere scale 0.3 + stick 0.15,元素默认配色),
  顶部信息条:化学式 + 原子数 + 间隙分析(level 着色:ok 灰 / warn 黄条 / crash 红字黄条);
  最近原子对画黄色虚线圆柱 + 距离标签;WebGL 不可用降级文案;关闭清理 viewer。
- `generate.js`:POSCAR 行加「3D」按钮 → showStructure(poscar 路径)。
- `jobs.js`:行内加「结构」按钮(同「收敛」样式)→ showStructure(dir, 'AUTO')。

## 测试策略

- structure_view.py:TDD——真实 POSCAR 夹具(Direct+Selective、Cartesian、负缩放因子、
  行数不足、垃圾输入);间隙分析手算断言(正常 3.0 Å 体系 / 2.0 warn / 1.24 crash 粘连 /
  跨胞边周期像 / 纯 slab 无分子)。
- api:tmp 目录夹具 happy + AUTO 回退 + 缺文件 error + 注入假件。
- 前端:node --check;真浏览器 harness 验收(渲染出 canvas、警告条、pair 虚线、dispose),
  同 C1 方法;pywebview 真窗留待人工。
- 全量 pytest 全绿(基线 368 passed 1 skipped)。

## 离线/安全约束(同 C1)

运行时零 CDN;3Dmol 仅本地文件;api 薄+注入+异常兜底;UI 零 emoji、中文文案、
`VCS.esc` 全插值;密码只 keyring;方法学键绝不改;窄 pathspec 提交。

## 风险与降级

| 风险 | 处理 |
|---|---|
| 3Dmol-min.js 体积 ~2.5MB | 可接受(exe 21.6→~24MB);仅 web 入口带 |
| WebView2 WebGL 被禁 | viewer 建失败 → 文案"当前环境不支持 WebGL,无法 3D 预览" |
| z 分离误判(台阶面/厚吸附层) | 报告 n_mol/化学式供人工核对;分离失败不拦功能,仍显示 3D + 全局重叠兜底检查 |
| 大体系(>2000 原子)渲染卡 | natoms > 5000 时降为仅 stick;notes 提示 |
