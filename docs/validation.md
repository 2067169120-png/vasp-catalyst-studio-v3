# Validation protocol / 验证基准协议

**Status: pipeline ready, real data pending cluster access.**
管线已就绪(`vcstudio/project/benchmark.py`,TDD 覆盖);真实数据回填需要
集群 VPN,见文末清单。

## Why / 为什么需要这一节

已发刊的同类工具无一例外带定量验证:Montoya & Persson 的吸附能高通量框架
(npj Comput. Mater. 3:14, 2017)对照 CE27 实验化学吸附数据库报告
吸附能 MAE ≈ 0.2 eV、表面能 MAE ≈ 0.02 eV;VASPilot(arXiv:2508.07035)用
MoS₂ 家族带隙/ENCUT 收敛/vdW 晶格常数做体系案例;AutoDFT 用 Materials
Project 子集报告逐性质 MAE。评审判断软件论文的第一问题就是
"工具算出来的数对不对"。

## Protocol / 协议

1. **体系集**:Li-S 单原子催化剂(SAC)吸附能样本——金属掺杂 C-N 骨架上的
   多硫化物(Li₂S, Li₂S₂, Li₂S₄, Li₂S₆, Li₂S₈, S₈)吸附构型,
   `E_ads = E(slab+ads) − E(slab) − E(ref)`(与 README 科学约定一致)。
2. **参考值**:已发表文献的同体系/同泛函吸附能表(RPBE 或 PBE+D3,
   与各自 INCAR 方法学严格对齐后才可比)。
3. **管线**:
   ```python
   from vcstudio.project import benchmark
   computed = benchmark.load_csv('computed.csv')    # 本工具全链算出
   reference = benchmark.load_csv('reference.csv')  # 文献参考值
   result = benchmark.compare_to_reference(computed, reference)
   print(benchmark.render_markdown(result))         # 直接进论文的表格
   ```
   CSV 两列 `name,energy_eV`,`#` 行为注释。无交集会显式抛错——
   绝不静默输出空基准。
4. **报告口径**:逐体系误差 + MAE + n + 未覆盖清单;
   方法学差异(泛函/色散修正/ENCUT)必须在表注中声明。

## Pending items / 待办(需集群/用户)

- [ ] VPN 恢复后,从已收敛作业目录导出 `computed.csv`
      (能量取 `job.yaml` 的 `results.energy_e0_eV`,全链由本工具生成/提交/回收)
- [ ] 选定文献参考数据集并录入 `reference.csv`(注明 DOI 与方法学)
- [ ] 把 `render_markdown` 输出的表格并入 `paper/paper.md` 的 Validation 节
