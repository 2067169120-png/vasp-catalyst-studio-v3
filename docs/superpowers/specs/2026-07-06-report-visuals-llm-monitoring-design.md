# M4 报告视觉层 + LLM 分析 + S5 监控 — 设计 spec

日期:2026-07-06　分支:`feature/gui-exe-config`　依据:用户 7 项决策(两轮问答)+ 原版提取报告(workflow `w740regsi`)

## 用户决策(锁定)

1. **图表用 Origin 出**(originpro→Origin2024b,本机已装),matplotlib 路线废弃;**SVG 兜底**(Origin 不可用时报告仍有图)。
2. 四种图全要:**ΔE 柱状图**(理想窗口带做成可配参数,默认 Cui 带 -2.90~-1.65)、**多体系热图**、**自由能阶梯**、**收敛曲线**。
3. 阶梯/收敛数据:**画图引擎+CSV/导入入口先行**;自由能=Li-S 分子转化(S8→Li₂S₈→…→Li₂S),能量可从 `E:\V2.0.0\results\done`(lis_results/mol_*、sac_results/*)导入。
4. **POV-Ray 全自动渲染**(本机 v3.7):拉回 CONTCAR 后后台渲顶/侧视,缓存,失败跳过。
5. **LLM 双模**:DeepSeek 兼容端点内置调用(key 存 keyring)+ 提示词包导出;**双语**(中文解读+英文论文段落)。
6. 报告**全 DONE 自动生成**(含图+结构图+AI 章节)到项目目录。
7. **S5 自动轮询**:任务页定时刷新开关(5/15/30 分钟)。

## 架构(延续三不变式)

```
project/charts.py      纯函数:4 图数据契约 + SVG 渲染(零依赖,离线测)
project/freeenergy.py  纯函数:Li-S 放电路径 ΔG/PDS/U_L + 旧结果导入
project/ai_analysis.py LLM:提示词构建(纯函数)+ urllib client(transport 可注入)
external/origin_charts.py  Origin 适配器:同一契约→PNG 600dpi+.opju;lazy import originpro
external/povray_render.py  POV-Ray 适配器:POSCAR→.pov/.ini 文本(纯函数)→pvengine64 调用
report.py 扩展         嵌图(figs/ 目录)+ AI 章节 + 结构图廊
submitter/fetch 钩子    拉回后自动渲染;jobs_tab 定时轮询 + 全 DONE 触发报告
```

- **外部软件 = 适配器**:运行时探测(配置→默认路径→PATH),失败降级,不进 EXE。
- **提示词修 9 条硬伤**:真实 INCAR/KPOINTS 注入(原版硬编码假参数=学术红线)、显式符号约定、禁编造文献、结构化输出(机理/趋势/caveats/置信度)、低温 0.2、数据不足要明说、输出持久化进报告。
- key 只进 keyring(service `vcstudio-llm`),config 存 base_url/model;原版泄漏 key 已提醒用户作废。

## 数据契约(与原版对齐,提取报告 §4)

- bar: `{rows:[系], cols:[物种], matrix:[[eV]], band:(lo,hi)|None, band_label}`
- heatmap: 同上(无 band);NaN 允许缺格
- ladder: `{steps:[{label, G, sub_label?}], pds_index, u_l?}`(ΔG 参照首态=0)
- convergence: `{x:[...], y:[eV], xlabel, epsilon}`(±ε 带,达标点标星)

## 验收

纯函数层 pytest 全绿;Origin/POV-Ray 在本机**真实渲染冒烟**(生成实际 PNG 目检);LLM fake transport 测试;GUI 无头构造;全量回归。
