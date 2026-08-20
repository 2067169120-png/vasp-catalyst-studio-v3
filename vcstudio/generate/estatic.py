"""电子结构静态作业派生:从完成弛豫的目录派生 PDOS/Bader/差分电荷/静电势静态单点。

上游供给 F19(PDOS/d 带中心)、F20(Bader)、F21(差分电荷):弛豫收敛后,取 CONTCAR
作几何、复用母 INCAR 的电子学设置,改成静态单点(NSW=0/IBRION=-1),按 purpose 补相应
输出标志,并把 KPOINTS 网格加密(DOS/电荷密度要更密的 k 网格)。

INCAR 派生原则:**保留电子学**(ENCUT/ISPIN/MAGMOM/LDAU/GGA/…原样透传),只覆盖离子
弛豫相关键 + 加严 EDIFF + 加 purpose 输出标志。ISMEAR=-5(四面体+Blöchl,DOS/能量最佳),
但四面体法要求 k 点数≥4,加密网格积<4 时自动回退 ISMEAR=0(高斯展宽)并 warning。
每一处改动落进 job.yaml 的 changes(口径可见)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import time
from collections import OrderedDict

from vcstudio.generate.incar_builder import incar_dict_to_str, parse_incar
from vcstudio.generate.kpoints import kpoints_str, recommend_kpoints
from vcstudio.generate.poscar import read_cell_vectors
from vcstudio.shared import manifest as manifest_mod

_PURPOSES = ('pdos', 'bader', 'chgdiff', 'esp', 'elf')

# purpose → 追加的输出标志(值为 True 序列化成 .TRUE.)
_PURPOSE_KEYS = {
    'pdos': OrderedDict([('LORBIT', 11), ('NEDOS', 2000)]),
    'bader': OrderedDict([('LAECHG', True), ('LCHARG', True)]),
    'chgdiff': OrderedDict([('LCHARG', True)]),
    'esp': OrderedDict([('LVTOT', True)]),
    # elf:电子局域函数,LELF=.TRUE. → 产出 ELFCAR(VESTA 可视化共价/孤对/金属键)
    'elf': OrderedDict([('LELF', True)]),
}

# 派生用途→GUI/manifest 的规范任务 key。esp 是一个带 LOCPOT
# 输出的静态单点；功函数封装器会在其后写成 workfunction manifest。
_PURPOSE_TASK_TYPES = {
    'pdos': 'dos_pdos',
    'bader': 'bader',
    'chgdiff': 'chgdiff',
    'esp': 'static',
    'elf': 'elf',
}


def _read_first(dir_path, names):
    """在 dir_path 下按顺序找第一个存在的文件,返回 (文本, 文件名) 或 (None, None)。"""
    for nm in names:
        p = os.path.join(dir_path, nm)
        if os.path.isfile(p):
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                return f.read(), nm
    return None, None


def _parse_kpoints_grid(text: str):
    """自动网格 KPOINTS → [kx,ky,kz];显式 k 点列表/非自动/畸形 → None(调用方降级)。"""
    lines = text.splitlines()
    if len(lines) < 4:
        return None
    try:
        nkpt = int(lines[1].split()[0])
    except (ValueError, IndexError):
        return None
    if nkpt != 0:                    # 第2行非 0 = 显式 k 点数,非自动网格
        return None
    parts = lines[3].split()
    if len(parts) < 3:
        return None
    try:
        return [int(round(float(parts[0]))), int(round(float(parts[1]))),
                int(round(float(parts[2])))]
    except ValueError:
        return None


def densify_kpoints(grid, multiplier: float):
    """网格按倍数加密:>1 的分量 ×倍数后奇数化;=1 的分量(slab 法向/分子)保持 1。"""
    out = []
    for k in grid:
        if k <= 1:
            out.append(1)
        else:
            nk = max(1, int(round(k * multiplier)))
            if nk % 2 == 0:
                nk += 1              # 奇数化(Gamma-centered 惯例)
            out.append(nk)
    return out


def _num(v, default=None):
    try:
        if isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def build_static_job(relax_dir, out_dir, *, purpose: str = 'pdos',
                     kpts_multiplier: float = 2.0, poscar_text=None,
                     potcar_text=None, copy_parent_potcar: bool = True,
                     incar_text=None, drop_incar=None,
                     extra_incar=None, extra_meta=None) -> dict:
    """从完成弛豫的目录派生一个电子结构静态单点作业到 out_dir。

    Args:
        relax_dir: 弛豫作业目录(读 CONTCAR/INCAR/KPOINTS/POTCAR)。
        out_dir: 输出目录(exist_ok)。
        purpose: 'pdos'|'bader'|'chgdiff'|'esp',决定追加的输出标志。
        kpts_multiplier: KPOINTS 加密倍数(默认 2.0)。
        poscar_text/potcar_text/incar_text: 覆盖(供 chgdiff 拆分子体系复用);
            缺省从 relax_dir 读 CONTCAR(优先)/POTCAR/INCAR。
        copy_parent_potcar: potcar_text 缺省时是否回退复制母 POTCAR(默认 True);
            子体系(物种与母体不同)应传 False,避免写入物种不匹配的母 POTCAR。
        drop_incar: 从派生 INCAR 移除的键(如子体系不套用母体 MAGMOM)。
        extra_incar: 显式覆盖/追加的 INCAR 键(dict)。
        extra_meta: 额外写入 job.yaml 的键(如 chgdiff_role/siblings)。

    Returns:
        ``{'out_dir','changes','warnings'}``。缺 CONTCAR/POSCAR 或 INCAR → ValueError。
    """
    if purpose not in _PURPOSES:
        raise ValueError(f"purpose 只能是 {_PURPOSES},收到 {purpose!r}")
    warnings: list[str] = []
    changes: list[str] = []

    if poscar_text is None:
        poscar_text, src = _read_first(relax_dir, ('CONTCAR', 'POSCAR'))
        if poscar_text is None:
            raise ValueError(f'弛豫目录缺 CONTCAR/POSCAR:{relax_dir};无法派生静态几何')
        if src == 'POSCAR':
            warnings.append('未找到 CONTCAR,回退用 POSCAR 作静态几何(请确认弛豫已收敛)')

    if incar_text is None:
        incar_text, _ = _read_first(relax_dir, ('INCAR',))
        if incar_text is None:
            raise ValueError(f'弛豫目录缺 INCAR:{relax_dir};无法派生电子学设置')
    parent = parse_incar(incar_text)

    # KPOINTS:优先加密母网格;母 KPOINTS 缺失/非自动网格 → 按 CONTCAR 重新推荐(slab)
    kp_text, _ = _read_first(relax_dir, ('KPOINTS',))
    grid = _parse_kpoints_grid(kp_text) if kp_text is not None else None
    if grid is not None:
        new_grid = densify_kpoints(grid, kpts_multiplier)
        changes.append(f'KPOINTS: {grid[0]}x{grid[1]}x{grid[2]} → '
                       f'{new_grid[0]}x{new_grid[1]}x{new_grid[2]}(×{kpts_multiplier} 加密)')
    else:
        try:
            new_grid = recommend_kpoints(read_cell_vectors(poscar_text), 'slab')
        except (ValueError, NotImplementedError, IndexError) as e:
            raise ValueError(f'无法确定 KPOINTS:母 KPOINTS 不可用且 CONTCAR 晶格解析失败:{e}')
        warnings.append('母 KPOINTS 缺失或非自动网格,已按 CONTCAR 重新推荐(slab 口径,kz=1)')
        changes.append(f'KPOINTS: 重新推荐 → {new_grid[0]}x{new_grid[1]}x{new_grid[2]}')

    derived = OrderedDict(parent)

    def _set(key, val, note):
        old = derived.get(key, '(缺省)')
        derived[key] = val
        changes.append(f'{key}: {old} → {val}({note})')

    # 派生目录只复制四件套，不复制母作业的 WAVECAR/CHGCAR。若把弛豫续算留下的
    # ISTART=1 / ICHARG=11 原样带过来，远端会在启动时缺文件，或更隐蔽地把本应自洽的
    # PDOS/Bader/功函数算成固定电荷非自洽结果。派生性质作业统一从原子叠加电荷自洽启动。
    _set('ISTART', 0, '派生目录不依赖母作业 WAVECAR')
    _set('ICHARG', 2, '性质静态作业必须自洽，且派生目录不依赖母作业 CHGCAR')
    _set('NSW', 0, '静态单点')
    _set('IBRION', -1, '不做离子步')
    for key, note in (
            ('ISIF', '静态性质作业不做应力/变胞'),
            ('EDIFFG', '静态性质作业无离子收敛判据')):
        if key in derived:
            old = derived.pop(key)
            changes.append(f'{key}: {old} → 移除({note})')
    nk_prod = new_grid[0] * new_grid[1] * new_grid[2]
    if nk_prod >= 4:
        _set('ISMEAR', -5, '四面体+Blöchl 校正,DOS/能量精度最佳')
    else:
        _set('ISMEAR', 0, f'k 点积={nk_prod}<4,四面体法不可用,回退高斯展宽')
        if 'SIGMA' not in derived:
            _set('SIGMA', 0.05, '高斯展宽宽度')
        warnings.append(f'ISMEAR=-5 需 k 点数≥4,当前加密网格 '
                        f'{new_grid[0]}x{new_grid[1]}x{new_grid[2]} 积={nk_prod},'
                        '已自动回退 ISMEAR=0(高斯展宽)')

    ediff = _num(parent.get('EDIFF'), 1e-4)
    if 'EDIFF' not in parent or ediff > 1e-6:
        _set('EDIFF', 1e-6, '静态自洽收敛加严至 ≤1e-6')

    for k, v in _PURPOSE_KEYS[purpose].items():
        _set(k, v, f'purpose={purpose} 需要')

    if purpose == 'elf':
        # ELF(LELF=.TRUE.)与并行 NPAR 兼容性:部分 VASP 版本 NPAR>1 时 ELFCAR 分区不正确,
        # 且 LELF 与 NCORE(NCORE·NPAR=总核)互斥语义。绝不静默,显式提醒核对。
        warnings.append(
            'ELF 计算(LELF=.TRUE.)产出 ELFCAR;注意 ELF 与并行 NPAR 兼容性——部分 VASP '
            '版本 NPAR>1 时 ELFCAR 可能不正确,建议设 NPAR=1(或改用 NCORE=1)并核对版本。')

    if drop_incar:
        for k in drop_incar:
            ku = str(k).upper()
            if ku in derived:
                derived.pop(ku)
                changes.append(f'{ku}: 移除(子体系原子集与母体不同,不套用母体 {ku})')
                if ku == 'MAGMOM':
                    warnings.append('已移除 MAGMOM(子体系原子集与母体不同);'
                                    '磁性体系请按子体系重设 MAGMOM 后提交')
    if extra_incar:
        for k, v in extra_incar.items():
            _set(str(k).upper(), v, '显式覆盖')

    os.makedirs(out_dir, exist_ok=True)
    header = f'# vcstudio 电子结构静态派生(purpose={purpose};母目录={relax_dir})\n'
    with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
        f.write(header + incar_dict_to_str(derived))
    with open(os.path.join(out_dir, 'POSCAR'), 'w', encoding='utf-8') as f:
        f.write(poscar_text)
    with open(os.path.join(out_dir, 'KPOINTS'), 'w', encoding='utf-8') as f:
        f.write(kpoints_str(new_grid))

    if potcar_text is None and copy_parent_potcar:
        potcar_text, _ = _read_first(relax_dir, ('POTCAR',))
    if potcar_text is not None:
        with open(os.path.join(out_dir, 'POTCAR'), 'w', encoding='utf-8') as f:
            f.write(potcar_text)
    else:
        warnings.append('未找到 POTCAR(母目录无);静态作业需自行补 POTCAR 后提交')

    # 与所有其他作业一样写统一 manifest：不再只写 purpose/changes
    # 的 ad-hoc YAML。这样生成后可直接进入提交、监控、续算与报告状态机。
    out_path = os.path.abspath(str(out_dir))
    parent_path = os.path.abspath(str(relax_dir))
    files = [name for name in ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')
             if os.path.isfile(os.path.join(out_path, name))]
    hashes = {name: manifest_mod.sha256_file(os.path.join(out_path, name))
              for name in files}
    derived_meta = dict(extra_meta or {})
    inputs = {
        'engine': 'vasp',
        'files': files,
        'sha256': hashes,
        'poscar': os.path.join(out_path, 'POSCAR'),
        'poscar_sha256': hashes['POSCAR'],
        'parent_job': parent_path,
        'purpose': purpose,
        'kpts_multiplier': float(kpts_multiplier),
        'kpoints': list(new_grid),
        'incar_changes': list(changes),
    }
    from vcstudio.generate.method_recipe import builder_recipe
    inputs['method_recipe'] = builder_recipe(
        builder='vcstudio.generate.estatic/v1',
        task_type=_PURPOSE_TASK_TYPES[purpose], calc_type='derived-parent',
        validate=True, completions={'incar_changes': list(changes)},
        kpoints_source='parent-density-multiplier',
        extra={'purpose': purpose, 'kpts_multiplier': float(kpts_multiplier),
               'kpoints': list(new_grid), 'derived_metadata': derived_meta})
    if derived_meta:
        inputs['derived_metadata'] = derived_meta

    # 继承母作业的 calc_type（分子/体相/表面）；无母 manifest 时按本
    # 生成器的 slab KPOINTS 口径明确降级。
    parent_manifest = manifest_mod.load_manifest(relax_dir)
    calc_type = str((parent_manifest or {}).get('calc_type') or 'slab')
    system = (poscar_text.splitlines()[0].strip()
              if poscar_text.strip() else os.path.basename(out_path))
    manifest = manifest_mod.new_manifest(
        job_id=f'{os.path.basename(out_path)}-{time.strftime("%Y%m%d-%H%M%S")}',
        system=system,
        task_type=_PURPOSE_TASK_TYPES[purpose],
        calc_type=calc_type,
        inputs=inputs,
        warnings=warnings,
    )
    manifest['parent_job'] = parent_path
    manifest['derivation'] = {
        'purpose': purpose,
        'kpts_multiplier': float(kpts_multiplier),
        'kpoints': list(new_grid),
        'changes': list(changes),
        **derived_meta,
    }

    # 暂保留旧版解析器使用的顶层扩展键；核心事实已落在标准 inputs
    # 与 derivation 命名空间。扩展不得覆盖 schema/state/inputs 等核心键。
    legacy = {
        'parent': str(relax_dir),
        'purpose': purpose,
        'kpts_multiplier': float(kpts_multiplier),
        'kpoints': list(new_grid),
        'changes': list(changes),
    }
    for key, value in {**legacy, **derived_meta}.items():
        if key not in manifest:
            manifest[key] = value
    from vcstudio.shared.scientific_inputs import record_input_closure
    record_input_closure(out_path, manifest)
    manifest_mod.save_manifest(out_path, manifest)

    return {'out_dir': str(out_dir), 'changes': changes, 'warnings': warnings}
