"""VASP Backend——参照实现(多引擎适配层的基准口径)。

VASP 是本软件主引擎,本 Backend 把 CalcSpec 翻成 INCAR/KPOINTS/POSCAR,并复用既有
链上的序列化件(incar_builder.incar_dict_to_str / kpoints.kpoints_str / poscar 解析)。
解析侧复用 freeenergy.read_e0(OSZICAR 末次 E0)+ OUTCAR 收敛标志。

**POTCAR 不在此生成**:赝势库属用户自备(与其他引擎"软件本体用户自备"一致口径),
且发刊级 POTCAR 拼接/溯源已由 job_builder/potcar 承担;本适配层只出 INCAR/KPOINTS/
POSCAR 三件,并在 warnings/check_inputs 提示补 POTCAR。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
import re
from collections import OrderedDict

from vcstudio.engines.calcspec import CalcSpec, EngineBackend, parse_structure
from vcstudio.generate.incar_builder import incar_dict_to_str, parse_incar
from vcstudio.generate.kpoints import kpoints_str, recommend_kpoints
from vcstudio.project.freeenergy import read_e0

# 泛函 → VASP 关键字。GGA 类走 GGA=,meta-GGA 走 METAGGA=;未登记 → 不写 + warning。
_GGA_MAP = {
    'PBE': ('GGA', 'PE'), 'RPBE': ('GGA', 'RP'), 'PBESOL': ('GGA', 'PS'),
    'PW91': ('GGA', '91'), 'AM05': ('GGA', 'AM'),
}
_METAGGA_MAP = {
    'SCAN': 'SCAN', 'R2SCAN': 'R2SCAN', 'RSCAN': 'RSCAN', 'TPSS': 'TPSS',
}
# 色散关键字 → IVDW(VASP)。未登记 → 不写 + warning。
_IVDW_MAP = {
    'D2': 10, 'D3': 11, 'D3(BJ)': 12, 'D3BJ': 12, 'DDSC': 4,
    'TS': 2, 'TS/HI': 21, 'MBD': 202, 'DFT-D4': 13, 'D4': 13,
}


def _functional_tags(functional: str, warnings: list) -> OrderedDict:
    """泛函名 → INCAR 泛函标签(GGA/METAGGA)。未识别只 warn 不猜。"""
    tags: OrderedDict = OrderedDict()
    key = str(functional).upper()
    if key in _GGA_MAP:
        k, v = _GGA_MAP[key]
        tags[k] = v
    elif key in _METAGGA_MAP:
        tags['METAGGA'] = _METAGGA_MAP[key]
    else:
        warnings.append(
            f"未识别泛函 {functional!r} 对应的 VASP 关键字;未写 GGA/METAGGA,"
            f"请在 extras['incar'] 手动指定(绝不代猜泛函)。")
    return tags


def _dispersion_tag(dispersion, warnings: list) -> OrderedDict:
    """色散关键字 → IVDW。未识别只 warn 不猜。"""
    tags: OrderedDict = OrderedDict()
    if not dispersion:
        return tags
    key = str(dispersion).upper().replace(' ', '')
    ivdw = _IVDW_MAP.get(key) or _IVDW_MAP.get(str(dispersion).upper())
    if ivdw is None:
        warnings.append(
            f'未识别色散关键字 {dispersion!r} 的 VASP IVDW;未写 IVDW,请手动指定。')
    else:
        tags['IVDW'] = ivdw
    return tags


def _task_tags(spec: CalcSpec) -> OrderedDict:
    """任务类型 → 离子步/频率相关 INCAR 标签。"""
    tags: OrderedDict = OrderedDict()
    if spec.task not in ('relax', 'static', 'freq'):
        raise ValueError(
            f'VASP Backend 不支持任务 {spec.task!r}；可选 relax/static/freq。'
            'NEB/AIMD/能带等请使用对应专用生成器。')
    force = _positive_finite(
        'force_ev_a',
        spec.convergence.get('force_ev_a', 0.02) if spec.convergence else 0.02)
    if spec.task == 'relax':
        tags['IBRION'] = 2
        tags['NSW'] = _positive_int('NSW', spec.extras.get('nsw', 200))
        raw_isif = spec.extras.get('isif', 2)
        try:
            isif_number = float(raw_isif)
            isif = int(isif_number)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('VASP ISIF 必须是 0–8 的整数。') from None
        if (isinstance(raw_isif, bool) or not math.isfinite(isif_number)
                or isif_number != isif or isif not in range(9)):
            raise ValueError('VASP ISIF 必须是 0–8 的整数。')
        tags['ISIF'] = isif
        tags['EDIFFG'] = -force                        # 力判据(负=力阈值)
    elif spec.task == 'static':
        tags['IBRION'] = -1
        tags['NSW'] = 0
    elif spec.task == 'freq':
        tags['IBRION'] = 5                             # 有限差分 Hessian
        tags['NSW'] = 1
        nfree = _positive_int('NFREE', spec.extras.get('nfree', 2))
        if nfree not in (2, 4):
            raise ValueError('VASP 有限差分频率 NFREE 必须为 2 或 4。')
        tags['NFREE'] = nfree
        tags['POTIM'] = _positive_finite('POTIM', spec.extras.get('potim', 0.015))
    return tags


def _positive_finite(name: str, value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'VASP {name} 必须是正有限数，收到 {value!r}。') from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'VASP {name} 必须是正有限数，收到 {value!r}。')
    return number


def _positive_int(name: str, value) -> int:
    try:
        number = float(value)
        integer = int(number)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'VASP {name} 必须是正整数，收到 {value!r}。') from None
    if isinstance(value, bool) or not math.isfinite(number) or number != integer or integer <= 0:
        raise ValueError(f'VASP {name} 必须是正整数，收到 {value!r}。')
    return integer


def build_incar_dict(spec: CalcSpec) -> tuple[OrderedDict, list]:
    """CalcSpec → INCAR dict(OrderedDict)+ warnings。extras['incar'] 最后覆盖。"""
    warnings: list = []
    incar: OrderedDict = OrderedDict()
    incar['PREC'] = 'Accurate'
    if spec.cutoff_ev is None:
        raise ValueError(
            'VASP 计算（包括真空盒中的分子）必须明确指定 ENCUT；'
            '请根据 POTCAR ENMAX 并经收敛测试设置 cutoff_ev。')
    incar['ENCUT'] = _positive_finite('ENCUT(eV)', spec.cutoff_ev)
    incar['EDIFF'] = _positive_finite(
        'EDIFF', spec.convergence.get('energy_ev', 1e-5) if spec.convergence else 1e-5)
    incar.update(_functional_tags(spec.functional, warnings))
    incar.update(_dispersion_tag(spec.dispersion, warnings))
    incar['ISPIN'] = 2 if spec.spin else 1
    # 展宽:分子/半导体安全默认 Gaussian(ISMEAR=0);金属请经 extras['incar'] 覆盖。
    incar['ISMEAR'] = 0
    incar['SIGMA'] = 0.05
    incar.update(_task_tags(spec))
    explicit_incar = {str(k).upper(): v for k, v in (spec.extras.get('incar') or {}).items()}
    if spec.charge and 'NELECT' not in explicit_incar:
        raise ValueError(
            f'VASP 带电体系(charge={spec.charge})需基于实际 POTCAR 价电子数显式设 NELECT；'
            '本 Backend 在没有 POTCAR 时不能安全猜测。请在 extras["incar"] 给出 NELECT '
            '并核对背景电荷/有限尺寸校正。')
    if spec.charge:
        _positive_finite('NELECT', explicit_incar['NELECT'])
        warnings.append(
            f'带电体系 charge={spec.charge} 已使用用户显式 NELECT；'
            '请继续核对背景电荷和有限尺寸校正。')
    if spec.spin and 'MAGMOM' not in explicit_incar:
        warnings.append(
            'ISPIN=2 但未显式给 MAGMOM；会使用 VASP 默认初始磁矩，'
            '过渡金属/多自旋态请在 extras["incar"] 指定。')
    # extras 私有 INCAR 覆盖(用户显式值最高优先)
    for k, v in explicit_incar.items():
        incar[k] = v
    return incar, warnings


class VaspBackend(EngineBackend):
    """VASP 文件级适配(参照实现)。"""

    name = 'vasp'

    def generate_inputs(self, spec: CalcSpec, out_dir: str) -> dict:
        """生成 INCAR/KPOINTS/POSCAR 到 out_dir(POTCAR 用户自备,不在此生成)。"""
        os.makedirs(out_dir, exist_ok=True)
        warnings: list = []
        struct = parse_structure(spec.structure)       # 校验结构 + 拿 cell(VASP4 冒泡)
        system = struct['comment'] or spec.extras.get('system', '')

        incar, incar_warn = build_incar_dict(spec)
        warnings += incar_warn
        incar_text = incar_dict_to_str(incar, system)

        # KPOINTS:分子 Γ;周期给定则照用,否则按 cell 推荐。
        if not spec.periodic:
            if spec.kpoints is not None:
                raise ValueError('VASP 非周期分子工作流固定使用 Γ 点；kpoints 请置 None。')
            kpts = [1, 1, 1]
        elif spec.kpoints is not None:
            if not isinstance(spec.kpoints, (tuple, list)) or len(spec.kpoints) != 3:
                raise ValueError(f'VASP kpoints 必须是 3 个正整数，收到 {spec.kpoints!r}。')
            kpts = [_positive_int(f'kpoints[{index}]', value)
                    for index, value in enumerate(spec.kpoints)]
        else:
            kpts = recommend_kpoints(struct['cell'], 'bulk')
            warnings.append(
                f'未指定 kpoints,已按 cell 推荐 {kpts}(建议自行做 k 点收敛测试)。')

        files = []
        for name, text in (('INCAR', incar_text),
                           ('KPOINTS', kpoints_str(kpts)),
                           ('POSCAR', spec.structure if spec.structure.endswith('\n')
                            else spec.structure + '\n')):
            path = os.path.join(out_dir, name)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            files.append(path)

        warnings.append(
            'POTCAR 未生成:VASP 赝势库属用户自备;请用既有 POTCAR 拼接链补齐并核对 ENMAX≤ENCUT。')
        return {'files': files, 'warnings': warnings}

    def parse_energy(self, out_dir: str) -> dict:
        """解析 OSZICAR/OUTCAR → 统一能量口径(eV)+ 收敛判定。

        - energy_ev:优先 OSZICAR 末次 E0；缺失时依次退到 OUTCAR 的
          ``energy(sigma->0)`` 和 TOTEN，并用 energy_kind/source 明示口径。
        - converged:按 INCAR 推断任务语义，并且所有任务都要求 OUTCAR 干净页脚。
          relax 看离子收敛串，static 看 EDIFF，freq 看 THz，AIMD 看 MD 步数。

        旧实现只要 OUTCAR 曾出现离子收敛串就返回 True，即使文件随后被截断；续算目录
        中残留的上一轮标志会因此冒充本轮完成。这里与 cluster.submitter 的任务化判定
        对齐，宁可把证据不完整的结果留给人工，也不产出假 DONE。
        """
        e0 = read_e0(out_dir)
        outcar = _read_text(os.path.join(out_dir, 'OUTCAR'))
        oszicar = _read_text(os.path.join(out_dir, 'OSZICAR'))
        if outcar is None and oszicar is None:
            return {'energy_ev': None, 'converged': False,
                    'error': f'缺 OSZICAR/OUTCAR(目录 {out_dir} 无 VASP 输出)'}

        energy = e0
        energy_source = 'OSZICAR:E0' if e0 is not None else None
        energy_kind = 'E0' if e0 is not None else None
        if energy is None and outcar:
            energy = _last_sigma0(outcar)
            if energy is not None:
                energy_source, energy_kind = 'OUTCAR:energy(sigma->0)', 'sigma0'
        if energy is None and outcar:
            energy = _last_toten(outcar)
            if energy is not None:
                energy_source, energy_kind = 'OUTCAR:TOTEN', 'TOTEN'

        text = outcar or ''
        task, incar = _infer_task(out_dir, text, oszicar or '')
        converged = _completed_for_task(task, text, oszicar or '', incar)

        error = None
        if energy is None:
            error = '未从 OSZICAR/OUTCAR 解析到能量(可能未完成首个离子步或输出损坏)'
        return {
            'energy_ev': energy,
            'energy_source': energy_source,
            'energy_kind': energy_kind,
            'task': task,
            'converged': converged,
            'error': error,
        }

    def check_inputs(self, out_dir: str) -> list:
        """独立静态检查:三件套存在性 + ENCUT(周期)+ POTCAR 提示。"""
        issues: list = []
        for name in ('INCAR', 'KPOINTS', 'POSCAR'):
            if not os.path.isfile(os.path.join(out_dir, name)):
                issues.append(f'缺 {name}(VASP 输入不完整)。')
        incar_path = os.path.join(out_dir, 'INCAR')
        if os.path.isfile(incar_path):
            with open(incar_path, 'r', encoding='utf-8', errors='replace') as f:
                incar = parse_incar(f.read())
            if 'ENCUT' not in incar:
                issues.append('INCAR 缺 ENCUT(周期性平面波计算必须指定截断能)。')
            if 'GGA' not in incar and 'METAGGA' not in incar:
                issues.append('INCAR 未指定 GGA/METAGGA(泛函未定,VASP 将用 POTCAR 默认)。')
        if not os.path.isfile(os.path.join(out_dir, 'POTCAR')):
            issues.append('缺 POTCAR(VASP 赝势库需用户自备并拼接)。')
        return issues


def _read_text(path: str) -> str | None:
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def _last_toten(outcar_text: str) -> float | None:
    """OUTCAR 末次 'free  energy   TOTEN = ... eV'(read_e0 缺 OSZICAR 时的兜底)。"""
    hits = []
    for line in outcar_text.splitlines():
        if 'TOTEN' in line and '=' in line:
            parts = line.split('=', 1)[1].split()
            if parts:
                try:
                    hits.append(float(parts[0]))
                except ValueError:
                    continue
    return hits[-1] if hits else None


_SIGMA0_RE = re.compile(
    r'energy\s*\(\s*sigma\s*->\s*0\s*\)\s*=\s*([-+0-9.EDed]+)', re.IGNORECASE)
_MD_STEP_RE = re.compile(r'^\s*\d+\s+T=\s*', re.MULTILINE)


def _last_sigma0(outcar_text: str) -> float | None:
    """OUTCAR 末次 ``energy(sigma->0)``；比自由能 TOTEN 更接近 OSZICAR E0 口径。"""
    hits = []
    for raw in _SIGMA0_RE.findall(outcar_text):
        try:
            hits.append(float(raw.replace('D', 'E').replace('d', 'e')))
        except ValueError:
            continue
    return hits[-1] if hits else None


def _infer_task(out_dir: str, outcar_text: str, oszicar_text: str):
    """从 INCAR 推断 VASP 任务；缺 INCAR 时只按明确输出证据做保守回退。"""
    incar = {}
    raw = _read_text(os.path.join(out_dir, 'INCAR'))
    if raw:
        incar = parse_incar(raw)
        try:
            ibrion = int(incar.get('IBRION', -1))
        except (TypeError, ValueError):
            ibrion = -1
        try:
            nsw = int(incar.get('NSW', 0))
        except (TypeError, ValueError):
            nsw = 0
        if 5 <= ibrion <= 8:
            return 'freq', incar
        if ibrion == 0 and nsw > 0:
            return 'aimd', incar
        if nsw > 0 and ibrion != -1:
            return 'relax', incar
        return 'static', incar

    if 'THz' in outcar_text:
        return 'freq', incar
    if _MD_STEP_RE.search(oszicar_text):
        return 'aimd', incar
    if 'reached required accuracy' in outcar_text:
        return 'relax', incar
    return 'static', incar


def _completed_for_task(task: str, outcar_text: str, oszicar_text: str,
                        incar: dict) -> bool:
    """任务化完成门：证据与干净退出必须同时成立。"""
    finished = ('General timing and accounting' in outcar_text
                or 'Total CPU time used' in outcar_text)
    if not finished:
        return False
    if task == 'relax':
        return 'reached required accuracy' in outcar_text
    if task == 'freq':
        return 'THz' in outcar_text
    if task == 'aimd':
        steps = len(_MD_STEP_RE.findall(oszicar_text))
        try:
            expected = max(int(incar.get('NSW', 0)), 0)
        except (TypeError, ValueError):
            expected = 0
        return steps > 0 and (expected == 0 or steps >= expected)
    return 'EDIFF is reached' in outcar_text
