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
import os
import re

from vcstudio.engines.calcspec import CalcSpec, EngineBackend, parse_structure

# 泛函 → CASTEP xc_functional(未登记走大写原名 + warning)。
_XC_MAP = {
    'PBE': 'PBE', 'RPBE': 'RPBE', 'PW91': 'PW91', 'BLYP': 'BLYP',
    'LDA': 'LDA', 'PBESOL': 'PBESOL', 'PBE0': 'PBE0', 'WC': 'WC',
}
# 任务 → CASTEP task。
_TASK_MAP = {'relax': 'GeometryOptimization', 'static': 'SinglePoint', 'freq': 'Phonon'}

# .castep 末次 'Final energy' / 'Final free energy'(**已是 eV**,不作 Hartree 换算)。
_FINAL_E_RE = re.compile(
    r'Final (?:free )?energy[^=]*=\s*([-+]?\d+\.\d+(?:[Ee][-+]?\d+)?)')


def _seedname(spec: CalcSpec, comment: str) -> str:
    """种子名:extras.seedname > 结构注释首 token > 'case'(仅字母数字/下划线)。"""
    raw = spec.extras.get('seedname') or (comment.split()[0] if comment.split() else '') \
        or 'case'
    return re.sub(r'[^A-Za-z0-9_.-]', '_', raw) or 'case'


def build_cell(spec: CalcSpec) -> tuple[str, list]:
    """CalcSpec → CASTEP .cell 文本 + warnings(晶格/分数坐标/k 点网格/FIX 约束)。"""
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
    task = _TASK_MAP.get(spec.task, 'SinglePoint')
    xc_key = str(spec.functional).upper()
    xc = _XC_MAP.get(xc_key)
    if xc is None:
        xc = xc_key
        warnings.append(
            f'未登记 CASTEP xc_functional 关键字 {spec.functional!r},已按大写原名写入,请核对。')

    energy = spec.convergence.get('energy_ev', 1e-5) if spec.convergence else 1e-5
    force = spec.convergence.get('force_ev_a', 0.05) if spec.convergence else 0.05

    L: list = []
    L.append(f'task                 : {task}')
    L.append(f'xc_functional        : {xc}')
    if spec.cutoff_ev is not None:
        L.append(f'cut_off_energy       : {float(spec.cutoff_ev):g} eV')
        warnings.append(
            'CASTEP 赝势(OTF/usp)与 VASP PAW 不同,能量不可直接比较,仅同引擎内可比;'
            'cut_off_energy 虽同为 eV,仍需各自收敛测试。')
    else:
        warnings.append('未指定 cutoff_ev,.param 未写 cut_off_energy,CASTEP 将用默认(不建议)。')
    L.append(f'elec_energy_tol      : {float(energy):g} eV')
    if spec.task == 'relax':
        L.append(f'geom_energy_tol      : {float(energy):g} eV')
        L.append(f'geom_force_tol       : {float(force):g} eV/ang')
        L.append(f'geom_max_iter        : {int(spec.extras.get("geom_max_iter", 100))}')
    if spec.dispersion:
        L.append('sedc_apply           : true')
        L.append(f'sedc_scheme          : {_sedc_scheme(spec.dispersion, warnings)}')
    if spec.spin or int(spec.multiplicity) > 1:
        L.append('spin_polarized       : true')
        L.append(f'spin                 : {int(spec.multiplicity) - 1}')
    if spec.charge:
        L.append(f'charge               : {int(spec.charge)}')
    # extras 私有 .param 覆盖/追加
    for k, v in (spec.extras.get('param') or {}).items():
        L.append(f'{k:<20s} : {v}')
    return '\n'.join(L) + '\n', warnings


def _sedc_scheme(dispersion, warnings: list) -> str:
    """色散 → CASTEP sedc_scheme(半经验色散修正方案)。"""
    key = str(dispersion).upper().replace(' ', '')
    mapping = {'D3': 'G06', 'D3(BJ)': 'G06', 'D3BJ': 'G06', 'TS': 'TS',
               'D2': 'G06', 'MBD': 'MBD*'}
    scheme = mapping.get(key)
    if scheme is None:
        scheme = 'G06'
        warnings.append(
            f'未登记 CASTEP sedc_scheme 对应 {dispersion!r},暂用 G06,请核对方案。')
    return scheme


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
        return {'files': files, 'warnings': warnings}

    def parse_energy(self, out_dir: str) -> dict:
        """.castep → 末次 'Final energy'(**已是 eV**)+ 几何优化完成标志。"""
        cands = sorted(glob.glob(os.path.join(out_dir, '*.castep')))
        if not cands:
            return {'energy_ev': None, 'converged': False,
                    'error': f'缺 .castep 输出(目录 {out_dir} 无 *.castep)'}
        with open(cands[0], 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()

        hits = _FINAL_E_RE.findall(text)
        energy = float(hits[-1]) if hits else None       # CASTEP 报 eV,不换算

        geo_done = 'Geometry optimization completed successfully' in text
        finished = 'Total time' in text or 'Peak Memory Use' in text
        converged = bool(geo_done or (finished and energy is not None))

        error = None if energy is not None else \
            '.castep 未见 "Final energy"(计算可能未完成/发散)'
        return {'energy_ev': energy, 'converged': converged, 'error': error}

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
        return issues
