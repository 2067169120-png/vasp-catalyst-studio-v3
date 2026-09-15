# 离线科学演示 / Offline analysis demo

**审稿人无需 VASP、无需集群,即可端到端验证 vcstudio 的分析链路。**
A reviewer with **no VASP and no cluster** can verify vcstudio's analysis chain
end-to-end.

本工具的**前半程**(生成输入 → 提交集群 → 监控)天然需要 VASP 与 HPC;但**后半程**
(失败诊断 → ΔE 汇总 → 出图 → 报告)是纯确定性数据处理,可完全离线复现。本演示用一组
**合成的"已完成"作业**驱动后半程:

    诊断分类  →  ΔE 汇总(门控)  →  出柱状图  →  HTML 报告

The first half (generate → submit → monitor) inherently needs VASP + HPC; the
second half (diagnose → gated ΔE → plot → report) is deterministic data
processing and is reproduced here fully offline from synthetic "completed" jobs.

## 跑起来 / Run

```bash
python examples/offline_analysis/run_demo.py            # 产物落 ./demo_out/
python examples/offline_analysis/run_demo.py /tmp/out   # 或指定输出目录
```

每一步都打印**实际结果**与**期望**,方便逐条核对。产物:
`demo_out/adsorption_bar.png`(装了 matplotlib;否则退化为 `.svg`)与
`demo_out/report.html`(自包含,浏览器直接打开)。

Each step prints its **actual result** next to the **expectation**. Outputs:
a paper-style bar chart and a self-contained HTML report.

## 数据来源 / Data

`jobs/` 下 5 个合成作业目录,各含 `job.yaml` + `CONTCAR` + `OSZICAR` + `OUTCAR` +
`INCAR`:

| 作业 | 角色 | 状态 | 演示点 |
|---|---|---|---|
| `slab_clean` | 清洁表面 | DONE | ΔE 公式的 E(slab) |
| `ref_S8` | 气相参考 | DONE | ΔE 公式的 E(ref) |
| `ads_Li2S4` | 吸附组态 | DONE | ΔE ≈ −2.50 eV |
| `ads_Li2S6` | 吸附组态 | DONE | ΔE ≈ −1.70 eV;含一次续算历史(报告注"续算×1") |
| `ads_Li2S8_zbrent` | 吸附组态 | UNCONVERGED | 诊断命中 ZBRENT(可续算);**ΔE 门控**:该行不给数 |

> **这些是合成样本,不是真实 VASP 产物**,仅用于离线验证分析链路的行为
> (诊断分类、ΔE 门控、出图、报告降级)。要跑真实数据,把真实作业目录接进
> 项目页/CLI 的正常流程即可。
>
> These are **synthetic samples, not real VASP outputs** — they exercise the
> analysis chain's behavior (classification, ΔE gating, plotting, graceful
> degradation) offline. For real data, use the normal project/CLI flow.

## 预期输出要点 / What you should see

- **诊断**:4 个 `CONVERGED→DONE`,`ads_Li2S8_zbrent` → `ZBRENT`(可续算 ♻)。
- **ΔE 门控**:仅两个 DONE 组态给出 ΔE;未完成组态明确留空并注明"组态未完成"。
- **出图**:仅对已完成组态出柱状图(matplotlib 出论文级 PNG,缺失则 SVG 兜底)。
- **报告**:Origin 与 LLM 都离线降级(改内嵌 SVG、AI 章节标注"未运行"),报告仍完整产出。
