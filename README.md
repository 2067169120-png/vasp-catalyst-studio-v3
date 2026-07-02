# VASP Catalyst Studio (vcstudio)

> 轻量化 DFT 自动化:交 **POSCAR + 你自己的 INCAR** → 自动生成 VASP 输入四件套 →(后续)多集群提交 / 零 token 监控 / LLM 分析出图。

独立轻量脚本包,无 GUI。核心原则:**尊重用户 INCAR,绝不强改方法学**。

📖 **完整用法见 [使用说明.md](使用说明.md)。**

## 四区(里程碑)

| 区 | 状态 | 职责 |
|---|---|---|
| generate 生成区 | **M1(已完成)** | POSCAR + 用户 INCAR → INCAR/POTCAR/KPOINTS(本地拼 + 校验补全) |
| cluster 集群区 | M2 | DPDispatcher 薄封装:多调度器(Torque/Slurm/LSF/Shell)提交/查询/回收 |
| monitor 监控区 | M3 | 纯 Python 零 token 轮询:sloshing/收敛/失败检测 → 契约文件 |
| analyze 分析区 | M4 | 读契约 → 吸附能图/台阶/火山 + Word 报告 + 可选 LLM 讨论 |

## 快速开始

```bash
pip install -e .
vcs gen --poscar POSCAR --incar my.incar --calc-type slab -o results/job1/
```

生成 `results/job1/`,含:

- `INCAR` —— 你的原文 + 校验补全(缺 ENCUT/MAGMOM/ISPIN 才追加,原文一字不改)
- `POTCAR` —— 按 POSCAR 物种顺序本地拼接,ENMAX≤ENCUT 硬校验(元素表覆盖 62 种,含 H/O/卤素/碱土)
- `KPOINTS` —— 自动推荐(或 `--kpoints "5 5 1"` 指定)
- `POSCAR` —— 原样拷入
- `job.yaml` —— 任务台账:状态机 + 输入溯源(sha256)+ 补全审计,贯穿后续提交/监控/分析

## 配置

`config.yaml` 的 `potcar_lib_root` 指向本地 PAW_PBE 库根。**首次使用请改成你自己的库路径。** 也可用环境变量 `VCSTUDIO_CONFIG` 指定配置文件,或用 CLI `--lib-root` 临时覆盖。详见 [使用说明.md](使用说明.md)。

## 目录结构

```
vcstudio/        核心包(generate 生成区 / shared 配置 / cli 入口)
config.yaml      本地配置(赝势库路径等)
results/         建议的作业输出目录(生成内容默认不入 git)
使用说明.md       完整使用文档
README.md        本文件(简介)
```
