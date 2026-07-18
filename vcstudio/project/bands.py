"""能带解析 + 带隙判定 + 能带出图:EIGENVAL / vasprun.xml → E(k)、带隙(直接/间接)、图。

解析两种来源:
- **EIGENVAL**(纯文本,首选):第1行末位=ISPIN;第6行=NELECT NKPTS NBANDS;之后每个 k 块为
  「空行 → kx ky kz weight → NBANDS 行(band 号 + 能量[+自旋2能量] + 占据)」。无 efermi(文件不存),
  带隙按电子数占据判定(ISPIN=1)。
- **vasprun.xml**:取 efermi 与 eigenvalues,带隙按 efermi 判定(任意自旋)。

带隙判定:
- 有 efermi:VBM=所有(自旋/带/k)中 ≤E_F 的最高能,CBM=>E_F 的最低能;有带穿越 E_F → 金属(gap=0)。
- 无 efermi 的 EIGENVAL(ISPIN=1):占据带数 = round(NELECT/2),VBM=价带顶(占据最高带各 k 最大),
  CBM=导带底(下一带各 k 最小)。gap=CBM−VBM;**direct = VBM 与 CBM 同 k**,否则间接;gap≤0 → 金属。

数据结构:bands[spin][band][k] = 能量(eV)。中文注释允许,英文标识符。
"""
from __future__ import annotations

_GAP_METAL_TOL = 1e-4          # gap 小于此视作金属(带穿越/重叠)


# ── EIGENVAL 解析 ────────────────────────────────────────────────────────────
def parse_eigenval(text: str) -> dict:
    """解析 EIGENVAL 文本 → ``{'kpath','bands','ispin','nelect','nbands','nkpts','efermi'}``。

    bands[spin][band][k]=能量。efermi 恒为 None(EIGENVAL 不含)。格式见模块 docstring。
    行数不足/头部畸形 → ValueError。
    """
    lines = text.splitlines()
    if len(lines) < 7:
        raise ValueError('EIGENVAL 行数不足,无法解析(需头部 6 行 + 数据)')
    try:
        ispin = int(lines[0].split()[-1])
    except (ValueError, IndexError):
        raise ValueError('EIGENVAL 第1行末位应为 ISPIN(1 或 2)')
    ispin = 2 if ispin == 2 else 1
    head6 = lines[5].split()
    if len(head6) < 3:
        raise ValueError('EIGENVAL 第6行应为 NELECT NKPTS NBANDS')
    try:
        nelect = float(head6[0])
        nkpts = int(head6[1])
        nbands = int(head6[2])
    except ValueError:
        raise ValueError('EIGENVAL 第6行 NELECT/NKPTS/NBANDS 解析失败')

    bands = [[[None] * nkpts for _ in range(nbands)] for _ in range(ispin)]
    kpath = []
    idx = 6
    n = len(lines)
    for kk in range(nkpts):
        while idx < n and not lines[idx].strip():      # 跳过 k 块前空行
            idx += 1
        if idx >= n:
            raise ValueError(f'EIGENVAL 在第 {kk + 1} 个 k 点处提前结束(缺 k 坐标行)')
        kp = lines[idx].split()
        if len(kp) < 3:
            raise ValueError(f'EIGENVAL 第 {kk + 1} 个 k 坐标行格式异常:{lines[idx]!r}')
        kpath.append([float(kp[0]), float(kp[1]), float(kp[2])])
        idx += 1
        for bb in range(nbands):
            if idx >= n:
                raise ValueError(f'EIGENVAL 能量数据不足(k={kk + 1}, band={bb + 1})')
            toks = lines[idx].split()
            idx += 1
            # toks[0]=band 号;ISPIN=1 → 能量在 toks[1];ISPIN=2 → toks[1]/toks[2]
            bands[0][bb][kk] = float(toks[1])
            if ispin == 2:
                bands[1][bb][kk] = float(toks[2])
    return {'kpath': kpath, 'bands': bands, 'ispin': ispin, 'nelect': nelect,
            'nbands': nbands, 'nkpts': nkpts, 'efermi': None}


