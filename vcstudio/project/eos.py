"""状态方程(EOS):等比缩放晶格的定容单点系列 + Birch-Murnaghan 三阶拟合 + E-V 出图。

用途:求平衡体积 V0、平衡能 E0、体弹模量 B0(GPa)与其压力导数 B0'。做法是把平衡结构
的晶格**等比缩放**(线性因子 0.94–1.06,体积 ∝ 因子³)得一串定容单点,拟合 E(V) 到三阶
Birch-Murnaghan 方程。

BM3 公式(Birch 1947,能量形式):

    E(V) = E0 + (9 V0 B0 / 16) · { [η−1]³ B0' + [η−1]² (6 − 4η) },   η = (V0/V)^{2/3}

关键恒等式:令 x = V^{−2/3},上式**恰为 x 的三次多项式** E(x)=a0+a1x+a2x²+a3x³(把 η=V0^{2/3}x
代入展开即得)。故 BM3 拟合 = 对 (x, E) 做纯 numpy 三次最小二乘,再由系数解析反解 V0/E0/B0/B0':
- 极值 x0 满足 P'(x0)=0(取 P''(x0)>0、x0>0 的物理极小根),V0 = x0^{−3/2},E0 = P(x0);
- B0 = V0·d²E/dV²|0 = (4/9) P''(x0) x0^{7/2}(eV/Å³),×160.21766 → GPa;
- B0' = 4 + 16 a3 / (9 B0[eV/Å³] V0³)。

单位:体积 Å³、能量 eV;B0 输出 GPa(1 eV/Å³ = 160.21766208 GPa)。
派生复用 conv_scan.derive_incar。中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from vcstudio.generate.conv_scan import _read_text, _structure_source, derive_incar
from vcstudio.generate.poscar import read_cell_vectors
from vcstudio.shared import manifest as manifest_mod

EV_A3_TO_GPA = 160.21766208        # 1 eV/Å³ → GPa
DEFAULT_SCALES = (0.94, 0.96, 0.98, 1.00, 1.02, 1.04, 1.06)   # 线性晶格缩放因子(七点)


def _det3(m) -> float:
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def _scale_poscar(poscar_text: str, linear_scale: float) -> str:
    """等比缩放晶格:把 POSCAR 第2行的**通用缩放因子** ×linear_scale(VASP 语义下同时缩放
    晶格矢量与笛卡尔坐标,Direct 坐标随晶格同缩放),得体积 ∝ linear_scale³ 的定容结构。

    只改第2行,其余逐字保留。缩放因子 ≤0(负=目标体积,不支持)→ ValueError。
    """
    lines = poscar_text.splitlines()
    if len(lines) < 2:
        raise ValueError('POSCAR 行数不足,无法缩放晶格')
    try:
        base = float(lines[1].split()[0])
    except (IndexError, ValueError):
        raise ValueError('POSCAR 第2行不是合法缩放因子')
    if base <= 0:
        raise ValueError('POSCAR 缩放因子须为正(负缩放=目标体积,EOS 系列不支持);请提供正缩放 POSCAR')
    lines[1] = f'{base * float(linear_scale):.10f}'
    return '\n'.join(lines) + ('\n' if poscar_text.endswith('\n') else '')


def _copy_if(src_dir, out_dir, name):
    src = os.path.join(src_dir, name)
    if os.path.isfile(src):
        import shutil
        shutil.copyfile(src, os.path.join(out_dir, name))
        return True
    return False


def build_eos_series(src_dir, out_root, scales=DEFAULT_SCALES) -> dict:
    """从平衡结构派生 EOS 定容单点系列:各作业等比缩放晶格(线性因子),定容单点(NSW=0)。

    KPOINTS/POTCAR 逐字复制(各体积同 k 网格,保能量可比);INCAR 派生为单点
    (NSW=0/IBRION=-1,剥离 ISIF/EDIFFG),电子学参数原样保留。

    Args:
        src_dir: 平衡结构目录(CONTCAR/POSCAR + INCAR + KPOINTS + POTCAR)。
        out_root: 输出根目录;作业目录名 ``eos_<因子>``(如 eos_0.94)。
        scales: 线性晶格缩放因子序列(默认 0.94–1.06 七点;体积 ∝ 因子³)。

    Returns:
        ``{'out_root','dirs','series','results','warnings'}``;series=[{'scale','volume','dir'},...]。
        结构含内部自由度(非高对称位)时 warning 提示应改 ISIF=2/4 定容弛豫而非纯单点。
    """
    src_dir = str(src_dir)
    _src, poscar_text = _structure_source(src_dir)
    if poscar_text is None:
        raise ValueError(f'源目录缺 CONTCAR/POSCAR(或均为空),无法派生 EOS 系列:{src_dir}')
    base_incar = _read_text(os.path.join(src_dir, 'INCAR'))
    if base_incar is None:
        raise ValueError(f'源目录缺 INCAR,无法派生 EOS 系列:{src_dir}')

    base_vol = abs(_det3(read_cell_vectors(poscar_text)))     # Å³(已含缩放因子)
    single_reasons = {'NSW': '定容单点(EOS 各体积单点取能)', 'IBRION': '不做离子步',
                      'ISIF': 'EOS 定容,不放开晶胞', 'EDIFFG': '单点无离子弛豫判据'}
    dirs, series, results = OrderedDict(), [], OrderedDict()
    for s in scales:
        s = float(s)
        vol = base_vol * s ** 3
        tag = f'{s:g}'
        out_dir = os.path.join(out_root, f'eos_{tag}')
        os.makedirs(out_dir, exist_ok=True)
        scaled_poscar = _scale_poscar(poscar_text, s)
        banner = f'# === vcstudio EOS 定容单点(线性缩放 ×{s:g},体积 {vol:.3f} Å³) ==='
        new_incar, changes = derive_incar(
            base_incar, set_keys={'NSW': 0, 'IBRION': -1},
            strip_keys=('ISIF', 'EDIFFG'), reasons=single_reasons, banner=banner)
        warnings = ['EOS 定容**单点**(NSW=0):仅适合无内部自由度的高对称结构;若原子有内部'
                    '坐标自由度,应改 ISIF=2/4 定容弛豫再取能(否则 E-V 偏高)。']
        with open(os.path.join(out_dir, 'POSCAR'), 'w', encoding='utf-8') as f:
            f.write(scaled_poscar)
        with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
            f.write(new_incar)
        if not _copy_if(src_dir, out_dir, 'KPOINTS'):
            warnings.append('源目录缺 KPOINTS,未复制;EOS 各体积须同一 k 网格,请补齐。')
        if not _copy_if(src_dir, out_dir, 'POTCAR'):
            warnings.append('源目录缺 POTCAR,未复制;提交前须补齐同一套赝势。')

        system = poscar_text.splitlines()[0].strip() if poscar_text.strip() else Path(out_dir).name
        parent = str(Path(src_dir).resolve())
        m = manifest_mod.new_manifest(
            job_id=f'{Path(out_dir).name}-eos', system=system, task_type='eos',
            calc_type='bulk',
            inputs={'parent_job': parent, 'scale': s, 'volume': vol,
                    'incar_changes': changes},
            warnings=warnings)
        m['parent_job'] = parent
        manifest_mod.save_manifest(out_dir, m)

        dirs[s] = out_dir
        series.append({'scale': s, 'volume': vol, 'dir': out_dir})
        results[s] = {'dir': out_dir, 'volume': vol, 'changes': changes, 'warnings': warnings}
    return {'out_root': str(out_root), 'dirs': dirs, 'series': series,
            'results': results, 'warnings': []}


def fit_birch_murnaghan(volumes, energies) -> dict:
    """三阶 Birch-Murnaghan 拟合(纯 numpy 最小二乘)→ ``{'v0','e0','b0_gpa','b0_prime','r2','b0_evA3'}``。

    做法见模块 docstring:x=V^{−2/3},对 (x,E) 三次多项式最小二乘,系数解析反解 BM 参数。
    volumes/energies 等长且 ≥4 点(推荐 ≥5;默认 EOS 七点)。体积须为正。数据非凸/无物理极小
    → ValueError(绝不给无意义参数)。r2 为 E 与拟合值的决定系数。
    """
    import numpy as np
    V = np.asarray(volumes, dtype=float)
    E = np.asarray(energies, dtype=float)
    if V.size != E.size:
        raise ValueError(f'volumes 与 energies 长度不一致:{V.size} != {E.size}')
    if V.size < 4:
        raise ValueError('BM3 拟合至少需 4 个 (V,E) 点(推荐 ≥5;EOS 默认 7 点)')
    if np.any(V <= 0):
        raise ValueError('体积须为正(Å³)')

    x = V ** (-2.0 / 3.0)
    a3, a2, a1, a0 = (float(c) for c in np.polyfit(x, E, 3))
    # dE/dx = 3a3 x² + 2a2 x + a1 = 0(等价 dE/dV=0),取 P''>0、x>0 的物理极小。
    # 用**数值稳定**的求根(citardauq 形式,避免 B0′≈4 时 a3→0 的抵消误差)。
    A, B, C = 3.0 * a3, 2.0 * a2, a1
    disc = B * B - 4.0 * A * C
    if disc < 0:
        raise ValueError('BM3 拟合无实极值(E-V 非凸?),请检查数据范围/单调性')
    sq = disc ** 0.5
    q = -0.5 * (B + sq) if B >= 0 else -0.5 * (B - sq)   # 稳定:两同号项相加不抵消
    candidates = []
    if abs(A) > 1e-300:
        candidates.append(q / A)                         # 一根;A→0 时此根跑向 ±inf
    if abs(q) > 1e-300:
        candidates.append(C / q)                         # 另一根;A→0 时退化为线性根 -C/B
    xmean = float(x.mean())
    valid = [r for r in candidates if r > 0 and (2.0 * a2 + 6.0 * a3 * r) > 0]   # P''>0=极小
    if not valid:
        raise ValueError('BM3 拟合未找到物理极小(V0);请检查 E-V 数据(体积范围是否跨过极小?)')
    x0 = min(valid, key=lambda r: abs(r - xmean))        # 物理极小应落在扫描 x 范围内(取最近)

    v0 = x0 ** (-1.5)
    e0 = a0 + a1 * x0 + a2 * x0 ** 2 + a3 * x0 ** 3
    p2 = 2.0 * a2 + 6.0 * a3 * x0                        # P''(x0)
    b0_evA3 = (4.0 / 9.0) * p2 * x0 ** 3.5               # eV/Å³
    b0_gpa = b0_evA3 * EV_A3_TO_GPA
    b0_prime = 4.0 + 16.0 * a3 / (9.0 * b0_evA3 * v0 ** 3)

    efit = a0 + a1 * x + a2 * x ** 2 + a3 * x ** 3
    ss_res = float(np.sum((E - efit) ** 2))
    ss_tot = float(np.sum((E - E.mean()) ** 2))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else (1.0 if ss_res < 1e-12 else 0.0)
    return {'v0': float(v0), 'e0': float(e0), 'b0_gpa': float(b0_gpa),
            'b0_prime': float(b0_prime), 'r2': float(r2), 'b0_evA3': float(b0_evA3)}


def eos_plot(points, fit, out_path, *, title: str = '', width=None,
             palette: str = 'tol_bright', panel: str = '',
             formats=('png', 'pdf'), style_kw: dict | None = None) -> list:
    """E-V 曲线:DFT 单点散点 + BM3 拟合平滑线,标 V0/B0 注释。

    points:[{'volume','energy'}, ...](或平行 (V,E),见下),≥4 点;fit:fit_birch_murnaghan 结果。
    也接受 points 为 (volumes, energies) 二元组。样式走 native_charts。返回导出路径列表。
    """
    from vcstudio.external.native_charts import (PALETTES, SINGLE_COL, apply_paper_style,
                                                 _new_figure, _save_dual, add_panel_label)
    import numpy as np
    if isinstance(points, (tuple, list)) and len(points) == 2 and \
            all(isinstance(p, (list, tuple)) for p in points):
        vs, es = [float(v) for v in points[0]], [float(e) for e in points[1]]
    else:
        vs = [float(p['volume']) for p in points]
        es = [float(p['energy']) for p in points]
    if len(vs) < 2:
        raise ValueError('E-V 图至少需 2 个点')

    v0, e0 = float(fit['v0']), float(fit['e0'])
    b0p = float(fit['b0_evA3'])
    b0prime = float(fit['b0_prime'])
    vv = np.linspace(min(vs + [v0]) * 0.99, max(vs + [v0]) * 1.01, 200)
    eta = (v0 / vv) ** (2.0 / 3.0)
    ecurve = e0 + (9.0 * v0 * b0p / 16.0) * (
        (eta - 1.0) ** 3 * b0prime + (eta - 1.0) ** 2 * (6.0 - 4.0 * eta))

    fig_w = width if width is not None else SINGLE_COL
    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.78)
        colors = PALETTES.get(palette, PALETTES['tol_bright'])
        ax.plot(vv, ecurve, color=colors[1], lw=1.5, zorder=2, label='BM3 fit')
        ax.scatter(vs, es, s=32, color=colors[0], edgecolors='black',
                   linewidths=0.5, zorder=3, label='DFT')
        ax.axvline(v0, color='#DDAA33', lw=0.9, ls=(0, (4, 3)), zorder=1)
        ax.annotate(rf'$V_0$ = {v0:.2f} Å$^3$' '\n' rf'$B_0$ = {fit["b0_gpa"]:.1f} GPa',
                    (v0, e0), xytext=(6, 12), textcoords='offset points',
                    ha='left', va='bottom', fontsize=7.5, linespacing=1.4)
        ax.set_xlabel(r'Volume (Å$^3$)')
        ax.set_ylabel('Energy (eV)')
        if title:
            ax.set_title(title)
        ax.legend(loc='best')
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)
