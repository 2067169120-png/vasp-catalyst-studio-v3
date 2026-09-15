"""CASTEP Backend(Materials Studio 文件级适配)——.cell/.param 生成 + .castep 解析。

**软件本体用户自备**:CASTEP 是商业码,通常经 Materials Studio 分发。使用本适配层
须用户**自行持有 Materials Studio / CASTEP 许可**;vcstudio **不捆绑** CASTEP、
Materials Studio 或其任何组件、赝势库,只做文件级输入生成与输出解析。

与 VASP **方法不等价**:CASTEP 赝势(OTFG/USP)与 VASP PAW 不同,绝对能量不可直接
比较,仅同引擎内可比(见 calcspec.NONEQUIV_MAP 的 vasp↔castep 条目)。cut_off_energy
虽同为 eV,仍需各自收敛测试。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import glob
import math
import os
import re

from vcstudio.engines.calcspec import CalcSpec, EngineBackend, parse_structure

# 泛函 → CASTEP xc_functional。未登记值不按拼写猜测，避免作业排队后
# 才因未知关键字失败。
_XC_MAP = {
    'PBE': 'PBE', 'RPBE': 'RPBE', 'PW91': 'PW91', 'BLYP': 'BLYP',
    'LDA': 'LDA', 'PBESOL': 'PBESOL', 'PBE0': 'PBE0', 'WC': 'WC',
}
# 任务 → CASTEP task。
_TASK_MAP = {'relax': 'GeometryOptimization', 'static': 'SinglePoint', 'freq': 'Phonon'}

_NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?'
# CASTEP may print three different energy conventions for metallic smearing.
# Prefer the documented 0 K estimate, then Final energy, and use free energy
# only as an explicitly labelled fallback; never let whichever line is last win.
_ZERO_K_E_RE = re.compile(rf'(?:NB\s+est\.?\s*)?0\s*K\s+energy[^=]*=\s*({_NUMBER})', re.I)
_FINAL_E_RE = re.compile(rf'Final\s+energy\s*,?\s*E?[^=]*=\s*({_NUMBER})', re.I)
_FREE_E_RE = re.compile(rf'Final\s+free\s+energy[^=]*=\s*({_NUMBER})', re.I)
_TASK_RE = re.compile(r'^\s*task\s*[:=]\s*([A-Za-z_]+)', re.MULTILINE | re.IGNORECASE)
_CASTEP_TASK = {
    'geometryoptimization': 'relax', 'geometry_optimization': 'relax',
    'singlepoint': 'static', 'single_point': 'static', 'phonon': 'freq',
}
_FAILURE_MARKERS = (
    'ERROR:', 'Error in routine', 'aborting', 'SCF loop failed to converge',
    'Geometry optimization failed', 'Calculation not completed',
)
_BOHR_TO_ANGSTROM = 0.529177210903


def _seedname(spec: CalcSpec, comment: str) -> str:
    """种子名:extras.seedname > 结构注释首 token > 'case'(仅字母数字/下划线)。"""
    raw = spec.extras.get('seedname') or (comment.split()[0] if comment.split() else '') \
        or 'case'
    return re.sub(r'[^A-Za-z0-9_.-]', '_', raw) or 'case'


def build_cell(spec: CalcSpec) -> tuple[str, list]:
    """CalcSpec → CASTEP .cell 文本 + warnings(晶格/分数坐标/k 点网格/FIX 约束)。"""
    if not spec.periodic:
        raise ValueError(
            'CASTEP 文件级适配按周期平面波体系生成；periodic=False 会把孤立分子静默变成'
            '周期超胞，已拒绝。分子请显式建立并收敛真空超胞后设 periodic=True，或使用 Gaussian/CP2K。')
    warnings: list = []
    struct = parse_structure(spec.structure)
    elements, frac, cell, sd = (struct['elements'], struct['frac'],
                                struct['cell'], struct['sd'])

    L: list = []
    L.append('%BLOCK LATTICE_CART')
    L.append('ang')
    for vec in cell:
        L.append(f'  {vec[0]:.8f} {vec[1]:.8f} {vec[2]:.8f}')
    L.append('%ENDBLOCK LATTICE_CART')
    L.append('')
    L.append('%BLOCK POSITIONS_FRAC')
    for el, f in zip(elements, frac):
        L.append(f'  {el:<2s} {f[0]:.8f} {f[1]:.8f} {f[2]:.8f}')
    L.append('%ENDBLOCK POSITIONS_FRAC')
    L.append('')

    kpts = list(spec.kpoints) if spec.kpoints is not None else [1, 1, 1]
    if len(kpts) != 3 or any(isinstance(value, bool) or int(value) <= 0
                             or float(value) != int(value) for value in kpts):
        raise ValueError(f'CASTEP kpoints 必须为 3 个正整数，收到 {spec.kpoints!r}。')
    L.append(f'KPOINTS_MP_GRID {int(kpts[0])} {int(kpts[1])} {int(kpts[2])}')

    # FIX 约束:Selective dynamics 'F' 方向 → IONIC_CONSTRAINTS(逐固定方向一条)。
    constraints = _ionic_constraints(elements, sd)
    if constraints:
        L.append('')
        L.append('%BLOCK IONIC_CONSTRAINTS')
        L.extend(constraints)
        L.append('%ENDBLOCK IONIC_CONSTRAINTS')
        warnings.append(
            'Selective dynamics 冻结原子已译为 CASTEP IONIC_CONSTRAINTS(按笛卡尔轴'
            '逐方向固定);与 VASP 逐原子 T/F 语义近似,弛豫前请核对约束块。')
    return '\n'.join(L) + '\n', warnings


def _ionic_constraints(elements: list, sd) -> list:
    """SD 标志 → IONIC_CONSTRAINTS 行列表(每固定方向一条,含引擎内元素序号)。"""
    if not sd:
        return []
    axes = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    per_el: dict = {}
    lines: list = []
    cid = 0
    for el, flag in zip(elements, sd):
        per_el[el] = per_el.get(el, 0) + 1
        idx = per_el[el]                                # 该元素内 1-based 序号
        toks = flag.split()
        for a in range(3):
            if a < len(toks) and toks[a].upper() == 'F':
                cid += 1
                vx, vy, vz = axes[a]
                lines.append(f'  {cid} {el} {idx}  {vx} {vy} {vz}')
    return lines


def build_param(spec: CalcSpec) -> tuple[str, list]:
    """CalcSpec → CASTEP .param 文本 + warnings(task/xc/cutoff/收敛/自旋)。"""
    warnings: list = []
    if spec.task not in _TASK_MAP:
        raise ValueError(
            f'CASTEP 文件级适配不支持任务 {spec.task!r}；可选 relax/static/freq。')
    task = _TASK_MAP[spec.task]
    xc_key = str(spec.functional).upper()
    xc = _XC_MAP.get(xc_key)
    if xc is None:
        raise ValueError(
            f'未登记 CASTEP xc_functional {spec.functional!r}；可选 '
            f'{" / ".join(sorted(_XC_MAP))}，拒绝按大写拼写猜测。')

    energy = _positive_finite(
        'elec_energy_tol(eV)',
        spec.convergence.get('energy_ev', 1e-5) if spec.convergence else 1e-5)
    force = _positive_finite(
        'geom_force_tol(eV/ang)',
        spec.convergence.get('force_ev_a', 0.05) if spec.convergence else 0.05)

    L: list = []
    L.append(f'task                 : {task}')
    L.append(f'xc_functional        : {xc}')
    if spec.cutoff_ev is not None:
        cutoff = _positive_finite('cut_off_energy(eV)', spec.cutoff_ev)
        L.append(f'cut_off_energy       : {cutoff:g} eV')
        warnings.append(
            'CASTEP 赝势(OTF/usp)与 VASP PAW 不同,能量不可直接比较,仅同引擎内可比;'
            'cut_off_energy 虽同为 eV,仍需各自收敛测试。')
    else:
        warnings.append('未指定 cutoff_ev,.param 未写 cut_off_energy,CASTEP 将用默认(不建议)。')
    L.append(f'elec_energy_tol      : {float(energy):g} eV')
    if spec.task == 'relax':
        L.append(f'geom_energy_tol      : {energy:g} eV')
        L.append(f'geom_force_tol       : {force:g} eV/ang')
        L.append(f'geom_max_iter        : {_positive_int("geom_max_iter", spec.extras.get("geom_max_iter", 100))}')
    if spec.dispersion:
        dispersion_key = str(spec.dispersion).upper().replace(' ', '')
        if dispersion_key in {'D3', 'D3(BJ)', 'D3BJ'} and xc_key not in {'PBE', 'PBE0'}:
            raise ValueError(
                f'CASTEP 默认 D3/D3-BJ 参数未为 {xc_key} 登记；当前适配未暴露'
                '自定义 D3 参数，不生成表面正常但不可复现的输入。'
                '请改用 PBE/PBE0，或在 CASTEP 中人工建立并验证自定义色散参数。')
        L.append('sedc_apply           : true')
        L.append(f'sedc_scheme          : {_sedc_scheme(spec.dispersion, warnings)}')
    if spec.spin or int(spec.multiplicity) > 1:
        L.append('spin_polarized       : true')
        L.append(f'spin                 : {int(spec.multiplicity) - 1}')
    if spec.charge:
        L.append(f'charge               : {int(spec.charge)}')
    # extras 私有 .param 覆盖/追加
    protected = {
        'task', 'xc_functional', 'cut_off_energy', 'elec_energy_tol',
        'geom_energy_tol', 'geom_force_tol', 'geom_max_iter', 'sedc_apply',
        'sedc_scheme', 'spin_polarized', 'spin', 'charge',
    }
    for k, v in (spec.extras.get('param') or {}).items():
        key = str(k).strip()
        if key.lower() in protected:
            raise ValueError(
                f'extras.param[{key!r}] 会覆盖 CalcSpec 已确定的 CASTEP 科学参数；'
                '请改对应 CalcSpec 字段，拒绝写入重复键。')
        L.append(f'{key:<20s} : {v}')
    return '\n'.join(L) + '\n', warnings


def _sedc_scheme(dispersion, warnings: list) -> str:
    """色散 → CASTEP sedc_scheme(半经验色散修正方案)。"""
    key = str(dispersion).upper().replace(' ', '')
    mapping = {'D3': 'D3', 'D3(BJ)': 'D3-BJ', 'D3BJ': 'D3-BJ',
               'TS': 'TS', 'D2': 'G06', 'G06': 'G06', 'MBD': 'MBD*'}
    scheme = mapping.get(key)
    if scheme is None:
        raise ValueError(
            f'未登记 CASTEP sedc_scheme 对应 {dispersion!r}；'
            '可选 D2/G06、D3、D3(BJ)、TS、MBD，拒绝静默改成 G06。')
    if scheme in {'D3', 'D3-BJ'}:
        warnings.append(
            f'CASTEP {scheme} 需运行版本已编译 Grimme D3 支持；'
            '提交前请在集群安装上核对。')
    return scheme


def _positive_finite(name: str, value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'CASTEP {name} 必须是正有限数，收到 {value!r}。') from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'CASTEP {name} 必须是正有限数，收到 {value!r}。')
    return number


def _positive_int(name: str, value) -> int:
    try:
        number = float(value)
        integer = int(number)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'CASTEP {name} 必须是正整数，收到 {value!r}。') from None
    if isinstance(value, bool) or not math.isfinite(number) or number != integer or integer <= 0:
        raise ValueError(f'CASTEP {name} 必须是正整数，收到 {value!r}。')
    return integer


def _task_from_param_text(text: str) -> str | None:
    match = _TASK_RE.search(text or '')
    if not match:
        return None
    raw = match.group(1).lower()
    return _CASTEP_TASK.get(raw, raw if raw in _TASK_MAP else None)


def _energy_record(text: str) -> tuple[float | None, str | None]:
    for source, pattern in (
            ('NB est. 0K energy', _ZERO_K_E_RE),
            ('Final energy', _FINAL_E_RE),
            ('Final free energy', _FREE_E_RE)):
        matches = pattern.findall(text or '')
        if not matches:
            continue
        try:
            value = float(matches[-1].replace('D', 'E').replace('d', 'e'))
        except ValueError:
            continue
        if math.isfinite(value):
            return value, source
    return None, None


def _frequencies(text: str) -> list[float]:
    values: list[float] = []
    # Verbose .castep forms such as ``Frequency = 123.4 cm-1``.
    explicit = re.compile(rf'frequency[^=:\n]*[=:]\s*({_NUMBER})\s*cm(?:\^?-?1|⁻¹)', re.I)
    for token in explicit.findall(text or ''):
        value = float(token.replace('D', 'E').replace('d', 'e'))
        if math.isfinite(value):
            values.append(value)
    # .phonon and q-point tables use ``mode-index frequency ...`` rows after a
    # q-pt header.  Restricting to those sections avoids mistaking energies or
    # iteration counters for frequencies.
    in_qpoint = False
    row_re = re.compile(rf'^\s*\d+\s+({_NUMBER})(?:\s+|$)')
    for line in (text or '').splitlines():
        if re.search(r'\bq-pt\s*=', line, re.I):
            in_qpoint = True
            continue
        if in_qpoint and (not line.strip() or line.lstrip().startswith('q-pt=')):
            continue
        if in_qpoint:
            match = row_re.match(line)
            if match:
                value = float(match.group(1).replace('D', 'E').replace('d', 'e'))
                if math.isfinite(value):
                    values.append(value)
            elif re.search(r'END\s+q|BEGIN\s+q', line, re.I):
                in_qpoint = False
    return values


def parse_output_text(text: str, *, task: str | None = None) -> dict:
    """Parse CASTEP output using energy convention and task-specific gates."""
    task_key = str(task or '').strip().lower()
    task_key = _CASTEP_TASK.get(task_key, task_key)
    if task_key not in _TASK_MAP:
        task_key = ('relax' if 'Geometry optimization' in text else
                    'freq' if ('q-pt=' in text or 'phonon' in text.lower()) else 'static')
    energy, source = _energy_record(text)
    frequencies = _frequencies(text)
    failures = [marker for marker in _FAILURE_MARKERS if marker.lower() in text.lower()]
    failed = bool(failures)
    normal = ('Calculation completed successfully' in text
              or 'Total time' in text or 'Peak Memory Use' in text)
    geo_done = 'Geometry optimization completed successfully' in text
    if task_key == 'relax':
        task_done = geo_done
    elif task_key == 'freq':
        task_done = bool(frequencies)
    else:
        task_done = energy is not None
    converged = bool(normal and task_done and energy is not None and not failed)
    error = None
    if failures:
        error = 'CASTEP 输出含失败标志：' + '、'.join(failures)
    elif energy is None:
        error = 'CASTEP 输出未见 Final/0 K 能量'
    elif not normal:
        error = 'CASTEP 输出缺正常结束页脚，拒绝把截断 .castep 判为完成'
    elif not task_done:
        detail = 'Geometry optimization completed successfully' if task_key == 'relax' \
            else '声子频率结果' if task_key == 'freq' else '单点完成证据'
        error = f'CASTEP {task_key} 输出缺 {detail}'
    return {
        'energy_ev': energy, 'energy_source': source,
        'converged': converged, 'normal_termination': normal,
        'task_converged': bool(task_done), 'failed': failed,
        'failure_markers': failures, 'task': task_key,
        'frequencies_cm1': frequencies,
        'n_imaginary': sum(value < 0 for value in frequencies) if frequencies else None,
        'error': error,
    }


def _castep_path(out_dir: str) -> tuple[str | None, str | None]:
    candidates = sorted(glob.glob(os.path.join(out_dir, '*.castep')))
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, ('发现多个 .castep，无法确定本轮主输出：'
                      + '、'.join(os.path.basename(path) for path in candidates))
    return None, None


def _parse_geom(out_dir: str) -> tuple[dict | None, str | None]:
    """Extract the last complete CASTEP ``.geom`` frame (Bohr -> Å)."""
    candidates = sorted(glob.glob(os.path.join(out_dir, '*.geom')))
    if len(candidates) != 1:
        return None, None
    path = candidates[0]
    try:
        lines = open(path, 'r', encoding='utf-8', errors='replace').read().splitlines()
    except OSError:
        return None, None
    frames = []
    cell, atoms = [], []

    def finish():
        if atoms:
            frames.append({
                'atoms': list(atoms),
                'cell_angstrom': list(cell[-3:]) if len(cell) >= 3 else None,
                'coordinate_system': 'cartesian_angstrom',
            })

    number = re.compile(_NUMBER)
    for line in lines:
        if '<-- E' in line:
            finish()
            cell, atoms = [], []
        elif '<-- h' in line:
            tokens = number.findall(line.split('<--', 1)[0])
            if len(tokens) >= 3:
                cell.append([float(value.replace('D', 'E').replace('d', 'e'))
                             * _BOHR_TO_ANGSTROM for value in tokens[-3:]])
        elif '<-- R' in line:
            parts = line.split('<--', 1)[0].split()
            if len(parts) >= 5:
                try:
                    xyz = [float(value.replace('D', 'E').replace('d', 'e'))
                           * _BOHR_TO_ANGSTROM for value in parts[-3:]]
                except ValueError:
                    continue
                atoms.append({'element': parts[0], 'xyz_angstrom': xyz})
    finish()
    return (frames[-1], os.path.basename(path)) if frames else (None, None)


class CastepBackend(EngineBackend):
    """CASTEP / Materials Studio 文件级适配。软件本体与许可用户自备。"""

    name = 'castep'

    def generate_inputs(self, spec: CalcSpec, out_dir: str) -> dict:
        os.makedirs(out_dir, exist_ok=True)
        struct = parse_structure(spec.structure)
        seed = _seedname(spec, struct['comment'])
        cell_text, cell_warn = build_cell(spec)
        param_text, param_warn = build_param(spec)

        files = []
        for suffix, text in (('.cell', cell_text), ('.param', param_text)):
            path = os.path.join(out_dir, seed + suffix)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            files.append(path)
        warnings = cell_warn + param_warn
        warnings.append(
            'CASTEP / Materials Studio 软件本体与赝势库、许可均需用户自备;vcstudio 不捆绑其任何组件。')
        return {
            'files': files, 'warnings': warnings,
            'output_files': list(self.run_contract.result_files(
                [os.path.basename(path) for path in files], task=spec.task)),
            'restart': {'supported': self.run_contract.restart_supported,
                        'note': self.run_contract.restart_note},
        }

    def parse_energy(self, out_dir: str) -> dict:
        """.castep → 末次 'Final energy'(**已是 eV**)+ 几何优化完成标志。"""
        path, select_error = _castep_path(out_dir)
        if path is None:
            return {'energy_ev': None, 'converged': False,
                    'error': select_error or f'缺 .castep 输出(目录 {out_dir} 无 *.castep)'}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        phonons = sorted(glob.glob(os.path.join(out_dir, '*.phonon')))
        aux_text = ''
        if len(phonons) == 1:
            try:
                aux_text = open(phonons[0], 'r', encoding='utf-8',
                                errors='replace').read()
            except OSError:
                aux_text = ''
        task = self.infer_task(out_dir)
        parsed = parse_output_text(text, task=task)
        # .phonon 是声子模的权威产物。若把 .castep 中的回显与 .phonon
        # 直接拼接再解析，会把同一频率数两次，进而误报虚频个数。
        if aux_text and task == 'freq':
            frequencies = _frequencies(aux_text)
            if frequencies:
                parsed['frequencies_cm1'] = frequencies
                parsed['n_imaginary'] = sum(value < 0 for value in frequencies)
                parsed['task_converged'] = True
                parsed['converged'] = bool(
                    parsed.get('normal_termination') and parsed.get('energy_ev') is not None
                    and not parsed.get('failed'))
                if parsed['converged']:
                    parsed['error'] = None
        parsed['output_file'] = os.path.basename(path)
        structure, source = _parse_geom(out_dir)
        if structure is not None:
            parsed['final_structure'] = structure
            parsed['structure_source'] = source
        return parsed

    def parse_output_text(self, text: str, *, task: str | None = None) -> dict:
        return parse_output_text(text, task=task)

    def infer_task(self, out_dir: str) -> str | None:
        candidates = sorted(glob.glob(os.path.join(out_dir, '*.param')))
        if len(candidates) != 1:
            return None
        try:
            with open(candidates[0], 'r', encoding='utf-8', errors='replace') as handle:
                return _task_from_param_text(handle.read())
        except OSError:
            return None

    def check_inputs(self, out_dir: str) -> list:
        """.cell + .param 存在性 + 关键块/键检查。"""
        cell_c = sorted(glob.glob(os.path.join(out_dir, '*.cell')))
        param_c = sorted(glob.glob(os.path.join(out_dir, '*.param')))
        issues: list = []
        if not cell_c:
            issues.append('缺 .cell(CASTEP 结构/k 点输入不存在)。')
        else:
            with open(cell_c[0], 'r', encoding='utf-8', errors='replace') as f:
                cell = f.read()
            if 'LATTICE_CART' not in cell:
                issues.append('.cell 缺 %BLOCK LATTICE_CART(晶格)。')
            if 'POSITIONS_FRAC' not in cell:
                issues.append('.cell 缺 %BLOCK POSITIONS_FRAC(分数坐标)。')
            if 'KPOINTS_MP_GRID' not in cell:
                issues.append('.cell 缺 KPOINTS_MP_GRID(k 点网格)。')
        if not param_c:
            issues.append('缺 .param(CASTEP 计算参数输入不存在)。')
        else:
            with open(param_c[0], 'r', encoding='utf-8', errors='replace') as f:
                param = f.read().lower()
            if 'task' not in param:
                issues.append('.param 缺 task(GeometryOptimization/SinglePoint…)。')
            if 'xc_functional' not in param:
                issues.append('.param 缺 xc_functional(泛函)。')
            if 'cut_off_energy' not in param:
                issues.append('.param 缺 cut_off_energy(平面波截断能)。')
        cell_stems = {os.path.splitext(os.path.basename(path))[0] for path in cell_c}
        param_stems = {os.path.splitext(os.path.basename(path))[0] for path in param_c}
        if cell_c and param_c and len(cell_stems & param_stems) != 1:
            issues.append(
                'CASTEP 要求同名 seed.cell + seed.param；当前没有唯一同 stem 配对，'
                '提交命令 castep.mpi {stem} 将读不到完整输入。')
        if len(cell_c) > 1 or len(param_c) > 1:
            issues.append('CASTEP 作业目录存在多组 .cell/.param，拒绝猜测要提交哪一个 seed。')
        return issues
