"""Methods 段自动生成(C3)——真实 INCAR/KPOINTS/POTCAR → 双语方法学散文 + BibTeX。

纯函数,零 IO。铁律:只写文件里真有的,缺件降级 warnings 明说,绝不编造;
**GGA 标签优先于 POTCAR 味**(本项目 GGA=RP + PAW_PBE 赝势 = RPBE 泛函 +
PBE 生成的 PAW 数据集,措辞必须精确区分泛函与赝势)。
复用 incar_builder.parse_incar;TITEL 正则复制 potcar.py 口径(不 import 私有名)。
"""
from __future__ import annotations

import re

from vcstudio.generate.incar_builder import parse_incar

_TITEL_RE = re.compile(r'TITEL\s*=\s*(.+)')

# ── 映射表(spec 定稿) ───────────────────────────────────────────────────────
GGA_MAP = {'PE': 'PBE', 'RP': 'RPBE', 'PS': 'PBEsol', '91': 'PW91',
           'RE': 'revPBE', 'AM': 'AM05'}
FLAVOR_MAP = {'PAW_PBE': 'PBE', 'PAW_GGA': 'PW91', 'PAW_LDA': 'LDA', 'PAW': 'LDA'}
IVDW_MAP = {1: 'DFT-D2', 10: 'DFT-D2', 11: 'DFT-D3(zero)', 12: 'DFT-D3(BJ)',
            2: 'TS', 20: 'TS', 21: 'TS+SCS', 4: 'dDsC'}
IBRION_MAP = {2: 'CG', 1: 'quasi-Newton', 3: 'damped MD', 0: 'MD'}


def parse_potcar_titels(text: str) -> list[dict]:
    """POTCAR 文本 → 逐 TITEL:{'titel','flavor','variant','element','date'}。"""
    out = []
    for m in _TITEL_RE.finditer(text or ''):
        titel = m.group(1).strip()
        parts = titel.split()
        flavor = parts[0] if parts else ''
        variant = parts[1] if len(parts) > 1 else ''
        date = parts[2] if len(parts) > 2 else ''
        out.append({'titel': titel, 'flavor': flavor, 'variant': variant,
                    'element': variant.split('_')[0] if variant else '',
                    'date': date})
    return out


def parse_kpoints_scheme(text: str) -> dict | None:
    """自动网格 KPOINTS → {'scheme','grid'};line-mode/显式列表 → {'scheme':'explicit','raw'}。"""
    if not text or not text.strip():
        return None
    lines = [ln.strip() for ln in text.splitlines()]
    if len(lines) < 4:
        return {'scheme': 'explicit', 'grid': None, 'raw': text.strip()[:120]}
    mode = lines[2][:1].upper()
    if lines[1].split()[:1] == ['0'] and mode in ('G', 'M'):
        try:
            grid = [int(t) for t in lines[3].split()[:3]]
        except ValueError:
            return {'scheme': 'explicit', 'grid': None, 'raw': text.strip()[:120]}
        return {'scheme': 'Gamma' if mode == 'G' else 'Monkhorst-Pack',
                'grid': grid}
    return {'scheme': 'explicit', 'grid': None, 'raw': text.strip()[:120]}


def extract_facts(incar_text: str, kpoints_text: str | None,
                  potcar_text: str | None) -> dict:
    """三文件文本 → {'facts', 'warnings'}。缺件降级明说,绝不编造。"""
    warnings: list[str] = []
    inc = parse_incar(incar_text or '')

    def _num(key):
        v = inc.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    potcars = parse_potcar_titels(potcar_text) if potcar_text else []
    if not potcar_text:
        warnings.append('缺 POTCAR:赝势条目与泛函回退不可用')
    flavor = potcars[0]['flavor'] if potcars else None

    gga = str(inc.get('GGA', '')).strip().upper() or None
    if gga:
        functional = GGA_MAP.get(gga, f'GGA={gga}')
    elif flavor:
        functional = FLAVOR_MAP.get(flavor, flavor)
    else:
        functional = None
        warnings.append('无 GGA 键且无 POTCAR,泛函未知')

    kpts = parse_kpoints_scheme(kpoints_text) if kpoints_text else None
    if not kpoints_text:
        warnings.append('缺 KPOINTS:K 网格信息不可用')

    ismear = _num('ISMEAR')
    sigma = _num('SIGMA')
    if ismear is None:
        smearing = None
    elif ismear == 0:
        smearing = 'Gaussian'
    elif ismear > 0:
        smearing = f'Methfessel-Paxton (order {int(ismear)})'
    elif ismear == -5:
        smearing = 'tetrahedron (Blöchl)'
    elif ismear == -1:
        smearing = 'Fermi'
    else:
        smearing = f'ISMEAR={int(ismear)}'

    ediffg = _num('EDIFFG')
    ivdw = inc.get('IVDW')
    try:
        ivdw_name = IVDW_MAP.get(int(ivdw)) if ivdw is not None else None
    except (TypeError, ValueError):
        ivdw_name = None
    ibrion = inc.get('IBRION')
    nsw = _num('NSW')
    relax = None
    if nsw and nsw > 0 and ibrion is not None:
        try:
            relax = IBRION_MAP.get(int(ibrion), f'IBRION={ibrion}')
        except (TypeError, ValueError):
            relax = None

    encut = _num('ENCUT')
    facts = {
        'functional': functional,
        'potcar_flavor': flavor,
        'potcars': potcars,
        'encut': int(encut) if encut else None,
        'kpoints': kpts,
        'smearing': smearing,
        'sigma': sigma,
        'ediff': _num('EDIFF'),
        'ediffg_force': abs(ediffg) if ediffg is not None and ediffg < 0 else None,
        'ediffg_energy': ediffg if ediffg is not None and ediffg > 0 else None,
        'ivdw': ivdw_name,
        'ispin': int(_num('ISPIN')) if _num('ISPIN') else None,
        'relax': relax,
        'nsw': int(nsw) if nsw else None,
        'ldau': bool(str(inc.get('LDAU', '')).strip().upper()
                     in ('.TRUE.', 'T', 'TRUE')),
    }
    return {'facts': facts, 'warnings': warnings}


