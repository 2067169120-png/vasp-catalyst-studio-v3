"""吸附能方法学顾问(纯函数,warn-only):抓"不崩但算错"的坑。

规则来自网研蒸馏(VASP Wiki / 官方 CO-Ni(111) 教程 / 文献,2026-07 核对,来源见
docs/工作日志.md):全部只告警不改键(方法学主权)。触发条件全部可离线判定
(共享 INCAR dict + 成员组成/晶胞),由 adsorption.create_project 调用,GUI 项目页展示。
中文注释允许,英文标识符。
"""
from __future__ import annotations

# 开壳层气相参考(elements 排序元组 → counts 元组;命中且 ISPIN≠2 即告警)
_OPEN_SHELL = {
    (('O',), (2,)): 'O₂ 基态是三重态(2 个未配对电子)',
    (('O',), (1,)): 'O 原子是开壳层',
    (('H',), (1,)): 'H 原子是开壳层',
    (('N',), (1,)): 'N 原子是开壳层',
    (('N', 'O'), (1, 1)): 'NO 是开壳层自由基',
    (('H', 'O'), (1, 1)): 'OH 是开壳层自由基',
}


VACUUM_MIN_A = 12.0     # Å:slab 真空层低于此值告警(周期镜像相互作用不可忽略)


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _vacuum_thin(calc_type, poscar_text):
    """slab 且能拿到 POSCAR 文本时算真空层厚度,< VACUUM_MIN_A 返回该厚度,否则 None。

    真空计算/解析任何失败都吞成 None(顾问 warn-only,绝不因真空检查抛错吞掉其余告警)。
    """
    if calc_type != 'slab' or not poscar_text:
        return None
    try:
        from vcstudio.generate.slab_builder import vacuum_thickness
        vac = vacuum_thickness(poscar_text)
    except Exception:                                    # noqa: BLE001 顾问不因此崩
        return None
    return vac if vac < VACUUM_MIN_A else None


