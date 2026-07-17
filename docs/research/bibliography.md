# DFT 自动化管线调研文献库（重建）

来源：`/tmp/dft-research/results/` 下 wave1～wave6 全部归档调研文件（32 个结构化 JSON + 44 个文本报告）。生成脚本：`/tmp/dft-research/build_bibliography.py`。

## 统计

- 文献总数（按标题模糊去重 + 同 ID 合并后）：**484**
- 有 ID（DOI/arXiv 或其他标识符）：**403**（83.3%），其中严格 DOI/arXiv：**396**
- 有 OA 链接（`[可下载]`）：**316**（65.3%）
- 去重前原始条目数：871（JSON 来源 712 条 + 文本报告提取 159 条）

### 按主题分布

| 分区 | 主题 | 条目数 |
|---|---|---:|
| 一·功能域 | 单原子催化剂/掺杂位点建模:石墨烯/BNC 晶格中 metal@NxCy/PxNy/BxNy 取代位构建、配位环境... | 38 |
| 一·功能域 | 吸附构型生成与位点枚举:多硫化物等分子在表面的摆放、取向采样、初始构型去重 | 12 |
| 一·功能域 | slab/表面模型构建:切面、真空层、固定底层、超胞选取、对称性 | 17 |
| 一·功能域 | 结构弛豫协议:INCAR 参数选择、分阶段弛豫(粗→细)、收敛失败自愈 | 16 |
| 一·功能域 | 静态自洽与精度控制:ENCUT/k网格/smearing 选择与收敛测试协议 | 14 |
| 一·功能域 | 吸附能计算规范:参考态选取、色散校正(D3/D3BJ)、自旋、常见错误 | 34 |
| 一·功能域 | 计算氢电极/计算锂电极自由能路径:ΔG台阶、平衡电位、过电位、决速步(Li-S放电路径为重点) | 17 |
| 一·功能域 | BEP 关系与火山图：描述符选取（吸附能/d带中心/ICOHP）、标度关系拟合、Sabatier 分析（锂硫 SA... | 19 |
| 一·功能域 | Li-S 反应路径设计:缔合/解离途径、中间体(*LiS/*LiS2/LixSy)定义与能量学 —— 锂硫SAC催... | 10 |
| 一·功能域 | CI-NEB 过渡态搜索:插值生成 images、climbing image、能垒提取、虚频验证、Li2S 分解... | 12 |
| 一·功能域 | 频率计算与热校正:IBRION=5/6、ZPE、熵、选择性放开原子、虚频处理 | 23 |
| 一·功能域 | PDOS/d带中心：LORBIT 投影、轨道杂化分析(3d-3p)、d带中心计算与图 | 23 |
| 一·功能域 | Bader 电荷与差分电荷密度:CHGCAR 操作、电荷转移量、等值面图 | 6 |
| 一·功能域 | COHP/ICOHP 键分析：LOBSTER 全流程（输入生成→运行→解析出图） | 12 |
| 一·功能域 | 磁性与自旋态:MAGMOM 初猜、自旋极化、多自旋态搜索 | 11 |
| 一·功能域 | 形成能/结合能/稳定性：SAC 溶出能、聚集倾向、内聚能对比（Li-S SAC 场景） | 16 |
| 一·功能域 | 溶剂化与电场:VASPsol 隐式溶剂、电极电位/电场对电池电催化的影响 | 31 |
| 一·功能域 | AIMD 分子动力学稳定性验证:NVT 热浴、SAC 热稳定性、轨迹分析 | 14 |
| 一·功能域 | 高通量筛选策略:候选空间枚举、漏斗式筛选(粗筛→精算)、描述符先行、批量任务组织(面向VASP催化自动化软件的DF... | 15 |
| 一·功能域 | 工作函数/静电势/ELF/自旋密度等其他电子结构量的计算与用途 | 23 |
| 二·管线产品 | AiiDA + aiida-vasp 工作流框架 | 3 |
| 二·管线产品 | atomate2 + FireWorks/jobflow + custodian（Materials Projec... | 2 |
| 二·管线产品 | pyiron | 4 |
| 二·管线产品 | ASE(Atomic Simulation Environment)生态:核心库 ASE + 任务调度器 MyQu... | 5 |
| 二·管线产品 | pymatgen + Materials Project 基础设施(InputSets 标准化输入集 / Cust... | 2 |
| 二·管线产品 | VASPKIT | 1 |
| 二·管线产品 | JARVIS-Tools (NIST) / AFLOW (Duke) / OQMD-qmpy (Northwest... | 6 |
| 二·管线产品 | CatKit (CatGen/CatFlow) · GASpy · Open Catalyst Project (... | 4 |
| 二·管线产品 | VASPilot / AutoDFT / DREAMS / TritonDFT / MatClaw(LLM·智能体... | 4 |
| 二·管线产品 | Materials Studio (BIOVIA) / MedeA (Materials Design) / Qu... | 7 |
| 二·管线产品 | Li-S电池SAC大规模筛选论文计算管线(群体调研:Nat Commun 2024 ML闭环、JACS Au 20... | 1 |
| 二·管线产品 | 非专家桌面/图形化 DFT 工具类:BURAI、pyiron、AiiDAlab Quantum ESPRESSO ... | 3 |
| 三·文本补充 | wave2 · cp2k | 5 |
| 三·文本补充 | wave2 · engine-abstraction | 1 |
| 三·文本补充 | wave2 · gaussian-orca | 5 |
| 三·文本补充 | wave2 · img2structure | 4 |
| 三·文本补充 | wave2 · multiwfn-vmd | 1 |
| 三·文本补充 | wave2 · starpivot | 2 |
| 三·文本补充 | wave3 · ai-paper2flow | 1 |
| 三·文本补充 | wave3 · candidate-gen | 1 |
| 三·文本补充 | wave3 · figs-battery | 7 |
| 三·文本补充 | wave3 · figs-electronic | 8 |
| 三·文本补充 | wave3 · figs-energetics | 1 |
| 三·文本补充 | wave3 · figs-structure | 1 |
| 三·文本补充 | wave3 · market-software | 18 |
| 三·文本补充 | wave3 · opensource-sci | 5 |
| 三·文本补充 | wave6 · peers-atomagents | 2 |
| 三·文本补充 | wave6 · peers-benchmarks | 3 |
| 三·文本补充 | wave6 · peers-chatdft | 1 |
| 三·文本补充 | wave6 · peers-chatmof | 2 |
| 三·文本补充 | wave6 · peers-chemcrow | 1 |
| 三·文本补充 | wave6 · peers-discover | 4 |
| 三·文本补充 | wave6 · peers-elagente | 2 |
| 三·文本补充 | wave6 · peers-skills-sci | 3 |
| 三·文本补充 | wave6 · peers-vaspilot | 1 |

---

## 一、DFT 功能域调研文献（wave1-a0, a2~a20）

### 单原子催化剂/掺杂位点建模:石墨烯/BNC 晶格中 metal@NxCy/PxNy/BxNy 取代位构建、配位环境枚举、候选空间生成

- **Embedding Transition-Metal Atoms in Graphene: Structure, Bonding, and Magnetism**  (Phys. Rev. Lett.; 2009)  · ID: 10.1103/PhysRevLett.102.126807  · [OA链接](https://users.aalto.fi/~asf/publications/Physical%20Review%20Letters%202009%20Krasheninnikov.pdf) `[可下载]`
- **Catalytic Properties of Transition Metal–N4 Moieties in Graphene for the Oxygen Reduction Reaction: Evidence of Spin-Dependent Mechanisms**  (J. Phys. Chem. C; 2013)  · ID: 10.1021/jp4002115
- **A universal principle for a rational design of single-atom electrocatalysts(已撤稿,作反面教材:结构/数据可靠性)**  (Nat. Catal.(RETRACTED); 2018)  · ID: 10.1038/s41929-018-0063-z
- **Cobalt in Nitrogen-Doped Graphene as Single-Atom Catalyst for High-Sulfur Content Lithium–Sulfur Batteries**  (J. Am. Chem. Soc.; 2019)  · ID: 10.1021/jacs.8b12973  · [OA链接](https://pubmed.ncbi.nlm.nih.gov/30764605/) `[可下载]`
- **Design of Single Atom Catalysts**  (Adv. Phys. X; 2021)  · ID: 10.1080/23746149.2021.1905545  · [OA链接](https://www.tandfonline.com/doi/full/10.1080/23746149.2021.1905545) `[可下载]`
- **Electronic Spin Moment As a Catalytic Descriptor for Fe Single-Atom Catalysts Supported on C2N**  (J. Am. Chem. Soc.; 2021)  · ID: 10.1021/jacs.1c00889
- **Engineering the Coordination Sphere of Isolated Active Sites to Explore the Intrinsic Activity in Single-Atom Catalysts**  (Nano-Micro Lett.; 2021)  · ID: 10.1007/s40820-021-00668-6  · [OA链接](https://link.springer.com/article/10.1007/s40820-021-00668-6) `[可下载]`
- **Machine Learning Derived Blueprint for Rational Design of the Effective Single-Atom Cathode Catalyst of the Lithium–Sulfur Battery**  (J. Phys. Chem. Lett.; 2021)  · ID: 10.1021/acs.jpclett.1c00927
- **Single atom catalysts supported on N-doped graphene toward fast kinetics in Li–S batteries: a theoretical study**  (J. Mater. Chem. A; 2021)  · ID: 10.1039/D1TA01948A  · [OA链接](https://pubs.rsc.org/en/content/articlelanding/2021/ta/d1ta01948a) `[可下载]`
- **Single-Atom Catalysts as Promising Cathode Materials for Lithium–Sulfur Batteries**  (J. Phys. Chem. C; 2021)  · ID: 10.1021/acs.jpcc.1c04491
- **Accelerating the theoretical study of Li-polysulfide adsorption on single-atom catalysts via machine learning approaches**  (Int. J. Quantum Chem.; 2022)  · ID: 10.1002/qua.26956  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC9541244/) `[可下载]`
- **Autonomous high-throughput computations in catalysis**  (Chem Catalysis; 2022)  · ID: 无  · [OA链接](https://www.cell.com/chem-catalysis/fulltext/S2667-1093(22)00104-X) `[可下载]`
- **Computational Screening of Single-Metal-Atom Embedded Graphene-Based Electrocatalysts Stabilized by Heteroatoms**  (Front. Chem.; 2022)  · ID: 10.3389/fchem.2022.873609  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC9019222/) `[可下载]`
- **Influence of Magnetic Moment on Single Atom Catalytic Activation Energy Barriers**  (Catal. Lett.; 2022)  · ID: 10.1007/s10562-021-03737-y  · [OA链接](https://escholarship.org/content/qt0dx502wk/qt0dx502wk.pdf?t=s9ztzq) `[可下载]`
- **Single Atoms Anchored in Hexagonal Boron Nitride for Propane Dehydrogenation (h-BN 载体 SAC 建模实例)**  (ChemCatChem; 2022)  · ID: 无  · [OA链接](https://www.osti.gov/servlets/purl/1881074) `[可下载]`
- **Universal Principles for the Rational Design of Single Atom Electrocatalysts? Handle with Care**  (ACS Catal.; 2022)  · ID: 10.1021/acscatal.2c01011  · [OA链接](https://www.boa.unimib.it/retrieve/20a9681a-01d5-41ca-bcad-d6467a9dd125/10281-377579_VoR.pdf) `[可下载]`
- **High-throughput screening of B/N-doped graphene supported single-atom catalysts for nitrogen reduction reaction**  (Chem. Synth.; 2023)  · ID: 无  · [OA链接](https://www.oaepublish.com/articles/cs.2023.09) `[可下载]`
- **Origin and Acceleration of Insoluble Li2S2−Li2S Reduction Catalysis in Ferromagnetic Atoms-based Lithium-Sulfur Battery Cathodes**  (2023)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC10107143/) `[可下载]`
- **Theoretical Calculations Facilitating Catalysis for Advanced Lithium-Sulfur Batteries(方法综述)**  (2023)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC10647639/) `[可下载]`
- **Theoretical Insights into Single-Atom Catalysts Supported on N-Doped Defective Graphene for Fast Reaction Redox Kinetics in Lithium–Sulfur Batteries**  (Small; 2023)  · ID: 10.1002/smll.202303760
- **Density functional theory methods applied to homogeneous and heterogeneous catalysis: a short review and a practical user guide**  (Phys. Chem. Chem. Phys.; 2024)  · ID: 10.1039/D4CP00266K  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2024/cp/d4cp00266k) `[可下载]`
- **Establishing reaction networks in the 16-electron sulfur reduction reaction**  (Nature; 2024)  · ID: 10.1038/s41586-023-06918-4  · [OA链接](https://escholarship.org/uc/item/7pv676tc) `[可下载]`
- **Predicting the Stability of Single-Atom Catalysts in Electrochemical Reactions**  (ACS Catal.; 2024)  · ID: 10.1021/acscatal.3c04801  · [OA链接](https://pubs.acs.org/doi/pdf/10.1021/acscatal.3c04801) `[可下载]`
- **Rational Design of Non-Noble Metal Single-Atom Catalysts in Lithium–Sulfur Batteries through First Principles Calculations**  (Nanomaterials (MDPI); 2024)  · ID: 10.3390/nano14080692  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC11053660/) `[可下载]`
- **Revealing the mechanism of TM@P1N3 single-atom catalyst in sulfur redox reactions**  (J. Energy Storage; 2024)  · ID: 无
- **Revisiting the universal principle for the rational design of single-atom electrocatalysts**  (Nat. Catal.; 2024)  · ID: 10.1038/s41929-023-01106-z
- **Tailoring coordination environments of single-atom electrocatalysts for hydrogen evolution by topological heteroatom transfer**  (Nat. Commun.; 2024)  · ID: 10.1038/s41467-024-47061-6  · [OA链接](https://www.nature.com/articles/s41467-024-47061-6) `[可下载]`
- **Theoretical Design and Study of a Single-Atom Catalyst in Lithium–Sulfur Batteries: Edge-Type FeN4 Active Site Electron Density Redistribution Driven by Heteroatoms**  (ACS Appl. Mater. Interfaces; 2024)  · ID: 10.1021/acsami.4c09435
- **Theoretical Investigation of Nonmetallic Single-Atom Catalysts for Polysulfide Immobilization and Kinetic Enhancement in Lithium–Sulfur Batteries**  (J. Phys. Chem. C; 2024)  · ID: 10.1021/acs.jpcc.4c00063
- **Theory-driven design of local spin-state modulation at atomically dispersed iron sites for enhanced CO2 electroreduction**  (Nano Energy; 2024)  · ID: 无
- **doped: Python toolkit for robust and repeatable charged defect supercell calculations**  (J. Open Source Softw.; 2024)  · ID: 10.21105/joss.06433  · [OA链接](https://joss.theoj.org/papers/10.21105/joss.06433) `[可下载]`
- **Atomate2: modular workflows for materials science**  (Digital Discovery; 2025)  · ID: 10.1039/D5DD00019J  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2025/dd/d5dd00019j) `[可下载]`
- **Graphene-based single-atom catalysts for electrochemical CO2 reduction: unraveling the roles of metals and dopants in tuning activity**  (Phys. Chem. Chem. Phys.; 2025)  · ID: 10.1039/D4CP04212C  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2025/cp/d4cp04212c) `[可下载]`
- **High-throughput screening of single-atom catalysts on defective graphene for advanced lithium-sulfur battery cathodes**  (J. Colloid Interface Sci.; 2025)  · ID: PII:S0021979725027110
- **P doping adjusts the coordination structure of single atoms to accelerate the catalytic bidirectional redox reaction in Li–S batteries**  (Rare Metals; 2025)  · ID: 10.1007/s12598-025-03233-x
- **Theoretical High-Throughput Screening of Single-Atom CO2 Electroreduction Catalysts to Methanol Using Active Learning**  (Engineering; 2025)  · ID: 无  · [OA链接](https://www.sciencedirect.com/science/article/pii/S2095809925004412) `[可下载]`
- **Toward Understanding the Stability of Single-Atom Catalysts in Oxygen Evolution Reactions**  (Adv. Energy Mater.; 2025)  · ID: 10.1002/aenm.202500635
- **AutoCat: Tools for automated structure generation of catalyst systems(开源工具)**  (GitHub/文档; 年份未知)  · ID: 无  · [OA链接](https://github.com/aced-differentiate/auto_cat) `[可下载]`

### 吸附构型生成与位点枚举:多硫化物等分子在表面的摆放、取向采样、初始构型去重

- **A high-throughput framework for determining adsorption energies on solid surfaces**  (npj Computational Materials; 2017)  · ID: 10.1038/s41524-017-0017-z  · [OA链接](https://www.nature.com/articles/s41524-017-0017-z) `[可下载]`
- **Efficient global structure optimization with a machine learned surrogate model (GOFEE)**  (Physical Review Letters; 2019)  · ID: arXiv:1907.05741  · [OA链接](https://arxiv.org/abs/1907.05741) `[可下载]`
- **Graph Theory Approach to High-Throughput Surface Adsorption Structure Generation (CatKit)**  (Journal of Physical Chemistry A; 2019)  · ID: 10.1021/acs.jpca.9b00311  · [OA链接](https://chemrxiv.org/engage/chemrxiv/article-details/60c73f5af96a000ca128608b) `[可下载]`
- **Graph theory approach to determine configurations of multidentate and high coverage adsorbates for heterogeneous catalysis**  (npj Computational Materials; 2020)  · ID: 10.1038/s41524-020-0345-2  · [OA链接](https://www.nature.com/articles/s41524-020-0345-2) `[可下载]`
- **DockOnSurf: A Python Code for the High-Throughput Screening of Flexible Molecules Adsorbed on Surfaces**  (Journal of Chemical Information and Modeling; 2021)  · ID: 10.1021/acs.jcim.1c00256  · [OA链接](https://hal.science/hal-03377107) `[可下载]`
- **The Open Catalyst 2020 (OC20) Dataset and Community Challenges**  (ACS Catalysis; 2021)  · ID: 10.1021/acscatal.0c04525  · [OA链接](https://arxiv.org/abs/2010.09990) `[可下载]`
- **Fast identification and construction of adsorbate-adsorbent geometries for high throughput computational applications: The Automatic Surface Adsorbate Structure Provider (ASAP) algorithm**  (Computational and Theoretical Chemistry; 2022)  · ID: 无
- **AdsorbML: a leap in efficiency for adsorption energy calculations using generalizable machine learning potentials**  (npj Computational Materials; 2023)  · ID: 10.1038/s41524-023-01121-5  · [OA链接](https://www.nature.com/articles/s41524-023-01121-5) `[可下载]`
- **Machine-learning driven global optimization of surface adsorbate geometries**  (npj Computational Materials; 2023)  · ID: 10.1038/s41524-023-01065-w  · [OA链接](https://www.nature.com/articles/s41524-023-01065-w) `[可下载]`
- **AdsorbDiff: Adsorbate Placement via Conditional Denoising Diffusion**  (arXiv/ICML; 2024)  · ID: arXiv:2405.03962  · [OA链接](https://arxiv.org/abs/2405.03962) `[可下载]`
- **Conformational Preference of Lithium Polysulfide Clusters Li2Sx (x = 4–8) in Lithium–Sulfur Batteries**  (Inorganic Chemistry; 2024)  · ID: 10.1021/acs.inorgchem.3c04537
- **Adsorb-Agent: autonomous identification of stable adsorption configurations via a large language model agent**  (Digital Discovery; 2026)  · ID: 10.1039/D5DD00298B  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2026/dd/d5dd00298b) `[可下载]`

### slab/表面模型构建:切面、真空层、固定底层、超胞选取、对称性

- **The stability of ionic crystal surfaces**  (J. Phys. C: Solid State Physics; 1979)  · ID: 10.1088/0022-3719/12/22/036
- **Extracting convergent surface energies from slab calculations**  (J. Phys.: Condens. Matter; 1996)  · ID: 10.1088/0953-8984/8/36/005  · [OA链接](https://arxiv.org/abs/cond-mat/9610046) `[可下载]`
- **Dipole correction for surface supercell calculations**  (Physical Review B; 1999)  · ID: 10.1103/PhysRevB.59.12301
- **Equivalence of dipole correction and Coulomb cutoff techniques in supercell calculations**  (Physical Review B; 2008)  · ID: 10.1103/PhysRevB.77.245102
- **Efficient creation and convergence of surface slabs**  (Surface Science; 2013)  · ID: 10.1016/j.susc.2013.05.016  · [OA链接](https://ceder.berkeley.edu/publications/2013_Wenhao_Sun_Surface_Slabs.pdf) `[可下载]`
- **Implications of coverage-dependent O adsorption for catalytic NO oxidation on the late transition metals**  (Catalysis Science & Technology; 2014)  · ID: 10.1039/C4CY00763H  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2014/cy/c4cy00763h) `[可下载]`
- **Categorization of surface polarity from a crystallographic approach**  (Computational Materials Science; 2015)  · ID: arXiv:1508.07194  · [OA链接](https://arxiv.org/pdf/1508.07194) `[可下载]`
- **MPInterfaces: A Materials Project based Python Tool for High-Throughput Computational Screening of Interfacial Systems**  (Computational Materials Science; 2016)  · ID: arXiv:1602.07784  · [OA链接](https://arxiv.org/abs/1602.07784) `[可下载]`
- **Surface energies of elemental crystals**  (Scientific Data; 2016)  · ID: 10.1038/sdata.2016.80  · [OA链接](https://www.nature.com/articles/sdata201680) `[可下载]`
- **The atomic simulation environment — a Python library for working with atoms**  (J. Phys.: Condens. Matter; 2017)  · ID: 10.1088/1361-648X/aa680e  · [OA链接](https://iopscience.iop.org/article/10.1088/1361-648X/aa680e) `[可下载]`
- **Spglib: a software library for crystal symmetry search**  (arXiv/Sci. Technol. Adv. Mater. Methods; 2018)  · ID: arXiv:1808.01590  · [OA链接](https://arxiv.org/abs/1808.01590) `[可下载]`
- **Finite-size correction for slab supercell calculations of materials with spontaneous polarization**  (npj Computational Materials; 2021)  · ID: 10.1038/s41524-021-00529-1  · [OA链接](https://www.nature.com/articles/s41524-021-00529-1) `[可下载]`
- **Single-Atom Catalysts for Improved Cathode Performance in Na–S Batteries: A Density Functional Theory (DFT) Study**  (J. Phys. Chem. C; 2021)  · ID: 10.1021/acs.jpcc.1c00467
- **A method of calculating surface energies for asymmetric slab models**  (Phys. Chem. Chem. Phys.; 2023)  · ID: 10.1039/D2CP04460A
- **Understanding the charge transfer effects of single atoms for boosting the performance of Na-S batteries**  (Nature Communications; 2024)  · ID: 10.1038/s41467-024-47628-3  · [OA链接](https://www.nature.com/articles/s41467-024-47628-3) `[可下载]`
- **Adsorption Mechanisms of Lithium Polysulfides on Graphene-Based Interlayers in Lithium Sulfur Batteries**  (ACS Applied Energy Materials; 年份未知)  · ID: 10.1021/acsaem.7b00096
- **Theoretical Calculations Facilitating Catalysis for Advanced Lithium-Sulfur Batteries (综述)**  (年份未知)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC10647639/) `[可下载]`

### 结构弛豫协议:INCAR 参数选择、分阶段弛豫(粗→细)、收敛失败自愈

- **Structural Relaxation Made Simple (FIRE算法)**  (Physical Review Letters; 2006)  · ID: 10.1103/PhysRevLett.97.170201  · [OA链接](https://www.math.uni-bielefeld.de/~gaehler/papers/fire.pdf) `[可下载]`
- **A high-throughput infrastructure for density functional theory calculations**  (Computational Materials Science; 2011)  · ID: 无  · [OA链接](https://perssongroup.lbl.gov/papers/compmatsci2011-infrastructure.pdf) `[可下载]`
- **Python Materials Genomics (pymatgen): A robust, open-source python library for materials analysis**  (Computational Materials Science; 2013)  · ID: 10.1016/j.commatsci.2012.10.028  · [OA链接](https://ceder.berkeley.edu/publications/2012_Python_materials_genomics.pdf) `[可下载]`
- **Python Materials Genomics (pymatgen): A robust, open-source python library for materials analysis(custodian官方引用文献)**  (Computational Materials Science; 2013)  · ID: 无  · [OA链接](https://ceder.berkeley.edu/publications/2012_Python_materials_genomics.pdf) `[可下载]`
- **Atomate: A high-level interface to generate, execute, and analyze computational materials science workflows**  (Computational Materials Science; 2017)  · ID: 10.1016/j.commatsci.2017.07.030  · [OA链接](https://escholarship.org/uc/item/32w5w7tm) `[可下载]`
- **Active learning across intermetallics to guide discovery of electrocatalysts for CO2 reduction and H2 evolution (GASpy自动吸附工作流)**  (Nature Catalysis; 2018)  · ID: 10.1038/s41929-018-0142-1
- **High-throughput prediction of the ground-state collinear magnetic order of inorganic materials using Density Functional Theory**  (npj Computational Materials; 2019)  · ID: 10.1038/s41524-019-0199-7  · [OA链接](https://www.nature.com/articles/s41524-019-0199-7) `[可下载]`
- **Common workflows for computing material properties using different quantum engines**  (npj Computational Materials; 2021)  · ID: 10.1038/s41524-021-00594-6  · [OA链接](https://www.nature.com/articles/s41524-021-00594-6) `[可下载]`
- **A universal graph deep learning interatomic potential for the periodic table (M3GNet)**  (Nature Computational Science; 2022)  · ID: 10.1038/s43588-022-00349-3
- **Numerical quality control for DFT-based materials databases**  (npj Computational Materials; 2022)  · ID: 10.1038/s41524-022-00744-4  · [OA链接](https://www.nature.com/articles/s41524-022-00744-4) `[可下载]`
- **Performance comparison of r2SCAN and SCAN metaGGA density functionals for solid materials via an automated, high-throughput computational workflow**  (Physical Review Materials; 2022)  · ID: 10.1103/PhysRevMaterials.6.013801  · [OA链接](https://escholarship.org/uc/item/0xw1799d) `[可下载]`
- **CHGNet as a pretrained universal neural network potential for charge-informed atomistic modelling**  (Nature Machine Intelligence; 2023)  · ID: 10.1038/s42256-023-00716-3  · [OA链接](https://arxiv.org/abs/2302.14231) `[可下载]`
- **Geometry Optimization: A Comparison of Different Open-Source Geometry Optimizers**  (Journal of Chemical Theory and Computation; 2023)  · ID: 10.1021/acs.jctc.3c00188  · [OA链接](https://chemrxiv.org/doi/pdf/10.26434/chemrxiv-2023-7r7qn-v2) `[可下载]`
- **How to verify the precision of density-functional-theory implementations via reproducible and universal workflows**  (Nature Reviews Physics; 2024)  · ID: 10.1038/s42254-023-00655-3  · [OA链接](https://arxiv.org/abs/2305.17274) `[可下载]`
- **Single Transition Metal Atom Catalyst for a High-Performance Li–S Battery with a Graphdiyne–Graphene Heterostructure Host: A DFT Investigation + ML Predictions**  (ACS Catalysis; 2024)  · ID: 10.1021/acscatal.4c02066
- **VASPilot: MCP-Facilitated Multi-Agent Intelligence for Autonomous VASP Calculations**  (arXiv; 2025)  · ID: arXiv:2508.07035  · [OA链接](https://arxiv.org/pdf/2508.07035) `[可下载]`

### 静态自洽与精度控制:ENCUT/k网格/smearing 选择与收敛测试协议

- **Special points for Brillouin-zone integrations**  (Physical Review B; 1976)  · ID: 10.1103/PhysRevB.13.5188
- **High-precision sampling for Brillouin-zone integration in metals**  (Physical Review B; 1989)  · ID: 10.1103/PhysRevB.40.3616
- **Improved tetrahedron method for Brillouin-zone integrations**  (Physical Review B; 1994)  · ID: 10.1103/PhysRevB.49.16223
- **Efficient iterative schemes for ab initio total-energy calculations using a plane-wave basis set**  (Physical Review B; 1996)  · ID: 10.1103/PhysRevB.54.11169
- **Commentary: The Materials Project: A materials genome approach to accelerating materials innovation**  (APL Materials; 2013)  · ID: 10.1063/1.4812323  · [OA链接](https://escholarship.org/uc/item/3h26p692) `[可下载]`
- **Reproducibility in density functional theory calculations of solids**  (Science; 2016)  · ID: 10.1126/science.aad3000  · [OA链接](https://backend.orbit.dtu.dk/ws/files/143898745/Lejaeghere_et_al._Reproducibility_in_density_functional_theory_calculations_of_solids.pdf) `[可下载]`
- **Precision and efficiency in solid-state pseudopotential calculations**  (npj Computational Materials; 2018)  · ID: 10.1038/s41524-018-0127-2  · [OA链接](https://www.nature.com/articles/s41524-018-0127-2) `[可下载]`
- **Convergence and machine learning predictions of Monkhorst-Pack k-points and plane-wave cut-off in high-throughput DFT calculations**  (Computational Materials Science; 2019)  · ID: arXiv:1809.01753  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC7066999/) `[可下载]`
- **AMP2: A fully automated program for ab initio calculations of crystalline materials**  (2020)  · ID: 无
- **Insights into the activity of single-atom Fe-N-C catalysts for oxygen reduction reaction**  (Nature Communications; 2022)  · ID: 10.1038/s41467-022-29797-1  · [OA链接](https://www.nature.com/articles/s41467-022-29797-1) `[可下载]`
- **Fermi energy determination for advanced smearing techniques**  (Physical Review B; 2023)  · ID: 10.1103/PhysRevB.107.195122  · [OA链接](https://arxiv.org/abs/2212.07988) `[可下载]`
- **Promising single-atom catalysts for lithium-sulfur batteries screened by theoretical density functional theory calculations**  (Science China Materials; 2023)  · ID: 10.1007/s40843-023-2585-1
- **Automated optimization and uncertainty quantification of convergence parameters in plane wave density functional theory calculations**  (npj Computational Materials; 2024)  · ID: 10.1038/s41524-024-01388-2  · [OA链接](https://www.nature.com/articles/s41524-024-01388-2) `[可下载]`
- **Tuning Transition Metal 3d Spin state on Single-atom Catalysts for Selective Electrochemical CO2 Reduction**  (Advanced Materials; 2025)  · ID: 10.1002/adma.202417034

### 吸附能计算规范:参考态选取、色散校正(D3/D3BJ)、自旋、常见错误

- **Improved adsorption energetics within density-functional theory using revised Perdew-Burke-Ernzerhof functionals**  (Physical Review B; 1999)  · ID: 10.1103/PhysRevB.59.7413  · [OA链接](https://orbit.dtu.dk/files/4114802/Hammer.pdf) `[可下载]`
- **Origin of the Overpotential for Oxygen Reduction at a Fuel-Cell Cathode**  (Journal of Physical Chemistry B; 2004)  · ID: 10.1021/jp047349j  · [OA链接](https://home.sato-gallery.com/busseiqanda/Origin_of_the_Overpotential_for_Oxygen_Reduction_a.pdf) `[可下载]`
- **A consistent and accurate ab initio parametrization of density functional dispersion correction (DFT-D) for the 94 elements H-Pu**  (Journal of Chemical Physics; 2010)  · ID: 10.1063/1.3382344  · [OA链接](https://pubs.aip.org/aip/jcp/article-pdf/doi/10.1063/1.3382344/15684000/154104_1_online.pdf) `[可下载]`
- **Accurate surface and adsorption energies from many-body perturbation theory**  (Nature Materials; 2010)  · ID: 10.1038/nmat2806
- **Effect of the damping function in dispersion corrected density functional theory**  (Journal of Computational Chemistry; 2011)  · ID: 10.1002/jcc.21759
- **A benchmark database for adsorption bond energies to transition metal surfaces and comparison to selected DFT functionals**  (Surface Science; 2015)  · ID: 无
- **Understanding the Anchoring Effect of Two-Dimensional Layered Materials for Lithium–Sulfur Batteries**  (Nano Letters; 2015)  · ID: 10.1021/acs.nanolett.5b00367
- **Implicit self-consistent electrolyte model in plane-wave density-functional theory (VASPsol)**  (J. Chem. Phys.; 2016)  · ID: arXiv:1601.03346  · [OA链接](https://arxiv.org/pdf/1601.03346) `[可下载]`
- **Revised Damping Parameters for the D3 Dispersion Correction to Density Functional Theory**  (Journal of Physical Chemistry Letters; 2016)  · ID: 10.1021/acs.jpclett.6b00780
- **DFT-Based Method for More Accurate Adsorption Energies: An Adaptive Sum of Energies from RPBE and vdW Density Functionals**  (Journal of Physical Chemistry C; 2017)  · ID: 10.1021/acs.jpcc.6b10187
- **Validation of Density Functionals for Adsorption Energies on Transition Metal Surfaces**  (Journal of Chemical Theory and Computation; 2017)  · ID: 10.1021/acs.jctc.6b01156  · [OA链接](https://www.osti.gov/servlets/purl/1388270) `[可下载]`
- **Improved DFT Adsorption Energies with Semiempirical Dispersion Corrections**  (Journal of Chemical Theory and Computation; 2019)  · ID: 10.1021/acs.jctc.9b00035
- **A Semiempirical Method to Detect and Correct DFT-Based Gas-Phase Errors and Its Application in Electrocatalysis**  (ACS Catalysis; 2020)  · ID: 10.1021/acscatal.0c01075  · [OA链接](https://pubs.acs.org/doi/10.1021/acscatal.0c01075) `[可下载]`
- **Generalized dipole correction for charged surfaces in the repeated-slab approach**  (Physical Review B; 2020)  · ID: 10.1103/PhysRevB.102.045403
- **Fast Correction of Errors in the DFT-Calculated Energies of Gaseous Nitrogen-Containing Species**  (ChemCatChem; 2021)  · ID: 10.1002/cctc.202100125
- **Importance of the gas-phase error correction for O2 when using DFT to model the oxygen reduction and evolution reactions**  (Journal of Electroanalytical Chemistry; 2021)  · ID: 无
- **Insight into the Anchoring Effect of Two-Dimensional TiX2 (X = S, Se, Te) Materials for Lithium-Sulfur Batteries: A DFT Study**  (Journal of The Electrochemical Society; 2021)  · ID: 10.1149/1945-7111/ac3ab2  · [OA链接](https://iopscience.iop.org/article/10.1149/1945-7111/ac3ab2) `[可下载]`
- **Regulating Fe-spin state by atomically dispersed Mn-N in Fe-N-C catalysts with high oxygen reduction activity**  (Nature Communications; 2021)  · ID: 10.1038/s41467-021-21919-5  · [OA链接](https://www.nature.com/articles/s41467-021-21919-5) `[可下载]`
- **A review on theoretical models for lithium–sulfur battery cathodes**  (InfoMat; 2022)  · ID: 10.1002/inf2.12304  · [OA链接](https://onlinelibrary.wiley.com/doi/10.1002/inf2.12304) `[可下载]`
- **Adsorption energies on transition metal surfaces: towards an accurate and balanced description**  (Nature Communications; 2022)  · ID: 10.1038/s41467-022-34507-y  · [OA链接](https://www.nature.com/articles/s41467-022-34507-y) `[可下载]`
- **Gas-Phase Errors Affect DFT-Based Electrocatalysis Models of Oxygen Reduction to Hydrogen Peroxide**  (ChemElectroChem; 2022)  · ID: 10.1002/celc.202200210
- **Two-dimensional biphenylene: a promising anchoring material for lithium-sulfur batteries**  (Scientific Reports; 2022)  · ID: 10.1038/s41598-022-08478-5  · [OA链接](https://www.nature.com/articles/s41598-022-08478-5) `[可下载]`
- **Spin Effects in Chemisorption and Catalysis**  (ACS Catalysis; 2023)  · ID: 10.1021/acscatal.2c06319
- **Theoretical Calculations Facilitating Catalysis for Advanced Lithium-Sulfur Batteries**  (Molecules; 2023)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC10647639/) `[可下载]`
- **Gas-phase errors in computational electrocatalysis: a review**  (EES Catalysis; 2024)  · ID: 10.1039/D3EY00126A  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2024/ey/d3ey00126a) `[可下载]`
- **W/V Dual-Atom Doping MoS2-Mediated Phase Transition for Efficient Polysulfide Adsorption/Conversion Kinetics in Lithium–Sulfur Battery**  (Nano-Micro Letters; 2025)  · ID: 10.1007/s40820-025-01957-0  · [OA链接](https://link.springer.com/article/10.1007/s40820-025-01957-0) `[可下载]`
- **Assessing van der Waals Corrections in the Description of Water Adsorption and Diffusion on Graphene and Hexagonal Boron Nitride**  (ACS Omega; 年份未知)  · ID: 10.1021/acsomega.6c02762
- **Extent of Spin Contamination Errors in DFT/Plane-wave Calculation of Surfaces: A Case of Au Atom Aggregation on a MgO Surface**  (年份未知)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC6385026/) `[可下载]`
- **Failure of Density Functional Dispersion Correction in Metallic Systems and Its Possible Solution Using a Modified Many-Body Dispersion Correction**  (年份未知)  · ID: 无
- **FeN4 Environments upon Reduction: A Computational Analysis of Spin States, Spectroscopic Properties, and Active Species**  (JACS Au; 年份未知)  · ID: 10.1021/jacsau.3c00714  · [OA链接](https://pubs.acs.org/doi/10.1021/jacsau.3c00714) `[可下载]`
- **Impact of Intrinsic Density Functional Theory Errors on the Predictive Power of Nitrogen Cycle Electrocatalysis Models**  (ACS Catalysis; 年份未知)  · ID: 10.1021/acscatal.1c05333
- **Reaction Pathway for Oxygen Reduction on FeN4 Embedded Graphene**  (Journal of Physical Chemistry Letters; 年份未知)  · ID: 10.1021/jz402717r
- **VASP Wiki: IVDW / DFT-D3(官方实现文档)**  (VASP Wiki; 年份未知)  · ID: 无  · [OA链接](https://vasp.at/wiki/IVDW) `[可下载]`
- **VASP Wiki: Smearing technique / ISMEAR(官方文档,分子与金属 smearing 规范)**  (VASP Wiki; 年份未知)  · ID: 无  · [OA链接](https://vasp.at/wiki/Smearing_technique) `[可下载]`

### 计算氢电极/计算锂电极自由能路径:ΔG台阶、平衡电位、过电位、决速步(Li-S放电路径为重点)

- **A climbing image nudged elastic band method for finding saddle points and minimum energy paths**  (J. Chem. Phys.; 2000)  · ID: 10.1063/1.1329672  · [OA链接](https://www.researchgate.net/publication/224877263_A_Climbing_Image_Nudged_Elastic_Band_Method_for_Finding_Saddle_Points_and_Minimum_Energy_Paths) `[可下载]`
- **Communications: Elementary oxygen electrode reactions in the aprotic Li-air battery**  (J. Chem. Phys.; 2010)  · ID: 无  · [OA链接](https://www.researchgate.net/publication/41506994_Communications_Elementary_oxygen_electrode_reactions_in_the_aprotic_Li-air_battery) `[可下载]`
- **How copper catalyzes the electroreduction of carbon dioxide into hydrocarbon fuels**  (Energy Environ. Sci.; 2010)  · ID: 10.1039/C0EE00071J  · [OA链接](https://citeseerx.ist.psu.edu/document?repid=rep1&type=pdf&doi=04f3c52fc30c89718897bf1dea73f3f2b9794dc9) `[可下载]`
- **Implicit solvation model for density-functional study of nanocrystal surfaces and reaction pathways**  (J. Chem. Phys.; 2014)  · ID: 10.1063/1.4865107  · [OA链接](https://www.osti.gov/servlets/purl/1383729) `[可下载]`
- **Catalytic oxidation of Li2S on the surface of metal sulfides for Li−S batteries**  (PNAS; 2017)  · ID: 10.1073/pnas.1615837114  · [OA链接](https://web.stanford.edu/group/cui_group/papers/Guangmin_Cui_PNAS_2017.pdf) `[可下载]`
- **A fundamental look at electrocatalytic sulfur reduction reaction**  (Nat. Catal.; 2020)  · ID: 10.1038/s41929-020-0498-x  · [OA链接](https://www.nature.com/articles/s41929-020-0498-x) `[可下载]`
- **Electrochemistry from first-principles in the grand canonical ensemble**  (J. Chem. Phys.; 2021)  · ID: 无  · [OA链接](https://eprints.soton.ac.uk/451045/1/GC_EDFT_2021_06_21.pdf) `[可下载]`
- **VASPKIT: A user-friendly interface facilitating high-throughput computing and analysis using VASP code**  (Comput. Phys. Commun.; 2021)  · ID: arXiv:1908.08269  · [OA链接](https://ar5iv.labs.arxiv.org/html/1908.08269) `[可下载]`
- **Cation-doped ZnS catalysts for polysulfide conversion in lithium–sulfur batteries**  (Nat. Catal.; 2022)  · ID: 10.1038/s41929-022-00804-4
- **Materials Screening by the Descriptor Gmax(η): The Free-Energy Span Model in Electrocatalysis**  (ACS Catal.; 2022)  · ID: 10.1021/acscatal.2c03997
- **Constant inner potential DFT for modelling electrochemical systems under constant potential and bias**  (npj Comput. Mater.; 2023)  · ID: 10.1038/s41524-023-01184-4  · [OA链接](https://www.nature.com/articles/s41524-023-01184-4) `[可下载]`
- **High-Throughput Screening of Sulfur Reduction Reaction Catalysts Utilizing Electronic Fingerprint Similarity**  (JACS Au; 2023)  · ID: 10.1021/jacsau.3c00710  · [OA链接](https://pubs.acs.org/doi/10.1021/jacsau.3c00710) `[可下载]`
- **DFT Equilibrium Thermodynamics of Lithium Polysulfides Transformations in Sulfolane Solution**  (Int. J. Quantum Chem.; 2025)  · ID: 10.1002/qua.70113
- **Reactivity descriptors for sulfur redox kinetics in lithium–sulfur batteries**  (Chem. Soc. Rev.; 2025)  · ID: 10.1039/d5cs00324e  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2025/cs/d5cs00324e) `[可下载]`
- **An Effective Single-Atom Catalytic Descriptor for Accelerating Sulfur Reduction Reaction in Lithium-Sulfur Batteries**  (Adv. Mater.; 2026)  · ID: 10.1002/adma.202515380
- **A Universal Approach To Determine the Free Energy Diagram of an Electrocatalytic Reaction**  (ACS Catal.; 年份未知)  · ID: 10.1021/acscatal.7b03142
- **Converging Divergent Paths: Constant Charge vs Constant Potential Energetics in Computational Electrochemistry**  (J. Phys. Chem. C; 年份未知)  · ID: 10.1021/acs.jpcc.3c07954

### BEP 关系与火山图：描述符选取（吸附能/d带中心/ICOHP）、标度关系拟合、Sabatier 分析（锂硫 SAC/SRR 场景）

- **The Brønsted–Evans–Polanyi relation and the volcano curve in heterogeneous catalysis**  (J. Catal.; 2004)  · ID: 无
- **A fast and robust algorithm for Bader decomposition of charge density**  (Comput. Mater. Sci.; 2006)  · ID: 无  · [OA链接](https://theory.cm.utexas.edu/henkelman/code/bader/download/henkelman06_354.pdf) `[可下载]`
- **Scaling Properties of Adsorption Energies for Hydrogen-Containing Molecules on Transition-Metal Surfaces**  (Phys. Rev. Lett.; 2007)  · ID: 10.1103/PhysRevLett.99.016105
- **Brønsted−Evans−Polanyi Relation of Multistep Reactions and Volcano Curve in Heterogeneous Catalysis**  (J. Phys. Chem. C; 2008)  · ID: 10.1021/jp711191j  · [OA链接](https://pubs.acs.org/doi/pdf/10.1021/jp711191j) `[可下载]`
- **Scaling Relationships for Adsorption Energies on Transition Metal Oxide, Sulfide, and Nitride Surfaces**  (Angew. Chem. Int. Ed.; 2008)  · ID: 10.1002/anie.200705739
- **Crystal Orbital Hamilton Population (COHP) Analysis As Projected from Plane-Wave Basis Sets**  (J. Phys. Chem. A; 2011)  · ID: 10.1021/jp202489s
- **CatMAP: A Software Package for Descriptor-Based Microkinetic Mapping of Catalytic Trends**  (Catal. Lett.; 2015)  · ID: 10.1007/s10562-015-1495-6  · [OA链接](https://link.springer.com/article/10.1007/s10562-015-1495-6) `[可下载]`
- **An improved d-band model of the catalytic activity of magnetic transition metal surfaces**  (Scientific Reports; 2016)  · ID: arXiv:1610.01746  · [OA链接](https://arxiv.org/pdf/1610.01746) `[可下载]`
- **Combining theory and experiment in electrocatalysis: Insights into materials design**  (Science; 2017)  · ID: 10.1126/science.aad4998  · [OA链接](https://backend.orbit.dtu.dk/ws/files/131069434/aad4998_Review_Article_Manuscript.pdf) `[可下载]`
- **The Genesis of Molecular Volcano Plots**  (Acc. Chem. Res.; 2021)  · ID: 10.1021/acs.accounts.0c00857  · [OA链接](https://infoscience.epfl.ch/server/api/core/bitstreams/7902d27c-d29f-47e8-93a2-2f64ad508666/content) `[可下载]`
- **The Sabatier Principle in Electrocatalysis: Basics, Limitations, and Extensions**  (Front. Energy Res.; 2021)  · ID: 10.3389/fenrg.2021.654460  · [OA链接](https://www.frontiersin.org/journals/energy-research/articles/10.3389/fenrg.2021.654460/full) `[可下载]`
- **An Electrocatalytic Model of the Sulfur Reduction Reaction in Lithium–Sulfur Batteries**  (Angew. Chem. Int. Ed.; 2022)  · ID: 10.1002/anie.202211448
- **Automated Bonding Analysis with Crystal Orbital Hamilton Populations (LobsterPy)**  (ChemPlusChem; 2022)  · ID: 10.1002/cplu.202200123  · [OA链接](https://chemrxiv.org/engage/api-gateway/chemrxiv/assets/orp/resource/item/6234a73c8ab3730425690991/original/automated-bonding-analysis-with-crystal-orbital-hamilton-populations.pdf) `[可下载]`
- **Constructing and interpreting volcano plots and activity maps to navigate homogeneous catalyst landscapes**  (Nat. Protoc.; 2022)  · ID: 10.1038/s41596-022-00726-2
- **A Volcano Correlation between Catalytic Activity for Sulfur Reduction Reaction and Fe Atom Count in Metal Center**  (J. Am. Chem. Soc.; 2024)  · ID: 10.1021/jacs.3c14312
- **Electrocatalytic Mechanism and Sabatier Principle in C2N-Supported Atomically Dispersed Catalysts for the Sulfur Reduction Reaction in Lithium–Sulfur Batteries**  (J. Phys. Chem. Lett.; 2024)  · ID: 10.1021/acs.jpclett.4c00474
- **Microkinetic Molecular Volcano Plots for Enhanced Catalyst Selectivity and Activity Predictions**  (ACS Catal.; 2024)  · ID: 10.1021/acscatal.4c01175  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC11232097/) `[可下载]`
- **Twenty years after: scaling relations in oxygen electrocatalysis and beyond**  (Chem. Soc. Rev.; 2025)  · ID: 10.1039/D5CS00597C  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2025/cs/d5cs00597c) `[可下载]`
- **Understanding the Electrocatalytic Trend of Sulfur Reduction Reaction and Design Rules of Advanced Electrocatalysts for Li–S Batteries**  (ACS Nano; 2025)  · ID: 10.1021/acsnano.5c05200

### Li-S 反应路径设计:缔合/解离途径、中间体(*LiS/*LiS2/LixSy)定义与能量学 —— 锂硫SAC催化DFT全通量管线

- **LOBSTER: A tool to extract chemical bonding from plane-wave based DFT**  (J. Comput. Chem.; 2020)  · ID: 10.1002/jcc.24300  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC5067632/) `[可下载]`
- **Theoretical Calculation Guided Design of Single-Atom Catalysts toward Fast Kinetic and Long-Life Li–S Batteries**  (Nano Letters; 2020)  · ID: 10.1021/acs.nanolett.9b04719  · [OA链接](https://web.stanford.edu/group/cui_group/papers/Guangmin_Cui_NANOLETT_2019.pdf) `[可下载]`
- **Universal-Descriptors-Guided Design of Single Atom Catalysts toward Oxidation of Li2S in Lithium–Sulfur Batteries**  (Advanced Science; 2021)  · ID: 10.1002/advs.202102809  · [OA链接](https://advanced.onlinelibrary.wiley.com/doi/10.1002/advs.202102809) `[可下载]`
- **Uncovering electrocatalytic conversion mechanisms from Li2S2 to Li2S: Generalization of computational hydrogen electrode**  (Energy Storage Materials; 2022)  · ID: 无  · [OA链接](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3971637) `[可下载]`
- **Identification and Catalysis of the Potential-Limiting Step in Lithium-Sulfur Batteries**  (J. Am. Chem. Soc.; 2023)  · ID: 10.1021/jacs.2c13776
- **Single-atom electrocatalysts for lithium–sulfur chemistry: Design principle, mechanism, and outlook**  (Carbon Energy; 2023)  · ID: 10.1002/cey2.286  · [OA链接](https://onlinelibrary.wiley.com/doi/full/10.1002/cey2.286) `[可下载]`
- **DFT and AIMD studies on the conversion and decomposition of Li2S2 to Li2S on 2D-FeS2**  (Computational Materials Science; 2024)  · ID: 无
- **Machine learning-based design of electrocatalytic materials towards high-energy lithium||sulfur batteries development**  (Nature Communications; 2024)  · ID: 10.1038/s41467-024-52550-9  · [OA链接](https://www.nature.com/articles/s41467-024-52550-9) `[可下载]`
- **Revisiting the unified principle for single-atom electrocatalysts in the sulfur reduction reaction: from liquid to solid-state electrolytes**  (Energy & Environmental Science; 2024)  · ID: 10.1039/d4ee01885k
- **Can single-atom precision rewire the electrochemical logic of Li–S chemistry? A comprehensive review of single-atom catalysts**  (Chemical Science; 2025)  · ID: 10.1039/D5SC05720E  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2025/sc/d5sc05720e) `[可下载]`

### CI-NEB 过渡态搜索:插值生成 images、climbing image、能垒提取、虚频验证、Li2S 分解/脱锂能垒

- **Nudged elastic band method for finding minimum energy paths of transitions**  (in Classical and Quantum Dynamics in Condensed Phase Simulations, World Scientific; 1998)  · ID: 10.1142/9789812839664_0016  · [OA链接](https://theory.cm.utexas.edu/henkelman/pubs/jonsson98_385.pdf) `[可下载]`
- **A dimer method for finding saddle points on high dimensional potential surfaces using only first derivatives**  (The Journal of Chemical Physics; 1999)  · ID: 10.1063/1.480097  · [OA链接](https://henkelmanlab.org/pubs/henkelman99_7010.pdf) `[可下载]`
- **Improved tangent estimate in the nudged elastic band method for finding minimum energy paths and saddle points**  (The Journal of Chemical Physics; 2000)  · ID: 10.1063/1.1323224
- **Comparison of methods for finding saddle points without knowledge of the final states**  (The Journal of Chemical Physics; 2004)  · ID: 无
- **Optimization methods for finding minimum energy paths**  (The Journal of Chemical Physics; 2008)  · ID: 10.1063/1.2841941  · [OA链接](https://theory.cm.utexas.edu/henkelman/pubs/sheppard08_134106.pdf) `[可下载]`
- **A generalized solid-state nudged elastic band method**  (The Journal of Chemical Physics; 2012)  · ID: 10.1063/1.3684549  · [OA链接](https://theory.cm.utexas.edu/henkelman/pubs/sheppard12_074103.pdf) `[可下载]`
- **Improved initial guess for minimum energy path calculations**  (The Journal of Chemical Physics; 2014)  · ID: arXiv:1406.1512  · [OA链接](https://arxiv.org/abs/1406.1512) `[可下载]`
- **An automated nudged elastic band method**  (The Journal of Chemical Physics; 2016)  · ID: 无
- **Nudged elastic band calculations accelerated with Gaussian process regression**  (The Journal of Chemical Physics; 2017)  · ID: arXiv:1706.04606  · [OA链接](https://arxiv.org/abs/1706.04606) `[可下载]`
- **Low-Scaling Algorithm for Nudged Elastic Band Calculations Using a Surrogate Machine Learning Model**  (Physical Review Letters; 2019)  · ID: 10.1103/PhysRevLett.122.156001  · [OA链接](https://www.osti.gov/servlets/purl/1526444) `[可下载]`
- **Isolated Fe-Co heteronuclear diatomic sites as efficient bifunctional catalysts for high-performance lithium-sulfur batteries**  (Nature Communications; 2023)  · ID: 10.1038/s41467-022-35736-x  · [OA链接](https://www.nature.com/articles/s41467-022-35736-x) `[可下载]`
- **Adsorption and Decomposition Mechanisms of Li2S on 2D Th-graphene Modulated by Doping and External Electrical Field**  (Materials; 2025)  · ID: 10.3390/ma18143269  · [OA链接](https://doi.org/10.3390/ma18143269) `[可下载]`

### 频率计算与热校正:IBRION=5/6、ZPE、熵、选择性放开原子、虚频处理

- **Partial Hessian vibrational analysis: The localization of the molecular vibrational energy and entropy**  (Theor. Chem. Acc.; 2002)  · ID: 无
- **Use of Solution-Phase Vibrational Frequencies in Continuum Models for the Free Energy of Solvation**  (J. Phys. Chem. B; 2011)  · ID: 10.1021/jp205508z
- **Supramolecular Binding Thermodynamics by Dispersion-Corrected Density Functional Theory**  (Chemistry – A European Journal; 2012)  · ID: 10.1002/chem.201200497
- **The Entropies of Adsorbed Molecules**  (J. Am. Chem. Soc.; 2012)  · ID: 10.1021/ja3080117
- **Entropies of Adsorbed Molecules Exceed Expectations**  (Science; 2013)  · ID: 10.1126/science.1231552
- **First principles phonon calculations in materials science**  (Scripta Materialia; 2015)  · ID: 10.1016/j.scriptamat.2015.07.021  · [OA链接](https://arxiv.org/pdf/1506.08498) `[可下载]`
- **Hindered Translator and Hindered Rotor Models for Adsorbates: Partition Functions and Entropies**  (J. Phys. Chem. C; 2016)  · ID: 10.1021/acs.jpcc.5b11616
- **Chemically Accurate Vibrational Free Energies of Adsorption from Density Functional Theory Molecular Dynamics: Alkanes in Zeolites**  (J. Chem. Theory Comput.; 2021)  · ID: 10.1021/acs.jctc.1c00519  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC8444336/) `[可下载]`
- **The hindered rotor theory: A review**  (WIREs Computational Molecular Science; 2022)  · ID: 10.1002/wcms.1583
- **Pynta—An Automated Workflow for Calculation of Surface and Gas-Surface Kinetics**  (J. Chem. Inf. Model.; 2023)  · ID: 10.1021/acs.jcim.3c00948  · [OA链接](https://chemrxiv.org/engage/chemrxiv/article-details/647114454f8b1884b755bc5a) `[可下载]`
- **Assessing the Partial Hessian Approximation in QM/MM-Based Vibrational Analysis**  (J. Chem. Theory Comput.; 2024)  · ID: 10.1021/acs.jctc.4c00882  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC11562069/) `[可下载]`
- **Mechanistic roles and design criteria for catalysts in lithium–sulfur batteries**  (Communications Materials; 2025)  · ID: 10.1038/s43246-025-01015-7  · [OA链接](https://www.nature.com/articles/s43246-025-01015-7) `[可下载]`
- **Enhanced climbing image nudged elastic band method with Hessian eigenmode alignment**  (Frontiers in Chemistry; 2026)  · ID: 10.3389/fchem.2026.1807063  · [OA链接](https://www.frontiersin.org/journals/chemistry/articles/10.3389/fchem.2026.1807063/full) `[可下载]`
- **A Universal Descriptor for the Entropy of Adsorbed Molecules in Confined Spaces**  (ACS Central Science; 年份未知)  · ID: 10.1021/acscentsci.8b00419
- **ASE thermochemistry/vibrations 模块文档**  (ASE GitLab; 年份未知)  · ID: 无  · [OA链接](https://gitlab.com/ase/ase/blob/master/doc/ase/thermochemistry/thermochemistry.rst) `[可下载]`
- **Adsorbate Free Energies from DFT-Derived Translational Energy Landscapes**  (J. Phys. Chem. C; 年份未知)  · ID: 10.1021/acs.jpcc.1c05917
- **Anharmonic Correction to Adsorption Free Energy from DFT-Based MD Using Thermodynamic Integration**  (J. Chem. Theory Comput.; 年份未知)  · ID: 10.1021/acs.jctc.0c01022
- **Dealing with imaginary frequencies (Aalto University IMM Wiki)**  (Aalto University Wiki; 年份未知)  · ID: 无  · [OA链接](https://wiki.aalto.fi/display/IMM/Dealing+with+imaginary+frequencies) `[可下载]`
- **Eliminating Imaginary Frequencies**  (Rowan Scientific(方法学博客); 年份未知)  · ID: 无  · [OA链接](https://rowansci.com/blog/eliminating-imaginary-frequencies) `[可下载]`
- **GoodVibes: quasi-harmonic free energy corrections from output files(开源工具,含低频/虚频处置实践)**  (GitHub/patonlab; 年份未知)  · ID: 无  · [OA链接](https://github.com/patonlab/GoodVibes) `[可下载]`
- **How to handle imaginary phonon modes (VASP Wiki官方文档)**  (VASP Wiki; 年份未知)  · ID: 无  · [OA链接](https://vasp.at/wiki/How_to_handle_imaginary_phonon_modes) `[可下载]`
- **Phonons from finite differences (VASP Wiki官方文档)**  (VASP Wiki; 年份未知)  · ID: 无  · [OA链接](https://vasp.at/wiki/Phonons_from_finite_differences) `[可下载]`
- **Transition State Tools for VASP (VTST) — Nudged Elastic Band文档**  (Henkelman Group; 年份未知)  · ID: 无  · [OA链接](https://henkelmangroup.github.io/vtsttools/neb.html) `[可下载]`

### PDOS/d带中心：LORBIT 投影、轨道杂化分析(3d-3p)、d带中心计算与图

- **Electronic factors determining the reactivity of metal surfaces**  (Surface Science; 1995)  · ID: 无  · [OA链接](https://ui.adsabs.harvard.edu/abs/1995SurSc.343..211H/abstract) `[可下载]`
- **Why gold is the noblest of all the metals**  (Nature; 1995)  · ID: 10.1038/376238a0
- **Electronic structure and catalysis on metal surfaces**  (Annual Review of Physical Chemistry; 2002)  · ID: 无  · [OA链接](https://www.eng.uc.edu/~beaucag/Research/050913_ArgonneWorkshop/CatalysisReviews/62CitationReview2002.pdf) `[可下载]`
- **Effects of d-band shape on the surface reactivity of transition-metal alloys**  (Physical Review B; 2014)  · ID: 10.1103/PhysRevB.89.115114
- **sumo: Command-line tools for plotting and analysis of periodic ab initio calculations**  (Journal of Open Source Software; 2018)  · ID: 无  · [OA链接](https://github.com/SMTG-Bham/sumo/blob/master/paper.md) `[可下载]`
- **LOBSTER: Local orbital projections, atomic charges, and chemical-bonding analysis from projector-augmented-wave-based density-functional theory**  (Journal of Computational Chemistry; 2020)  · ID: 10.1002/jcc.26353  · [OA链接](https://chemrxiv.org/engage/api-gateway/chemrxiv/assets/orp/resource/item/60c749164c891972efad3037/original/lobster-local-orbital-projections-atomic-charges-and-chemical-bonding-analysis-from-projector-augmented-wave-based-dft.pdf) `[可下载]`
- **PyProcar: A Python library for electronic structure pre/post-processing**  (Computer Physics Communications; 2020)  · ID: 无
- **Engineering d-p Orbital Hybridization in Single-Atom Metal-Embedded Three-Dimensional Electrodes for Li-S Batteries**  (Advanced Materials; 2021)  · ID: 10.1002/adma.202105947  · [OA链接](https://onlinelibrary.wiley.com/doi/full/10.1002/adma.202105947) `[可下载]`
- **Vertical-orbital band center as an activity descriptor for hydrogen evolution**  (arXiv; 2021)  · ID: arXiv:2102.06335  · [OA链接](https://arxiv.org/pdf/2102.06335) `[可下载]`
- **Electronic structure factors and the importance of adsorbate effects in chemisorption on surface alloys**  (npj Computational Materials; 2022)  · ID: 10.1038/s41524-022-00846-z  · [OA链接](https://www.nature.com/articles/s41524-022-00846-z) `[可下载]`
- **Strengthened d-p Orbital Hybridization through Asymmetric Coordination Engineering of Single-Atom Catalysts for Durable Lithium-Sulfur Batteries**  (Nano Letters; 2022)  · ID: 10.1021/acs.nanolett.2c02183
- **Regulating the electronic structure through charge redistribution in dense single-atom catalysts**  (Nature Communications; 2023)  · ID: 10.1038/s41467-023-38310-1  · [OA链接](https://www.nature.com/articles/s41467-023-38310-1) `[可下载]`
- **Central role of d-band energy level in Cu-based intermetallic alloys**  (npj Computational Materials; 2024)  · ID: 10.1038/s41524-024-01257-y  · [OA链接](https://www.nature.com/articles/s41524-024-01257-y) `[可下载]`
- **Modulating the d-band center of single-atom catalysts for efficient Li2S2-Li2S conversion in durable lithium-sulfur batteries**  (Energy Storage Materials; 2024)  · ID: 无
- **Modulation of d-Orbital Interactions in Dual-Atom Catalysts for Enhanced Polysulfide Anchoring and Kinetics in Lithium-Sulfur Batteries**  (ACS Applied Materials & Interfaces; 2024)  · ID: 10.1021/acsami.4c11523
- **D-Band Center Modulation of Metallic Co-Incorporated Co7Fe3 Alloy Heterostructure for Regulating Polysulfides in Highly Efficient Lithium-Sulfur Batteries**  (Advanced Functional Materials; 2025)  · ID: 10.1002/adfm.202416826
- **Design rules for anion-doped catalysts revealed by p-p-s orbital coupling in Li-S chemistry**  (Nature Communications; 2025)  · ID: 10.1038/s41467-025-65908-4  · [OA链接](https://www.nature.com/articles/s41467-025-65908-4) `[可下载]`
- **High Rate and Long-Cycle Life of Lithium-Sulfur Battery Enabled by High d-Band Center of High-Entropy Alloys**  (ACS Nano; 2025)  · ID: 10.1021/acsnano.4c18642
- **Te-Modulated Fe Single Atom with Synergistic Bidirectional Catalysis for High-Rate and Long-Cycling Lithium-Sulfur Battery**  (Nano-Micro Letters; 2025)  · ID: 10.1007/s40820-025-01873-3  · [OA链接](https://link.springer.com/article/10.1007/s40820-025-01873-3) `[可下载]`
- **p-d orbital hybridization induced by transition metal atom sites for room-temperature sodium-sulfur batteries**  (National Science Review; 2025)  · ID: 10.1093/nsr/nwaf241  · [OA链接](https://academic.oup.com/nsr/advance-article/doi/10.1093/nsr/nwaf241/8160416) `[可下载]`
- **Balanced d-Band Model: A Framework for Balancing Redox Reactions in Lithium-Sulfur Batteries**  (ACS Nano; 年份未知)  · ID: 10.1021/acsnano.4c10348
- **Expanding PyProcar for new features, maintainability, and reliability**  (Computer Physics Communications; 年份未知)  · ID: 无
- **Rationalizing the d-Band Model from Theory to Practice in Catalyst Design**  (Journal of the American Chemical Society; 年份未知)  · ID: 10.1021/jacs.5c17673

### Bader 电荷与差分电荷密度:CHGCAR 操作、电荷转移量、等值面图

- **Atoms in Molecules: A Quantum Theory (R.F.W. Bader)**  (Oxford University Press (专著,奠基理论); 1990)  · ID: 无
- **A grid-based Bader analysis algorithm without lattice bias**  (Journal of Physics: Condensed Matter 21, 084204; 2009)  · ID: 10.1088/0953-8984/21/8/084204  · [OA链接](https://theory.cm.utexas.edu/henkelman/code/bader/download/tang09_084204.pdf) `[可下载]`
- **Accurate and efficient algorithm for Bader charge integration**  (Journal of Chemical Physics 134, 064111; 2011)  · ID: arXiv:1010.4916  · [OA链接](https://arxiv.org/abs/1010.4916) `[可下载]`
- **VESTA 3 for three-dimensional visualization of crystal, volumetric and morphology data**  (Journal of Applied Crystallography 44, 1272-1276; 2011)  · ID: 10.1107/S0021889811038970
- **Introducing DDEC6 atomic population analysis: part 1. Charge partitioning theory and methodology**  (RSC Advances; 2016)  · ID: 10.1039/C6RA04656H  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2016/ra/c6ra04656h) `[可下载]`
- **A Linear Relationship between the Charge Transfer Amount and Level Alignment in Molecule/Two-Dimensional Adsorption Systems**  (年份未知)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC7581258/) `[可下载]`

### COHP/ICOHP 键分析：LOBSTER 全流程（输入生成→运行→解析出图）

- **Crystal orbital Hamilton populations (COHP): energy-resolved visualization of chemical bonding in solids based on density-functional calculations**  (The Journal of Physical Chemistry; 1993)  · ID: 10.1021/j100135a014
- **Analytic projection from plane-wave and PAW wavefunctions and application to chemical-bonding analysis in solids**  (Journal of Computational Chemistry; 2013)  · ID: 10.1002/jcc.23424  · [OA链接](http://qcc.ru/tch/papers/107.pdf) `[可下载]`
- **Increased Back-Bonding Explains Step-Edge Reactivity and Particle Size Effect for CO Activation on Ru Nanoparticles**  (Journal of the American Chemical Society; 2016)  · ID: 10.1021/jacs.6b08697
- **The Crystal Orbital Hamilton Population (COHP) Method as a Tool to Visualize and Analyze Chemical Bonding in Intermetallic Compounds**  (Crystals; 2018)  · ID: 无  · [OA链接](https://www.mdpi.com/2073-4352/8/5/225) `[可下载]`
- **Crystal Orbital Bond Index: Covalent Bond Orders in Solids**  (The Journal of Physical Chemistry C; 2021)  · ID: 10.1021/acs.jpcc.1c00718
- **The Orbital Origins of Chemical Bonding in Ge-Sb-Te Phase-Change Materials**  (2022)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC9306605/) `[可下载]`
- **A Quantum-Chemical Bonding Database for Solid-State Materials**  (Scientific Data; 2023)  · ID: 10.1038/s41597-023-02477-5  · [OA链接](https://www.nature.com/articles/s41597-023-02477-5) `[可下载]`
- **d-p Hybridization-Induced "Trapping-Coupling-Conversion" Enables High-Efficiency Nb Single-Atom Catalysis for Li-S Batteries**  (2023)  · ID: 无  · [OA链接](https://pubmed.ncbi.nlm.nih.gov/36640116/) `[可下载]`
- **LobsterPy: A package to automatically analyze LOBSTER runs**  (Journal of Open Source Software; 2024)  · ID: 10.21105/joss.06286  · [OA链接](https://www.theoj.org/joss-papers/joss.06286/10.21105.joss.06286.pdf) `[可下载]`
- **Prediction of O and OH Adsorption on Transition Metal Oxide Surfaces from Bulk Descriptors**  (ACS Catalysis; 2024)  · ID: 10.1021/acscatal.4c00111
- **LOPOSTER: A Cascading Postprocessor for LOBSTER**  (Journal of Computational Chemistry; 2025)  · ID: 10.1002/jcc.70167  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC12207952/) `[可下载]`
- **Selective Orbital Coupling: An Adsorption Mechanism in Single-Atom Catalysis**  (Journal of the American Chemical Society; 年份未知)  · ID: 10.1021/jacs.3c13119

### 磁性与自旋态:MAGMOM 初猜、自旋极化、多自旋态搜索

- **Validation of Exchange-Correlation Functionals for Spin States of Iron Complexes**  (Journal of Physical Chemistry A; 2004)  · ID: 10.1021/jp049043i
- **Occupation matrix control of d- and f-electron localisations using DFT + U**  (Physical Chemistry Chemical Physics; 2014)  · ID: 10.1039/c4cp01083c  · [OA链接](https://scispace.com/pdf/occupation-matrix-control-of-d-and-f-electron-localisations-37xxf0ys6b.pdf) `[可下载]`
- **Bridging the Homogeneous-Heterogeneous Divide: Modeling Spin for Reactivity in Single Atom Catalysis**  (Frontiers in Chemistry; 2019)  · ID: 10.3389/fchem.2019.00219  · [OA链接](https://www.frontiersin.org/journals/chemistry/articles/10.3389/fchem.2019.00219/full) `[可下载]`
- **High-throughput search for magnetic and topological order in transition metal oxides**  (Science Advances; 2020)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC7725452/) `[可下载]`
- **Tuning Fe-spin state of FeN4 structure by axial bonds as efficient catalyst in Li-S batteries**  (Energy Storage Materials; 2022)  · ID: 无
- **Assessing the performance of approximate density functional theory on 95 experimentally characterized Fe(II) spin crossover complexes**  (Journal of Chemical Physics; 2023)  · ID: 无
- **Regulating the Spin State Configuration in Bimetallic Phosphorus Trisulfides for Promoting Sulfur Redox Kinetics**  (Journal of the American Chemical Society; 2023)  · ID: 10.1021/jacs.3c07213
- **The spin-forbidden transition in iron(IV)-oxo catalysts relevant to two-state reactivity**  (Science Advances; 2024)  · ID: 10.1126/sciadv.ado1603
- **AQCat25: Unlocking spin-aware, high-fidelity machine learning potentials for heterogeneous catalysis**  (arXiv; 2025)  · ID: arXiv:2510.22938  · [OA链接](https://arxiv.org/abs/2510.22938) `[可下载]`
- **Circumventing the Metastable States within DFT+U through Random Orbital-Dependent Local Perturbation**  (Journal of Chemical Theory and Computation; 2025)  · ID: 10.1021/acs.jctc.4c01520
- **Breaking the rate limiting barrier in lithium||sulfur batteries via spin state engineering**  (Nature Communications; 2026)  · ID: 10.1038/s41467-026-70974-3  · [OA链接](https://www.nature.com/articles/s41467-026-70974-3) `[可下载]`

### 形成能/结合能/稳定性：SAC 溶出能、聚集倾向、内聚能对比（Li-S SAC 场景）

- **Computational high-throughput screening of electrocatalytic materials for hydrogen evolution**  (Nature Materials; 2006)  · ID: 10.1038/nmat1752
- **Electrochemical dissolution of surface alloys in acids: thermodynamic trends from first-principles calculations**  (Electrochimica Acta; 2007)  · ID: 无
- **Surface Pourbaix diagrams and oxygen reduction activity of Pt, Ag and Ni(111) surfaces studied by DFT**  (Physical Chemistry Chemical Physics; 2008)  · ID: 10.1039/B803956A  · [OA链接](https://core.ac.uk/outputs/13720450) `[可下载]`
- **Atomistic Theory of Ostwald Ripening and Disintegration of Supported Metal Particles under Reaction Conditions**  (Journal of the American Chemical Society; 2013)  · ID: 10.1021/ja3087054
- **Single-atom Fe and N co-doped graphene for lithium-sulfur batteries: a density functional theory study**  (Materials Research Express; 2019)  · ID: 10.1088/2053-1591/ab33ad
- **Theoretical Approach To Predict the Stability of Supported Single-Atom Catalysts**  (ACS Catalysis; 2019)  · ID: 10.1021/acscatal.9b00252
- **Acid Stability and Demetalation of PGM-Free ORR Electrocatalyst Structures from Density Functional Theory: A Model for "Single-Atom Catalyst" Dissolution**  (ACS Catalysis; 2020)  · ID: 10.1021/acscatal.0c02856
- **Analysis of Acid-Stable and Active Oxides for the Oxygen Evolution Reaction**  (ACS Energy Letters; 2020)  · ID: 10.1021/acsenergylett.0c02030
- **Stability of heterogeneous single-atom catalysts: a scaling law mapping thermodynamics to kinetics**  (npj Computational Materials; 2020)  · ID: 10.1038/s41524-020-00411-6  · [OA链接](https://www.nature.com/articles/s41524-020-00411-6) `[可下载]`
- **Theoretical Understandings of Graphene-based Metal Single-Atom Catalysts: Stability and Catalytic Performance**  (Chemical Reviews; 2020)  · ID: 10.1021/acs.chemrev.0c00818
- **Acid-Stable and Active M–N–C Catalysts for the Oxygen Reduction Reaction: The Role of Local Structure**  (ACS Catalysis; 2021)  · ID: 10.1021/acscatal.1c02941
- **High-Throughput Screening of Stable Single-Atom Catalysts in CO2 Reduction Reactions**  (ACS Catalysis; 2022)  · ID: 10.1021/acscatal.2c02149  · [OA链接](https://pubs.acs.org/doi/abs/10.1021/acscatal.2c02149) `[可下载]`
- **Understanding the Dynamic Aggregation in Single-Atom Catalysis**  (Advanced Science; 2024)  · ID: 10.1002/advs.202308046  · [OA链接](https://advanced.onlinelibrary.wiley.com/doi/10.1002/advs.202308046) `[可下载]`
- **Analysis of tin oxide supported transition metal single-atom catalysts for oxygen evolution reaction**  (Journal of Materials Chemistry A; 2025)  · ID: 10.1039/d5ta01974e  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2025/ta/d5ta01974e) `[可下载]`
- **Understanding stability and reactivity of transition metal single-atoms on graphene**  (Scientific Reports; 2025)  · ID: 10.1038/s41598-025-00126-y  · [OA链接](https://www.nature.com/articles/s41598-025-00126-y) `[可下载]`
- **Thermodynamic stability of single-atom catalysts in electrochemical conditions from first principles: Role of the local coordination**  (Electrochimica Acta; 2026)  · ID: 无

### 溶剂化与电场:VASPsol 隐式溶剂、电极电位/电场对电池电催化的影响

- **Electrochemical Barriers Made Simple**  (J. Phys. Chem. Lett.; 2015)  · ID: 10.1021/acs.jpclett.5b01043
- **Using Implicit Solvent in Ab Initio Electrochemical Modeling: Investigating Li+/Li Electrochemistry at a Li/Solvent Interface**  (J. Chem. Theory Comput.; 2015)  · ID: 10.1021/acs.jctc.5b00170
- **Solvation free energies for periodic surfaces: comparison of implicit and explicit solvation models**  (Phys. Chem. Chem. Phys.; 2016)  · ID: 无  · [OA链接](https://pubs.rsc.org/en/content/articlelanding/2016/cp/c6cp04094b) `[可下载]`
- **Grand canonical electronic density-functional theory: Algorithms and applications to electrochemistry**  (J. Chem. Phys.; 2017)  · ID: 无  · [OA链接](https://pubs.aip.org/aip/jcp/article/146/11/114104/195000/Grand-canonical-electronic-density-functional) `[可下载]`
- **Insight on lithium polysulfide intermediates in a Li/S battery by density functional theory**  (RSC Adv.; 2017)  · ID: 10.1039/C7RA04673A  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2017/ra/c7ra04673a) `[可下载]`
- **Grand canonical simulations of electrochemical interfaces in implicit solvation models**  (J. Chem. Phys.; 2019)  · ID: 无  · [OA链接](https://pubs.aip.org/aip/jcp/article/150/4/041730/1062335) `[可下载]`
- **Calculated Reduction Potentials of Electrolyte Species in Lithium–Sulfur Batteries**  (J. Phys. Chem. C; 2020)  · ID: 10.1021/acs.jpcc.0c04173
- **Electrosorption at metal surfaces from first principles**  (npj Comput. Mater.; 2020)  · ID: 10.1038/s41524-020-00394-4  · [OA链接](https://www.nature.com/articles/s41524-020-00394-4) `[可下载]`
- **Substantial potential effects on single-atom catalysts for the oxygen evolution reaction simulated via a fixed-potential method**  (J. Catal.; 2020)  · ID: 无
- **Understanding potential-dependent competition between electrocatalytic dinitrogen and proton reduction reactions**  (Nat. Commun.; 2021)  · ID: 10.1038/s41467-021-24539-1  · [OA链接](https://www.nature.com/articles/s41467-021-24539-1) `[可下载]`
- **Implicit Solvation Methods for Catalysis at Electrified Interfaces**  (Chem. Rev.; 2022)  · ID: 10.1021/acs.chemrev.1c00675
- **Interfacial electric field effect on electrochemical carbon dioxide reduction reaction**  (Chem Catalysis; 2022)  · ID: 无  · [OA链接](https://www.cell.com/chem-catalysis/fulltext/S2667-1093(22)00399-2) `[可下载]`
- **Single-Atom Electrocatalysis for Hydrogen Evolution Based on the Constant Charge and Constant Potential Models**  (J. Phys. Chem. Lett.; 2022)  · ID: 10.1021/acs.jpclett.2c01288
- **Strong internal electric field enhanced polysulfide trapping and ameliorates redox kinetics for lithium-sulfur battery**  (J. Energy Chem.; 2022)  · ID: 无  · [OA链接](https://www.sciencedirect.com/science/article/pii/S209549562200585X) `[可下载]`
- **An implicit electrolyte model for plane wave density functional theory exhibiting nonlinear response and a nonlocal cavity definition (VASPsol++)**  (J. Chem. Phys.; 2023)  · ID: arXiv:2307.04551  · [OA链接](https://arxiv.org/abs/2307.04551) `[可下载]`
- **Controlled Electrochemical Barrier Calculations without Potential Control**  (J. Chem. Theory Comput.; 2023)  · ID: 10.1021/acs.jctc.3c00836
- **Grand Canonical Ensemble Modeling of Electrochemical Interfaces Made Simple**  (J. Chem. Theory Comput.; 2023)  · ID: 10.1021/acs.jctc.3c00237
- **Guiding maps of solvents for lithium-sulfur batteries via a computational data-driven approach**  (Patterns; 2023)  · ID: 无  · [OA链接](https://www.cell.com/patterns/fulltext/S2666-3899(23)00154-X) `[可下载]`
- **On the Challenge of Obtaining an Accurate Solvation Energy Estimate in Simulations of Electrocatalysis**  (Top. Catal.; 2023)  · ID: 10.1007/s11244-023-01829-0  · [OA链接](https://link.springer.com/article/10.1007/s11244-023-01829-0) `[可下载]`
- **A Comparison of Modern Solvation Models for Oxygen Reduction at the Pt(111) Interface**  (J. Phys. Chem. C; 2024)  · ID: 10.1021/acs.jpcc.4c04924
- **Nanocurvature-induced field effects enable control over the activity of single-atom electrocatalysts**  (Nat. Commun.; 2024)  · ID: 10.1038/s41467-024-46175-1  · [OA链接](https://www.nature.com/articles/s41467-024-46175-1) `[可下载]`
- **Effect of Interfacial Electric Field on 2D Metal/Graphene Electrocatalysts for CO2 Reduction Reaction**  (ChemSusChem; 2025)  · ID: 10.1002/cssc.202401673  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC11789969/) `[可下载]`
- **How Computational Theorists Deal with Electrode Potential**  (J. Electrochem. Soc.; 2025)  · ID: 10.1149/1945-7111/adba16  · [OA链接](https://iopscience.iop.org/article/10.1149/1945-7111/adba16) `[可下载]`
- **Theoretical Insights into the Molecular Interaction in Li-Ion Battery Electrolytes from the Perspective of the Dielectric Continuum Solvation Model**  (Crystals (MDPI); 2025)  · ID: 无  · [OA链接](https://www.mdpi.com/2073-4352/15/9/796) `[可下载]`
- **Absolute Potential of the Standard Hydrogen Electrode and the Problem of Interconversion of Potentials in Different Solvents**  (年份未知)  · ID: 无  · [OA链接](https://pubmed.ncbi.nlm.nih.gov/20496903/) `[可下载]`
- **Absolute standard hydrogen electrode potential and redox potentials of atoms and molecules: machine learning aided first principles calculations**  (Chem. Sci.; 年份未知)  · ID: 10.1039/D4SC03378G  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2025/sc/d4sc03378g) `[可下载]`
- **Built-In Interfacial Electric Fields Resolve the Adsorption-Kinetics Trade-Off in Lithium-Sulfur Batteries**  (Adv. Funct. Mater.; 年份未知)  · ID: 无
- **Constant Potential Thermodynamic Integration for Obtaining the Free Energy Profile of Electrochemical Reaction**  (J. Phys. Chem. Lett.; 年份未知)  · ID: 10.1021/acs.jpclett.3c03318
- **How covalence breaks adsorption-energy scaling relations and solvation restores them**  (Chem. Sci.; 年份未知)  · ID: 无  · [OA链接](https://pubs.rsc.org/sc/article/8/1/124/545984/How-covalence-breaks-adsorption-energy-scaling) `[可下载]`
- **Solvation effects on DFT predictions of ORR activity on metal surfaces**  (Catal. Today; 年份未知)  · ID: 无
- **Solvation-property relationship of lithium-sulphur battery electrolytes**  (Nat. Commun.; 年份未知)  · ID: 10.1038/s41467-023-44527-x  · [OA链接](https://www.nature.com/articles/s41467-023-44527-x) `[可下载]`

### AIMD 分子动力学稳定性验证:NVT 热浴、SAC 热稳定性、轨迹分析

- **A unified formulation of the constant temperature molecular dynamics methods**  (The Journal of Chemical Physics; 1984)  · ID: 10.1063/1.447334
- **Canonical dynamics: Equilibrium phase-space distributions**  (Physical Review A; 1985)  · ID: 10.1103/PhysRevA.31.1695
- **Canonical sampling through velocity rescaling**  (The Journal of Chemical Physics; 2007)  · ID: 无  · [OA链接](https://iris.sissa.it/retrieve/dd8a4bf7-109d-20a0-e053-d805fe0a8cb0/JChemPhys_126_014101.pdf) `[可下载]`
- **MDAnalysis: A toolkit for the analysis of molecular dynamics simulations**  (Journal of Computational Chemistry; 2011)  · ID: 10.1002/jcc.21787  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC3144279/) `[可下载]`
- **On-the-fly machine learning force field generation: Application to melting points**  (Physical Review B; 2019)  · ID: 10.1103/PhysRevB.100.014105  · [OA链接](https://arxiv.org/abs/1904.12961) `[可下载]`
- **On-the-fly active learning of interpretable Bayesian force fields for atomistic rare events**  (npj Computational Materials; 2020)  · ID: 10.1038/s41524-020-0283-z  · [OA链接](https://www.nature.com/articles/s41524-020-0283-z) `[可下载]`
- **Theoretical probing the anchoring properties of BNP2 monolayer for lithium-sulfur batteries**  (Applied Surface Science; 2022)  · ID: 无
- **Ab Initio Molecular Dynamics Simulations of Amorphous Metal Sulfides as Cathode Materials for Lithium–Sulfur Batteries**  (The Journal of Physical Chemistry C; 2023)  · ID: 10.1021/acs.jpcc.3c04835  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.jpcc.3c04835) `[可下载]`
- **Dynamic Catalytic Structures of Single-Atom (or Cluster) Catalysts: A Perspective Review**  (Small Structures; 2025)  · ID: 10.1002/sstr.202400479
- **From Atomic Motif to Realistic Single Atom Catalysts through Machine Learning Interatomic Potentials**  (ACS Energy Letters; 年份未知)  · ID: 10.1021/acsenergylett.5c03288
- **MPmorph: tools to run and analyze ab-initio molecular dynamics (AIMD) calculations with VASP (Materials Project 软件仓库)**  (年份未知)  · ID: 无  · [OA链接](https://github.com/materialsproject/mpmorph) `[可下载]`
- **On-the-fly machine learning-augmented constrained AIMD to design new routes from glassy carbon to quenchable amorphous diamond with low pressure and temperature**  (年份未知)  · ID: arXiv:2507.09639  · [OA链接](https://arxiv.org/abs/2507.09639) `[可下载]`
- **Thermostat Algorithms for Molecular Dynamics Simulations**  (年份未知)  · ID: 无  · [OA链接](https://physics.iisc.ac.in/~maiti/course_website/Huenberger_thermostat.pdf) `[可下载]`
- **xdatbus: Python package for enhancing VASP AIMD simulations and analysis (软件仓库)**  (年份未知)  · ID: 无  · [OA链接](https://github.com/jcwang587/xdatbus) `[可下载]`

### 高通量筛选策略:候选空间枚举、漏斗式筛选(粗筛→精算)、描述符先行、批量任务组织(面向VASP催化自动化软件的DFT全通量管线;锂硫SAC复现场景)

- **The high-throughput highway to computational materials design**  (Nature Materials; 2013)  · ID: 10.1038/nmat3568
- **PASTA: Python Algorithms for Searching Transition stAtes**  (arXiv; 2018)  · ID: arXiv:1805.00085  · [OA链接](https://arxiv.org/pdf/1805.00085) `[可下载]`
- **Theory-guided design of catalytic materials using scaling relationships and reactivity descriptors**  (Nature Reviews Materials; 2019)  · ID: 10.1038/s41578-019-0152-x  · [OA链接](https://www.henkelmanlab.org/pubs/zhao19_792.pdf) `[可下载]`
- **AiiDA 1.0, a scalable computational infrastructure for automated reproducible workflows and data provenance**  (Scientific Data; 2020)  · ID: 10.1038/s41597-020-00638-4  · [OA链接](https://www.nature.com/articles/s41597-020-00638-4) `[可下载]`
- **O-coordinated W-Mo dual-atom catalyst for pH-universal electrocatalytic hydrogen evolution**  (Science Advances; 2020)  · ID: 10.1126/sciadv.aba6586
- **High-throughput screening of carbon-supported single metal atom catalysts for oxygen reduction reaction**  (Nano Research; 2021)  · ID: 10.1007/s12274-021-3598-2  · [OA链接](https://link.springer.com/article/10.1007/s12274-021-3598-2) `[可下载]`
- **A multi-fidelity machine learning approach to high throughput materials screening**  (npj Computational Materials; 2022)  · ID: 10.1038/s41524-022-00947-9  · [OA链接](https://www.nature.com/articles/s41524-022-00947-9) `[可下载]`
- **Fundamental mechanistic insights into the catalytic reactions of Li─S redox by Co single-atom electrocatalysts via operando methods**  (Science Advances; 2023)  · ID: 10.1126/sciadv.adi5108  · [OA链接](https://www.science.org/doi/10.1126/sciadv.adi5108) `[可下载]`
- **Machine-learning-assisted design of a binary descriptor to decipher electronic and structural effects on sulfur reduction kinetics**  (Nature Catalysis; 2023)  · ID: 10.1038/s41929-023-01041-z
- **Design principle of single-atom catalysts for sulfur reduction reaction—interplay between coordination patterns and transition metals**  (Science China Materials; 2024)  · ID: 10.1007/s40843-024-3068-5
- **Convolutional neural networks and volcano plots for screening and predicting two-dimensional single-atom catalysts in CO2 reduction**  (Cell Reports Physical Science; 2025)  · ID: 无
- **Data-Driven Insight into the Universal Structure–Property Relationship of Catalysts in Lithium–Sulfur Batteries**  (J. Am. Chem. Soc.; 2025)  · ID: 10.1021/jacs.5c04960
- **Descriptors construction and application in catalytic site design**  (iScience; 2025)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC12312069/) `[可下载]`
- **Challenges and Opportunities of Pretrained Machine Learning Interatomic Potentials in Heterogeneous Catalysis**  (年份未知)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC12976938/) `[可下载]`
- **Nudged Elastic Band — Transition State Tools for VASP (VTST)**  (VTST 工具文档(Henkelman 组); 年份未知)  · ID: 无  · [OA链接](https://henkelmangroup.github.io/vtsttools/neb.html) `[可下载]`

### 工作函数/静电势/ELF/自旋密度等其他电子结构量的计算与用途

- **Theory of Metal Surfaces: Work Function**  (Phys. Rev. B; 1971)  · ID: 10.1103/PhysRevB.3.1215  · [OA链接](https://harvest.aps.org/v2/journals/articles/10.1103/PhysRevB.3.1215/fulltext) `[可下载]`
- **A simple measure of electron localization in atomic and molecular systems**  (J. Chem. Phys.; 1990)  · ID: 10.1063/1.458517
- **Adsorbate-substrate and adsorbate-adsorbate interactions of Na and K adlayers on Al(111)**  (Phys. Rev. B; 1992)  · ID: 10.1103/PhysRevB.46.16067
- **Classification of chemical bonds based on topological analysis of electron localization functions**  (Nature; 1994)  · ID: 10.1038/371683a0
- **ELF: The Electron Localization Function**  (Angew. Chem. Int. Ed.; 1997)  · ID: 无  · [OA链接](https://www.lct.jussieu.fr/pagesperso/savin/papers/nesper-hon-hgs/SavNesWenFae-97.pdf) `[可下载]`
- **Deriving accurate work functions from thin-slab calculations**  (J. Phys.: Condens. Matter; 1999)  · ID: 10.1088/0953-8984/11/13/006
- **Multiwfn: A multifunctional wavefunction analyzer**  (J. Comput. Chem.; 2012)  · ID: 10.1002/jcc.22885  · [OA链接](https://onlinelibrary.wiley.com/doi/abs/10.1002/jcc.22885) `[可下载]`
- **Accurate and efficient band-offset calculations from density functional theory**  (Comput. Mater. Sci.; 2018)  · ID: 无
- **Anisotropic work function of elemental crystals**  (Surf. Sci.; 2019)  · ID: 10.1016/j.susc.2019.05.002  · [OA链接](https://materialsvirtuallab.org/pubs/10.1016_j.susc.2019.05.002.pdf) `[可下载]`
- **Distinguishing between chemical bonding and physical binding using electron localization function (ELF)**  (J. Phys.: Condens. Matter; 2020)  · ID: 10.1088/1361-648X/ab7fd8  · [OA链接](https://iopscience.iop.org/article/10.1088/1361-648X/ab7fd8) `[可下载]`
- **Spin-polarized oxygen evolution reaction under magnetic field**  (Nat. Commun.; 2021)  · ID: 无  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC8110536/) `[可下载]`
- **Spin engineering of single-site metal catalysts**  (The Innovation; 2022)  · ID: 无  · [OA链接](https://www.sciencedirect.com/science/article/am/pii/S2666675822000649) `[可下载]`
- **Boosting bidirectional conversion of polysulfide driven by the built-in electric field of MoS2/MoP Mott-Schottky heterostructures in lithium-sulfur batteries**  (J. Adv. Ceram.; 2023)  · ID: 10.26599/JAC.2023.9220794  · [OA链接](https://www.sciopen.com/article/10.26599/JAC.2023.9220794) `[可下载]`
- **Electrostatic Potential as Solvent Descriptor to Enable Rational Electrolyte Design for Lithium Batteries**  (Adv. Energy Mater.; 2023)  · ID: 10.1002/aenm.202300259
- **Unraveling Polysulfide's Adsorption and Electrocatalytic Conversion on Metal Oxides for Li-S Batteries**  (Adv. Sci.; 2023)  · ID: 10.1002/advs.202204930  · [OA链接](https://pmc.ncbi.nlm.nih.gov/articles/PMC9929279/) `[可下载]`
- **Electrostatic potential characteristics of ionic liquids: A strategy for computational screening of electrolytes for Lithium metal batteries**  (Comput. Theor. Chem.; 2025)  · ID: 无
- **Magnetic Moment Descriptor-Guided Multifunctional Co Single-Atom Catalysts Enable Wide-Temperature Uninterrupted Seawater Splitting**  (Adv. Mater.; 2025)  · ID: 10.1002/adma.202504476
- **NbP-NbO Heterostructures with Engineered Built-In Electric Fields for Accelerated Lithium Polysulfide Conversion**  (Nano Lett.; 2025)  · ID: 10.1021/acs.nanolett.5c04018
- **Charge Redistribution of Atomic Catalysts in Electrocatalysis**  (Adv. Funct. Mater.; 2026)  · ID: 10.1002/adfm.202527230
- **A Quantitative Electrostatic Potential Descriptor Enables Deep Learning-Accelerated Discovery of High-Performance Lithium-Ion Battery Electrolytes**  (Angew. Chem.; 年份未知)  · ID: 10.1002/ange.6825619
- **Band alignments of complex heterojunctions from first principles: the case of the α-Fe2O3/BaTiO3 interface**  (J. Phys.: Mater.; 年份未知)  · ID: 10.1088/2515-7639/ae5b28  · [OA链接](https://iopscience.iop.org/article/10.1088/2515-7639/ae5b28) `[可下载]`
- **Discovery of stable surfaces with extreme work functions by high-throughput density functional theory and machine learning**  (年份未知)  · ID: arXiv:2011.10905  · [OA链接](https://arxiv.org/html/2011.10905v2) `[可下载]`
- **Electron Localization in Molecules and Solids: The Meaning of ELF**  (J. Phys. Chem. A; 年份未知)  · ID: 10.1021/jp9820774

## 二、DFT 自动化管线产品调研文献（wave1-a21~a32）

### AiiDA + aiida-vasp 工作流框架

- **AiiDA: automated interactive infrastructure and database for computational science**  (Computational Materials Science 111, 218-230; 2016)  · ID: DOI:10.1016/j.commatsci.2015.09.013 ; arXiv:1504.01163  · [OA链接](https://arxiv.org/abs/1504.01163) `[可下载]`
- **Workflows in AiiDA: Engineering a high-throughput, event-based engine for robust and modular computational workflows**  (Computational Materials Science 187, 110086; 2021)  · ID: DOI:10.1016/j.commatsci.2020.110086 ; arXiv:2007.10312  · [OA链接](https://arxiv.org/abs/2007.10312) `[可下载]`
- **Automated reproducible workflows and data provenance with AiiDA**  (Nature Reviews Physics 4 (Comment); 2022)  · ID: DOI:10.1038/s42254-022-00463-1

### atomate2 + FireWorks/jobflow + custodian（Materials Project 计算工作流栈）

- **FireWorks: a dynamic workflow system designed for high-throughput applications**  (Concurrency and Computation: Practice and Experience 27, 5037-5059; 2015)  · ID: 10.1002/cpe.3505  · [OA链接](https://perssongroup.lbl.gov/papers/cpe2015-fireworks.pdf) `[可下载]`
- **Jobflow: Computational Workflows Made Simple**  (Journal of Open Source Software 9(93); 2024)  · ID: 10.21105/joss.05995  · [OA链接](https://joss.theoj.org/papers/10.21105/joss.05995) `[可下载]`

### pyiron

- **pyiron: An integrated development environment for computational materials science**  (Computational Materials Science 163, 24-36; 2019)  · ID: 10.1016/j.commatsci.2018.07.043
- **A Lightweight Procedural Layer for Hybrid Experimental-Computational Workflows in Materials Science**  (Advanced Engineering Materials; 2025)  · ID: 10.1002/adem.202503176
- **A python workflow definition for computational materials design**  (Digital Discovery 4, 3149-3161; 2025)  · ID: 10.1039/D5DD00231A  · [OA链接](https://pubs.rsc.org/en/content/articlehtml/2025/dd/d5dd00231a) `[可下载]`
- **Executorlib - Up-scaling Python workflows for hierarchical heterogenous high-performance computing**  (Journal of Open Source Software 10(108), 7782; 2025)  · ID: 10.21105/joss.07782  · [OA链接](https://joss.theoj.org/papers/10.21105/joss.07782) `[可下载]`

### ASE(Atomic Simulation Environment)生态:核心库 ASE + 任务调度器 MyQueue + 工作流框架 ASR(均出自 DTU/哥本哈根 GPAW 学派)

- **An object-oriented scripting interface to a legacy electronic structure code (ASE 原始论文)**  (Comput. Sci. Eng. 4(3), 56–66; 2002)  · ID: 10.1109/5992.998641
- **Improved initial guess for minimum energy path calculations (IDPP 插值)**  (J. Chem. Phys. 140, 214106; 2014)  · ID: 10.1063/1.4878664 ; arXiv:1406.1512  · [OA链接](https://arxiv.org/abs/1406.1512) `[可下载]`
- **An automated nudged elastic band method (AutoNEB)**  (J. Chem. Phys. 145, 094107; 2016)  · ID: 10.1063/1.4961868
- **MyQueue: Task and workflow scheduling system**  (J. Open Source Softw. 5(45), 1844; 2020)  · ID: 10.21105/joss.01844  · [OA链接](https://joss.theoj.org/papers/10.21105/joss.01844) `[可下载]`
- **Atomic Simulation Recipes — A Python framework and library for automated workflows**  (Comput. Mater. Sci. 199, 110731; 2021)  · ID: 10.1016/j.commatsci.2021.110731 ; arXiv:2104.13431  · [OA链接](https://arxiv.org/abs/2104.13431) `[可下载]`

### pymatgen + Materials Project 基础设施(InputSets 标准化输入集 / Custodian 即时纠错 / FireWorks·jobflow·atomate2 工作流 / maggma·emmet Builder 数据管线)

- **Custodian: A simple, robust and flexible just-in-time (JIT) job management framework(错误自愈/有界恢复核心组件,无独立论文,随 pymatgen/atomate 论文描述)**  (开源软件仓库(GitHub),文档 http://materialsproject.github.io/custodian/; 2013-)  · ID: 无  · [OA链接](https://github.com/materialsproject/custodian) `[可下载]`
- **maggma + emmet:Builder 数据管线与 pydantic 文档 schema(数据/溯源管理组件,无独立论文)**  (开源软件仓库(GitHub),文档 https://materialsproject.github.io/maggma/; 2018-)  · ID: 无  · [OA链接](https://github.com/materialsproject/maggma) `[可下载]`

### VASPKIT

- **Empowering materials science with VASPKIT: a toolkit for enhanced simulation and analysis**  (Nature Protocols, 20, 3143-3169; 2025)  · ID: DOI:10.1038/s41596-025-01160-w

### JARVIS-Tools (NIST) / AFLOW (Duke) / OQMD-qmpy (Northwestern) 三大高通量第一性原理(以VASP为主)数据库生成与管理管线

- **AFLOW: An automatic framework for high-throughput materials discovery**  (Computational Materials Science 58, 218-226 (Curtarolo, Setyawan, Hart, et al.); 2012)  · ID: AFLOW-2012  · [OA链接](https://arxiv.org/abs/1308.5715) `[可下载]`
- **Materials Design and Discovery with High-Throughput Density Functional Theory: The Open Quantum Materials Database (OQMD)**  (JOM 65, 1501-1509 (Saal, Kirklin, Aykol, Meredig, Wolverton); DOI 10.1007/s11837-013-0755-4; 2013)  · ID: OQMD-JOM-2013  · [OA链接](https://www.osti.gov/biblio/1383293) `[可下载]`
- **The AFLOW standard for high-throughput materials science calculations**  (Computational Materials Science 108, 233-238 (Calderon, Hicks, Rose, Oses, ... Curtarolo); DOI 10.1016/j.commatsci.2015.07.019; 2015)  · ID: AFLOW-standard-2015  · [OA链接](https://arxiv.org/abs/1506.00303) `[可下载]`
- **The Open Quantum Materials Database (OQMD): assessing the accuracy of DFT formation energies**  (npj Computational Materials 1, 15010 (Kirklin, Saal, Meredig, ... Wolverton); DOI 10.1038/npjcompumats.2015.10; 2015)  · ID: OQMD-npj-2015  · [OA链接](https://www.nature.com/articles/npjcompumats201510) `[可下载]`
- **The joint automated repository for various integrated simulations (JARVIS) for data-driven materials design**  (npj Computational Materials 6, 173 (Choudhary, Garrity, Reid, DeCost, et al.); DOI 10.1038/s41524-020-00440-1; 2020)  · ID: JARVIS-npj-2020  · [OA链接](https://www.nature.com/articles/s41524-020-00440-1) `[可下载]`
- **Reflections on one million compounds in the open quantum materials database (OQMD)**  (Journal of Physics: Materials 5, 031001; DOI 10.1088/2515-7639/ac7ba9; 2022)  · ID: OQMD-1M-2022  · [OA链接](https://iopscience.iop.org/article/10.1088/2515-7639/ac7ba9) `[可下载]`

### CatKit (CatGen/CatFlow) · GASpy · Open Catalyst Project (OC20/OC22 + AdsorbML)

- **Active learning across intermetallics to guide discovery of electrocatalysts for CO2 reduction and H2 evolution (GASpy application)**  (Nature Catalysis; 2018)  · ID: DOI:10.1038/s41929-018-0142-1  · [OA链接](https://www.osti.gov/biblio/1543784) `[可下载]`
- **Dynamic Workflows for Routine Materials Discovery in Surface Science (GASpy)**  (Journal of Chemical Information and Modeling; 2018)  · ID: DOI:10.1021/acs.jcim.8b00386  · [OA链接](https://www.osti.gov/pages/servlets/purl/1543623) `[可下载]`
- **Graph Theory Approach to High-Throughput Surface Adsorption Structure Generation (CatGen/CatKit)**  (The Journal of Physical Chemistry A; 2019)  · ID: DOI:10.1021/acs.jpca.9b00311  · [OA链接](https://chemrxiv.org/engage/chemrxiv/article-details/60c73f5af96a000ca128608b) `[可下载]`
- **The Open Catalyst 2022 (OC22) Dataset and Challenges for Oxide Electrocatalysts**  (ACS Catalysis; 2023)  · ID: arXiv:2206.08917; DOI:10.1021/acscatal.2c05426  · [OA链接](https://arxiv.org/abs/2206.08917) `[可下载]`

### VASPilot / AutoDFT / DREAMS / TritonDFT / MatClaw(LLM·智能体驱动的 DFT 自动化框架,覆盖 VASP 与 Quantum ESPRESSO)

- **DREAMS: Density Functional Theory Based Research Engine for Agentic Materials Simulation**  (arXiv(CMU/Viswanathan 组);分层多智能体+共享 canvas,含 CO/Pt(111) 吸附能; 2025)  · ID: arXiv:2507.14267  · [OA链接](https://arxiv.org/abs/2507.14267) `[可下载]`
- **AutoDFT: A Closed-Loop Multi-Agent Framework for Autonomous DFT Calculations**  (arXiv(NTU + SMU);七智能体闭环,VASP 34 任务基准 94.1% 成功; 2026)  · ID: arXiv:2605.26179  · [OA链接](https://arxiv.org/abs/2605.26179) `[可下载]`
- **MatClaw: An Autonomous Code-First LLM Agent for End-to-End Materials Exploration**  (arXiv(Rice, Yakobson 组);code-as-action + 四层记忆 + RAG-over-source; 2026)  · ID: arXiv:2604.02688  · [OA链接](https://arxiv.org/abs/2604.02688) `[可下载]`
- **TritonDFT: Automating DFT with a Multi-Agent Framework**  (arXiv;Quantum ESPRESSO,Pareto 感知参数推断+DFTBench;代码 github.com/Leo9660/TritonDFT; 2026)  · ID: arXiv:2603.03372  · [OA链接](https://arxiv.org/abs/2603.03372) `[可下载]`

### Materials Studio (BIOVIA) / MedeA (Materials Design) / QuantumATK NanoLab (Synopsys) / MatCloud & MatCloud+(中科院CNIC→迈高科技);另以 ALKEMIE(北航)作错误自愈机制的文献参照

- **First principles methods using CASTEP(Materials Studio 核心模块论文;MS 工作流/网关机制本身无论文,依据官方 WebHelp 文档)**  (Zeitschrift für Kristallographie – Crystalline Materials 220(5-6), 567-570; 2005)  · ID: DOI 10.1524/zkri.220.5.567.65075  · [OA链接](https://www.tcm.phy.cam.ac.uk/castep/documentation/WebHelp/content/modules/castep/tskcasteprunremote.htm) `[可下载]`
- **MatCloud, a high-throughput computational materials infrastructure: Present, future visions, and challenges**  (Chinese Physics B 27(11), 110301; 2018)  · ID: DOI 10.1088/1674-1056/27/11/110301  · [OA链接](https://cpb.iphy.ac.cn/article/2018/1962/cpb_27_11_110301.html) `[可下载]`
- **MatCloud: A high-throughput computational infrastructure for integrated management of materials simulation, data and resources**  (Computational Materials Science 146, 319-333; 2018)  · ID: DOI 10.1016/j.commatsci.2018.01.039
- **Unravelling the Potential of Density Functional Theory through Integrated Computational Environments: Recent Applications of the Vienna Ab Initio Simulation Package in the MedeA Software**  (Computation 6(4), 63; 2018)  · ID: DOI 10.3390/computation6040063  · [OA链接](https://www.mdpi.com/2079-3197/6/4/63) `[可下载]`
- **QuantumATK: an integrated platform of electronic and atomic-scale modelling tools**  (Journal of Physics: Condensed Matter 32, 015901; 2020)  · ID: DOI 10.1088/1361-648X/ab4007; arXiv:1905.02794  · [OA链接](https://arxiv.org/abs/1905.02794) `[可下载]`
- **ALKEMIE: An intelligent computational platform for accelerating materials discovery and design**  (Computational Materials Science 186, 110064; 2021)  · ID: DOI 10.1016/j.commatsci.2020.110064
- **高通量自动流程集成计算与数据管理智能平台及其在合金设计中的应用(ALKEMIE 2.0,含自动纠错/最大重试机制细节)**  (金属学报 58(1), 75-88; 2022)  · ID: DOI 10.11900/0412.1961.2021.00041  · [OA链接](https://www.ams.org.cn/article/2022/0412-1961/0412-1961-2022-58-1-75.shtml) `[可下载]`

### Li-S电池SAC大规模筛选论文计算管线(群体调研:Nat Commun 2024 ML闭环、JACS Au 2024电子指纹漏斗、Nano Lett 2020 DFT导向设计、CSR 2025描述符综述等)

- **Unravelling the Catalytic Activity of Dual-Metal Doped N6-Graphene for Sulfur Reduction via Machine Learning-Accelerated First-Principles Calculations**  (Small; 2026)  · ID: 10.1002/smll.202514877

### 非专家桌面/图形化 DFT 工具类:BURAI、pyiron、AiiDAlab Quantum ESPRESSO app（对照 Materials Project 的 Custodian/atomate 错误恢复栈）

- **Python Materials Genomics (pymatgen): A Robust, Open-Source Python Library for Materials Analysis（含 Custodian 的 JIT 作业管理与纠错）**  (Computational Materials Science, 68, 314-319; 2013)  · ID: doi:10.1016/j.commatsci.2012.10.028
- **FireWorks: a dynamic workflow system designed for high-throughput applications（DAG、FIZZLED/rerun、动态 detour）**  (Concurrency and Computation: Practice and Experience; 2015)  · ID: doi:10.1002/cpe.3505  · [OA链接](https://escholarship.org/uc/item/51f6j8fd) `[可下载]`
- **Making atomistic materials calculations accessible with the AiiDAlab Quantum ESPRESSO app**  (npj Computational Materials; 2025)  · ID: doi:10.1038/s41524-025-01936-4  · [OA链接](https://www.nature.com/articles/s41524-025-01936-4) `[可下载]`

## 三、文本报告中提取的补充文献（wave1-a1/a33/a34, wave2~wave6）

仅收录正文中带 DOI 或 arXiv 编号、且未与第一/二部分重复的引用；未附 DOI/arXiv 的散引不收录。

### wave2 · cp2k

- **NCI / RDG 弱相互作用理论**:Johnson et al.**  (J. Am. Chem. Soc.; 2010)  · ID: 10.1021/ja100936w
- **critic2(固体周期 NCI + QTAIM,vcstudio 差异化核心)**:Otero-de-la-Roza, Johnson, Luaña**  (Comput. Phys. Commun.; 2014)  · ID: 10.1016/j.cpc.2013.10.026
- **ETKDG 构象生成(RDKit 用)**:Riniker, Landrum**  (J. Chem. Inf. Model.; 2015)  · ID: 10.1021/acs.jcim.5b00654
- **Rajan, Zielesny, Steinbeck**  (J. Cheminform.; 2020)  · ID: 10.1186/s13321-020-00469-w
- **Lu, *J. Chem. Phys.* 2024, 161, 082503 — DOI 10.1063/5.0216272**  (J. Chem. Phys.; 2024)  · ID: 10.1063/5.0216272

### wave2 · engine-abstraction

- **年 Steensen 等《The Interoperability Challenge in DFT Workflows Across Implementations》**  (年份未知)  · ID: arXiv:2511.11524  · [OA链接](https://arxiv.org/html/2511.11524v1) `[可下载]`

### wave2 · gaussian-orca

- **能量跨度模型（Kozuch & Shaik, Acc. Chem. Res. 2011）**  (2011)  · ID: 10.1021/ar1000956  · [OA链接](https://pubs.acs.org/doi/10.1021/ar1000956) `[可下载]`
- **Best-Practice DFT(Bursch, Angew 2022)**  (2022)  · ID: 10.1002/anie.202205735  · [OA链接](https://onlinelibrary.wiley.com/doi/10.1002/anie.202205735) `[可下载]`
- **WIREs 2023**  (2023)  · ID: 10.1002/wcms.1663  · [OA链接](https://wires.onlinelibrary.wiley.com/doi/full/10.1002/wcms.1663) `[可下载]`
- **JCTC 2025 论文**  (2025)  · ID: 10.1021/acs.jctc.5c02141  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.jctc.5c02141) `[可下载]`
- **未命名文献片段（10.1021/acs.jctc.2c00395）**  (年份未知)  · ID: 10.1021/acs.jctc.2c00395  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.jctc.2c00395) `[可下载]`

### wave2 · img2structure

- **MolScribe(J. Chem. Inf. Model. 2022)**  (2022)  · ID: 10.1021/acs.jcim.2c01480  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.jcim.2c01480) `[可下载]`
- **MolNexTR(J. Cheminformatics 2024,当前 SOTA)**  (2024)  · ID: 10.1186/s13321-024-00926-w  · [OA链接](https://link.springer.com/article/10.1186/s13321-024-00926-w) `[可下载]`
- **AutoMat**  (年份未知)  · ID: arXiv:2505.12650  · [OA链接](https://arxiv.org/abs/2505.12650) `[可下载]`
- **OCSR 综述**  (年份未知)  · ID: 10.1186/s13321-022-00642-3  · [OA链接](https://jcheminf.biomedcentral.com/articles/10.1186/s13321-022-00642-3) `[可下载]`

### wave2 · multiwfn-vmd

- **IGMH 论文（ChemRxiv，后正式发表于 J. Comput. Chem.）**  (年份未知)  · ID: 10.26434/chemrxiv-2021-628vh-v2  · [OA链接](https://chemrxiv.org/doi/full/10.26434/chemrxiv-2021-628vh-v2) `[可下载]`

### wave2 · starpivot

- **已发表 JPCB 2025**  (2025)  · ID: 10.1021/acs.jpcb.5c05851  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.jpcb.5c05851) `[可下载]`
- **《CP2K Made Simple》**  (年份未知)  · ID: arXiv:2508.15559  · [OA链接](https://arxiv.org/html/2508.15559v1) `[可下载]`

### wave3 · ai-paper2flow

- **《AI 时代电子结构包》**  (年份未知)  · ID: arXiv:2501.08697  · [OA链接](https://arxiv.org/abs/2501.08697) `[可下载]`

### wave3 · candidate-gen

- **科学软件许可证选择指南（PLOS Comput Biol）**  (年份未知)  · ID: 10.1371/journal.pcbi.1002598  · [OA链接](https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1002598) `[可下载]`

### wave3 · figs-battery

- **Moderate LiPS solubility electrolytes, PNAS 2023**  (2023)  · ID: 10.1073/pnas.2301260120  · [OA链接](https://www.pnas.org/doi/10.1073/pnas.2301260120) `[可下载]`
- **Steering 2e/4e ORR selectivity: importance of the volcano slope, ACS Phys. Chem. Au 2023**  (2023)  · ID: 10.1021/acsphyschemau.2c00054  · [OA链接](https://pubs.acs.org/doi/10.1021/acsphyschemau.2c00054) `[可下载]`
- **第一性原理计算应用于锂硫电池研究的评述, 化学进展 2023**  (2023)  · ID: 10.7536/PC220819  · [OA链接](https://manu56.magtech.com.cn/progchem/CN/10.7536/PC220819) `[可下载]`
- **CNN + volcano plots for 2D SAC CO2RR, arXiv 2024**  (2024)  · ID: arXiv:2402.03876  · [OA链接](https://arxiv.org/html/2402.03876v1) `[可下载]`
- **Data- and mechanistic-driven volcano plots, Electrochem. Sci. Adv. 2024 (Exner)**  (2024)  · ID: 10.1002/elsa.202200014  · [OA链接](https://chemistry-europe.onlinelibrary.wiley.com/doi/10.1002/elsa.202200014) `[可下载]`
- **MD diffusion analysis in β-Li3PS4, ACS Appl. Energy Mater.**  (年份未知)  · ID: 10.1021/acsaem.8b00457  · [OA链接](https://pubs.acs.org/doi/10.1021/acsaem.8b00457) `[可下载]`
- **Universality in ORR on metal surfaces, ACS Catalysis**  (年份未知)  · ID: 10.1021/cs300227s  · [OA链接](https://pubs.acs.org/doi/10.1021/cs300227s) `[可下载]`

### wave3 · figs-electronic

- **Campbell, The Degree of Rate Control(ACS Catal. 2017)**  (2017)  · ID: 10.1021/acscatal.7b00115  · [OA链接](https://pubs.acs.org/doi/10.1021/acscatal.7b00115) `[可下载]`
- **Quantifying Confidence in DFT Surface Pourbaix Diagrams(Langmuir 2018,不确定性)**  (2018)  · ID: 10.1021/acs.langmuir.8b02219  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.langmuir.8b02219) `[可下载]`
- **Quantifying Confidence in DFT Surface Pourbaix Diagrams(Langmuir 2018,不确定性): (预印本**  (2018)  · ID: arXiv:1710.08407  · [OA链接](https://arxiv.org/pdf/1710.08407) `[可下载]`
- **Energy Maps of Complex Catalyst Surfaces(ACS Catal. 2024)**  (2024)  · ID: 10.1021/acscatal.4c01308  · [OA链接](https://pubs.acs.org/doi/10.1021/acscatal.4c01308) `[可下载]`
- **CatMaster: An Agentic Autonomous System for Computational Heterogeneous Catalysis**  (年份未知)  · ID: arXiv:2601.13508  · [OA链接](https://arxiv.org/html/2601.13508v2) `[可下载]`
- **Information-Rich Graphical Representation of Catalytic Cycles(Organometallics)**  (年份未知)  · ID: 10.1021/acs.organomet.9b00563  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.organomet.9b00563) `[可下载]`
- **Liao, DFT for Electrocatalysis(综述)**  (年份未知)  · ID: 10.1002/eem2.12204  · [OA链接](https://onlinelibrary.wiley.com/doi/full/10.1002/eem2.12204) `[可下载]`
- **ioChem-BD 平台(JCIM)**  (年份未知)  · ID: 10.1021/ci500593j  · [OA链接](https://pubs.acs.org/doi/10.1021/ci500593j) `[可下载]`

### wave3 · figs-energetics

- **DensityTool(空间+自旋分辨 DOS/密度后处理)**  (年份未知)  · ID: arXiv:2112.11050  · [OA链接](https://arxiv.org/pdf/2112.11050) `[可下载]`

### wave3 · figs-structure

- **计算论文出图建议(Organometallics)**  (年份未知)  · ID: 10.1021/acs.organomet.8b00942  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.organomet.8b00942) `[可下载]`

### wave3 · market-software

- **ChemDataExtractor(JCIM 2016)**  (2016)  · ID: 10.1021/acs.jcim.6b00207  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.jcim.6b00207) `[可下载]`
- **中文:物理学报 2025 综述**  (2025)  · ID: 10.7498/aps.74.20250497  · [OA链接](https://wulixb.iphy.ac.cn/article/doi/10.7498/aps.74.20250497) `[可下载]`
- **2.0 本体**  (年份未知)  · ID: 10.1021/acs.jcim.1c00446  · [OA链接](https://pubs.acs.org/doi/abs/10.1021/acs.jcim.1c00446) `[可下载]`
- **AdsMind**  (年份未知)  · ID: arXiv:2606.19152  · [OA链接](https://arxiv.org/html/2606.19152) `[可下载]`
- **Adsorb-Agent**(arXiv 2410.16658**  (年份未知)  · ID: arXiv:2410.16658  · [OA链接](https://arxiv.org/abs/2410.16658) `[可下载]`
- **BatteryBERT**  (年份未知)  · ID: 10.1021/acs.jcim.2c00035  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.jcim.2c00035) `[可下载]`
- **Catalyst-Agent**  (年份未知)  · ID: arXiv:2603.01311  · [OA链接](https://arxiv.org/abs/2603.01311) `[可下载]`
- **El Agente Q — arXiv 2505.02484**  (年份未知)  · ID: arXiv:2505.02484  · [OA链接](https://arxiv.org/abs/2505.02484) `[可下载]`
- **El Agente Quntur — arXiv 2602.04850**  (年份未知)  · ID: arXiv:2602.04850  · [OA链接](https://arxiv.org/abs/2602.04850) `[可下载]`
- **MOF:ChatGPT 文本挖掘(JACS)**  (年份未知)  · ID: 10.1021/jacs.3c05819  · [OA链接](https://pubs.acs.org/doi/10.1021/jacs.3c05819) `[可下载]`
- **OQMD**  (年份未知)  · ID: 10.1088/2515-7639/ac7ba9  · [OA链接](https://iopscience.iop.org/article/10.1088/2515-7639/ac7ba9) `[可下载]`
- **Text-to-Battery Recipe**  (年份未知)  · ID: arXiv:2407.15459  · [OA链接](https://arxiv.org/pdf/2407.15459) `[可下载]`
- **自进化催化数字孪生**  (年份未知)  · ID: arXiv:2606.05050  · [OA链接](https://arxiv.org/html/2606.05050) `[可下载]`
- **表面体系评测**  (年份未知)  · ID: 10.1021/acsami.4c03815  · [OA链接](https://pubs.acs.org/doi/10.1021/acsami.4c03815) `[可下载]`
- **辅助:k 点自动生成**  (年份未知)  · ID: arXiv:2512.15303  · [OA链接](https://arxiv.org/html/2512.15303) `[可下载]`
- **通用材料计算 agent**  (年份未知)  · ID: arXiv:2512.19458  · [OA链接](https://arxiv.org/abs/2512.19458) `[可下载]`
- **链接：arXiv 2602.00185**  (年份未知)  · ID: arXiv:2602.00185  · [OA链接](https://arxiv.org/html/2602.00185v1) `[可下载]`
- **高分辨电子成像解码**  (年份未知)  · ID: 10.1126/sciadv.aaw1949  · [OA链接](https://www.science.org/doi/10.1126/sciadv.aaw1949) `[可下载]`

### wave3 · opensource-sci

- **定制杂元素掺杂石墨烯发现 SAC(Acc. Mater. Res. 2021)**  (2021)  · ID: 10.1021/accountsmr.1c00016  · [OA链接](https://pubs.acs.org/doi/10.1021/accountsmr.1c00016) `[可下载]`
- **杂原子稳定单金属原子石墨烯电催化剂计算筛选(Frontiers in Chemistry 2022)**  (2022)  · ID: 10.3389/fchem.2022.873609/full  · [OA链接](https://www.frontiersin.org/journals/chemistry/articles/10.3389/fchem.2022.873609/full) `[可下载]`
- **SAC 内禀描述符数据驱动设计 CO2RR(Trans. Tianjin Univ. 2024)**  (2024)  · ID: 10.1007/s12209-024-00413-1  · [OA链接](https://link.springer.com/article/10.1007/s12209-024-00413-1) `[可下载]`
- **SAC 配位环境综述(Acc. Chem. Res. 2025)**  (2025)  · ID: 10.1021/acs.accounts.5c00140  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.accounts.5c00140) `[可下载]`
- **计算 SAC 数据库 + ML 辅助设计(JPCC 2025)**  (2025)  · ID: 10.1021/acs.jpcc.5c00491  · [OA链接](https://pubs.acs.org/doi/10.1021/acs.jpcc.5c00491) `[可下载]`

### wave6 · peers-atomagents

- **arXiv 2502.09565。）**  (年份未知)  · ID: arXiv:2502.09565
- **两仓 `LICENSE`。论文：arXiv 2407.10022 / PNAS 122(4):e2414074122**  (年份未知)  · ID: arXiv:2407.10022

### wave6 · peers-benchmarks

- **ScienceAgentBench (ICLR'25) — arXiv 2410.05080**  (年份未知)  · ID: arXiv:2410.05080  · [OA链接](https://arxiv.org/abs/2410.05080) `[可下载]`
- **arXiv 2308.09115**  (年份未知)  · ID: arXiv:2308.09115  · [OA链接](https://arxiv.org/abs/2308.09115) `[可下载]`
- **arXiv 2404.01475**  (年份未知)  · ID: arXiv:2404.01475  · [OA链接](https://arxiv.org/abs/2404.01475) `[可下载]`

### wave6 · peers-chatdft

- **LLaMP arXiv 2401.17244**  (年份未知)  · ID: arXiv:2401.17244  · [OA链接](https://arxiv.org/abs/2401.17244) `[可下载]`

### wave6 · peers-chatmof

- **arXiv 2308.01423**  (年份未知)  · ID: arXiv:2308.01423  · [OA链接](https://arxiv.org/abs/2308.01423) `[可下载]`
- **arXiv 2410.03963（preprint）**  (年份未知)  · ID: arXiv:2410.03963  · [OA链接](https://arxiv.org/html/2410.03963v1) `[可下载]`

### wave6 · peers-chemcrow

- **ChemCrow arXiv:2304.05376**  (年份未知)  · ID: arXiv:2304.05376  · [OA链接](https://arxiv.org/abs/2304.05376) `[可下载]`

### wave6 · peers-discover

- **Eindhoven 理工的「力场自动抽取多智能体框架」**  (年份未知)  · ID: arXiv:2509.10210
- **数据 Zenodo doi:10.5281/zenodo.20626167**  (年份未知)  · ID: 10.5281/zenodo.20626167
- **链接：arXiv 2512.10034**  (年份未知)  · ID: arXiv:2512.10034  · [OA链接](https://arxiv.org/abs/2512.10034) `[可下载]`
- **链接：arXiv 2606.09422**  (年份未知)  · ID: arXiv:2606.09422  · [OA链接](https://arxiv.org/abs/2606.09422) `[可下载]`

### wave6 · peers-elagente

- **El Agente Forjador — arXiv 2604.14609**  (年份未知)  · ID: arXiv:2604.14609  · [OA链接](https://arxiv.org/abs/2604.14609) `[可下载]`
- **基准数据在 U of T Borealis Dataverse**  (年份未知)  · ID: 10.5683/SP3/JU2BQK

### wave6 · peers-skills-sci

- **AtomisticSkills**  (年份未知)  · ID: arXiv:2605.24002  · [OA链接](https://arxiv.org/abs/2605.24002) `[可下载]`
- **arXiv 2603.25522**  (年份未知)  · ID: arXiv:2603.25522  · [OA链接](https://arxiv.org/abs/2603.25522) `[可下载]`
- **jinzhe / computational-chemistry-agent-skills**  (年份未知)  · ID: 10.1021/acs.jctc.6c00622  · [OA链接](https://doi.org/10.1021/acs.jctc.6c00622) `[可下载]`

### wave6 · peers-vaspilot

- **Chinese Physics B / IOPscience DOI 10.1088/1674-1056/ae0681**  (年份未知)  · ID: 10.1088/1674-1056/ae0681  · [OA链接](https://iopscience.iop.org/article/10.1088/1674-1056/ae0681) `[可下载]`

