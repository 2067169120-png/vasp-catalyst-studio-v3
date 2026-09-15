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
import math
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
# CP2K ``&XC_FUNCTIONAL <shortcut>`` 的官方快捷值。不在此表的泛函不能
# 把名字直接塞进 section parameter，否则 CP2K 会在启动阶段报 unknown enum。
_XC_SHORTCUTS = {
    'PBE', 'BLYP', 'BP', 'PADE', 'LDA', 'TPSS', 'HCTH120', 'OLYP', 'BEEFVDW',
}
# 表面催化常用 RPBE 不是 CP2K shortcut；用明确的交换+相关子节组合。
# 这些 section 名与现代 CP2K/LibXC 输入参考一致，不把 RPBE 静默退化成 PBE。
_XC_COMPONENTS = {
    'RPBE': ('GGA_X_RPBE', 'GGA_C_PBE'),
}
_PBE_PARAMETRIZATIONS = {
    'PBESOL': 'PBESOL',
    'REVPBE': 'REVPBE',
}
# 色散 → &PAIR_POTENTIAL TYPE。
_D3_TYPE = {'D3': 'DFTD3', 'D3(BJ)': 'DFTD3(BJ)', 'D3BJ': 'DFTD3(BJ)'}

# 输出能量行:'ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]: -17.1461'。
# 取含该标记行的**末个浮点**(对不同 CP2K 版本的单位标注/尾随文本都稳健)。
_FLOAT_RE = re.compile(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?')
_RUN_TYPE_RE = re.compile(r'^\s*RUN_TYPE\s+([A-Za-z_]+)', re.MULTILINE | re.IGNORECASE)
_RUN_TYPE_TASK = {value: key for key, value in _RUN_TYPE.items()}
_RUN_TYPE_TASK.update({'ENERGY': 'static', 'ENERGY_FORCE': 'static'})
_FAILURE_MARKERS = (
    'SCF run NOT converged', 'ABORT', 'PROGRAM STOPPED IN',
    'SIGSEGV', 'Cholesky decomposition failed', 'CPASSERT failed',
)


def _cp2k_energy_hartree(text: str) -> float | None:
    """CP2K 输出 → 末次 'ENERGY| Total FORCE_EVAL' 行的能量(Hartree)。无 → None。"""
    val = None
    for line in text.splitlines():
        if 'ENERGY|' in line and 'Total FORCE_EVAL' in line:
            floats = _FLOAT_RE.findall(line)
            if floats:
                val = float(floats[-1].replace('D', 'E').replace('d', 'e'))
    return val


def _out_path(out_dir: str) -> tuple[str | None, str | None]:
    """定位 CP2K 输出；多个非标准 ``*.out`` 时拒绝猜旧轮次。"""
    fixed = os.path.join(out_dir, 'cp2k.out')
    if os.path.isfile(fixed):
        return fixed, None
    cands = sorted(glob.glob(os.path.join(out_dir, '*.out')))
    matching = []
    for path in cands:
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                head = handle.read(256000)
        except OSError:
            continue
        if ('CP2K|' in head or 'ENERGY|' in head or 'PROGRAM ENDED AT' in head):
            matching.append(path)
    if len(matching) == 1:
        return matching[0], None
    if len(matching) > 1:
        return None, ('发现多个 CP2K *.out，无法确定本轮主输出：'
                      + '、'.join(os.path.basename(path) for path in matching))
    return None, None


def _task_from_input_text(text: str) -> str | None:
    match = _RUN_TYPE_RE.search(text or '')
    return _RUN_TYPE_TASK.get(match.group(1).upper()) if match else None


def _cp2k_frequencies(text: str) -> list[float]:
    """Extract the ``VIB|Frequency (cm^-1)`` rows emitted by CP2K."""
    values: list[float] = []
    for line in (text or '').splitlines():
        if 'VIB|' not in line or not re.search(r'Frequency\s*\(cm\^-?1\)', line, re.I):
            continue
        tail = re.split(r'Frequency\s*\(cm\^-?1\)', line, maxsplit=1, flags=re.I)[-1]
        for token in _FLOAT_RE.findall(tail):
            try:
                value = float(token.replace('D', 'E').replace('d', 'e'))
            except ValueError:
                continue
            if math.isfinite(value):
                values.append(value)
    return values


def parse_output_text(text: str, *, task: str | None = None) -> dict:
    """Parse CP2K text with task-specific completion and failure evidence."""
    task_key = str(task or '').strip().lower()
    task_key = _RUN_TYPE_TASK.get(task_key.upper(), task_key)
    if task_key not in _RUN_TYPE:
        if 'GEOMETRY OPTIMIZATION' in text.upper():
            task_key = 'relax'
        elif 'VIBRATIONAL ANALYSIS' in text.upper() or 'VIB|FREQUENCY' in text.upper():
            task_key = 'freq'
        else:
            task_key = 'static'
    e_hartree = _cp2k_energy_hartree(text)
    energy = e_hartree * HARTREE_TO_EV if e_hartree is not None else None
    normal = 'PROGRAM ENDED AT' in text
    failures = [marker for marker in _FAILURE_MARKERS if marker.lower() in text.lower()]
    failed = bool(failures)
    scf_ok = 'SCF run converged' in text and 'SCF run NOT converged' not in text
    geo_done = 'GEOMETRY OPTIMIZATION COMPLETED' in text
    frequencies = _cp2k_frequencies(text)
    if task_key == 'relax':
        task_done = geo_done
    elif task_key == 'freq':
        task_done = bool(frequencies)
    else:
        # Some CP2K versions do not print the explicit SCF sentence at low
        # verbosity; a finite final ENERGY plus clean footer is sufficient.
        task_done = scf_ok or energy is not None
    converged = bool(normal and task_done and energy is not None and not failed)
    error = None
    if failures:
        error = 'CP2K 输出含失败标志：' + '、'.join(failures)
    elif energy is None:
        error = 'CP2K 输出未见 "ENERGY| Total FORCE_EVAL" 最终能量'
    elif not normal:
        error = 'CP2K 输出缺 "PROGRAM ENDED AT" 正常结束页脚，拒绝把截断输出判为完成'
    elif not task_done:
        detail = ('几何优化完成标志' if task_key == 'relax' else
                  '振动频率结果' if task_key == 'freq' else 'SCF 完成证据')
        error = f'CP2K {task_key} 输出缺{detail}'
    return {
        'energy_ev': energy, 'energy_source': 'ENERGY|Total FORCE_EVAL',
        'converged': converged, 'normal_termination': normal,
        'task_converged': bool(task_done), 'failed': failed,
        'failure_markers': failures, 'task': task_key,
        'frequencies_cm1': frequencies,
        'n_imaginary': sum(value < 0 for value in frequencies) if frequencies else None,
        'error': error,
    }


def _positive_finite(name: str, value) -> float:
    """将 CP2K 数值参数收紧为正有限数，防止写出 nan/inf/负截断。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'CP2K {name} 必须是正有限数，收到 {value!r}。') from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'CP2K {name} 必须是正有限数，收到 {value!r}。')
    return number


def _positive_int(name: str, value) -> int:
    """解析 CP2K 正整数参数，拒绝 bool/小数被 ``int`` 静默截断。"""
    try:
        number = float(value)
        integer = int(number)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'CP2K {name} 必须是正整数，收到 {value!r}。') from None
    if isinstance(value, bool) or not math.isfinite(number) or number != integer or integer <= 0:
        raise ValueError(f'CP2K {name} 必须是正整数，收到 {value!r}。')
    return integer


def _emit_xc_functional(emit, xc_key: str) -> None:
    """按 CP2K 真实 section 语义写泛函；未登记值失败即停。"""
    if xc_key in _XC_SHORTCUTS:
        emit(3, f'&XC_FUNCTIONAL {xc_key}')
        emit(3, '&END XC_FUNCTIONAL')
        return
    if xc_key in _PBE_PARAMETRIZATIONS:
        emit(3, '&XC_FUNCTIONAL')
        emit(4, '&PBE')
        emit(5, f'PARAMETRIZATION {_PBE_PARAMETRIZATIONS[xc_key]}')
        emit(4, '&END PBE')
        emit(3, '&END XC_FUNCTIONAL')
        return
    components = _XC_COMPONENTS.get(xc_key)
    if components:
        emit(3, '&XC_FUNCTIONAL')
        for section in components:
            emit(4, f'&{section}')
            emit(4, f'&END {section}')
        emit(3, '&END XC_FUNCTIONAL')
        return
    supported = sorted(_XC_SHORTCUTS | set(_PBE_PARAMETRIZATIONS) | set(_XC_COMPONENTS))
    raise ValueError(
        f'CP2K 泛函 {xc_key!r} 未登记为可验证的 XC 输入；可选 {" / ".join(supported)}。'
        '其它泛函需显式 LibXC/混合泛函配方，拒绝按名称猜写。')


def build_input(spec: CalcSpec) -> tuple[str, list]:
    """CalcSpec → CP2K 输入文本(cp2k.inp)+ warnings(中文)。"""
    warnings: list = []
    struct = parse_structure(spec.structure)
    elements = struct['elements']
    cell, cart = struct['cell'], struct['cart']
    project_raw = (struct['comment'] or spec.extras.get('system') or 'vcstudio').split()[0]
    project = re.sub(r'[^A-Za-z0-9_.-]', '_', project_raw) or 'vcstudio'

    # CUTOFF:GTH 密度网格截断(Ry),绝不从 ENCUT 换算。
    cutoff_ry = spec.extras.get('cutoff_ry')
    if cutoff_ry is None:
        cutoff_ry = _DEF_CUTOFF_RY
        warnings.append(
            f'CP2K CUTOFF 需独立收敛测试,不从 ENCUT 换算;'
            f'extras 未给 cutoff_ry,暂用占位 {cutoff_ry:g} Ry,请自行收敛后覆盖。')
    cutoff_ry = _positive_finite('CUTOFF(Ry)', cutoff_ry)
    rel_cutoff_ry = _positive_finite(
        'REL_CUTOFF(Ry)', spec.extras.get('rel_cutoff_ry', _DEF_REL_CUTOFF_RY))

    eps_scf = _positive_finite('EPS_SCF', spec.extras.get('eps_scf', 1e-6))
    max_scf = _positive_int('MAX_SCF', spec.extras.get('max_scf', 50))
    basis_file = spec.extras.get('basis_set_file', _DEF_BASIS_FILE)
    pot_file = spec.extras.get('potential_file', _DEF_POT_FILE)
    kind_over = spec.extras.get('kind') or {}

    if spec.task not in _RUN_TYPE:
        raise ValueError(
            f'CP2K 文件级适配不支持任务 {spec.task!r}；可选 relax/static/freq。')
    run_type = _RUN_TYPE[spec.task]

    xc_key = str(spec.functional).upper()

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
    _emit_xc_functional(emit, xc_key)
    if spec.dispersion:
        dispersion_key = str(spec.dispersion).upper().replace(' ', '')
        d3type = _D3_TYPE.get(dispersion_key)
        if d3type is None:
            raise ValueError(
                f'CP2K 色散 {spec.dispersion!r} 未登记；可选 D3 或 D3(BJ)，'
                '拒绝静默改成 DFTD3。')
        emit(3, '&VDW_POTENTIAL')
        emit(4, 'POTENTIAL_TYPE PAIR_POTENTIAL')
        emit(4, '&PAIR_POTENTIAL')
        emit(5, f'TYPE {d3type}')
        emit(5, 'PARAMETER_FILE_NAME dftd3.dat')
        emit(5, f'REFERENCE_FUNCTIONAL {xc_key}')
        emit(4, '&END PAIR_POTENTIAL')
        emit(3, '&END VDW_POTENTIAL')
    emit(2, '&END XC')
    if spec.periodic:
        if spec.kpoints is not None:
            if not isinstance(spec.kpoints, (tuple, list)) or len(spec.kpoints) != 3:
                raise ValueError(f'CP2K kpoints 必须为 3 个正整数，收到 {spec.kpoints!r}。')
            kx, ky, kz = (
                _positive_int(f'kpoints[{index}]', value)
                for index, value in enumerate(spec.kpoints)
            )
            emit(2, '&KPOINTS')
            emit(3, f'SCHEME MONKHORST-PACK {kx} {ky} {kz}')
            emit(2, '&END KPOINTS')
        else:
            warnings.append('周期 CP2K 未指定 kpoints，将使用 CP2K 默认 Γ 点；金属/表面体系请明确收敛。')
    elif spec.kpoints is not None:
        raise ValueError('CP2K 非周期分子不应设置 kpoints；请置 None。')
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
        force = _positive_finite(
            'MAX_FORCE(eV/Å)',
            spec.convergence.get('force_ev_a', 0.02) if spec.convergence else 0.02)
        emit(0, '&MOTION')
        fixed_groups = _fixed_atom_groups(struct.get('sd'))
        if fixed_groups:
            emit(1, '&CONSTRAINT')
            for components, indices in fixed_groups:
                emit(2, '&FIXED_ATOMS')
                emit(3, f'COMPONENTS_TO_FIX {components}')
                emit(3, 'LIST ' + ' '.join(str(index) for index in indices))
                emit(2, '&END FIXED_ATOMS')
            emit(1, '&END CONSTRAINT')
            warnings.append(
                'POSCAR Selective dynamics 冻结方向已写入 CP2K FIXED_ATOMS；提交前请核对原子序号。')
        emit(1, '&GEO_OPT')
        emit(2, 'OPTIMIZER BFGS')
        emit(2, f'MAX_FORCE {force * _EV_A_TO_HB:.6E}')   # eV/Å → Hartree/Bohr
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


def _fixed_atom_groups(sd) -> list[tuple[str, list[int]]]:
    """POSCAR selective flags -> CP2K FIXED_ATOMS groups (1-based)."""
    groups: dict[str, list[int]] = {}
    for index, flags in enumerate(sd or (), 1):
        tokens = str(flags).upper().split()
        components = ''.join(axis for axis, value in zip('XYZ', tokens) if value == 'F')
        if components:
            groups.setdefault(components, []).append(index)
    return list(groups.items())


def _parse_last_xyz(out_dir: str) -> tuple[dict | None, str | None]:
    """Read the final complete frame from a CP2K trajectory XYZ, if present."""
    candidates = sorted(
        set(glob.glob(os.path.join(out_dir, '*-pos-*.xyz'))
            + glob.glob(os.path.join(out_dir, '*.xyz'))))
    if not candidates:
        return None, None
    # Multiple trajectory files usually mean multiple restart generations.  Use
    # the newest local artifact but expose the source so reports remain auditable.
    path = max(candidates, key=lambda item: (os.path.getmtime(item), item))
    try:
        lines = open(path, 'r', encoding='utf-8', errors='replace').read().splitlines()
    except OSError:
        return None, None
    cursor, last = 0, None
    while cursor < len(lines):
        try:
            count = int(lines[cursor].strip())
        except (ValueError, IndexError):
            break
        end = cursor + count + 2
        if count <= 0 or end > len(lines):
            break
        atoms = []
        complete = True
        for line in lines[cursor + 2:end]:
            parts = line.split()
            if len(parts) < 4:
                complete = False
                break
            try:
                xyz = [float(value.replace('D', 'E').replace('d', 'e'))
                       for value in parts[1:4]]
            except ValueError:
                complete = False
                break
            atoms.append({'element': parts[0], 'xyz_angstrom': xyz})
        if complete:
            last = {'atoms': atoms, 'comment': lines[cursor + 1]}
        cursor = end
    return last, os.path.basename(path) if last else None


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
        return {
            'files': [path], 'warnings': warnings,
            'output_files': list(self.run_contract.result_files([os.path.basename(path)],
                                                                 task=spec.task)),
            'restart': {'supported': self.run_contract.restart_supported,
                        'note': self.run_contract.restart_note},
        }

    def parse_energy(self, out_dir: str) -> dict:
        """cp2k.out → 'ENERGY| Total FORCE_EVAL' 末次值(Hartree→eV)+ SCF/几何收敛判定。"""
        path, select_error = _out_path(out_dir)
        if path is None:
            return {'energy_ev': None, 'converged': False,
                    'error': select_error or
                             f'缺 CP2K 输出(目录 {out_dir} 无 cp2k.out / 唯一可识别 *.out)'}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()

        parsed = parse_output_text(text, task=self.infer_task(out_dir))
        parsed['output_file'] = os.path.basename(path)
        structure, source = _parse_last_xyz(out_dir)
        if structure is not None:
            parsed['final_structure'] = structure
            parsed['structure_source'] = source
        return parsed

    def parse_output_text(self, text: str, *, task: str | None = None) -> dict:
        return parse_output_text(text, task=task)

    def infer_task(self, out_dir: str) -> str | None:
        cands = sorted(glob.glob(os.path.join(out_dir, '*.inp')))
        if len(cands) != 1:
            return None
        try:
            with open(cands[0], 'r', encoding='utf-8', errors='replace') as handle:
                return _task_from_input_text(handle.read())
        except OSError:
            return None

    def check_inputs(self, out_dir: str) -> list:
        """cp2k.inp 必需 section + CUTOFF 存在性检查。"""
        fixed = os.path.join(out_dir, 'cp2k.inp')
        candidates = ([fixed] if os.path.isfile(fixed) else
                      sorted(glob.glob(os.path.join(out_dir, '*.inp'))))
        if not candidates:
            return ['缺 .inp(CP2K 输入不存在)。']
        if len(candidates) > 1:
            return ['存在多个 CP2K .inp，无法确定 {input}/{stem} 对应的主输入。']
        path = candidates[0]
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        issues: list = []
        for sect in ('&GLOBAL', '&FORCE_EVAL', '&DFT', '&SUBSYS', '&CELL',
                     '&COORD', '&KIND'):
            if sect not in text:
                issues.append(f'cp2k.inp 缺 {sect} section。')
        if 'CUTOFF' not in text:
            issues.append('cp2k.inp 缺 &MGRID CUTOFF(GTH 密度网格截断,须独立收敛)。')
        if _task_from_input_text(text) is None:
            issues.append('CP2K 输入缺合法 RUN_TYPE(GEO_OPT/ENERGY_FORCE/VIBRATIONAL_ANALYSIS)。')
        return issues
