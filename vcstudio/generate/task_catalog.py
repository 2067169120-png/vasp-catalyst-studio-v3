"""DFT 任务类型目录:GUI 消费的总注册表——「选类型 → 派生作业 → 解析 → 出图」的入口清单。

把周期性 DFT 常用计算类型汇成一张表:每条含唯一 key、中文名、分类、说明、派生器引用
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

# 每条:key / name_zh / category / description / builder_ref / requires / outputs / figure
CATALOG = [
    # ── 基础 ──────────────────────────────────────────────────────────────────
    {'key': 'relax', 'name_zh': '结构优化(固定胞弛豫)', 'category': '基础',
     'description': '固定晶胞、弛豫离子到力收敛,催化建模第一步。',
     'builder_ref': 'vcstudio.generate.job_builder:build_job_dir',
     'requires': 'POSCAR + 用户 INCAR', 'outputs': 'CONTCAR/OUTCAR/OSZICAR',
     'figure': None},
    {'key': 'cellopt', 'name_zh': '晶胞优化(变胞弛豫 ISIF=3)', 'category': '基础',
     'description': '放开晶胞求平衡晶格常数;注意 Pulay 应力(建议 ENCUT×1.3)。',
     'builder_ref': 'vcstudio.generate.cell_opt:build_cellopt_job',
     'requires': '完成固定胞弛豫', 'outputs': 'CONTCAR(平衡晶格)', 'figure': None},
    {'key': 'static', 'name_zh': '静态单点', 'category': '基础',
     'description': '固定几何单点自洽,取能量/电荷密度;下游电子结构分析的母作业。',
     'builder_ref': 'vcstudio.generate.estatic:build_static_job',
     'requires': '完成弛豫', 'outputs': 'OUTCAR/CHGCAR/WAVECAR', 'figure': None},
    {'key': 'adsorption_project', 'name_zh': '吸附能项目', 'category': '基础',
     'description': 'clean slab / slab+吸附质 / 气相参考 三成员组织成 ΔE 可比项目(统一 ENCUT)。',
     'builder_ref': 'vcstudio.project.adsorption:create_project',
     'requires': 'slab 与吸附质结构', 'outputs': '项目目录 + ΔE 台账',
     'figure': 'adsorption_bar'},
    {'key': 'spin_scan', 'name_zh': '多自旋态扫描', 'category': '基础',
     'description': '枚举不同 NUPDOWN/初始磁矩变体,挑磁性基态,避免落到亚稳自旋态。',
     'builder_ref': 'vcstudio.project.spin_scan:build_spin_variants',
     'requires': '完成弛豫(含磁性元素)', 'outputs': '各自旋变体作业 + 基态判定',
     'figure': None},

    # ── 电子结构 ──────────────────────────────────────────────────────────────
    {'key': 'dos_pdos', 'name_zh': 'DOS / 投影 PDOS', 'category': '电子结构',
     'description': '加密 k 网格静态 + LORBIT=11,出总态密度与轨道投影(d 带中心)。',
     'builder_ref': 'vcstudio.generate.estatic:build_static_job',
     'requires': '完成弛豫', 'outputs': 'DOSCAR/vasprun.xml', 'figure': 'pdos'},
    {'key': 'bands', 'name_zh': '能带结构', 'category': '电子结构',
     'description': '两步法:自洽 CHGCAR → 非自洽(ICHARG=11)沿高对称路径,出 E(k) 与带隙。',
     'builder_ref': 'vcstudio.generate.bands_builder:build_bands_job',
     'requires': '自洽 CHGCAR', 'outputs': 'EIGENVAL/vasprun.xml', 'figure': 'band'},
    {'key': 'bader', 'name_zh': 'Bader 电荷', 'category': '电子结构',
     'description': '静态 + LAECHG,Bader 分析得原子净电荷/电荷转移。',
     'builder_ref': 'vcstudio.generate.estatic:build_static_job',
     'requires': '完成弛豫', 'outputs': 'AECCAR/CHGCAR → ACF.dat', 'figure': None},
    {'key': 'chgdiff', 'name_zh': '差分电荷密度', 'category': '电子结构',
     'description': 'Δρ=ρ(AB)−ρ(A)−ρ(B) 三静态派生 + 网格代数 + 面平均 Δρ̄(z)。',
     'builder_ref': 'vcstudio.project.chgdiff:build_chgdiff_jobs',
     'requires': '完成吸附态弛豫', 'outputs': 'CHGDIFF.vasp', 'figure': 'charge_profile'},
    {'key': 'elf', 'name_zh': 'ELF 电子局域函数', 'category': '电子结构',
     'description': '静态 + LELF=.TRUE. 产出 ELFCAR,VESTA 可视化共价/孤对/金属键。',
     'builder_ref': 'vcstudio.generate.estatic:build_static_job',
     'requires': '完成弛豫', 'outputs': 'ELFCAR', 'figure': None},

    # ── 热力学与动力学 ──────────────────────────────────────────────────────────
    {'key': 'freq', 'name_zh': '频率 / ZPE 热校正', 'category': '热力学与动力学',
     'description': '有限差分频率(IBRION=5),出 ZPE/熵,把 ΔE 升到 ΔG;虚频质量闸验证。',
     'builder_ref': 'vcstudio.generate.freq_builder:build_freq_job',
     'requires': '完成弛豫', 'outputs': 'OUTCAR(频率)', 'figure': None},
    {'key': 'aimd', 'name_zh': 'AIMD 热稳定性', 'category': '热力学与动力学',
     'description': 'NVT/NVE 分子动力学(Nose-Hoover),看有限温度下能量-时间是否平稳。',
     'builder_ref': 'vcstudio.generate.aimd_builder:build_aimd_job',
     'requires': '完成弛豫', 'outputs': 'OSZICAR/XDATCAR', 'figure': None},
    {'key': 'neb', 'name_zh': 'NEB 过渡态(始末已知)', 'category': '热力学与动力学',
     'description': '始态+末态插值 + CI-NEB 找最小能量路径与能垒。',
     'builder_ref': 'vcstudio.generate.neb_builder:build_neb_dir',
     'requires': '始态/末态弛豫', 'outputs': '各像 OUTCAR', 'figure': 'neb_profile'},
    {'key': 'dimer', 'name_zh': 'Dimer 过渡态(单端点)', 'category': '热力学与动力学',
     'description': '仅需鞍点初猜 + 初始模式(VTST),爬向一阶鞍点;需 VTST 编译的 VASP。',
     'builder_ref': 'vcstudio.generate.dimer_builder:build_dimer_job',
     'requires': '鞍点初猜结构 + VTST 版 VASP', 'outputs': 'CONTCAR(鞍点)', 'figure': None},

    # ── 性质 ──────────────────────────────────────────────────────────────────
    {'key': 'eos', 'name_zh': '状态方程 EOS(体弹模量)', 'category': '性质',
     'description': '等比缩放晶格定容单点系列 + Birch-Murnaghan 拟合得 V0/E0/B0/B0′。',
     'builder_ref': 'vcstudio.project.eos:build_eos_series',
     'requires': '平衡结构', 'outputs': 'E-V 系列 → BM3 参数', 'figure': 'eos'},
    {'key': 'surface_energy', 'name_zh': '表面能 γ', 'category': '性质',
     'description': 'γ=(E_slab−N·E_bulk)/2A;需体相每原子能与同口径 slab 能。',
     'builder_ref': 'vcstudio.project.surface_energy:surface_energy',
     'requires': 'slab 能 + 体相每原子能', 'outputs': 'γ (J/m²)', 'figure': None},
    {'key': 'workfunction', 'name_zh': '功函数 φ', 'category': '性质',
     'description': 'slab 静态 + LVTOT → LOCPOT 面平均,φ=真空能级−E_F;非对称加偶极校正。',
     'builder_ref': 'vcstudio.project.workfunction:build_workfunction_job',
     'requires': '完成 slab 弛豫', 'outputs': 'LOCPOT → φ', 'figure': 'work_function'},
    {'key': 'formation_binding', 'name_zh': '形成能 / 结合能', 'category': '性质',
     'description': '相对参考态的形成能与吸附/掺杂结合能(须统一泛函/赝势口径)。',
     'builder_ref': 'vcstudio.project.references:binding_energy',
     'requires': '体系能 + 参考态能', 'outputs': 'E_form / E_bind (eV)', 'figure': None},
    {'key': 'vaspsol', 'name_zh': 'VASPsol 隐式溶剂化', 'category': '性质',
     'description': 'LSOL/EB_K 隐式溶剂化(需 VASPsol 补丁编译);真空/溶剂同几何相减得溶剂化能。',
     'builder_ref': 'vcstudio.generate.incar_builder:vaspsol_keys',
     'requires': '完成真空弛豫 + VASPsol 版 VASP', 'outputs': '溶剂化单点能', 'figure': None},

    # ── 收敛与校验 ──────────────────────────────────────────────────────────────
    {'key': 'conv_encut', 'name_zh': '收敛扫描:ENCUT', 'category': '收敛与校验',
     'description': '只改 ENCUT 的一串单点,判平面波截断能收敛点(发文必做)。',
     'builder_ref': 'vcstudio.generate.conv_scan:build_encut_series',
     'requires': '任一结构 + INCAR', 'outputs': 'E vs ENCUT', 'figure': 'convergence'},
    {'key': 'conv_kmesh', 'name_zh': '收敛扫描:k 网格', 'category': '收敛与校验',
     'description': '只改 KPOINTS 网格的一串单点,判 k 点收敛点。',
     'builder_ref': 'vcstudio.generate.conv_scan:build_kmesh_series',
     'requires': '任一结构 + INCAR', 'outputs': 'E vs k 点数', 'figure': 'convergence'},
    {'key': 'conv_vacuum', 'name_zh': '收敛扫描:真空层', 'category': '收敛与校验',
     'description': '只改 slab c 真空的一串单点,判真空层是否足够(消除周期镜像作用)。',
     'builder_ref': 'vcstudio.generate.conv_scan:build_vacuum_series',
     'requires': 'slab 结构 + INCAR', 'outputs': 'E vs 真空', 'figure': 'convergence'},
    {'key': 'conv_thickness', 'name_zh': '收敛扫描:slab 层厚', 'category': '收敛与校验',
     'description': '重建不同层数 slab 的一串单点,判层厚收敛(需 slab 模板可再生)。',
     'builder_ref': 'vcstudio.generate.conv_scan:build_slab_thickness_series',
     'requires': 'slab 模板(可再生)', 'outputs': 'E vs 层数', 'figure': 'convergence'},
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