def advise(incar: dict, *, has_configs: bool = False,
           gas: dict | None = None, unified_encut: int | None = None,
           calc_type: str | None = None, poscar_text: str | None = None) -> list:
    """共享 INCAR + 项目组成 → 告警清单 [(priority, name, message)],按优先级排序。

    incar: 大写键 dict(parse_incar 产物);gas: 气相参考信息
    {'elements','counts','cell'}(无参考传 None);unified_encut: 全员元素并集算出的
    推荐 ENCUT(调用方可算不出时传 None)。calc_type/poscar_text:可选,给足时对 slab
    做真空层厚度检查(向后兼容,老调用不传即跳过)。
    """
    out = []
    up = {str(k).upper(): v for k, v in (incar or {}).items()}
    ismear = _num(up.get('ISMEAR'), default=1)          # VASP 默认 ISMEAR=1
    sigma = _num(up.get('SIGMA'), default=0.2)          # VASP 默认 SIGMA=0.2
    nsw = _num(up.get('NSW'), default=0)
    ldipol = up.get('LDIPOL') is True
    idipol = _num(up.get('IDIPOL'), default=0)

    # ── P0 ──
    if 'ENCUT' not in up and unified_encut:
        out.append(('P0', 'ENCUT_UNIFY',
                    f'INCAR 未显式 ENCUT:逐成员自动补全会按各自元素得到不同截断能,'
                    f'吸附能是大数相减、基组必须三算一致,ΔE 会被静默破坏。'
                    f'请在共享 INCAR 显式写入 ENCUT = {unified_encut}'
                    f'(按全项目元素并集 1.3×max ENMAX 计算)。'))
    if gas is not None:
        if ismear != 0 or sigma > 0.05:
            out.append(('P0', 'GAS_REF_SMEARING',
                        f'气相参考分子须用 ISMEAR=0(Gaussian)+ 小 SIGMA(0.01–0.05);'
                        f'当前 ISMEAR={ismear:g}、SIGMA={sigma:g}'
                        f'(VASP 默认 1/0.2 正好踩坑)会给孤立分子非物理部分占据,'
                        f'污染所有以它为参考的 ΔE。金属 slab 与分子的 ISMEAR/SIGMA 允许不同,'
                        f'ENCUT/泛函/IVDW/POTCAR 等共享方法必须可比；'
                        f'ISPIN 应分别取各体系的基态，分子参考与周期表面可不同，'
                        f'但 clean slab 与 slab+ads 必须使用同一自旋口径。'))
        key = (tuple(sorted(gas.get('elements') or [])),
               tuple(c for _, c in sorted(zip(gas.get('elements') or [],
                                              gas.get('counts') or []))))
        why = _OPEN_SHELL.get(key)
        if why and _num(up.get('ISPIN'), default=1) != 2:
            out.append(('P0', 'GAS_REF_OPEN_SHELL_SPIN',
                        f'{why},ISPIN=1 的参考能量系统性偏高(文献偏差可达 ~0.85 eV),'
                        f'会污染每一个吸附能;请设 ISPIN=2 并给 MAGMOM'
                        f'(O₂ 可加 NUPDOWN=2 锁三重态,核对 OUTCAR 末尾 mag≈2.0)。'))
        cell = gas.get('cell') or []
        lens = [sum(x * x for x in v) ** 0.5 for v in cell if v]
        if lens and min(lens) < 10.0:
            out.append(('P1', 'GAS_BOX_TOO_SMALL',
                        f'气相参考盒最短边 {min(lens):.1f} Å < 10 Å,分子与周期镜像自相互作用'
                        f'不可忽略;建议盒边 ≥10–15 Å 并保持 Γ 点采样。'))
    if ldipol and idipol == 0:
        out.append(('P0', 'LDIPOL_WITHOUT_IDIPOL',
                    'LDIPOL=.TRUE. 必须搭配 IDIPOL 指定偶极方向(slab 法向通常 IDIPOL=3,'
                    '孤立分子 IDIPOL=4),否则修正无从施加。'))

    # ── P1 ──
    if has_configs and nsw <= 0:
        out.append(('P1', 'NSW_ZERO',
                    '作业将为单点计算,吸附质不会弛豫;弛豫请设 NSW>0 与 IBRION=2。'))
    if has_configs and idipol == 0:
        out.append(('P1', 'ADS_SLAB_NO_DIPOLE',
                    '单面吸附 slab 必然有垂直净偶极,周期镜像赝电场对 E_ads 典型影响 '
                    '0.01–0.3 eV;建议 IDIPOL=3 + LDIPOL=.TRUE. 并设 DIPOL 为质心。'))
    if ismear == -5 and nsw and nsw > 0:
        out.append(('P1', 'TETRAHEDRON_RELAX',
                    '四面体法(ISMEAR=-5)对部分占据非变分,金属弛豫力可偏差数个百分点;'
                    '弛豫请用 ISMEAR=1、SIGMA≈0.2,收敛后再以 ISMEAR=-5 做静态单点。'))
    _vac = _vacuum_thin(calc_type, poscar_text)
    if _vac is not None:
        out.append(('P1', 'VACUUM_TOO_THIN',
                    f'slab 真空层仅 {_vac:.1f} Å(<{VACUUM_MIN_A:g} Å),周期镜像 slab 间'
                    f'可能相互作用/偶极耦合,吸附能失真;建议真空 ≥12–15 Å。'))

    # ── P2 ──
    if nsw > 0 and 'EDIFFG' not in up:
        out.append(('P2', 'NO_EDIFFG',
                    '未设 EDIFFG,VASP 将按能量判据收敛;吸附能建议力判据 '
                    'EDIFFG=-0.02~-0.03 eV/Å。'))
    if (has_configs or gas is not None) and 'IVDW' not in up and 'LUSE_VDW' not in up:
        out.append(('P2', 'NO_DISPERSION',
                    '未启用色散校正,分子/物理吸附能可能系统性偏弱(常用 IVDW=12 D3-BJ);'
                    '金属化学吸附可酌情忽略。'))
    if ldipol and 'DIPOL' not in up:
        out.append(('P2', 'LDIPOL_WITHOUT_DIPOL',
                    '未设 DIPOL 时 VASP 自动猜偶极中心,可能很差且拖慢收敛;'
                    '建议 DIPOL 设为体系质心分数坐标(如官方教程 0.5 0.5 0.45)。'))
    if gas is not None and (gas.get('elements'), gas.get('counts')) == (['O'], [2]) \
            and _num(up.get('ISPIN'), default=1) == 2 and 'NUPDOWN' not in up:
        out.append(('P2', 'O2_NUPDOWN',
                    'MAGMOM 只是初猜,自洽中 O₂ 磁矩可能漂离三重态;'
                    '建议 NUPDOWN=2 锁定,并核对 OUTCAR 末尾 mag≈2.0 μB。'))

    order = {'P0': 0, 'P1': 1, 'P2': 2}
    return sorted(out, key=lambda t: order[t[0]])