# ── vasprun.xml 解析(取 efermi + eigenvalues) ───────────────────────────────
def parse_vasprun_bands(text: str) -> dict:
    """解析 vasprun.xml 文本 → 与 parse_eigenval 同结构(efermi 从 <i name="efermi">)。

    eigenvalues 路径:eigenvalues/array/set/set(spin N)/set(kpoint N)/r(能量 占据)。
    kpath 从 kpoints/varray[@name="kpointlist"]。缺 eigenvalues 段 → ValueError。
    """
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)

    efermi = None
    for i in root.iter('i'):
        if i.get('name') == 'efermi':
            try:
                efermi = float(i.text)
            except (TypeError, ValueError):
                efermi = None
            break

    kpath = []
    for va in root.iter('varray'):
        if va.get('name') == 'kpointlist':
            for v in va.findall('v'):
                parts = v.text.split()
                if len(parts) >= 3:
                    kpath.append([float(parts[0]), float(parts[1]), float(parts[2])])
            break

    eig = None
    for e in root.iter('eigenvalues'):
        eig = e
        break
    if eig is None:
        raise ValueError('vasprun.xml 未找到 <eigenvalues> 段')
    spin_sets = []
    outer = eig.find('array/set')
    if outer is None:
        raise ValueError('vasprun.xml 的 eigenvalues 缺 array/set 结构')
    for sset in outer.findall('set'):                  # 每个 = 一个自旋通道
        kblocks = []
        for kset in sset.findall('set'):               # 每个 = 一个 k 点
            energies = []
            for r in kset.findall('r'):
                energies.append(float(r.text.split()[0]))   # 首列=能量,次列=占据
            kblocks.append(energies)
        spin_sets.append(kblocks)

    ispin = len(spin_sets) if spin_sets else 1
    nkpts = len(spin_sets[0]) if spin_sets and spin_sets[0] else len(kpath)
    nbands = len(spin_sets[0][0]) if (spin_sets and spin_sets[0] and spin_sets[0][0]) else 0
    # 转 bands[spin][band][k]
    bands = [[[spin_sets[s][k][b] for k in range(nkpts)] for b in range(nbands)]
             for s in range(ispin)]
    if not kpath:
        kpath = [[0.0, 0.0, 0.0] for _ in range(nkpts)]
    return {'kpath': kpath, 'bands': bands, 'ispin': ispin, 'nelect': None,
            'nbands': nbands, 'nkpts': nkpts, 'efermi': efermi}


def band_gap(bands, *, efermi=None, nelect=None, ispin: int = 1) -> dict:
    """带隙判定 → ``{'value','direct','vbm','cbm','metal','note'}``(value 单位 eV)。

    见模块 docstring:有 efermi 按费米阈判(任意自旋);否则用 NELECT 电子计数(仅 ISPIN=1)。
    vbm/cbm 各为 ``{'energy','k'}`` 或 None;metal=True 时 value=0.0。判据不足 → value=None + note。
    """
    if efermi is not None:
        below, above = [], []
        crosses = False
        for sb in bands:
            for ks in sb:
                vals = [e for e in ks if e is not None]
                if vals and min(vals) < efermi < max(vals):
                    crosses = True
                for k, e in enumerate(ks):
                    if e is None:
                        continue
                    (below if e <= efermi else above).append((e, k))
        if not below or not above:
            return {'value': None, 'direct': None, 'vbm': None, 'cbm': None,
                    'metal': None, 'note': '全部本征值在 E_F 同侧,数据异常,无法判带隙。'}
        vbm = max(below)
        cbm = min(above)
        gap = cbm[0] - vbm[0]
        if crosses or gap < _GAP_METAL_TOL:
            return {'value': 0.0, 'direct': None, 'vbm': {'energy': vbm[0], 'k': vbm[1]},
                    'cbm': {'energy': cbm[0], 'k': cbm[1]}, 'metal': True,
                    'note': '有能带穿越 E_F(或 VBM/CBM 重叠),判为金属(带隙 0)。'}
        return {'value': gap, 'direct': vbm[1] == cbm[1],
                'vbm': {'energy': vbm[0], 'k': vbm[1]}, 'cbm': {'energy': cbm[0], 'k': cbm[1]},
                'metal': False,
                'note': f'带隙 {gap:.3f} eV,{"直接" if vbm[1] == cbm[1] else "间接"}(VBM@k={vbm[1]},CBM@k={cbm[1]})。'}

    if nelect is not None and int(ispin) == 1:
        sb = bands[0]
        nbands = len(sb)
        n_occ = int(round(float(nelect) / 2.0))
        if n_occ <= 0 or n_occ >= nbands:
            return {'value': None, 'direct': None, 'vbm': None, 'cbm': None, 'metal': None,
                    'note': f'占据带数 {n_occ} 超出带数范围(nbands={nbands}),无法判带隙'
                            '(可能金属或带数不足)。'}
        valence = sb[n_occ - 1]
        conduction = sb[n_occ]
        vbm_k = max(range(len(valence)), key=lambda k: valence[k])
        cbm_k = min(range(len(conduction)), key=lambda k: conduction[k])
        vbm_e, cbm_e = valence[vbm_k], conduction[cbm_k]
        gap = cbm_e - vbm_e
        if gap <= _GAP_METAL_TOL:
            return {'value': 0.0, 'direct': None, 'vbm': {'energy': vbm_e, 'k': vbm_k},
                    'cbm': {'energy': cbm_e, 'k': cbm_k}, 'metal': True,
                    'note': 'VBM ≥ CBM(价导带重叠),判为金属(带隙 0)。'}
        return {'value': gap, 'direct': vbm_k == cbm_k,
                'vbm': {'energy': vbm_e, 'k': vbm_k}, 'cbm': {'energy': cbm_e, 'k': cbm_k},
                'metal': False,
                'note': f'带隙 {gap:.3f} eV,{"直接" if vbm_k == cbm_k else "间接"}'
                        f'(VBM@k={vbm_k},CBM@k={cbm_k};按 NELECT={nelect:g} 电子计数)。'}

    return {'value': None, 'direct': None, 'vbm': None, 'cbm': None, 'metal': None,
            'note': 'ISPIN=2 且无 efermi 时无法仅凭电子数判带隙;请提供 efermi(或用 vasprun)。'}


