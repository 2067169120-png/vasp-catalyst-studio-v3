"""DFT 任务类型目录:GUI 消费的总注册表——「选类型 → 派生作业 → 解析 → 出图」的入口清单。

把周期性 DFT 常用计算类型汇成一张表:每条含唯一 key、中英文名称/分类/说明/前置/产物、派生器引用
(``模块:函数`` 字符串,resolve_builder 可 import 解析)、前置条件、产物、关联图型。GUI 据此
渲染「新建任务」菜单,选中即知道调哪个派生器、需要什么前置、出什么图。

分类(五类):
- 基础:结构/晶胞优化、静态单点、吸附能项目、多自旋(建模与基本能量)。
- 电子结构:DOS/PDOS、能带、Bader、差分电荷、ELF。
- 热力学与动力学:频率/ZPE、AIMD、NEB、Dimer。
- 性质:EOS、表面能、功函数、形成能/结合能、VASPsol 溶剂化。
- 收敛与校验:ENCUT/k/真空/层厚 收敛扫描。

builder_ref 一律 ``vcstudio.xxx.module:function``;requires 写前置(如「完成弛豫」「自洽 CHGCAR」),
outputs 写产物,figure 为关联图型 key(或 None)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import importlib

CATEGORIES = ('基础', '电子结构', '热力学与动力学', '性质', '收敛与校验')
CATEGORY_EN = {
    '基础': 'Fundamentals',
    '电子结构': 'Electronic Structure',
    '热力学与动力学': 'Thermodynamics and Kinetics',
    '性质': 'Properties',
    '收敛与校验': 'Convergence and Validation',
}

# 每条:key / name_zh|name_en / category|category_en / description|description_en /
# builder_ref / requires|requires_en / outputs|outputs_en / next_action_en / figure
CATALOG = [
    # ── 基础 ──────────────────────────────────────────────────────────────────
    {'key': 'relax', 'name_zh': '结构优化(固定胞弛豫)',
     'name_en': 'Structural Relaxation (Fixed Cell)',
     'category': '基础', 'category_en': 'Fundamentals',
     'description': '固定晶胞、弛豫离子到力收敛,催化建模第一步。',
     'description_en': ('Relax the ions at fixed cell parameters until the force criterion is met; '
                        'the first step in catalyst modeling.'),
     'builder_ref': 'vcstudio.generate.job_builder:build_job_dir',
     'requires': 'POSCAR + 用户 INCAR', 'outputs': 'CONTCAR/OUTCAR/OSZICAR',
     'requires_en': 'POSCAR + user-supplied INCAR',
     'outputs_en': 'CONTCAR/OUTCAR/OSZICAR',
     'next_action_en': ('Inspect the final-step energy, forces, and ionic steps; after convergence, '
                        'derive a static calculation.'),
     'figure': None},
    {'key': 'cellopt', 'name_zh': '晶胞优化(变胞弛豫 ISIF=3)',
     'name_en': 'Cell Optimization (Variable Cell, ISIF=3)',
     'category': '基础', 'category_en': 'Fundamentals',
     'description': '放开晶胞求平衡晶格常数;注意 Pulay 应力(建议 ENCUT×1.3)。',
     'description_en': ('Optimize the cell to obtain equilibrium lattice parameters; control '
                        'Pulay stress (ENCUT × 1.3 recommended).'),
     'builder_ref': 'vcstudio.generate.cell_opt:build_cellopt_job',
     'requires': '完成固定胞弛豫', 'outputs': 'CONTCAR(平衡晶格)',
     'requires_en': 'Completed fixed-cell relaxation',
     'outputs_en': 'CONTCAR (equilibrium lattice)',
     'next_action_en': ('Inspect the final energy and forces, then use CONTCAR as the equilibrium '
                        'structure for downstream calculations.'),
     'figure': None},
    {'key': 'static', 'name_zh': '静态单点',
     'name_en': 'Static Single-Point Calculation',
     'category': '基础', 'category_en': 'Fundamentals',
     'description': '固定几何单点自洽,取能量/电荷密度;下游电子结构分析的母作业。',
     'description_en': ('Run a self-consistent single-point calculation at fixed geometry to obtain '
                        'the energy and charge density; this is the parent job for downstream '
                        'electronic-structure analyses.'),
     'builder_ref': 'vcstudio.generate.estatic:build_static_job',
     'requires': '完成弛豫', 'outputs': 'OUTCAR/CHGCAR/WAVECAR',
     'requires_en': 'Completed relaxation',
     'outputs_en': 'OUTCAR/CHGCAR/WAVECAR',
     'next_action_en': 'Inspect the single-point energy and electronic-convergence evidence.',
     'figure': None},
    {'key': 'adsorption_project', 'name_zh': '吸附能项目',
     'name_en': 'Adsorption-Energy Project',
     'category': '基础', 'category_en': 'Fundamentals',
     'description': 'clean slab / slab+吸附质 / 气相参考 三成员组织成 ΔE 可比项目(统一 ENCUT)。',
     'description_en': ('Organize a clean slab, an adsorbate-covered slab, and a gas-phase reference '
                        'into a comparable ΔE project with a consistent ENCUT.'),
     'builder_ref': 'vcstudio.project.adsorption:create_project',
     'requires': 'slab 与吸附质结构', 'outputs': '项目目录 + ΔE 台账',
     'requires_en': 'Slab and adsorbate structures',
     'outputs_en': 'Project directory + ΔE ledger',
     'next_action_en': ('Open the Adsorption-Energy Project and select the clean slab, adsorbed '
                        'state, and reference state.'),
     'figure': 'adsorption_bar'},
    {'key': 'spin_scan', 'name_zh': '多自旋态扫描',
     'name_en': 'Spin-State Scan',
     'category': '基础', 'category_en': 'Fundamentals',
     'description': '枚举不同 NUPDOWN/初始磁矩变体,挑磁性基态,避免落到亚稳自旋态。',
     'description_en': ('Enumerate variants with different NUPDOWN values and initial magnetic '
                        'moments to identify the magnetic ground state and avoid metastable '
                        'spin states.'),
     'builder_ref': 'vcstudio.project.spin_scan:build_spin_variants',
     'requires': '完成弛豫(含磁性元素)', 'outputs': '各自旋变体作业 + 基态判定',
     'requires_en': 'Completed relaxation (system containing magnetic elements)',
     'outputs_en': 'Spin-variant jobs + ground-state determination',
     'next_action_en': ('Open Spin-State Comparison, aggregate every variant, and determine the '
                        'magnetic ground state from comparable evidence.'),
     'figure': None},

    # ── 电子结构 ──────────────────────────────────────────────────────────────
    {'key': 'dos_pdos', 'name_zh': 'DOS / 投影 PDOS',
     'name_en': 'Total DOS / Projected DOS (PDOS)',
     'category': '电子结构', 'category_en': 'Electronic Structure',
     'description': '加密 k 网格静态 + LORBIT=11,出总态密度与轨道投影(d 带中心)。',
     'description_en': ('Run a static calculation with a denser k-point mesh and LORBIT=11 to obtain '
                        'the total and orbital-projected densities of states, including the '
                        'd-band center.'),
     'builder_ref': 'vcstudio.generate.estatic:build_static_job',
     'requires': '完成弛豫', 'outputs': 'DOSCAR/vasprun.xml',
     'requires_en': 'Completed relaxation',
     'outputs_en': 'DOSCAR/vasprun.xml',
     'next_action_en': ('Parse the projected DOS from vasprun.xml and inspect the system-wide '
                        'd-band center.'),
     'figure': 'pdos'},
    {'key': 'bands', 'name_zh': '能带结构',
     'name_en': 'Band Structure',
     'category': '电子结构', 'category_en': 'Electronic Structure',
     'description': '两步法:自洽 CHGCAR → 非自洽(ICHARG=11)沿高对称路径,出 E(k) 与带隙。',
     'description_en': ('Use a two-step workflow: a self-consistent CHGCAR followed by a '
                        'non-self-consistent calculation (ICHARG=11) along a high-symmetry path, '
                        'yielding E(k) and the band gap.'),
     'builder_ref': 'vcstudio.generate.bands_builder:build_bands_job',
     'requires': '自洽 CHGCAR', 'outputs': 'EIGENVAL/vasprun.xml',
     'requires_en': 'Self-consistent CHGCAR',
     'outputs_en': 'EIGENVAL/vasprun.xml',
     'next_action_en': 'Read EIGENVAL/vasprun.xml and evaluate the band structure and band gap.',
     'figure': 'band'},
    {'key': 'bader', 'name_zh': 'Bader 电荷',
     'name_en': 'Bader Charge Analysis',
     'category': '电子结构', 'category_en': 'Electronic Structure',
     'description': '静态 + LAECHG,Bader 分析得原子净电荷/电荷转移。',
     'description_en': ('Run a static calculation with LAECHG, then perform Bader analysis to '
                        'obtain atomic net charges and charge transfer.'),
     'builder_ref': 'vcstudio.generate.estatic:build_static_job',
     'requires': '完成弛豫', 'outputs': 'AECCAR/CHGCAR → ACF.dat',
     'requires_en': 'Completed relaxation',
     'outputs_en': 'AECCAR/CHGCAR → ACF.dat',
     'next_action_en': ('Run the Henkelman Bader program on CHGCAR/AECCAR0/AECCAR2 to create '
                        'ACF.dat, then import ACF.dat for analysis and reporting.'),
     'figure': None},
    {'key': 'chgdiff', 'name_zh': '差分电荷密度',
     'name_en': 'Charge-Density Difference',
     'category': '电子结构', 'category_en': 'Electronic Structure',
     'description': 'Δρ=ρ(AB)−ρ(A)−ρ(B) 三静态派生 + 网格代数 + 面平均 Δρ̄(z)。',
     'description_en': ('Derive three static calculations for Δρ=ρ(AB)−ρ(A)−ρ(B), perform grid '
                        'arithmetic, and compute the planar average Δρ̄(z).'),
     'builder_ref': 'vcstudio.project.chgdiff:build_chgdiff_jobs',
     'requires': '完成吸附态弛豫', 'outputs': 'CHGDIFF.vasp',
     'requires_en': 'Completed relaxation of the adsorbed state',
     'outputs_en': 'CHGDIFF.vasp',
     'next_action_en': 'Read CHGDIFF.vasp and generate the surface-normal planar average Δρ(z).',
     'figure': 'charge_profile'},
    {'key': 'elf', 'name_zh': 'ELF 电子局域函数',
     'name_en': 'Electron Localization Function (ELF)',
     'category': '电子结构', 'category_en': 'Electronic Structure',
     'description': '静态 + LELF=.TRUE. 产出 ELFCAR,VESTA 可视化共价/孤对/金属键。',
     'description_en': ('Run a static calculation with LELF=.TRUE. to produce ELFCAR for VESTA '
                        'visualization of covalent bonds, lone pairs, and metallic bonding.'),
     'builder_ref': 'vcstudio.generate.estatic:build_static_job',
     'requires': '完成弛豫', 'outputs': 'ELFCAR',
     'requires_en': 'Completed relaxation', 'outputs_en': 'ELFCAR',
     'next_action_en': 'Verify ELFCAR, then inspect its isosurfaces in VESTA or an equivalent tool.',
     'figure': None},

    # ── 热力学与动力学 ──────────────────────────────────────────────────────────
    {'key': 'freq', 'name_zh': '频率 / ZPE 热校正',
     'name_en': 'Vibrational Frequencies / ZPE Thermal Corrections',
     'category': '热力学与动力学', 'category_en': 'Thermodynamics and Kinetics',
     'description': '有限差分频率(IBRION=5),出 ZPE/熵,把 ΔE 升到 ΔG;虚频质量闸验证。',
     'description_en': ('Compute finite-difference vibrational frequencies (IBRION=5), ZPE, and '
                        'entropy to convert ΔE to ΔG; validate the result with an '
                        'imaginary-frequency quality gate.'),
     'builder_ref': 'vcstudio.generate.freq_builder:build_freq_job',
     'requires': '完成弛豫', 'outputs': 'OUTCAR(频率)',
     'requires_en': 'Completed relaxation',
     'outputs_en': 'OUTCAR (vibrational frequencies)',
     'next_action_en': ('Apply the imaginary-frequency quality gate before allowing thermal '
                        'corrections into a free-energy analysis.'),
     'figure': None},
    {'key': 'aimd', 'name_zh': 'AIMD 热稳定性',
     'name_en': 'Ab Initio Molecular Dynamics (AIMD) Thermal Stability',
     'category': '热力学与动力学', 'category_en': 'Thermodynamics and Kinetics',
     'description': 'NVT/NVE 分子动力学(Nose-Hoover),看有限温度下能量-时间是否平稳。',
     'description_en': ('Run NVT or NVE molecular dynamics with a Nose–Hoover thermostat and assess '
                        'the stability of the energy-time trajectory at finite temperature.'),
     'builder_ref': 'vcstudio.generate.aimd_builder:build_aimd_job',
     'requires': '完成弛豫', 'outputs': 'OSZICAR/XDATCAR',
     'requires_en': 'Completed relaxation',
     'outputs_en': 'OSZICAR/XDATCAR',
     'next_action_en': ('Inspect the energy and temperature series; draw thermal-stability '
                        'conclusions only from a sufficiently long trajectory.'),
     'figure': None},
    {'key': 'neb', 'name_zh': 'NEB 过渡态(始末已知)',
     'name_en': 'Nudged Elastic Band (NEB) Transition State (Known Endpoints)',
     'category': '热力学与动力学', 'category_en': 'Thermodynamics and Kinetics',
     'description': '始态+末态插值 + CI-NEB 找最小能量路径与能垒。',
     'description_en': ('Interpolate between the initial and final states, then use CI-NEB to find '
                        'the minimum-energy path and activation barrier.'),
     'builder_ref': 'vcstudio.generate.neb_builder:build_neb_dir',
     'requires': '始态/末态弛豫', 'outputs': '各像 OUTCAR',
     'requires_en': 'Relaxed initial and final states',
     'outputs_en': 'OUTCAR for each image',
     'next_action_en': ('Inspect the barrier, highest-energy image, and force convergence of every '
                        'NEB image.'),
     'figure': 'neb_profile'},
    {'key': 'dimer', 'name_zh': 'Dimer 过渡态(单端点)',
     'name_en': 'Dimer Transition State (Single Endpoint)',
     'category': '热力学与动力学', 'category_en': 'Thermodynamics and Kinetics',
     'description': '仅需鞍点初猜 + 初始模式(VTST),爬向一阶鞍点;需 VTST 编译的 VASP。',
     'description_en': ('Starting from an initial saddle-point guess and mode (VTST), climb toward '
                        'a first-order saddle point; this requires a VTST-enabled VASP build.'),
     'builder_ref': 'vcstudio.generate.dimer_builder:build_dimer_job',
     'requires': '鞍点初猜结构 + VTST 版 VASP', 'outputs': 'CONTCAR(鞍点)',
     'requires_en': 'Initial saddle-point guess + VTST-enabled VASP',
     'outputs_en': 'CONTCAR (saddle point)',
     'next_action_en': ('After convergence, run a frequency calculation and confirm exactly one '
                        'significant imaginary mode.'),
     'figure': None},

    # ── 性质 ──────────────────────────────────────────────────────────────────
    {'key': 'eos', 'name_zh': '状态方程 EOS(体弹模量)',
     'name_en': 'Equation of State (EOS; Bulk Modulus)',
     'category': '性质', 'category_en': 'Properties',
     'description': '等比缩放晶格定容单点系列 + Birch-Murnaghan 拟合得 V0/E0/B0/B0′。',
     'description_en': ('Generate a series of fixed-volume single-point calculations by '
                        'isotropically scaling the lattice; fit the Birch–Murnaghan equation to '
                        'obtain V0/E0/B0/B0′.'),
     'builder_ref': 'vcstudio.project.eos:build_eos_series',
     'requires': '平衡结构', 'outputs': 'E-V 系列 → BM3 参数',
     'requires_en': 'Equilibrium structure',
     'outputs_en': 'E–V series → BM3 parameters',
     'next_action_en': 'Complete at least three E–V points, then perform the BM3 fit.',
     'figure': 'eos'},
    {'key': 'surface_energy', 'name_zh': '表面能 γ',
     'name_en': 'Surface Energy γ',
     'category': '性质', 'category_en': 'Properties',
     'description': 'γ=(E_slab−N·E_bulk)/2A;需体相每原子能与同口径 slab 能。',
     'description_en': ('Compute γ=(E_slab−N·E_bulk)/2A using the bulk energy per atom and a slab '
                        'energy obtained with a consistent method.'),
     'builder_ref': 'vcstudio.project.surface_energy:surface_energy',
     'requires': 'slab 能 + 体相每原子能', 'outputs': 'γ (J/m²)',
     'requires_en': 'Slab energy + bulk energy per atom',
     'outputs_en': 'γ (J/m²)',
     'next_action_en': ('Open the Surface-Energy Calculator and select the slab and bulk reference '
                        'together.'),
     'figure': None},
    {'key': 'workfunction', 'name_zh': '功函数 φ',
     'name_en': 'Work Function φ',
     'category': '性质', 'category_en': 'Properties',
     'description': 'slab 静态 + LVTOT → LOCPOT 面平均,φ=真空能级−E_F;非对称加偶极校正。',
     'description_en': ('Run a static slab calculation with LVTOT and obtain the planar average of '
                        'LOCPOT; φ=vacuum level−E_F. Apply a dipole correction for asymmetric slabs.'),
     'builder_ref': 'vcstudio.project.workfunction:build_workfunction_job',
     'requires': '完成 slab 弛豫', 'outputs': 'LOCPOT → φ',
     'requires_en': 'Completed slab relaxation',
     'outputs_en': 'LOCPOT → φ',
     'next_action_en': 'Read LOCPOT and the Fermi energy in OUTCAR, then calculate the work function.',
     'figure': 'work_function'},
    {'key': 'formation_binding', 'name_zh': '形成能 / 结合能',
     'name_en': 'Formation Energy / Binding Energy',
     'category': '性质', 'category_en': 'Properties',
     'description': '相对参考态的形成能与吸附/掺杂结合能(须统一泛函/赝势口径)。',
     'description_en': ('Compute formation energies relative to reference states and binding '
                        'energies for adsorption or doping using a consistent functional and '
                        'pseudopotential setup.'),
     'builder_ref': 'vcstudio.project.references:binding_energy',
     'requires': '体系能 + 参考态能', 'outputs': 'E_form / E_bind (eV)',
     'requires_en': 'System energy + reference-state energies',
     'outputs_en': 'E_form / E_bind (eV)',
     'next_action_en': ('Open the Formation/Binding-Energy Calculator and explicitly select every '
                        'reference state.'),
     'figure': None},
    {'key': 'vaspsol', 'name_zh': 'VASPsol 隐式溶剂化',
     'name_en': 'VASPsol Implicit Solvation',
     'category': '性质', 'category_en': 'Properties',
     'description': '一次派生同几何真空/溶剂静态配对；完成后严格核对方法并计算 E_sol−E_vac。',
     'description_en': ('Derive paired vacuum and solvated static calculations at the same geometry; '
                        'after completion, strictly verify method consistency and compute '
                        'E_sol−E_vac.'),
     'builder_ref': 'vcstudio.generate.vaspsol_pair:build_pair',
     'requires': '已完成结构 + VASPsol 版 VASP', 'outputs': '真空/溶剂配对 + ΔE_solv 报告',
     'requires_en': 'Completed structure + VASP built with VASPsol',
     'outputs_en': 'Vacuum/solvent pair + ΔE_solv report',
     'next_action_en': ('Select the vacuum and solvated results at identical geometry, verify '
                        'method consistency, and calculate the solvation energy.'),
     'figure': None},

    # ── 收敛与校验 ──────────────────────────────────────────────────────────────
    {'key': 'conv_encut', 'name_zh': '收敛扫描:ENCUT',
     'name_en': 'ENCUT Convergence Scan',
     'category': '收敛与校验', 'category_en': 'Convergence and Validation',
     'description': '只改 ENCUT 的一串单点,判平面波截断能收敛点(发文必做)。',
     'description_en': ('Run a series of single-point calculations varying only ENCUT to determine '
                        'the converged plane-wave cutoff for publication-quality work.'),
     'builder_ref': 'vcstudio.generate.conv_scan:build_encut_series',
     'requires': '任一结构 + INCAR', 'outputs': 'E vs ENCUT',
     'requires_en': 'Any structure + INCAR',
     'outputs_en': 'E vs ENCUT',
     'next_action_en': ('Complete the full scan, then select the converged ENCUT from the '
                        'per-atom energy differences.'),
     'figure': 'convergence'},
    {'key': 'conv_kmesh', 'name_zh': '收敛扫描:k 网格',
     'name_en': 'k-Point Mesh Convergence Scan',
     'category': '收敛与校验', 'category_en': 'Convergence and Validation',
     'description': '只改 KPOINTS 网格的一串单点,判 k 点收敛点。',
     'description_en': ('Run a series of single-point calculations varying only the KPOINTS mesh '
                        'to determine k-point convergence.'),
     'builder_ref': 'vcstudio.generate.conv_scan:build_kmesh_series',
     'requires': '任一结构 + INCAR', 'outputs': 'E vs k 点数',
     'requires_en': 'Any structure + INCAR',
     'outputs_en': 'E vs number of k-points',
     'next_action_en': ('Complete the full scan, then select the converged k-point mesh from the '
                        'per-atom energy differences.'),
     'figure': 'convergence'},
    {'key': 'conv_vacuum', 'name_zh': '收敛扫描:真空层',
     'name_en': 'Vacuum-Thickness Convergence Scan',
     'category': '收敛与校验', 'category_en': 'Convergence and Validation',
     'description': '只改 slab c 真空的一串单点,判真空层是否足够(消除周期镜像作用)。',
     'description_en': ('Run a series of single-point calculations varying only the vacuum along '
                        'the slab c axis to determine a sufficient separation from periodic images.'),
     'builder_ref': 'vcstudio.generate.conv_scan:build_vacuum_series',
     'requires': 'slab 结构 + INCAR', 'outputs': 'E vs 真空',
     'requires_en': 'Slab structure + INCAR',
     'outputs_en': 'E vs vacuum thickness',
     'next_action_en': 'Complete the full scan, then verify convergence with vacuum thickness.',
     'figure': 'convergence'},
    {'key': 'conv_thickness', 'name_zh': '收敛扫描:slab 层厚',
     'name_en': 'Slab-Thickness Convergence Scan',
     'category': '收敛与校验', 'category_en': 'Convergence and Validation',
     'description': ('重建不同层数 slab 的一串单点,判层厚收敛;源作业须带可再生配方'
                     '(来自「金属 slab 建模」,job.yaml inputs.recipe)。'),
     'description_en': ('Rebuild slabs with different numbers of layers and run a series of '
                        'single-point calculations to determine thickness convergence; the source '
                        'job must include a reproducible recipe from Metal Slab Modeling '
                        '(job.yaml inputs.recipe).'),
     'builder_ref': 'vcstudio.generate.conv_scan:build_slab_thickness_series',
     'requires': '金属 slab 建模作业(配方可再生)', 'outputs': 'E vs 层数',
     'requires_en': 'Metal-slab modeling job (reproducible recipe available)',
     'outputs_en': 'E vs number of layers',
     'next_action_en': 'Complete the full scan, then verify convergence with the number of slab layers.',
     'figure': 'convergence'},
]


def resolve_builder(ref: str):
    """``'module:function'`` → 真实可调对象(import + getattr)。格式错/找不到 → ValueError/ImportError。"""
    if ':' not in str(ref):
        raise ValueError(f'builder_ref 须为 "module:function" 格式,收到 {ref!r}')
    mod_name, func_name = str(ref).split(':', 1)
    mod = importlib.import_module(mod_name)
    if not hasattr(mod, func_name):
        raise ValueError(f'{mod_name} 无函数 {func_name}(builder_ref={ref!r})')
    return getattr(mod, func_name)


def list_catalog(category: str | None = None) -> list:
    """列出任务目录(深拷贝);category 非空则按分类过滤。未知分类 → ValueError。"""
    if category is not None and category not in CATEGORIES:
        raise ValueError(f'未知分类 {category!r};可选:{CATEGORIES}')
    import copy
    return [copy.deepcopy(t) for t in CATALOG
            if category is None or t['category'] == category]


def get_task(key: str) -> dict:
    """按 key 取任务条目(深拷贝)。未知 key → KeyError(中文,列出可用 key)。"""
    import copy
    for t in CATALOG:
        if t['key'] == key:
            return copy.deepcopy(t)
    raise KeyError(f'未知任务 key:{key!r}(可用:{[t["key"] for t in CATALOG]})')
