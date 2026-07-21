"""Methods 段自动生成(C3)——真实 INCAR/KPOINTS/POTCAR → 双语方法学散文 + BibTeX。

纯函数,零 IO。铁律:只写文件里真有的,缺件降级 warnings 明说,绝不编造;
**GGA 标签优先于 POTCAR 味**(本项目 GGA=RP + PAW_PBE 赝势 = RPBE 泛函 +
PBE 生成的 PAW 数据集,措辞必须精确区分泛函与赝势)。
复用 incar_builder.parse_incar;TITEL 正则复制 potcar.py 口径(不 import 私有名)。
"""
from __future__ import annotations

import re

from vcstudio.generate.incar_builder import parse_incar
from vcstudio.shared.vasp_identity import (
    canonical_kpoints_effective_text, canonical_kpoints_sha256,
)

_TITEL_RE = re.compile(r'TITEL\s*=\s*(.+)')

# ── 映射表(spec 定稿) ───────────────────────────────────────────────────────
GGA_MAP = {'PE': 'PBE', 'RP': 'RPBE', 'PS': 'PBEsol', '91': 'PW91',
           'RE': 'revPBE', 'AM': 'AM05'}
FLAVOR_MAP = {'PAW_PBE': 'PBE', 'PAW_GGA': 'PW91', 'PAW_LDA': 'LDA', 'PAW': 'LDA'}
IVDW_MAP = {1: 'DFT-D2', 10: 'DFT-D2', 11: 'DFT-D3(zero)', 12: 'DFT-D3(BJ)',
            2: 'TS', 20: 'TS', 21: 'TS+SCS', 4: 'dDsC',
            202: 'MBD@rsSCS', 263: 'rVV10'}
