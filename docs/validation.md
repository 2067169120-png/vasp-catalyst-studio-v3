# Validation protocol / 验证基准协议

- **Applies to:** VASP Catalyst Studio V4.0 working tree
- **Last reviewed:** 2026-08-11
- **Status:** pipeline ready; like-for-like real data pending cluster access

管线代码和 fail-closed 测试已就绪（`vcstudio/project/benchmark.py`）；真实
Li-S 数据尚未回填。这个状态必须保持为 **pending**，不能由软件测试、报告
`ValidationResult`、格式生成成功或无障碍字段检查代替。

## Scope / 适用范围

This protocol validates one bounded scientific question: adsorption-energy
agreement for a Li-S single-atom-catalyst set under explicitly matched methods.
It is not a validation of every engine adapter, every analysis plugin, a report's
scientific finality, or PDF/UA conformance.

本协议只验证一个有明确分母的问题：方法学对齐的 Li-S 单原子催化剂吸附能。
它不等于 CP2K/Gaussian/CASTEP 与 VASP 等价，不覆盖所有分析插件，也不能证明
报告已经通过人工科学审阅或当前 ReportLab PDF 符合 PDF/UA。

## Why / 为什么需要这一节

已发表同类工具通常包含定量验证。Montoya & Persson 的吸附能高通量框架
(npj Comput. Mater. 3:14, 2017) 对照 CE27 实验化学吸附数据库报告吸附能
MAE 约 0.2 eV、表面能 MAE 约 0.02 eV；VASPilot (arXiv:2508.07035)
用 MoS₂ 家族带隙、ENCUT 收敛和 vdW 晶格常数做体系案例；AutoDFT 用
Materials Project 子集报告逐性质 MAE。这些公开数值只说明常见验证形式，
**不是 vcstudio 当前结果，也不是本项目的通过阈值**。

## Protocol / 协议

1. **体系集**：Li-S 单原子催化剂（SAC）吸附能样本——金属掺杂 C-N
   骨架上的 Li₂S、Li₂S₂、Li₂S₄、Li₂S₆、Li₂S₈、S₈ 吸附构型。
2. **公式**：
   `E_ads = E(slab+ads) - E(slab) - E(ref)`。
   未校正值采用 `E0`。如使用 ZPE/热/熵修正，必须另行记录频率证据、温度、
   修正模式、低频处理、虚频质量门和 correction fingerprint；缺失频率时
   不得推断修正。
3. **参考值**：只使用同体系、同泛函/色散修正且 ENCUT 等关键方法学可对齐的
   已发表数据。无法对齐者进入不可比较清单，不得通过重命名或均值填补。
4. **管线**：

   ```python
   from vcstudio.project import benchmark

   computed = benchmark.load_csv("computed.csv")
   reference = benchmark.load_csv("reference.csv")
   result = benchmark.compare_to_reference(computed, reference)
   print(benchmark.render_markdown(result))
   ```

   CSV 使用 `name,energy_eV` 两列，`#` 行为注释。无交集时显式失败，绝不
   静默输出空基准。
5. **报告口径**：逐体系误差、MAE、有效 `n`、缺失分母、不可比较原因和
   方法学差异必须同时给出。报告 revision/manifest 只证明工件与冻结输入
   的绑定，不证明 MAE 的科学合理性。

## Pending items / 待办（需集群与用户证据）

- [ ] VPN/集群访问恢复后，从已收敛且方法学一致的作业导出
      `computed.csv`；能量来源绑定到 `job.yaml` 和原始结果哈希。
- [ ] 由用户选定可合法使用的文献参考集，录入 `reference.csv`，逐行记录
      DOI、泛函、色散修正、ENCUT、参考态与单位。
- [ ] 运行比较并审查有效分母、异常值与不可比较项；不得只报告一个 MAE。
- [ ] 由具备领域资格的人完成科学审阅后，再把
      `benchmark.render_markdown` 输出并入 `paper/paper.md`。

Until every item above is complete, the repository must not claim a measured
vcstudio adsorption-energy MAE or a completed scientific benchmark.
