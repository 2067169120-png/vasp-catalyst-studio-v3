"""CP2K Backend——&GLOBAL/&FORCE_EVAL 输入生成 + output 能量/收敛解析。

CP2K 为高斯+平面波(GPW)方法,与 VASP(纯平面波+PAW)**方法不等价**:
- CUTOFF 是密度网格截断(Ry,GTH 口径),**不是** VASP 的 ENCUT(平面波截断,eV),
  绝不从 ENCUT 换算——从 spec.extras['cutoff_ry'] 取;缺则显式 warning 并用占位默认值。
- 基组为原子中心高斯(GTH 命名惯例),每元素 &KIND 指定 BASIS_SET/POTENTIAL。

CP2K 软件本体用户自备,本适配只出输入文件(cp2k.inp)、解析输出(cp2k.out)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import glob
import os
import re

from vcstudio.engines.calcspec import (
    HARTREE_TO_EV, CalcSpec, EngineBackend, parse_structure,
)

# eV/Å → Hartree/Bohr(CP2K 力判据 a.u. 口径)。1 Hartree/Bohr = 51.4221 eV/Å。
_BOHR_A = 0.52917721
_EV_A_TO_HB = _BOHR_A / HARTREE_TO_EV

# 默认 GTH 命名惯例(用户可经 extras 覆盖)。
_DEF_BASIS = 'DZVP-MOLOPT-SR-GTH'
_DEF_POTENTIAL = 'GTH-PBE'
_DEF_BASIS_FILE = 'BASIS_MOLOPT'
_DEF_POT_FILE = 'GTH_POTENTIALS'
_DEF_CUTOFF_RY = 400.0
_DEF_REL_CUTOFF_RY = 60.0

_RUN_TYPE = {'relax': 'GEO_OPT', 'static': 'ENERGY_FORCE', 'freq': 'VIBRATIONAL_ANALYSIS'}
# CP2K &XC_FUNCTIONAL 直接支持的关键字(其余走原名 + warning)。
_XC_KNOWN = {'PBE', 'BLYP', 'BP', 'PADE', 'PW92', 'HCTH120'}
# 色散 → &PAIR_POTENTIAL TYPE。
_D3_TYPE = {'D3': 'DFTD3', 'D3(BJ)': 'DFTD3(BJ)', 'D3BJ': 'DFTD3(BJ)'}

# 输出能量行:'ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]: -17.1461'。
# 取含该标记行的**末个浮点**(对不同 CP2K 版本的单位标注/尾随文本都稳健)。
_FLOAT_RE = re.compile(r'[-+]?\d+\.\d+(?:[Ee][-+]?\d+)?')


def _cp2k_energy_hartree(text: str) -> float | None:
    """CP2K 输出 → 末次 'ENERGY| Total FORCE_EVAL' 行的能量(Hartree)。无 → None。"""
    val = None
    for line in text.splitlines():
        if 'ENERGY|' in line and 'Total FORCE_EVAL' in line:
            floats = _FLOAT_RE.findall(line)
            if floats:
                val = float(floats[-1])
    return val


def _out_path(out_dir: str) -> str | None:
    """定位 CP2K 输出:优先 cp2k.out,否则目录内首个 *.out。"""
    fixed = os.path.join(out_dir, 'cp2k.out')
    if os.path.isfile(fixed):
        return fixed
    cands = sorted(glob.glob(os.path.join(out_dir, '*.out')))
    return cands[0] if cands else None


def build_input(spec: CalcSpec) -> tuple[str, list]:
    """CalcSpec → CP2K 输入文本(cp2k.inp)+ warnings(中文)。"""
    warnings: list = []
    struct = parse_structure(spec.structure)
    elements = struct['elements']
    cell, cart = struct['cell'], struct['cart']
    project = (struct['comment'] or spec.extras.get('system') or 'vcstudio').split()[0]

    # CUTOFF:GTH 密度网格截断(Ry),绝不从 ENCUT 换算。
    cutoff_ry = spec.extras.get('cutoff_ry')
    if cutoff_ry is None:
        cutoff_ry = _DEF_CUTOFF_RY
        warnings.append(
            f'CP2K CUTOFF 需独立收敛测试,不从 ENCUT 换算;'
            f'extras 未给 cutoff_ry,暂用占位 {cutoff_ry:g} Ry,请自行收敛后覆盖。')
    rel_cutoff_ry = spec.extras.get('rel_cutoff_ry', _DEF_REL_CUTOFF_RY)

    eps_scf = spec.extras.get('eps_scf', 1e-6)
    max_scf = int(spec.extras.get('max_scf', 50))
    basis_file = spec.extras.get('basis_set_file', _DEF_BASIS_FILE)
    pot_file = spec.extras.get('potential_file', _DEF_POT_FILE)
    kind_over = spec.extras.get('kind') or {}

    run_type = _RUN_TYPE.get(spec.task, 'ENERGY_FORCE')

    xc_key = str(spec.functional).upper()
    if xc_key not in _XC_KNOWN:
        warnings.append(
            f'CP2K &XC_FUNCTIONAL 关键字 {spec.functional!r} 不在直支列表,'
            f'已按原名写入,可能需 &LIBXC 或显式泛函节,请核对。')

    L = []                                              # 行缓冲

    def emit(indent, text):
        L.append('  ' * indent + text)

    # &GLOBAL
    emit(0, '&GLOBAL')
    emit(1, f'PROJECT {project}')
    emit(1, f'RUN_TYPE {run_type}')
    emit(1, 'PRINT_LEVEL MEDIUM')
    emit(0, '&END GLOBAL')

    # &FORCE_EVAL
    emit(0, '&FORCE_EVAL')
    emit(1, 'METHOD Quickstep')
    emit(1, '&DFT')
    emit(2, f'BASIS_SET_FILE_NAME {basis_file}')
    emit(2, f'POTENTIAL_FILE_NAME {pot_file}')
    if spec.charge:
        emit(2, f'CHARGE {int(spec.charge)}')
    if spec.spin or int(spec.multiplicity) > 1:
        emit(2, f'MULTIPLICITY {int(spec.multiplicity)}')
        emit(2, 'UKS .TRUE.')
    emit(2, '&QS')
    emit(3, 'EPS_DEFAULT 1.0E-10')
    emit(2, '&END QS')
    emit(2, '&MGRID')
    emit(3, f'CUTOFF {float(cutoff_ry):g}')
    emit(3, f'REL_CUTOFF {float(rel_cutoff_ry):g}')
    emit(2, '&END MGRID')
    emit(2, '&XC')
    emit(3, '&XC_FUNCTIONAL')
    emit(4, xc_key)
    emit(3, '&END XC_FUNCTIONAL')
    if spec.dispersion:
        d3type = _D3_TYPE.get(str(spec.dispersion).upper().replace(' ', ''),
                              _D3_TYPE.get(str(spec.dispersion), 'DFTD3'))
        emit(3, '&VDW_POTENTIAL')
        emit(4, 'POTENTIAL_TYPE PAIR_POTENTIAL')
        emit(4, '&PAIR_POTENTIAL')
        emit(5, f'TYPE {d3type}')
        emit(5, 'PARAMETER_FILE_NAME dftd3.dat')
        emit(5, f'REFERENCE_FUNCTIONAL {xc_key}')
        emit(4, '&END PAIR_POTENTIAL')
        emit(3, '&END VDW_POTENTIAL')
    emit(2, '&END XC')
    emit(2, '&SCF')
    emit(3, f'EPS_SCF {float(eps_scf):g}')
    emit(3, f'MAX_SCF {max_scf}')
    emit(3, 'SCF_GUESS ATOMIC')
    emit(2, '&END SCF')
    if not spec.periodic:
        # 孤立分子:Poisson 非周期(Martyna-Tuckerman),需盒 ≥ 2×分子尺寸。
        emit(2, '&POISSON')
        emit(3, 'PERIODIC NONE')
        emit(3, 'POISSON_SOLVER MT')
        emit(2, '&END POISSON')
    emit(1, '&END DFT')

    # &SUBSYS
    periodic_kw = 'NONE' if not spec.periodic else 'XYZ'
    emit(1, '&SUBSYS')
    emit(2, '&CELL')
    for tag, vec in zip(('A', 'B', 'C'), cell):
        emit(3, f'{tag} {vec[0]:.8f} {vec[1]:.8f} {vec[2]:.8f}')
    emit(3, f'PERIODIC {periodic_kw}')
    emit(2, '&END CELL')
    emit(2, '&COORD')
    for el, xyz in zip(elements, cart):
        emit(3, f'{el} {xyz[0]:.8f} {xyz[1]:.8f} {xyz[2]:.8f}')
    emit(2, '&END COORD')
    for el in _unique(elements):
        over = kind_over.get(el, {})
        emit(2, f'&KIND {el}')
        emit(3, f'BASIS_SET {over.get("BASIS_SET", _DEF_BASIS)}')
        emit(3, f'POTENTIAL {over.get("POTENTIAL", _DEF_POTENTIAL)}')
        emit(2, '&END KIND')
    emit(1, '&END SUBSYS')
    emit(0, '&END FORCE_EVAL')

    # &MOTION(GEO_OPT 力判据)/ &VIBRATIONAL_ANALYSIS(freq)
    if spec.task == 'relax':
        force = spec.convergence.get('force_ev_a', 0.02) if spec.convergence else 0.02
        emit(0, '&MOTION')
        emit(1, '&GEO_OPT')
        emit(2, 'OPTIMIZER BFGS')
        emit(2, f'MAX_FORCE {abs(float(force)) * _EV_A_TO_HB:.6E}')   # eV/Å → Hartree/Bohr
        emit(1, '&END GEO_OPT')
        emit(0, '&END MOTION')
    elif spec.task == 'freq':
        emit(0, '&VIBRATIONAL_ANALYSIS')
        emit(1, 'DX 0.01')
        emit(0, '&END VIBRATIONAL_ANALYSIS')

    return '\n'.join(L) + '\n', warnings


def _unique(seq):
    """保序去重。"""
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


class Cp2kBackend(EngineBackend):
    """CP2K 文件级适配。"""

    name = 'cp2k'

    def generate_inputs(self, spec: CalcSpec, out_dir: str) -> dict:
        os.makedirs(out_dir, exist_ok=True)
        text, warnings = build_input(spec)
        path = os.path.join(out_dir, 'cp2k.inp')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        warnings.append(
            'CP2K 软件本体与 BASIS/POTENTIAL 数据文件用户自备;本适配仅出 cp2k.inp。')
        return {'files': [path], 'warnings': warnings}

    def parse_energy(self, out_dir: str) -> dict:
        """cp2k.out → 'ENERGY| Total FORCE_EVAL' 末次值(Hartree→eV)+ SCF/几何收敛判定。"""
        path = _out_path(out_dir)
        if path is None:
            return {'energy_ev': None, 'converged': False,
                    'error': f'缺 CP2K 输出(目录 {out_dir} 无 cp2k.out / *.out)'}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()

        e_hartree = _cp2k_energy_hartree(text)
        energy = e_hartree * HARTREE_TO_EV if e_hartree is not None else None

        scf_ok = 'SCF run converged' in text
        scf_bad = 'SCF run NOT converged' in text
        geo_done = 'GEOMETRY OPTIMIZATION COMPLETED' in text
        converged = bool((geo_done or scf_ok) and not scf_bad)

        error = None if energy is not None else \
            'CP2K 输出未见 "ENERGY| Total FORCE_EVAL" 能量行(运行可能未完成/发散)'
        return {'energy_ev': energy, 'converged': converged, 'error': error}

    def check_inputs(self, out_dir: str) -> list:
        """cp2k.inp 必需 section + CUTOFF 存在性检查。"""
        path = os.path.join(out_dir, 'cp2k.inp')
        if not os.path.isfile(path):
            return ['缺 cp2k.inp(CP2K 输入不存在)。']
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        issues: list = []
        for sect in ('&GLOBAL', '&FORCE_EVAL', '&DFT', '&SUBSYS', '&CELL',
                     '&COORD', '&KIND'):
            if sect not in text:
                issues.append(f'cp2k.inp 缺 {sect} section。')
        if 'CUTOFF' not in text:
            issues.append('cp2k.inp 缺 &MGRID CUTOFF(GTH 密度网格截断,须独立收敛)。')
        return issues
