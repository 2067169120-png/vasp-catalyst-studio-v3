# 研究工作台领域/工作流设计：官方来源清单

核验日期：2026-08-15。以下只用于架构语义、字段组织和科学限制的交叉核对；本仓库未下载相关数据集，未复制来源代码或图文，也未把这些系统作为运行时依赖。

## Provenance 与领域实体

1. **AiiDA provenance concepts**
   <https://aiida.readthedocs.io/projects/aiida-core/en/stable/topics/provenance/concepts.html>
   一手要点：区分 data provenance（计算如何产生数据）与 logical provenance（为什么按某工作流选择和编排计算）。本实现据此分开 `job.yaml` 执行事实与 WorkflowRecipe 逻辑意图，但不复刻 AiiDA graph/storage。

2. **NOMAD Catalysis plugin 官方文档**
   <https://fairmat-nfdi.github.io/nomad-catalysis-plugin/>
   一手要点：插件以 `CatalystSample` 和 `CatalyticReaction` 作为主要 entry types。本文据此确认催化对象应是领域实体而不是目录名；本实现采用更小的计算研究 DTO，不导入 NOMAD schema/plugin。

3. **NOMAD Catalysis plugin 官方仓库**
   <https://github.com/FAIRmat-NFDI/nomad-catalysis-plugin>
   用于核对项目身份、文档归属和 schema 参考入口；没有复制其代码。

## 版本化工作流与 DAG

4. **atomate2：开发 workflow 的官方指南**
   <https://materialsproject.github.io/atomate2/dev/workflow_tutorial.html>
   一手要点：Maker 持有计算参数，生成由 Job/Flow 组成的工作流；TaskDocument 描述输出 schema。本实现只采用“冻结配方参数 + 显式节点/依赖”的设计原则，不依赖 atomate2。

5. **jobflow Maker / Flow 官方 API**
   <https://materialsproject.github.io/jobflow/jobflow.core.html>
   一手要点：Maker 是 Job/Flow factory，Flow 按 input/output reference 形成 connectivity；Maker update 返回新对象而非就地修改。这支持 recipe version 不可变、override 来源显式以及 preview DAG 的设计。

## 催化反应记录组织

6. **Catalysis-Hub Surface Reactions 官方页面**
   <https://www.catalysis-hub.org/energies>
   一手要点：反应检索显式组织 reactants、products、surface composition、facet，并关联反应结构。

7. **Catalysis-Hub 官方 GraphQL 文档**
   <https://docs.catalysis-hub.org/en/stable/reference/app.html>
   一手要点：reaction record 暴露 reaction energy、activation energy，并能关联 systems/structures。

8. **Catalysis-Hub 官方教程**
   <https://docs.catalysis-hub.org/en/latest/tutorials/index.html>
   一手要点：检索和结果可包含 surface、facet、sites、reactionEnergy、activationEnergy 及关联结构。它只影响本地字段/关系设计；应用不查询或缓存其在线数据。

## 配方科学限制与官方方法页

9. **VASP Wiki — Nudged elastic bands**
   <https://vasp.at/wiki/Nudged_elastic_bands>

10. **VASP Wiki — Improved dimer method**
    <https://vasp.at/wiki/Improved_dimer_method>

11. **VASP Wiki — Phonons from finite differences**
    <https://vasp.at/wiki/Phonons_from_finite_differences>

12. **VASP Wiki — ENCUT**
    <https://vasp.at/wiki/ENCUT>

13. **VASP Wiki — KPOINTS**
    <https://vasp.at/wiki/KPOINTS>

14. **ASE — Surface builders**
    <https://wiki.fysik.dtu.dk/ase/ase/build/surface.html>

15. **ASE — Thermochemistry**
    <https://wiki.fysik.dtu.dk/ase/ase/thermochemistry/thermochemistry.html>

这些页面用于确认 NEB、振动有限差分、ENCUT/k-point 收敛、几何 surface/site 构造及热化学近似的边界。配方参数仍是 VASP Catalyst Studio 自有、显式版本化的建议值；其 `ready` 状态不代表这些值已对具体体系完成科学验证。