def _grid_str(kpts, sep=' × '):
    if kpts and kpts.get('grid'):
        return sep.join(str(g) for g in kpts['grid'])
    return None


def render_zh(f: dict) -> str:
    """facts → 中文 Methods 段。只陈述已知事实。"""
    parts = []
    lead = '所有密度泛函理论(DFT)计算均使用 VASP 程序完成'
    if f.get('potcar_flavor'):
        lead += (f',采用投影缀加波(PAW)方法描述离子实与价电子相互作用'
                 f'({f["potcar_flavor"]} 赝势')
        if f.get('potcars'):
            lead += ':' + ', '.join(p['variant'] for p in f['potcars'])
        lead += ')'
    parts.append(lead + '。')
    if f.get('functional'):
        s = f'交换关联作用采用广义梯度近似(GGA)下的 {f["functional"]} 泛函处理'
        if f.get('functional') == 'RPBE' and f.get('potcar_flavor') == 'PAW_PBE':
            s += '(PAW 数据集由 PBE 生成)'
        parts.append(s + '。')
    if f.get('encut'):
        parts.append(f'平面波截断能设为 {f["encut"]} eV。')
    grid = _grid_str(f.get('kpoints'))
    if grid:
        parts.append(f'布里渊区采用 {f["kpoints"]["scheme"]} 方案的 {grid} K 点网格采样。')
    if f.get('smearing'):
        s = f'电子占据采用 {f["smearing"]} 展宽'
        if f.get('sigma'):
            s += f'(σ = {f["sigma"]:g} eV)'
        parts.append(s + '。')
    if f.get('ediff'):
        parts.append(f'电子自洽收敛判据为 {f["ediff"]:.0e} eV。')
    if f.get('ediffg_force'):
        parts.append(f'离子弛豫至各原子受力小于 {f["ediffg_force"]:g} eV/Å'
                     + (f'({f["relax"]} 算法)' if f.get('relax') else '') + '。')
    elif f.get('relax'):
        parts.append(f'结构弛豫采用 {f["relax"]} 算法。')
    if f.get('ivdw'):
        parts.append(f'范德华相互作用通过 {f["ivdw"]} 色散校正描述。')
    if f.get('ispin') == 2:
        parts.append('计算考虑自旋极化。')
    if f.get('ldau'):
        parts.append('对局域 d/f 电子施加了 DFT+U 校正(参数见 INCAR)。')
    return ''.join(parts)


