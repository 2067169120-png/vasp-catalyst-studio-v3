# VASP Catalyst Studio — UI 重构(PyWebView)+ 论文向功能扩展 设计文档

日期:2026-07-14 | 状态:已获用户批准
背景:现 tkinter GUI 被用户评价"丑且 AI 感重"(emoji 按钮、密集工具栏、原生控件);
且与 V2.0.0 相比存在行为差距(DONE 不自动拉回文件)。经调研(sv-ttk/ttkbootstrap/
CustomTkinter/PySide6/PyWebView 对比;CatGo、VASPilot、vaspkit、atomate2 功能对标)
与用户确认,定案如下。

## 决策摘要(用户已拍板)

1. 技术路线:**PyWebView + HTML/CSS**(弃 tkinter 页签)。vcstudio 逻辑层零改动,只换 GUI 层。
2. 范围:UI 重做 + 高价值新功能一起设计,分三期交付。
3. remote_root = `/home/Maple123/done`(已改 clusters.yaml)。

## A. UI 壳

- **布局**:左侧窄图标导航(仪表盘/生成/吸附能项目/任务/集群)+ 顶部细状态条
  (当前集群、连接状态、自动刷新开关)。内容区卡片化。
- **设计语言**:浅色默认;中性灰底 + 白卡片;主色 `#4477AA`(与 charts.py PUB_COLORS
  同源);状态用小圆点 pill(排队灰/运行蓝/收敛绿/失败红/需人工黄);**全面去 emoji**;
  作业号/能量/路径等宽右对齐;每页有空态(一句说明 + 主行动按钮)。
- **字体**:打包思源黑体(OFL 可分发;微软雅黑仅作 font-family 回退)+ JetBrains Mono。
- **技术栈**:pywebview(BSD)+ 原生 HTML/CSS/JS(不引前端框架,离线打包);
  ECharts(Apache-2.0,离线)出曲线;3Dmol.js(BSD)做 3D。
  Python↔JS 走 `js_api`;后台线程沿用 `runner` 模型(js_api 处理器保持薄,
  逻辑仍在 vcstudio 纯函数层,pytest 可测)。
- **打包**:PyInstaller 单文件不变;HTML/JS/字体作为 data 资源打进 exe
  (注意 `sys._MEIPASS` 资源路径)。Win11 自带 WebView2。

## B. 行为修复(对齐 V2.0.0 习惯)

1. **DONE 自动拉回**:`refresh_job` 判定 DONE 后自动下载轻量三件套
   (CONTCAR/OSZICAR/OUTCAR)到本地作业目录 + 渲结构图(figs/);失败不阻塞、记日志。
   大文件(vasprun.xml/CHGCAR 等)仍走手动拉回。
   —— 修复用户报告的"全 DONE 只出 html 报告、本地没 OUTCAR、结构图廊为空"。
2. **智能核数推荐**:测试连接成功后自动 `pbsnodes -a` 探测(移植 V2.0.0
   layer1_hpc 节点探测),按队列推荐整节点用满(1w: batch=24/fat=112/fata=128),
   一键填入资源参数。禁止硬编码核数(继承 V2.0.0 铁律)。

## C. 新功能(按低难度高价值排序)

1. **收敛过程可视化**:作业详情画 E0/ΔE/|F|max vs 离子步(数据通道复用
   `submitter._live_check`);运行中实时刷新;SCF 震荡直观可见。竞品几乎无 GUI 做好。
2. **结构 3D 预览**:提交前/拉回后直接查看 POSCAR/CONTCAR(3Dmol.js);
   自动标注分子-衬底最短距离(S8 撞车类问题提交前拦截)。
3. **Methods 段自动生成**:读真实 INCAR/KPOINTS/POTCAR TITEL → 中英双语 Methods
   段 + BibTeX(VASP/PAW/泛函/IVDW/收敛标准)。与实际计算强一致,非模板文字。
4. **DOS/能带出图**(收尾期):拉 vasprun.xml → 出版风格 DOS/PDOS。
   文件大、解析重;做不完不阻塞前三项。

## D. 分期

- **P1 UI 壳迁移**:先出 2 版静态 mockup(同布局、两种视觉浓度:文档感淡雅 vs
  控制台感紧凑)供用户挑;定稿后迁移四页 parity + 空态。
- **P2 行为修复**:B1 + B2。
- **P3 新功能**:C1→C2→C3→C4。
- 每期独立可发布;每期结束跑全量 pytest + 重打包 exe 验证。

## 测试策略

- js_api 处理器:纯函数化,pytest 直测(不起 webview)。
- 逻辑层测试(现 260 项)不受影响,持续全绿。
- 视觉验收:mockup 阶段用户确认;每期打包 exe 人工过一遍主流程。

## 不做(YAGNI)

- 深色模式(后续可加,首版浅色)。
- 前端框架(React/Vue)、npm 构建链。
- NEB/Bader/差分电荷(记入 backlog,见调研报告;差分电荷是锂硫论文标配,P3 后评估)。
- 结构编辑(只读预览;编辑仍去 VESTA)。
