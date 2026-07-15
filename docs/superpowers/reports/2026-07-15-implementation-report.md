# 实施报告 2026-07-15 — P1b 收尾 + 仓库整理 + 发刊向功能 C1–C4

> 分支 `feature/gui-exe-config`(未合 master,待用户人工验收)。
> 本报告覆盖计划任务 continue-p1b-webui 的 阶段A收尾/B/C/D 全部工作。

## 一、做了什么(按阶段)

### 阶段 A · P1b(生成页/项目页迁移 + 认领改造 + 入口切换)
- Tasks 1–6 此前已完成(生成页、项目页、一键认领、web 默认入口,ffad291..f58b1dd)。
- 本轮完成 **Task 7 终审**:Ready 判定 + 2 must-fix 落地——
  `3f20d62` pytest 污染真实用户注册表/台账 → conftest 5-seam 隔离(回归实证零污染);
  `6f00985` 认领本地目录撞名 → 追加 `_<jid>` 避让。
- 基线 354 passed 1 skipped。

### 阶段 B · 仓库整理
- `f3f2f15` + 并行会话 `80eb7c1`:新增 `STRUCTURE.md`(顶层+子包地图+入口点);
  `_待处理归档/`(README 清单:原路径|判定|建议,**零删除**);过程性散件、SDD 中间产物、
  旧 Web.exe 全部入归档;删空的 `_回收待删/`。保护线(vcstudio/results/dist/tests/docs)未动。

### 阶段 C · 发刊向功能(每项:spec/plan 先行 → TDD → 真浏览器验收 → 终审+修复轮)

**C1 收敛过程可视化**(6 提交 445900c..543e1cd)
- `cluster/convergence.py`:OSZICAR/OUTCAR → 逐离子步 E0/ΔE/|F|max(流式、坏行降级)。
- 离线 ECharts 5.5.1(Apache-2.0,SHA 入库);任务页行内「收敛」→ 双轴曲线模态。
- 终审 READY;2 Minor 修复:关模态期间加载泄漏守卫、OUTCAR F13 溢出行容错。

**C2 结构 3D 预览**(7 提交 871edd2..b856728)
- `generate/structure_view.py`:POSCAR/CONTCAR → 笛卡尔坐标 + **分子-衬底间隙分析**
  (z 分离、±1 面内周期像最近对、共价半径撞车判据——历史 S8 1.24 Å 事故直接命中)。
- 离线 3Dmol.js 2.4.2(BSD-3-Clause);生成页「3D」+ 任务页「结构」(CONTCAR 优先)。
- 终审 NEEDS-FIX → 修复轮:**z 边界回卷展开**(CONTCAR 回卷原子致假 ok)、
  **重叠扫描无条件执行**(分子内部融合漏网)——两个真实漏判面,各有 TDD 复现。
- 复核:评审子代理额度中断,改本会话 6 边界探针代验(等大间隙/全同 z/极小 Lz/
  边界重合/字段一致性/负 z 输入)全过。

**C3 计算方法段自动生成**(4 提交 be69857..80b5cfd,差异化点)
- `generate/methods_text.py`:读**真实** INCAR/KPOINTS/POTCAR → 中英双语 Methods 段
  + BibTeX(只引真用到的 8 条文献,逐条核对无误)。
- 核心正确性:**GGA 标签覆盖 POTCAR 味**——GGA=RP + PAW_PBE 精确措辞为
  "RPBE 泛函(PAW 数据集由 PBE 生成)";缺件 warnings 明说,绝不编造。
- 任务页「方法」→ 三段复制模态(clipboard 失败降级选中)。终审自验探针全过。

**C4 DOS 出图**(5 提交 1be371e..b6b1b6d,MVP=总 DOS)
- 架构决策:**零 matplotlib**,走 charts.py 纯 SVG 双引擎兜底路线(EXE 零依赖增重)。
- `project/dosparse.py`:vasprun.xml iterparse 流式,`</dos>` 即停
  (11MB 假尾段实测 0.013s);`charts.render_dos_svg`:E−E_F/自旋镜像/费米虚线。
- api 自动落 `job_dir/dos.svg`(矢量,期刊可用);任务页「DOS」按钮。
- 自验探针**抓到真 bug**(单点数据除零)已修+TDD。

### 阶段 D · 收尾
- web exe 重打包 23.26MB(含 C1–C4 全部前端资产),启动 15s 存活冒烟过。
- 本报告。

## 二、为什么(角度依据)
研究生用户日常痛点 + 论文证据链完整性:收敛曲线(竞品 GUI 几乎没有)、
提交前撞车拦截(历史真实事故复发防线)、Methods 段与实际计算强一致(查重/审稿可辩护)、
DOS 矢量图直接进论文。全部离线运行(集群数据不出本机)。

## 三、证据
- **测试**:354 → **413 passed 1 skipped**(净增 59,全 TDD 先红后绿),全程每提交全量绿。
- **提交**:本轮 22 个(f3f2f15..b6b1b6d,全窄 pathspec);终审 4 轮
  (C1 子代理 READY、C2 子代理 NEEDS-FIX→修复→探针代验、C3/C4 自验探针)。
- **真浏览器验收**:C1 图表渲染/降级/dispose、C2 3D 出图/红警/清理、
  C3 三段复制降级、C4 SVG 渲染——均在本机浏览器实测(非仅 node --check)。
- **离线约束**:运行时零 CDN;vendor/README.md 记全部第三方(来源/版本/license/SHA256)。

## 四、遗留清单(按优先级)
1. **用户人工验收**(阻塞合 master):桌面双击 exe,过一遍 生成→3D→提交→收敛→方法→DOS
   全链;集群相关需 VPN(台账多处标"待真窗/VPN 验收")。
2. C2 独立复核:评审额度中断改探针代验,下轮有额度可补一次独立评审(非阻塞)。
3. Backlog(各 spec 已列):C1.1 运行中「拉取最新」重画;C2 超胞/晶格盒/轨迹;
   C4 PDOS/能带/拉回清单加 vasprun.xml;proj_report 同步阻塞桥改后台线程;
   生成页 calc_type/K 选择器;species_refs 后端就绪无 UI(P1b 终审附赠规划输入)。
4. `_待处理归档/` 内容等用户逐条定夺(README 有清单,零删除)。