def render_en(f: dict) -> str:
    """facts → English methods paragraph. States only known facts."""
    parts = []
    lead = ('All density functional theory (DFT) calculations were performed '
            'with the VASP code')
    if f.get('potcar_flavor'):
        lead += (', using the projector augmented-wave (PAW) method '
                 f'({f["potcar_flavor"]} datasets')
        if f.get('potcars'):
            lead += ': ' + ', '.join(p['variant'] for p in f['potcars'])
        lead += ')'
    parts.append(lead + '.')
    if f.get('functional'):
        s = (f' Exchange-correlation effects were treated within the '
             f'generalized gradient approximation using the {f["functional"]} '
             f'functional')
        if f.get('functional') == 'RPBE' and f.get('potcar_flavor') == 'PAW_PBE':
            s += ' (with PBE-generated PAW datasets)'
        parts.append(s + '.')
    if f.get('encut'):
        parts.append(f' The plane-wave cutoff energy was set to {f["encut"]} eV.')
    grid = _grid_str(f.get('kpoints'))
    if grid:
        parts.append(f' The Brillouin zone was sampled with a '
                     f'{f["kpoints"]["scheme"]} {grid} k-point mesh.')
    if f.get('smearing'):
        s = f' Electronic occupations were treated with {f["smearing"]} smearing'
        if f.get('sigma'):
            s += f' (sigma = {f["sigma"]:g} eV)'
        parts.append(s + '.')
    if f.get('ediff'):
        parts.append(f' The electronic self-consistency criterion was '
                     f'{f["ediff"]:.0e} eV.')
    if f.get('ediffg_force'):
        s = (f' Structures were relaxed until residual forces were below '
             f'{f["ediffg_force"]:g} eV/Å')
        if f.get('relax'):
            s += f' using the {f["relax"]} algorithm'
        parts.append(s + '.')
    elif f.get('relax'):
        parts.append(f' Structural relaxations used the {f["relax"]} algorithm.')
    if f.get('ivdw'):
        parts.append(f' Van der Waals interactions were described by the '
                     f'{f["ivdw"]} dispersion correction.')
    if f.get('ispin') == 2:
        parts.append(' Spin polarization was included.')
    if f.get('ldau'):
        parts.append(' A DFT+U correction was applied to localized d/f '
                     'electrons (parameters as in the INCAR).')
    return ''.join(parts)


# ── BibTeX 库(只引真用到的) ────────────────────────────────────────────────
_BIB = {
    'vasp1': """@article{Kresse1996PRB,
  author  = {Kresse, G. and Furthm\\"uller, J.},
  title   = {Efficient iterative schemes for ab initio total-energy calculations using a plane-wave basis set},
  journal = {Phys. Rev. B},
  volume  = {54},
  pages   = {11169--11186},
  year    = {1996}
}""",
    'vasp2': """@article{Kresse1996CMS,
  author  = {Kresse, G. and Furthm\\"uller, J.},
  title   = {Efficiency of ab-initio total energy calculations for metals and semiconductors using a plane-wave basis set},
  journal = {Comput. Mater. Sci.},
  volume  = {6},
  pages   = {15--50},
  year    = {1996}
}""",
    'paw1': """@article{Blochl1994,
  author  = {Bl\\"ochl, P. E.},
  title   = {Projector augmented-wave method},
  journal = {Phys. Rev. B},
  volume  = {50},
  pages   = {17953--17979},
  year    = {1994}
}""",
    'paw2': """@article{Kresse1999,
  author  = {Kresse, G. and Joubert, D.},
  title   = {From ultrasoft pseudopotentials to the projector augmented-wave method},
  journal = {Phys. Rev. B},
  volume  = {59},
  pages   = {1758--1775},
  year    = {1999}
}""",
    'pbe': """@article{Perdew1996,
  author  = {Perdew, J. P. and Burke, K. and Ernzerhof, M.},
  title   = {Generalized Gradient Approximation Made Simple},
  journal = {Phys. Rev. Lett.},
  volume  = {77},
  pages   = {3865--3868},
  year    = {1996}
}""",
    'rpbe': """@article{Hammer1999,
  author  = {Hammer, B. and Hansen, L. B. and N\\o{}rskov, J. K.},
  title   = {Improved adsorption energetics within density-functional theory using revised Perdew-Burke-Ernzerhof functionals},
  journal = {Phys. Rev. B},
  volume  = {59},
  pages   = {7413--7421},
  year    = {1999}
}""",
    'd3': """@article{Grimme2010,
  author  = {Grimme, S. and Antony, J. and Ehrlich, S. and Krieg, H.},
  title   = {A consistent and accurate ab initio parametrization of density functional dispersion correction (DFT-D) for the 94 elements H-Pu},
  journal = {J. Chem. Phys.},
  volume  = {132},
  pages   = {154104},
  year    = {2010}
}""",
    'd3bj': """@article{Grimme2011,
  author  = {Grimme, S. and Ehrlich, S. and Goerigk, L.},
  title   = {Effect of the damping function in dispersion corrected density functional theory},
  journal = {J. Comput. Chem.},
  volume  = {32},
  pages   = {1456--1465},
  year    = {2011}
}""",
}


def render_bibtex(f: dict) -> str:
    """facts → BibTeX,只含真用到的条目(VASP 恒引;PAW 有赝势才引;泛函/色散按实)。"""
    keys = ['vasp1', 'vasp2']
    if f.get('potcar_flavor'):
        keys += ['paw1', 'paw2']
    func = f.get('functional')
    if func == 'PBE':
        keys.append('pbe')
    elif func == 'RPBE':
        keys += ['pbe', 'rpbe']   # RPBE 是 PBE 的修订,惯例两篇都引
    ivdw = f.get('ivdw') or ''
    if ivdw.startswith('DFT-D3'):
        keys.append('d3')
        if 'BJ' in ivdw:
            keys.append('d3bj')
    return '\n\n'.join(_BIB[k] for k in keys)
