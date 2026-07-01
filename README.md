# VASP Catalyst Studio (vcstudio)

> 轻量化 DFT 自动化:交 **POSCAR + 你自己的 INCAR** → 自动生成 VASP 输入四件套 →(后续)多集群提交 / 零 token 监控 / LLM 分析出图。

独立轻量脚本包,无 GUI,不依赖 `E:\V2.0.0`。核心原则:**尊重用户 INCAR,绝不强改方法学**。

## 四区(里程碑)

| 区 | 状态 | 职责 |
|---|---|---|
| generate 生成区 | **M1(本期)** | POSCAR + 用户 INCAR → INCAR/POTCAR/KPOINTS(本地拼 + 校验补全) |
| cluster 集群区 | M2 | DPDispatcher 薄封装:多调度器(Torque/Slurm/LSF/Shell)提交/查询/回收 |
| monitor 监控区 | M3 | 纯 Python 零 token 轮询:sloshing/收敛/失败检测 → 契约文件 |
| analyze 分析区 | M4 | 读契约 → 吸附能图/台阶/火山 + Word 报告 + 可选 LLM 讨论 |

## M1 用法

```bash
pip install -e .[dev]
vcs gen --poscar POSCAR --incar my.incar --calc-type slab -o job1/
```

生成 `job1/` 含 `INCAR`(你的原文 + 校验补全)、`POTCAR`(按 POSCAR 物种顺序本地拼、ENMAX≤ENCUT 硬校验)、`KPOINTS`(自动推荐或 `--kpoints` 指定)、`POSCAR`(原样拷入)。

## 配置

`config.yaml` 的 `potcar_lib_root` 指向本地 PAW_PBE 库根;可用环境变量 `VCSTUDIO_CONFIG` 指定配置文件路径,或 CLI `--lib-root` 覆盖。

## 测试

```bash
pytest
```
