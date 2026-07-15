# 发刊水平补齐(Publication Readiness)设计文档 2026-07-15

## 背景与调研结论(deep-research,104 断言,0 驳回)

### 对标竞品版图(均已正式发表)

| 工具 | 期刊/年份 | 形态 | 核心卖点 |
|---|---|---|---|
| VASPKIT | Comput. Phys. Commun. 267:108033 (2021) | 交互终端 | 280+ 前/后处理功能;高通量 2D 筛选验证案例 |
| qvasp | Comput. Phys. Commun. 257:107535 (2020) | 终端 | MIT 开源 + CPC Program Library 存档;卖点=便利性+可扩展 |
| ALKEMIE | Comput. Mater. Sci. 186:110064 (2021) | **GUI 平台** | GUI 透明工作流+数据库+ML;证明"GUI 易用性"本身可作为接收创新点 |
| AiiDA | Sci. Data 7:300 (2020) | 工作流基础设施 | DAG provenance;35k proc/h 定量引擎基准 |
| AiiDAlab | Digital Discovery | GUI 层 | Jupyter Web + Zenodo DOI + Docker 一键部署 |
| atomate2 | Digital Discovery 4:1944 (2025) | 工作流库 | jobflow + custodian 自愈;多计算器互操作 |
| pyiron | npj Comput. Mater. (2024) | 工作流 | 代码无关接口;Al-Li 相图端到端验证案例 |
| Montoya & Persson | npj Comput. Mater. 3:14 (2017) | **吸附能高通量工作流** | **黄金参照**:CE27 实验化学吸附库对比,吸附能 MAE 0.2 eV,表面能 MAE 0.02 eV,PBE vs RPBE 泛函基准,200+ 计算归约为单次提交 |
| VASPilot | arXiv:2508.07035 (2025) | LLM 多智能体 | CrewAI+MCP,Slurm,Flask Web UI;验证=真实体系案例(MoS₂ 家族带隙/ENCUT 收敛/vdW 晶格常数) |
| AutoDFT | arXiv:2605.26179 | LLM 7-agent 闭环 | VASPBench 34 任务基准;MP 20 材料 MAE 定量验证 |

### 发刊必备清单(按证据强度排序)

1. **OSI 许可证纯文本 LICENSE 文件**(JOSS 硬性;CPC CPiP 必须开源;qvasp 先例=MIT)
2. **公开仓库 + issue tracker + 持续开发史**(JOSS:公开 >6 个月;需用户推 GitHub——本计划只能备好本地件)
3. **文档四件套**:statement of need + 安装说明 + example usage + API 文档 + CONTRIBUTING(JOSS 逐项审)
4. **自动化测试 + CI**(JOSS 强烈要求;本项目已有 413 测试,缺 CI 配置)
5. **定量验证基准**:真实物理体系 + 与独立参考值的定量对比(Montoya: MAE;VASPilot: 体系案例;AutoDFT: MAE 表)
6. **竞品定位声明**(statement of need 必须与 ASE/atomate2/AiiDA 级现有生态差异化;JOSS 编辑明确偏好"扩展生态"而非重造)
7. **可复现性**:代码 DOI(Zenodo);Digital Discovery 有专职"数据审稿人"实际跑代码;LLM 功能须交 inference log
8. **Feature-complete**(JOSS 拒收半成品/瘦 API 客户端——本项目全链已实现,达标)

### 本项目差异化定位(写进 statement of need / comparison)

对比矩阵中 vcstudio 独有的组合:**桌面单文件 EXE(零安装)+ PBS 双跳集群 + 确定性零 token 核心 + 提交前结构撞车拦截 + Methods 段与真实输入强一致自动生成 + 全离线(数据不出本机)**。竞品要么是终端工具(VASPKIT/qvasp)、要么需要数据库/服务端(AiiDA/ALKEMIE)、要么自愈依赖 LLM 在线推理(VASPilot/AutoDFT)。

## 决策

- **许可证:MIT**(qvasp 同类先例;版权行 "2026 VASP Catalyst Studio Contributors",**用户可改真实署名**)
- **目标期刊路线**:主投 JOSS(要件清单最明确、周期短)兼容 CPC CPiP;paper 草稿按 JOSS 格式
- **英文化策略**:README 双语(英文为主+中文速览);使用说明保持中文,另出英文精简版 user guide;代码注释不动(中文注释是仓库既有约定)
- **验证基准**:先落**基准管线代码+文档协议**(TDD,合成数据测试);真实 Li-S SAC 吸附能数据回填需集群/VPN,标"待用户"
- **示例可跑性**:examples 附假 POTCAR 库生成脚本(醒目警告:仅演示,真实计算必须用有许可的 PAW 库)——POTCAR 有版权,绝不能入库真赝势

## 不做(backlog)

- 文档站(mkdocs/readthedocs)部署——需公开仓库,本地先备 markdown
- Zenodo DOI / GitHub 公开 / tagged release——需用户账号操作
- 真实集群验证数据回填——需 VPN
- LLM-agent 层扩展(对标 VASPilot)——超出本计划范围,另立项
- CHANGELOG——JOSS 不硬性要求,tagged release 时再补

## 需用户完成的事项(计划外,发刊前必办)

1. LICENSE/CITATION.cff/paper.md 中的作者真实署名 + ORCID + 单位
2. 建公开 GitHub 仓库并推送(CI 才会激活;JOSS 要求公开 ≥6 个月开发史)
3. VPN 恢复后跑真实验证基准数据(工具链本计划已备好)
4. Zenodo 存档拿 DOI;决定投稿期刊(JOSS vs CPC)
