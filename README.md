# VASP Catalyst Studio (vcstudio)

> 📂 仓库目录结构速查见 **[STRUCTURE.md](STRUCTURE.md)**。

> 轻量化 VASP 自动化桌面平台:**生成输入 → 多集群提交 → 失败诊断 → 有界恢复 → ΔE/自由能分析 → 出版级报告**。Windows 单文件 EXE,确定性核心零 token,LLM 只在分析层。

📖 完整用法与代码分区块地图见 **[使用说明.md](使用说明.md)**。

## 架构(对齐 OpenClaw 论文的分层,全部已实现)

| 层 | 模块 | 职责与验证条件 |
|---|---|---|
| 生成 | `vcstudio/generate/` | POSCAR+用户 INCAR → 四件套;缺项才补全,**绝不改用户键**;`job.yaml` 记 sha256 溯源 |
| 提交 | `cluster/{schedulers,script_builder,submitter,connection}` | PBS/Slurm 双方言(纯函数);preflight 不过不出手;脚本双轨(auto/模板透传) |
| 监控+失败验证 | `submitter.refresh_job` + `cluster/diagnose.py` | 调度器终态原因+退出码+输出完整性+日志签名+收敛串+能量合理性 → 12+11 类分类 → 4 个终态;查询哨兵防瞬时抖动误判 |
| 有界恢复 | `submitter.continue_from_contcar` | 仅可续算分类;CONTCAR 校验;INCAR 冻结;**上限 3 轮**;规则不覆盖 → NEEDS_HUMAN 停机 |
| 分析+报告 | `project/` + `external/` | ΔE 门控(全 DONE 才给数)、Li-S 放电路径(ΔG/PDS/U_L)、Origin/SVG 双引擎图表、POV-Ray 结构图、LLM 双语分析(真实 INCAR 注入,禁编造文献) |

三条不变式:①方法学主权(用户 INCAR/模板逐字透传)②确定性核心零 token ③显式状态绝不静默(job.yaml 状态机 + 审计历史)。

## 科学约定

- `E_ads = E(slab+ads) − E(slab) − E(ref)`,负 = 有利吸附
- `μ_Li = (E(Li₂S) − E(S₈)/8) / 2`;放电路径 ΔG 参照 S8* = 0;U_L = −max(ΔG/Δn_e)(CHE)
- 全部能量为 DFT 电子能(未含 ZPE/熵),报告与 AI 提示词均明示
- 凭据(集群密码/LLM key)只进 Windows 凭据库,不落明文

## 快速开始

**GUI**:双击 `dist\VASP Catalyst Studio.exe`(四页:生成/吸附能项目/任务/集群)。
**CLI**:`pip install -e .` 后 `vcs gen --poscar POSCAR --incar my.incar --calc-type slab -o results/job1/`。
**重打包**:双击 `重新打包EXE.bat`。**测试**:`python -m pytest`(207 用例;Origin 真机冒烟 `VCS_ORIGIN_SMOKE=1`)。

## 目录

```
vcstudio/       核心包(generate/cluster/project/external/gui/shared/cli)
tests/          207 测试   docs/superpowers/specs/  设计文档
config.yaml     本地配置(赝势库/分子库/理想窗口/llm 端点)
dist/           打包产物 EXE(gitignore)   results/  作业输出(不入 git)
```