IBRION_MAP = {2: 'CG', 1: 'quasi-Newton', 3: 'damped MD', 0: 'MD'}
KNOWN_METAGGA = frozenset(('SCAN', 'R2SCAN', 'RSCAN', 'TPSS', 'RTPSS', 'M06L', 'MBJ'))


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
    def explicit_record():
        # The first line is a free-form comment.  Hash every effective line
        # after whitespace normalisation so equal-length explicit meshes cannot
        # be mistaken for one another merely because their point counts match.
        effective = canonical_kpoints_effective_text(text)
        return {
            'scheme': 'explicit', 'grid': None,
            'raw': effective[:120],
            'raw_sha256': canonical_kpoints_sha256(text),
        }

    if len(lines) < 4:
        return explicit_record()
    mode = lines[2][:1].upper()
    if lines[1].split()[:1] == ['0'] and mode in ('G', 'M'):
        try:
            grid = [int(t) for t in lines[3].split()[:3]]
        except ValueError:
            return explicit_record()
        try:
            shift = [float(t) for t in lines[4].split()[:3]] if len(lines) > 4 else []
        except ValueError:
            shift = []
        if len(shift) != 3:
            shift = [0.0, 0.0, 0.0]
        return {'scheme': 'Gamma' if mode == 'G' else 'Monkhorst-Pack',
                'grid': grid, 'shift': shift}
    return explicit_record()


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

    def _bool(key, default=False):
        value = inc.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        normalised = str(value).strip().strip('.').upper()
        if normalised in ('T', 'TRUE'):
            return True
        if normalised in ('F', 'FALSE'):
            return False
        warnings.append(f'{key}={value!r} 不是可识别的布尔值')
        return None

    potcars = parse_potcar_titels(potcar_text) if potcar_text else []
    if not potcar_text:
        warnings.append('缺 POTCAR:赝势条目与泛函回退不可用')
    flavor = potcars[0]['flavor'] if potcars else None

    gga = str(inc.get('GGA', '')).strip().strip('"\'').upper() or None
    if gga:
        base_functional = GGA_MAP.get(gga, f'GGA={gga}')
    elif flavor:
        base_functional = FLAVOR_MAP.get(flavor, flavor)
    else:
        base_functional = None
        warnings.append('无 GGA 键且无 POTCAR,泛函未知')

    metagga = str(inc.get('METAGGA', '')).strip().strip('"\'').upper() or None
    if metagga in ('NONE', 'FALSE', 'F'):
        metagga = None
    lhfcalc = _bool('LHFCALC', False)
    aexx_explicit = _num('AEXX')
    hfscreen_explicit = _num('HFSCREEN')
    xc_numeric_keys = ('AEXX', 'HFSCREEN', 'AGGAX', 'AGGAC', 'ALDAC')
    invalid_xc_numeric = [
        key for key in xc_numeric_keys if key in inc and _num(key) is None
    ]
    for key in invalid_xc_numeric:
        warnings.append(f'{key}={inc.get(key)!r} 不是可识别的数值')
    # VASP's documented hybrid defaults are part of the effective method even
    # when not written explicitly.  Recording the effective values avoids a
    # false mismatch between an omitted default and the same explicit value.
    aexx = (0.25 if lhfcalc is True and 'AEXX' not in inc else aexx_explicit)
    hfscreen = (0.0 if lhfcalc is True and 'HFSCREEN' not in inc
                else hfscreen_explicit)
    lasph = _bool('LASPH', False)
    luse_vdw = _bool('LUSE_VDW', False)

    if metagga:
        meta_label = {'R2SCAN': 'r2SCAN'}.get(metagga, metagga)
        functional = meta_label
        functional_class = 'meta-GGA'
    else:
        functional = base_functional
        functional_class = 'GGA' if functional else None
    if lhfcalc is True:
        if metagga:
            functional = f'hybrid {functional}'
            functional_class = 'hybrid meta-GGA'
        elif (base_functional == 'PBE' and aexx is not None
              and abs(aexx - 0.25) < 1e-12 and hfscreen is not None
              and abs(hfscreen - 0.2) < 1e-12):
            functional, functional_class = 'HSE06', 'screened hybrid'
        elif (base_functional == 'PBE' and aexx is not None
              and abs(aexx - 0.25) < 1e-12 and hfscreen is not None
              and abs(hfscreen - 0.3) < 1e-12):
            functional, functional_class = 'HSE03', 'screened hybrid'
        elif (base_functional == 'PBE' and aexx is not None
              and abs(aexx - 0.25) < 1e-12 and hfscreen == 0.0):
            functional, functional_class = 'PBE0', 'hybrid'
        else:
            functional = f'hybrid {base_functional or "unknown semilocal base"}'
            functional_class = 'hybrid'
    elif lhfcalc is None:
        functional_class = None

    known_base = bool(
        (gga and gga in GGA_MAP) or (not gga and flavor in FLAVOR_MAP))
    orphan_hybrid_parameters = bool(
        lhfcalc is not True and any(key in inc for key in ('AEXX', 'HFSCREEN')))
    functional_known = bool(
        lhfcalc is not None and (not metagga or metagga in KNOWN_METAGGA)
        and (known_base or bool(metagga)) and not invalid_xc_numeric
        and not orphan_hybrid_parameters)

    if metagga and lasph is not True:
        warnings.append(
            f'METAGGA={metagga} 但 LASPH 未明确开启；请核对 meta-GGA 的 PAW 球内项')
    if orphan_hybrid_parameters:
        warnings.append('设置了 AEXX/HFSCREEN 但 LHFCALC 未开启；混合泛函设置不完整')

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
    try:
        ivdw_setting = int(ivdw) if ivdw is not None else 0
    except (TypeError, ValueError):
        ivdw_setting = str(ivdw).strip() if ivdw is not None else 0
        warnings.append(f'IVDW={ivdw!r} 无法识别，色散设置需人工核对')
    if ivdw_name is None and luse_vdw is True:
        ivdw_name = 'nonlocal vdW-DF (LUSE_VDW)'
    ibrion = inc.get('IBRION')
    nsw = _num('NSW')
    relax = None
    if nsw and nsw > 0 and ibrion is not None:
        try:
            relax = IBRION_MAP.get(int(ibrion), f'IBRION={ibrion}')
        except (TypeError, ValueError):
            relax = None

    encut = _num('ENCUT')
    ldau = str(inc.get('LDAU', '')).strip().upper() in ('.TRUE.', 'T', 'TRUE')
    ldauu = (str(inc.get('LDAUU', '')).strip() or None) if ldau else None
    facts = {
        'functional': functional,
        'functional_class': functional_class,
        'functional_known': functional_known,
        'base_functional': base_functional,
        'gga': gga,
        'metagga': metagga,
        'lhfcalc': lhfcalc,
        'aexx': aexx,
        'hfscreen': hfscreen,
        'aggax': _num('AGGAX'),
        'aggac': _num('AGGAC'),
        'aldac': _num('ALDAC'),
        'lasph': lasph,
        'luse_vdw': luse_vdw,
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
        'ivdw_setting': ivdw_setting,
        # Preserve an explicit non-integral value for downstream validity
        # gates; only a genuinely omitted ISPIN receives the VASP default.
        'ispin': (1 if 'ISPIN' not in inc else _num('ISPIN')),
        'relax': relax,
        'nsw': int(nsw) if nsw else None,
        'ldau': ldau,
        'ldauu': ldauu,       # LDAUU 有效 U 值原串(如 '0 0 3.9 0');无则 None
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
        family = f.get('functional_class')
        if family == 'meta-GGA':
            s = f'交换关联作用采用 meta-GGA {f["functional"]} 泛函处理'
        elif family in ('hybrid', 'screened hybrid', 'hybrid meta-GGA'):
            s = f'交换关联作用采用{family} {f["functional"]} 泛函处理'
            if f.get('aexx') is not None:
                s += f'(AEXX = {f["aexx"]:g}'
                if f.get('hfscreen') is not None:
                    s += f', HFSCREEN = {f["hfscreen"]:g}'
                s += ')'
        else:
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
        s = '对局域 d/f 电子施加了 DFT+U 校正(Dudarev 方案'
        s += (f',有效 U 值 LDAUU = {f["ldauu"]} eV' if f.get('ldauu')
              else ',参数见 INCAR')
        parts.append(s + ')。')
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
        family = f.get('functional_class')
        if family == 'meta-GGA':
            s = (f' Exchange-correlation effects were treated with the '
                 f'meta-GGA {f["functional"]} functional')
        elif family in ('hybrid', 'screened hybrid', 'hybrid meta-GGA'):
            s = (f' Exchange-correlation effects were treated with the '
                 f'{family} {f["functional"]} functional')
            if f.get('aexx') is not None:
                s += f' (AEXX = {f["aexx"]:g}'
                if f.get('hfscreen') is not None:
                    s += f', HFSCREEN = {f["hfscreen"]:g}'
                s += ')'
        else:
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
        s = (' A DFT+U correction (Dudarev scheme) was applied to localized '
             'd/f electrons')
        s += (f' with effective U values LDAUU = {f["ldauu"]} eV'
              if f.get('ldauu') else ' (parameters as in the INCAR)')
        parts.append(s + '.')
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
    'hse': """@article{Heyd2003,
  author  = {Heyd, J. and Scuseria, G. E. and Ernzerhof, M.},
  title   = {Hybrid functionals based on a screened Coulomb potential},
  journal = {J. Chem. Phys.},
  volume  = {118},
  pages   = {8207--8215},
  year    = {2003}
}""",
    'pbe0': """@article{Adamo1999,
  author  = {Adamo, C. and Barone, V.},
  title   = {Toward reliable density functional methods without adjustable parameters: The PBE0 model},
  journal = {J. Chem. Phys.},
  volume  = {110},
  pages   = {6158--6170},
  year    = {1999}
}""",
    'scan': """@article{Sun2015,
  author  = {Sun, J. and Ruzsinszky, A. and Perdew, J. P.},
  title   = {Strongly constrained and appropriately normed semilocal density functional},
  journal = {Phys. Rev. Lett.},
  volume  = {115},
  pages   = {036402},
  year    = {2015}
}""",
    'r2scan': """@article{Furness2020,
  author  = {Furness, J. W. and Kaplan, A. D. and Ning, J. and Perdew, J. P. and Sun, J.},
  title   = {Accurate and numerically efficient r2SCAN meta-generalized gradient approximation},
  journal = {J. Phys. Chem. Lett.},
  volume  = {11},
  pages   = {8208--8215},
  year    = {2020}
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
    'd2': """@article{Grimme2006,
  author  = {Grimme, S.},
  title   = {Semiempirical GGA-type density functional constructed with a long-range dispersion correction},
  journal = {J. Comput. Chem.},
  volume  = {27},
  pages   = {1787--1799},
  year    = {2006}
}""",
    'ts': """@article{Tkatchenko2009,
  author  = {Tkatchenko, A. and Scheffler, M.},
  title   = {Accurate molecular van der Waals interactions from ground-state electron density and free-atom reference data},
  journal = {Phys. Rev. Lett.},
  volume  = {102},
  pages   = {073005},
  year    = {2009}
}""",
    'pw91': """@article{Perdew1992,
  author  = {Perdew, J. P. and Chevary, J. A. and Vosko, S. H. and Jackson, K. A. and Pederson, M. R. and Singh, D. J. and Fiolhais, C.},
  title   = {Atoms, molecules, solids, and surfaces: Applications of the generalized gradient approximation for exchange and correlation},
  journal = {Phys. Rev. B},
  volume  = {46},
  pages   = {6671--6687},
  year    = {1992}
}""",
    'pbesol': """@article{Perdew2008,
  author  = {Perdew, J. P. and Ruzsinszky, A. and Csonka, G. I. and Vydrov, O. A. and Scuseria, G. E. and Constantin, L. A. and Zhou, X. and Burke, K.},
  title   = {Restoring the density-gradient expansion for exchange in solids and surfaces},
  journal = {Phys. Rev. Lett.},
  volume  = {100},
  pages   = {136406},
  year    = {2008}
}""",
    'revpbe': """@article{Zhang1998,
  author  = {Zhang, Y. and Yang, W.},
  title   = {Comment on ``Generalized Gradient Approximation Made Simple''},
  journal = {Phys. Rev. Lett.},
  volume  = {80},
  pages   = {890},
  year    = {1998}
}""",
    'am05': """@article{Armiento2005,
  author  = {Armiento, R. and Mattsson, A. E.},
  title   = {Functional designed to include surface effects in self-consistent density functional theory},
  journal = {Phys. Rev. B},
  volume  = {72},
  pages   = {085108},
  year    = {2005}
}""",
    'dftu': """@article{Dudarev1998,
  author  = {Dudarev, S. L. and Botton, G. A. and Savrasov, S. Y. and Humphreys, C. J. and Sutton, A. P.},
  title   = {Electron-energy-loss spectra and the structural stability of nickel oxide: An LSDA+U study},
  journal = {Phys. Rev. B},
  volume  = {57},
  pages   = {1505--1509},
  year    = {1998}
}""",
}


def render_bibtex(f: dict) -> str:
    """facts → BibTeX,只含真用到的条目(VASP 恒引;PAW 有赝势才引;泛函/色散/+U 按实)。"""
    keys = ['vasp1', 'vasp2']
    if f.get('potcar_flavor'):
        keys += ['paw1', 'paw2']
    func = f.get('functional')
    if func == 'PBE':
        keys.append('pbe')
    elif func in ('HSE03', 'HSE06'):
        keys += ['pbe', 'hse']
    elif func == 'PBE0':
        keys += ['pbe', 'pbe0']
    elif func == 'SCAN':
        keys.append('scan')
    elif func == 'r2SCAN':
        keys += ['scan', 'r2scan']
    elif func == 'RPBE':
        keys += ['pbe', 'rpbe']    # RPBE 是 PBE 的修订,惯例两篇都引
    elif func == 'revPBE':
        keys += ['pbe', 'revpbe']  # 同 RPBE:PBE 的修订,两篇都引
    elif func == 'PBEsol':
        keys += ['pbe', 'pbesol']  # PBEsol 为固体/表面复原梯度展开的 PBE 修订,两篇都引
    elif func == 'PW91':
        keys.append('pw91')        # PW91 先于 PBE,独立引
    elif func == 'AM05':
        keys.append('am05')        # AM05 独立构造,单引
    ivdw = f.get('ivdw') or ''
    if ivdw.startswith('DFT-D3'):
        keys.append('d3')
        if 'BJ' in ivdw:
            keys.append('d3bj')
    elif ivdw == 'DFT-D2':
        keys.append('d2')
    elif ivdw.startswith('TS'):    # TS 与 TS+SCS 均引 Tkatchenko-Scheffler 原文
        keys.append('ts')
    if f.get('ldau'):
        keys.append('dftu')        # Dudarev 方案 DFT+U
    return '\n\n'.join(_BIB[k] for k in keys)
