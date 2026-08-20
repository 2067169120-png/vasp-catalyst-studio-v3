"""功函数 φ(work function):slab 静态(LVTOT)→ LOCPOT 沿 z 面平均 → 真空平台 − 费米能。

物理:φ = E_vac − E_F,E_vac 为真空能级(远离表面处静电势的平台值),E_F 费米能。做法是
slab 静态单点开 LVTOT=.TRUE. 产出 LOCPOT(总局域势),沿表面法向 z 做面平均得 V̄(z),在真空
区平台取值即 E_vac。

口径要点(绝不静默):
- **非对称 slab**(单面吸附/掺杂,上下表面不等价):两侧真空能级不同 → 两个 φ,且须加偶极
  校正(LDIPOL/IDIPOL=3),否则两面真空能级经周期边界混叠。build_workfunction_job 自动对
  非对称 slab 加偶极校正;work_function 检出双平台时返回两个 φ + warning。
- LVTOT 是**总**局域势(含交换关联,真空区仍平);部分流程用 LVHAR(纯 Hartree)。本模块按
  任务要求用 LVTOT,真空平台判定对二者一致(真空区 XC≈0)。

LOCPOT 网格解析复用 chgdiff.read_chgcar(同 POSCAR 头 + 网格格式);但 LOCPOT 存的是势(eV)、
**不乘体积**,故面平均不除以体积(与 CHGCAR 电荷密度口径不同,单独实现)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
from pathlib import Path

from vcstudio.generate.conv_scan import _structure_source
from vcstudio.shared import manifest as manifest_mod

_AXIS_IDX = {'x': 0, 'y': 1, 'z': 2}


def build_workfunction_job(src_dir, out_root, *, add_dipole: str = 'auto') -> dict:
    """从弛豫目录派生功函数静态作业(slab 静态 + LVTOT=.TRUE. + 偶极校正建议)。

    复用 estatic.build_static_job(purpose='esp' → LVTOT=.TRUE.);对**非对称** slab 自动加
    偶极校正键(LDIPOL/IDIPOL=3/DIPOL,见 incar_builder.dipole_correction_keys),并把 job.yaml
    重写为 task_type='workfunction'(带溯源)。

    Args:
        add_dipole: 'auto'(默认,非对称 slab 才加)/ True(总加)/ False(仅建议不加)。

    Returns:
        ``{'out_dir','changes','warnings','dipole'}``。源缺 CONTCAR/POSCAR 或 INCAR → ValueError。
    """
    from vcstudio.generate import estatic
    from vcstudio.generate.incar_builder import dipole_correction_keys

    src_dir = str(src_dir)
    source_name, poscar_text = _structure_source(src_dir)
    if poscar_text is None:
        raise ValueError(f'源目录缺 CONTCAR/POSCAR(或均为空),无法派生功函数作业:{src_dir}')

    dipole = {}
    if add_dipole in ('auto', True):
        dipole = dipole_correction_keys(poscar_text, 'slab')      # 非对称才非空
        if add_dipole is True and not dipole:
            # 强制加:对称 slab dipole_correction_keys 返回空,此处补最小偶极校正键
            dipole = {'LDIPOL': '.TRUE.', 'IDIPOL': '3'}

    res = estatic.build_static_job(src_dir, out_root, purpose='esp',
                                   extra_incar=(dict(dipole) or None),
                                   extra_meta={'task': 'workfunction'})
    warnings = list(res['warnings'])
    if dipole:
        warnings.append('已加偶极校正(LDIPOL/IDIPOL=3):非对称 slab 上下真空能级不同,功函数须'
                        '校正,否则两面势经周期边界混叠;对称 slab 可忽略。')
    else:
        warnings.append('功函数:对称 slab 可不加偶极校正;若为单面吸附/掺杂(非对称),强烈建议 '
                        'LDIPOL=.TRUE./IDIPOL=3,否则 φ 偏差。可用 add_dipole=True 强制加。')
    warnings.append('LVTOT=.TRUE. 产出 LOCPOT;φ = 真空能级 − E_F,真空能级取 LOCPOT 沿 z 面平均'
                    '在真空平台处的值(见 parse_locpot_planar / work_function)。')

    # estatic 已落标准 manifest；在原清单上改任务语义，保留四件套文件表、
    # SHA256、状态历史和父任务溯源。重新 new_manifest 会丢掉这些提交前证据。
    system = poscar_text.splitlines()[0].strip() if poscar_text.strip() else Path(out_root).name
    parent = str(Path(src_dir).resolve())
    m = manifest_mod.load_manifest(out_root) or manifest_mod.new_manifest(
        job_id=f'{Path(out_root).name}-workfunction', system=system,
        task_type='workfunction', calc_type='slab', inputs={}, warnings=warnings)
    m['task_type'] = 'workfunction'
    m['calc_type'] = 'slab'
    m['warnings'] = warnings
    m.setdefault('inputs', {}).update({
        'parent_job': parent, 'derived_from': source_name, 'purpose': 'esp',
        'dipole': bool(dipole), 'incar_changes': res['changes'],
    })
    from vcstudio.generate.method_recipe import builder_recipe
    m['inputs']['method_recipe'] = builder_recipe(
        builder='vcstudio.project.workfunction/v1', task_type='workfunction',
        calc_type='slab', validate=True,
        completions={'incar_changes': res['changes']},
        kpoints_source='parent-density-multiplier',
        extra={'purpose': 'workfunction', 'dipole_correction': bool(dipole)})
    m['parent_job'] = parent
    m.setdefault('derivation', {}).update({
        'purpose': 'workfunction', 'derived_from': source_name,
        'dipole': bool(dipole), 'changes': list(res['changes']),
    })
    from vcstudio.shared.scientific_inputs import record_input_closure
    record_input_closure(out_root, m)
    manifest_mod.save_manifest(out_root, m)
    return {'out_dir': str(out_root), 'changes': res['changes'], 'warnings': warnings,
            'dipole': bool(dipole)}


def parse_locpot_planar(locpot_text, axis: str = 'z') -> dict:
    """LOCPOT(路径或文本)→ 沿 axis 的面平均势 ``{'z','v_planar','axis'}``。

    对每个 axis 切片求面内平均**(不除体积!LOCPOT 存势 eV,非 ρ×V)**。z 为沿该晶格矢量的
    坐标(Å,z[k]=k/NG_axis·|a_axis|)。线性索引 i = ix + NGX·(iy + NGY·iz)(x 最快,VASP 顺序)。
    axis ∈ {'x','y','z'}。解析复用 chgdiff.read_chgcar(同 POSCAR 头 + 网格)。
    """
    from vcstudio.project.chgdiff import read_chgcar
    c = read_chgcar(locpot_text)
    ngx, ngy, ngz, grid = c['ngx'], c['ngy'], c['ngz'], c['grid']
    axis = str(axis).lower()
    if axis == 'x':
        na, key = ngx, (lambda i: i % ngx)
    elif axis == 'y':
        na, key = ngy, (lambda i: (i // ngx) % ngy)
    elif axis == 'z':
        na, key = ngz, (lambda i: i // (ngx * ngy))
    else:
        raise ValueError(f"axis 只能是 x/y/z,收到 {axis!r}")
    sums = [0.0] * na
    cnts = [0] * na
    for i, v in enumerate(grid):
        k = key(i)
        sums[k] += v
        cnts[k] += 1
    cell = c.get('cell') or [[c['scale'] * x for x in vec] for vec in c['lattice']]
    axis_vec = cell[_AXIS_IDX[axis]]
    length = math.sqrt(sum(x * x for x in axis_vec))
    zs = [(k / na) * length for k in range(na)]
    v_planar = [(sums[k] / cnts[k]) if cnts[k] else 0.0 for k in range(na)]   # 不除体积
    return {'z': zs, 'v_planar': v_planar, 'axis': axis}


def _flat_runs(v, z, deriv_thresh):
    """返回平坦段列表 [(start, end_exclusive, mean_V, length), ...](|dV/dz|<阈)。"""
    n = len(v)
    if n < 2:
        return []
    flat = [False] * n
    for k in range(n - 1):
        dz = z[k + 1] - z[k]
        if dz == 0:
            continue
        if abs((v[k + 1] - v[k]) / dz) < deriv_thresh:
            flat[k] = True
    runs = []
    k = 0
    while k < n:
        if flat[k]:
            j = k
            while j < n and flat[j]:
                j += 1
            seg = v[k:j + 1] if j < n else v[k:j]      # 段含右端点(flat[k] 关联 k→k+1)
            runs.append((k, j, sum(seg) / len(seg), len(seg)))
            k = j
        else:
            k += 1
    return runs


def work_function(v_planar, z, efermi: float, *, deriv_thresh: float = 0.02,
                  min_plateau_pts: int = 3, plateau_tol: float = 0.05) -> dict:
    """面平均势 + 费米能 → 功函数 ``{'phi','vacuum_level','phi_values','note','warnings'}``。

    真空平台判定:沿 z 找 |dV̄/dz|<deriv_thresh 的平坦段(长度 ≥min_plateau_pts),取**势最高**
    的平坦段为真空(静电势在真空区最高且最平),其均值 = 真空能级;φ = 真空能级 − E_F。

    非对称 slab:若存在第二个平坦段其均值与真空平台相差 >plateau_tol → 判定上下两面不等价,
    返回两个 φ(phi_values 两元素)并 warning(须偶极校正、两面分别报告)。找不到平坦段 →
    退化取全场最高点邻域并 warning。
    """
    v = [float(x) for x in v_planar]
    z = [float(x) for x in z]
    if len(v) != len(z) or len(v) < 2:
        raise ValueError(f'v_planar 与 z 长度须一致且 ≥2:{len(v)} / {len(z)}')
    warnings: list[str] = []

    runs = [r for r in _flat_runs(v, z, deriv_thresh) if r[3] >= min_plateau_pts]
    if not runs:
        # 无明显平台:退化取最高值点为真空能级(附 warning,勿轻信)
        vac = max(v)
        warnings.append(f'未找到满足 |dV/dz|<{deriv_thresh:g} 且长度 ≥{min_plateau_pts} 的真空平台;'
                        '已退化取面平均势最高点作真空能级,结果不可靠,请加大真空层或核对 LOCPOT。')
        phi = vac - float(efermi)
        return {'phi': phi, 'vacuum_level': vac, 'phi_values': [phi],
                'note': f'退化:真空能级≈{vac:.3f} eV(最高点),φ={phi:.3f} eV。', 'warnings': warnings}

    runs.sort(key=lambda r: r[2], reverse=True)           # 按平台均值降序,势最高=真空
    vac = runs[0][2]
    phi = vac - float(efermi)
    phi_values = [phi]

    for r in runs[1:]:
        if abs(r[2] - vac) > plateau_tol:
            phi2 = r[2] - float(efermi)
            phi_values.append(phi2)
            warnings.append(
                f'检测到两个不同真空平台(能级差 {abs(r[2] - vac):.3f} eV):slab 上下不对称,'
                f'两侧功函数不同 φ≈{phi:.3f} / {phi2:.3f} eV;须加偶极校正并两面分别报告。')
            break

    note = (f'真空能级 {vac:.3f} eV,E_F {float(efermi):.3f} eV → φ = {phi:.3f} eV'
            + ('(检出双平台,见 warnings/phi_values)。' if len(phi_values) > 1 else '。'))
    return {'phi': phi, 'vacuum_level': vac, 'phi_values': phi_values,
            'note': note, 'warnings': warnings}


def wf_plot(v_planar, z, efermi: float, out_path, *, vacuum_level=None, phi=None,
            title: str = '', width=None, palette: str = 'tol_bright', panel: str = '',
            formats=('png', 'pdf'), style_kw: dict | None = None) -> list:
    """功函数图:面平均势 V̄(z) 曲线 + 费米能级横线 + 真空能级横线,标注 φ。

    v_planar/z 见 parse_locpot_planar;vacuum_level/phi 缺省时内部调 work_function 求得。
    样式走 native_charts。返回导出文件绝对路径列表。
    """
    from vcstudio.external.native_charts import (PALETTES, ZERO_LINE_COLOR, SINGLE_COL,
                                                 apply_paper_style, _new_figure,
                                                 _save_dual, add_panel_label)
    v = [float(x) for x in v_planar]
    zz = [float(x) for x in z]
    if vacuum_level is None or phi is None:
        wf = work_function(v, zz, efermi)
        vacuum_level = wf['vacuum_level'] if vacuum_level is None else vacuum_level
        phi = wf['phi'] if phi is None else phi

    fig_w = width if width is not None else SINGLE_COL
    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.66)
        colors = PALETTES.get(palette, PALETTES['tol_bright'])
        ax.plot(zz, v, color=colors[0], lw=1.3, zorder=3)
        ax.axhline(float(vacuum_level), color='#228833', lw=1.0, ls=(0, (5, 3)), zorder=2)
        ax.annotate(rf'$E_\mathrm{{vac}}$ = {float(vacuum_level):.2f} eV',
                    (max(zz), float(vacuum_level)), xytext=(-4, 3),
                    textcoords='offset points', ha='right', va='bottom',
                    fontsize=7.5, color='#228833')
        ax.axhline(float(efermi), color=ZERO_LINE_COLOR, lw=1.0, ls=(0, (2, 2)), zorder=2)
        ax.annotate(rf'$E_\mathrm{{F}}$ = {float(efermi):.2f} eV',
                    (max(zz), float(efermi)), xytext=(-4, -3),
                    textcoords='offset points', ha='right', va='top',
                    fontsize=7.5, color=ZERO_LINE_COLOR)
        ax.annotate(rf'$\phi$ = {float(phi):.2f} eV', (0.03, 0.5),
                    xycoords='axes fraction', ha='left', va='center',
                    fontsize=8.5, fontweight='bold')
        ax.set_xlim(min(zz), max(zz))
        ax.set_xlabel(r'$z$ (Å)')
        ax.set_ylabel(r'$\bar V$ (eV)')
        if title:
            ax.set_title(title)
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)
