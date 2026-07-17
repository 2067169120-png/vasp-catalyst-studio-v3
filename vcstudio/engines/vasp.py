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

import os
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
    force = spec.convergence.get('force_ev_a', 0.02) if spec.convergence else 0.02
    if spec.task == 'relax':
        tags['IBRION'] = 2
        tags['NSW'] = int(spec.extras.get('nsw', 200))
        tags['ISIF'] = int(spec.extras.get('isif', 2))
        tags['EDIFFG'] = -abs(float(force))            # 力判据(负=力阈值)
    elif spec.task == 'static':
        tags['IBRION'] = -1
        tags['NSW'] = 0
    elif spec.task == 'freq':
        tags['IBRION'] = 5                             # 有限差分 Hessian
        tags['NSW'] = 1
        tags['NFREE'] = int(spec.extras.get('nfree', 2))
        tags['POTIM'] = float(spec.extras.get('potim', 0.015))
    return tags


def build_incar_dict(spec: CalcSpec) -> tuple[OrderedDict, list]:
    """CalcSpec → INCAR dict(OrderedDict)+ warnings。extras['incar'] 最后覆盖。"""
    warnings: list = []
    incar: OrderedDict = OrderedDict()
    incar['PREC'] = 'Accurate'
    if spec.cutoff_ev is not None:
        incar['ENCUT'] = int(round(float(spec.cutoff_ev)))
    incar['EDIFF'] = spec.convergence.get('energy_ev', 1e-5) if spec.convergence else 1e-5
    incar.update(_functional_tags(spec.functional, warnings))
    incar.update(_dispersion_tag(spec.dispersion, warnings))
    incar['ISPIN'] = 2 if spec.spin else 1
    # 展宽:分子/半导体安全默认 Gaussian(ISMEAR=0);金属请经 extras['incar'] 覆盖。
    incar['ISMEAR'] = 0
    incar['SIGMA'] = 0.05
    incar.update(_task_tags(spec))
    if spec.charge:
        warnings.append(
            f'VASP 带电体系(charge={spec.charge})需设 NELECT 并计背景电荷校正,'
            f'本适配未自动处理;请在 extras["incar"] 显式给出 NELECT。')
    # extras 私有 INCAR 覆盖(用户显式值最高优先)
    for k, v in (spec.extras.get('incar') or {}).items():
        incar[str(k).upper()] = v
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
            kpts = [1, 1, 1]
        elif spec.kpoints is not None:
            kpts = list(spec.kpoints)
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

        - energy_ev:read_e0(OSZICAR 末次 E0,σ→0 外推);缺则退 OUTCAR 末次 TOTEN。
        - converged:OUTCAR 出现 'reached required accuracy'(离子弛豫收敛),或
          (整体正常结束 'General timing' 且电子步 'EDIFF is reached')判单点收敛。
          说明:relax 跑满 NSW 未收敛时 VASP 无独特标志,力序列判定见 cluster.convergence。
        """
        e0 = read_e0(out_dir)
        outcar = _read_text(os.path.join(out_dir, 'OUTCAR'))
        oszicar = _read_text(os.path.join(out_dir, 'OSZICAR'))
        if outcar is None and oszicar is None:
            return {'energy_ev': None, 'converged': False,
                    'error': f'缺 OSZICAR/OUTCAR(目录 {out_dir} 无 VASP 输出)'}

        energy = e0
        if energy is None and outcar:
            energy = _last_toten(outcar)

        text = outcar or ''
        reached = 'reached required accuracy' in text
        finished = ('General timing and accounting' in text
                    or 'Total CPU time used' in text)
        scf_reached = 'EDIFF is reached' in text
        converged = bool(reached or (finished and scf_reached))

        error = None
        if energy is None:
            error = '未从 OSZICAR/OUTCAR 解析到能量(可能未完成首个离子步或输出损坏)'
        return {'energy_ev': energy, 'converged': converged, 'error': error}

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
