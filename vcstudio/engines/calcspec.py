"""引擎无关中间表示(IR)——CalcSpec + 校验 + 跨引擎不等价清单。

多引擎适配层的**科学层/引擎层解耦点**(采纳蓝图 E22):上游科学层(CHE/ZPE/
参考态)只产出一份与引擎无关的 CalcSpec(结构 + 任务 + 泛函 + 收敛判据…),各引擎
Backend 负责把它翻成自家输入文件、再把自家输出解析回统一能量口径。

三条铁律在本模块落地:
- **不静默翻译**:跨引擎方法映射不等价(ENCUT≠CUTOFF、PAW≠GTH…)必须显式弹清单
  逐项确认。NONEQUIV_MAP 是这份清单的数据源,nonequivalence_report 逐条产中文。
- **显式报错**:CalcSpec.validate 只报问题不静默改;字段不自洽逐条点名(中文)。
- **文件级适配**:CalcSpec 只承载"算什么",不承载"用哪套软件"——软件本体用户自备。

纯 python(不引 numpy);POSCAR 解析复用 structure_view/poscar 既有件(只读)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from vcstudio.generate.poscar import parse_poscar_species, read_cell_vectors
from vcstudio.generate.structure_view import parse_positions

# Hartree → eV(CODATA;CP2K/Gaussian 总能为 a.u.,统一到 eV 口径用它)。
HARTREE_TO_EV = 27.211386

# 合法任务集(与 manifest.KNOWN_TASK_TYPES 的 relax/static/freq 子集对齐;多引擎
# 首期只适配这三类,dos/band/neb/aimd 暂不跨引擎)。
VALID_TASKS = ('relax', 'static', 'freq')

# 已注册引擎名(get_backend 的键;castep 即 Materials Studio 文件级适配)。
KNOWN_ENGINES = ('vasp', 'cp2k', 'gaussian', 'castep')


@dataclass
class CalcSpec:
    """引擎无关的一次计算描述(Intermediate Representation)。

    字段:
        structure:    POSCAR 文本(统一内部结构表示;各引擎自行转 CELL/COORD)。
        task:         'relax' | 'static' | 'freq'。
        functional:   泛函名(如 'PBE'/'RPBE'/'PBEsol';各引擎映射到自家关键字)。
        periodic:     是否周期性体系。True=周期(slab/bulk,需 cutoff/k 点);
                      False=孤立分子(参考态/溶剂化,无 k 点)。**必显字段**:分子与
                      周期的输入模板/校验分支完全不同,绝不从结构猜(盒装分子也有 cell)。
        dispersion:   色散关键字(如 'D3'/'D3(BJ)'/'TS';None=不加色散)。
        cutoff_ev:    平面波截断能(eV;VASP ENCUT / CASTEP cut_off_energy 口径)。
                      注意:CP2K 的 CUTOFF(Ry,GTH)与此**不等价**,不从这里换算。
        kpoints:      k 点网格 (kx,ky,kz);None=让引擎按 cell 推荐或分子用 Γ。
        spin:         是否自旋极化。
        charge:       体系净电荷(分子参考态/带电缺陷)。
        multiplicity: 自旋多重度 2S+1(分子;周期体系一般用 spin/NUPDOWN,此值忽略)。
        convergence:  {'energy_ev','force_ev_a'} 收敛判据(能量/力阈值)。
        extras:       引擎私有透传(如 CP2K 的 cutoff_ry/basis、CASTEP 的 seedname、
                      VASP 的 incar 覆盖)。绝不跨引擎自动搬运。Gaussian 侧支持:
                      basis(单一基组名);nproc→%nprocshared、mem_gb→%mem=NGB、
                      chk(bool/名)→%chk 资源行;solvent={'model':'smd'|'pcm'|'cpcm',
                      'name':'water'} 或自定义介电 {'model':...,'eps':...,'epsinf':...}
                      →SCRF(后者走 Solvent=Generic,Read + 尾段);mixed_basis=
                      {'default','per_element':{El:basis},'ecp_elements':[El]}→Gen/GenECP
                      (元素分组基组段 + ECP 段,附加输入区顺序:坐标→ModRedundant→基组→
                      ECP→SCRF-Read)。gaussian_task(覆盖 task 映射,见 gaussian.GAUSSIAN_TASKS:
                      opt/freq/opt_freq/sp/td/irc/scan/nmr/opt_ts;未给时退回 task 的
                      relax/static/freq 旧行为);td_nstates(td 激发态数,默认 6)、
                      irc_maxpoints(irc 路径点数,默认 20)、modredundant(scan 冗余内坐标
                      扫描定义 list[str],如 ['B 1 2 S 10 0.1'])。
    """

    structure: str
    task: str = 'relax'
    functional: str = 'PBE'
    periodic: bool = True
    dispersion: str | None = None
    cutoff_ev: float | None = None
    kpoints: tuple | None = None
    spin: bool = False
    charge: int = 0
    multiplicity: int = 1
    convergence: dict = field(
        default_factory=lambda: {'energy_ev': 1e-5, 'force_ev_a': 0.02})
    extras: dict = field(default_factory=dict)


def validate(spec: CalcSpec) -> list[str]:
    """校验 CalcSpec 各字段自洽,返回中文问题清单(空=通过)。只报不改。

    覆盖分支:
    - 任务非法(不在 relax/static/freq)。
    - 周期体系缺 cutoff_ev(平面波截断能必须给)。
    - 分子体系却给了 kpoints(非周期无 k 点采样,配置矛盾)。
    - 多重度 < 1(物理无意义)。
    - 开壳(multiplicity>1)却未开自旋极化(spin=False,磁/开壳态会丢失)。
    - 周期体系给了非平凡多重度(周期 DFT 用 spin/NUPDOWN,分子多重度将被忽略)。
    - 收敛阈值非正(能量/力阈值须 > 0)。
    """
    issues: list[str] = []

    if spec.task not in VALID_TASKS:
        issues.append(
            f'任务类型非法:{spec.task!r};合法值:{", ".join(VALID_TASKS)}。')

    if spec.periodic:
        if spec.cutoff_ev is None or float(spec.cutoff_ev) <= 0:
            issues.append(
                '周期性计算必须指定 cutoff_ev(平面波截断能,eV);缺失或非正值。')
    else:
        if spec.kpoints is not None:
            issues.append(
                '分子(非周期)计算不应指定 kpoints;非周期体系无 k 点采样,请置 None。')

    if int(spec.multiplicity) < 1:
        issues.append(
            f'自旋多重度须为正整数(2S+1),收到 multiplicity={spec.multiplicity}。')
    elif int(spec.multiplicity) > 1 and not spec.spin:
        issues.append(
            f'多重度 {spec.multiplicity}(开壳)但 spin=False(未开自旋极化);'
            f'开壳/磁性态将丢失,请置 spin=True。')

    if spec.periodic and int(spec.multiplicity) > 1:
        issues.append(
            f'周期性计算通常不使用分子多重度(multiplicity={spec.multiplicity});'
            f'周期 DFT 请用 spin/NUPDOWN 表达净磁矩,此多重度将被忽略。')

    conv = spec.convergence or {}
    for key, label in (('energy_ev', '能量收敛阈值'), ('force_ev_a', '力收敛阈值')):
        if key in conv and conv[key] is not None:
            try:
                if float(conv[key]) <= 0:
                    issues.append(f'{label} convergence[{key!r}] 须 > 0。')
            except (TypeError, ValueError):
                issues.append(f'{label} convergence[{key!r}] 非数值:{conv[key]!r}。')

    return issues


# ── 引擎 Backend 统一接口 ─────────────────────────────────────────────────────
class EngineBackend:
    """引擎 Backend 统一接口(文件级适配契约;每引擎独立实现,互不耦合)。

    三件事(采纳蓝图 E22:每引擎独立 checker,科学层不感知引擎细节):
    - generate_inputs(spec, out_dir) -> {'files':[...],'warnings':[...]}
        据 CalcSpec 生成该引擎输入文件到 out_dir;files 为写出路径,warnings 中文
        (不等价提醒/需自备赝势库等)。
    - parse_energy(out_dir) -> {'energy_ev','converged','error'}
        解析该引擎输出并统一到 eV 口径;缺文件/解析失败 → error 非空、energy_ev=None。
        个别引擎可附加键(如 Gaussian 的 n_imaginary 虚频数),但三键恒在。
    - check_inputs(out_dir) -> [中文问题]
        独立静态检查已生成输入(缺件/缺关键字…);空列表=通过。

    name:引擎注册名(get_backend 的键)。
    """

    name = ''

    def generate_inputs(self, spec: 'CalcSpec', out_dir: str) -> dict:
        raise NotImplementedError

    def parse_energy(self, out_dir: str) -> dict:
        raise NotImplementedError

    def check_inputs(self, out_dir: str) -> list:
        raise NotImplementedError


# ── 结构解析(POSCAR → 各引擎共用的结构字典;纯 python) ────────────────────────
def _mat_inv3(m: list) -> list:
    """3×3 矩阵求逆(伴随/行列式,纯 python,避免给本模块引 numpy)。奇异 → ValueError。"""
    (a, b, c), (d, e, f), (g, h, i) = m[0], m[1], m[2]
    ca, cb, cc = e * i - f * h, f * g - d * i, d * h - e * g
    det = a * ca + b * cb + c * cc
    if abs(det) < 1e-12:
        raise ValueError('晶格矢量退化(行列式≈0),无法求分数坐标')
    inv = 1.0 / det
    return [[ca * inv, (c * h - b * i) * inv, (b * f - c * e) * inv],
            [cb * inv, (a * i - c * g) * inv, (c * d - a * f) * inv],
            [cc * inv, (b * g - a * h) * inv, (a * e - b * d) * inv]]


def _cart_to_frac(cart: list, cell: list) -> list:
    """笛卡尔 → 分数(frac_j = Σ_k cart_k·inv[k][j];与 slab/neb 同口径)。"""
    inv = _mat_inv3(cell)
    return [[c[0] * inv[0][j] + c[1] * inv[1][j] + c[2] * inv[2][j] for j in range(3)]
            for c in cart]


def _read_sd_flags(poscar_text: str, natoms: int):
    """POSCAR → 每原子 Selective dynamics 标志串列表('T T T'/'F F F');无 SD → None。

    仅当第 8 行(index 7)以 s/S 开头(Selective dynamics)时有约束;此时坐标块自
    第 10 行(index 9)起(注释/缩放/三矢量/元素/计数/SD/模式 = 前 9 行),与
    neb_builder/slab_builder 同口径。缺列兜底 'T T T'(放开)。
    """
    lines = poscar_text.splitlines()
    if len(lines) <= 7 or lines[7].strip()[:1].lower() != 's':
        return None
    coord_start = 9
    flags = []
    for k in range(natoms):
        idx = coord_start + k
        parts = lines[idx].split() if idx < len(lines) else []
        flags.append(' '.join(parts[3:6]) if len(parts) >= 6 else 'T T T')
    return flags


def parse_structure(poscar_text: str) -> dict:
    """POSCAR 文本 → 各引擎共用结构字典。

    返回 {'elements'(逐原子)、'cart'(笛卡尔 Å)、'frac'(分数)、'cell'(3×3 Å)、
          'sd'(每原子 SD 标志串 或 None)、'comment'(首行)}。

    复用 structure_view.parse_positions(已处理 Direct/Cartesian/负缩放拒绝/VASP4
    报错),再补分数坐标与 SD 标志。VASP4/畸形 POSCAR → ValueError(冒泡)。
    """
    p = parse_positions(poscar_text)                 # cart + cell + 逐原子 elements
    cart, cell, elements = p['coords'], p['cell'], p['elements']
    frac = _cart_to_frac(cart, cell)
    sd = _read_sd_flags(poscar_text, len(cart))
    comment = poscar_text.splitlines()[0].strip() if poscar_text.strip() else ''
    return {'elements': elements, 'cart': cart, 'frac': frac, 'cell': cell,
            'sd': sd, 'comment': comment}


def structure_species(poscar_text: str) -> tuple[list, list]:
    """POSCAR → (species, counts)(物种顺序);VASP4/畸形 → ([],[])(调用方降级)。

    薄封装 poscar.parse_poscar_species,给引擎侧一个稳定入口。
    """
    return parse_poscar_species(poscar_text)


def _ensure_cell(poscar_text: str) -> list:
    """POSCAR → 3×3 晶格矢量(已乘缩放因子)。薄封装,负缩放沿用其显式拒绝。"""
    return read_cell_vectors(poscar_text)


# ── 跨引擎不等价清单(NONEQUIV_MAP)——不静默翻译的数据底座 ─────────────────────
# 键为 (src_engine, dst_engine);值为逐字段不等价条目 {'field','note'}(中文)。
# 物理上不等价是对称的,故只登记一个方向,nonequivalence_report 会双向查表。
# 覆盖 VASP 为枢纽的三对(用户裁决:VASP 主 + 三引擎文件级适配)。
NONEQUIV_MAP: dict = {
    ('vasp', 'cp2k'): [
        {'field': 'cutoff',
         'note': 'VASP ENCUT(PAW 平面波截断,eV)与 CP2K CUTOFF(GTH 高斯+平面波'
                 '的密度网格截断,Ry)物理含义不同,数值不可直接换算,需各自收敛测试。'},
        {'field': 'pseudopotential',
         'note': 'VASP PAW 价电子数与 CP2K GTH 赝势价电子数可能不同,LDA+U 的 U 值、'
                 '磁矩初猜不可跨引擎沿用。'},
        {'field': 'basis',
         'note': 'VASP 纯平面波基组由 ENCUT 单一参数定义;CP2K 为原子中心高斯基组'
                 '(如 DZVP-MOLOPT-SR-GTH),须另行指定 BASIS_SET,无一一对应。'},
        {'field': 'smearing',
         'note': 'VASP ISMEAR/SIGMA 与 CP2K 电子温度/占据(&SMEAR)实现不同,'
                 '金属体系展宽需各自设定。'},
        {'field': 'energy_reference',
         'note': '两引擎总能零点不同,绝对能量不可比,只能比较同引擎内的相对能/反应能。'},
    ],
    ('vasp', 'gaussian'): [
        {'field': 'boundary',
         'note': 'VASP 为周期性平面波;Gaussian 默认孤立分子(无周期),仅适用于'
                 '参考态/溶剂化分子,周期体系无法对应(适配层将直接拒绝)。'},
        {'field': 'basis',
         'note': 'VASP 平面波+PAW 与 Gaussian 原子中心高斯基组(如 def2-SVP)本质不同,'
                 '基组完备性与 BSSE 特性不同,绝对能量不可比。'},
        {'field': 'cutoff',
         'note': 'VASP ENCUT 无 Gaussian 对应物(高斯基组无平面波截断概念),不可映射。'},
        {'field': 'dispersion',
         'note': 'VASP D3(IVDW=11/12)与 Gaussian EmpiricalDispersion=GD3/GD3BJ 的'
                 '阻尼与参数化默认可能不同,需逐项核对。'},
        {'field': 'energy_reference',
         'note': '赝势 vs 全电子/有效核势,总能零点不同,只能比较同引擎内相对能。'},
    ],
    ('vasp', 'castep'): [
        {'field': 'pseudopotential',
         'note': 'VASP PAW 与 CASTEP OTFG/USP 赝势不同,芯电子处理与价电子数可能不同,'
                 '绝对能量不可直接比较,仅同引擎内可比。'},
        {'field': 'cutoff',
         'note': 'VASP ENCUT 与 CASTEP cut_off_energy 同为 eV 且均为平面波截断,含义'
                 '相近,但因赝势不同仍需各自收敛测试,不保证同值等精度。'},
        {'field': 'xc_functional',
         'note': '泛函关键字命名不同(VASP GGA=PE ↔ CASTEP xc_functional=PBE),'
                 '需逐一确认对应,RPBE/optB88 等实现细节尤须核对。'},
        {'field': 'kpoints',
         'note': 'VASP KPOINTS 与 CASTEP kpoints_mp_grid 均为 MP 网格,但偏移/对称'
                 '约简实现不同,建议各自收敛。'},
    ],
}

# 未登记引擎对的兜底提示(绝不静默返回空——只要跨引擎就必须显式提醒人工评估)。
_GENERIC_NONEQUIV = (
    '尚未登记该引擎对的逐字段不等价清单;跨引擎混用前请人工核对赝势/基组/截断/'
    '泛函关键字与总能零点,绝对能量不可直接比较,仅同引擎内相对能可比。')


def nonequivalence_report(src_engine: str, dst_engine: str) -> list[str]:
    """跨引擎不等价清单 → 中文逐条(供 UI 弹窗逐项确认)。

    - 同引擎 → 空列表(无跨引擎不等价问题)。
    - 已登记引擎对(任一方向)→ 逐字段中文条目 '[字段] 说明'。
    - 未登记的不同引擎对 → 单条兜底提示(绝不静默放行)。
    """
    src, dst = str(src_engine).lower(), str(dst_engine).lower()
    if src == dst:
        return []
    entries = NONEQUIV_MAP.get((src, dst)) or NONEQUIV_MAP.get((dst, src))
    if not entries:
        return [_GENERIC_NONEQUIV]
    return [f'[{e["field"]}] {e["note"]}' for e in entries]