def parse_bands(source: str, *, efermi: float | None = None) -> dict:
    """解析能带来源(EIGENVAL 文本或 vasprun.xml 文本)→ ``{'kpath','bands','efermi','gap',...}``。

    自动分派:内容含 XML 头(``<?xml`` / ``<modeling``)→ vasprun,否则按 EIGENVAL 解析。
    efermi 参数可**显式覆盖**(EIGENVAL 无 efermi 时,传入可改用费米阈判带隙)。
    gap 见 band_gap:含 value/direct/vbm/cbm/metal/note。
    """
    head = source.lstrip()[:200]
    if head.startswith('<?xml') or '<modeling' in head:
        data = parse_vasprun_bands(source)
    else:
        data = parse_eigenval(source)
    ef = efermi if efermi is not None else data.get('efermi')
    data['efermi'] = ef
    data['gap'] = band_gap(data['bands'], efermi=ef, nelect=data.get('nelect'),
                           ispin=data.get('ispin', 1))
    return data


# ── 能带出图 ─────────────────────────────────────────────────────────────────
def _kdistance(kpath):
    """k 路径累积距离(分数倒空间欧氏,作横轴代理);相邻重合点(段断点)距离置 0。"""
    d = [0.0]
    for i in range(1, len(kpath)):
        step = sum((kpath[i][j] - kpath[i - 1][j]) ** 2 for j in range(3)) ** 0.5
        d.append(d[-1] + step)
    return d


def band_plot(data, out_path, *, efermi=None, ticks=None, ylim=(-6, 6), title: str = '',
              width=None, palette: str = 'tol_bright', panel: str = '',
              formats=('png', 'pdf'), style_kw: dict | None = None) -> list:
    """能带图:E−E_F vs k,费米零点横线,高对称点竖线+标签,自旋双色,带隙标注。

    data:parse_bands 结果(需 bands / kpath;efermi 参数可覆盖 data['efermi'])。
    ticks:``[(k_index, label), ...]`` 高对称点位置与标签(缺省不画竖线)。ylim 为相对 E_F 范围。
    横轴用 k 路径累积距离(见 _kdistance)。样式走 native_charts。返回导出路径列表。
    """
    from vcstudio.external.native_charts import (PALETTES, ZERO_LINE_COLOR, SINGLE_COL,
                                                 apply_paper_style, _new_figure,
                                                 _save_dual, add_panel_label, chem_label)
    bands = data['bands']
    kpath = data.get('kpath') or []
    ef = efermi if efermi is not None else data.get('efermi')
    if ef is None:
        gap = data.get('gap') or {}
        vbm = gap.get('vbm')
        ef = vbm['energy'] if vbm else 0.0        # 半导体惯例:无 efermi 时取 VBM 作零点
    xs = _kdistance(kpath) if kpath else list(range(len(bands[0][0]) if bands and bands[0] else 0))

    fig_w = width if width is not None else SINGLE_COL
    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.95)
        spin_colors = [PALETTES.get(palette, PALETTES['tol_bright'])[0], '#EE6677']
        for s, sb in enumerate(bands):
            c = spin_colors[s % 2]
            for band in sb:
                ys = [(e - ef) if e is not None else None for e in band]
                ax.plot(xs, ys, color=c, lw=0.9, zorder=3)
        ax.axhline(0.0, color=ZERO_LINE_COLOR, lw=0.9, ls=(0, (5, 3)), zorder=2)
        ax.annotate(r'$E_\mathrm{F}$', (xs[-1] if xs else 1.0, 0.0),
                    xytext=(-2, 2), textcoords='offset points', ha='right', va='bottom',
                    fontsize=7.5, color=ZERO_LINE_COLOR)
        if ticks:
            for kidx, label in ticks:
                if 0 <= kidx < len(xs):
                    ax.axvline(xs[kidx], color='#8C8C8C', lw=0.7, zorder=1)
            ax.set_xticks([xs[kidx] for kidx, _ in ticks if 0 <= kidx < len(xs)])
            ax.set_xticklabels([chem_label(lbl) for kidx, lbl in ticks if 0 <= kidx < len(xs)])
        else:
            ax.set_xticks([])
        gap = data.get('gap') or {}
        if gap.get('value'):
            kind = '直接' if gap.get('direct') else '间接'
            ax.set_title((title + '  ' if title else '')
                         + rf'$E_g$ = {gap["value"]:.2f} eV ({kind})')
        elif title:
            ax.set_title(title)
        if xs:
            ax.set_xlim(xs[0], xs[-1])
        ax.set_ylim(*ylim)
        ax.set_ylabel(r'$E - E_\mathrm{F}$ (eV)')
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)
