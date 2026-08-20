"""Dimer 过渡态搜索(VTST)作业生成端——不需要末态的单端点鞍点搜索。

NEB 需要给定始态+末态两端插值找过渡态;Dimer(Henkelman VTST)只需**一个**接近鞍点的
初猜构型 + 一个初始模式方向(dimer 轴,MODECAR),沿最低曲率模式爬向一阶鞍点。适合末态
未知/难构造的解离、扩散、翻转过程。

**硬前提:需集群 VASP 用 VTST(vtstcode)补丁重新编译。** 标准 VASP 不认 IOPT/ICHAIN,
仅 IBRION=3+POTIM=0 会让 VASP 的内置离子步"什么都不做"(阻尼 MD 零步长),既不 dimer 也
不弛豫——即**不报错、原地不动、拿不到鞍点**。故本模块把该前提写进 docstring/warning,绝不静默。

VTST Dimer 关键键:
- ICHAIN=2 → 选 dimer 方法;IBRION=3 + POTIM=0 → 交出离子步给 VTST 优化器;
- IOPT=2 → VTST 共轭梯度(CG)优化器(须 IBRION=3、POTIM=0 配合)。

MODECAR(初始 dimer 轴):两构型差向量(初猜→位移构型,指向鞍点跨越方向)或随机微扰,
整个 3N 向量归一化。收敛后须另跑频率计算,用 thermo.classify_imaginary(context='ts')验证
**恰一个大虚频**(verify_saddle 一步接好)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
import random
from collections import OrderedDict
from pathlib import Path

from vcstudio.generate.conv_scan import _read_text, _structure_source, derive_incar
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.generate.structure_view import parse_positions
from vcstudio.shared import manifest as manifest_mod

DEFAULT_DIMER_NSW = 200            # dimer 迭代上限(源无有效 NSW 时补)
DEFAULT_PERTURB = 0.01            # 随机微扰幅度(Å,归一前)

_REASONS = {
    'ICHAIN': 'ICHAIN=2 选择 dimer 方法(VTST)',
    'IBRION': 'IBRION=3 + POTIM=0 交出离子步给 VTST 优化器',
    'POTIM': 'POTIM=0:步长由 VTST 优化器控制(VASP 内置步长置零)',
    'IOPT': 'IOPT=2:VTST 共轭梯度优化器(须 IBRION=3、POTIM=0)',
    'NSW': 'dimer 迭代步上限(须 NSW>0)',
}


def _copy_if(src_dir, out_dir, name):
    src = os.path.join(src_dir, name)
    if os.path.isfile(src):
        import shutil
        shutil.copyfile(src, os.path.join(out_dir, name))
        return True
    return False


def build_modecar(initial_poscar: str, displaced_poscar: str | None = None, *,
                  seed: int = 0, amplitude: float = DEFAULT_PERTURB) -> tuple:
    """生成 MODECAR(初始 dimer 轴)→ ``(文本, method, warnings)``。

    - 给 ``displaced_poscar``:模式 = 位移构型 − 初猜构型(逐原子笛卡尔差,指向鞍点跨越方向)。
      两构型原子数须一致;差向量近乎为零(构型相同)→ 回退随机微扰并 warning。
    - 不给:每原子随机微扰(seed 定种子,可复现)。
    整个 3N 向量归一化为单位长度(dimer 轴只需方向)。每行 3 分量,与原子顺序对齐。
    """
    p0 = parse_positions(initial_poscar)
    c0 = p0['coords']
    n = len(c0)
    warnings: list[str] = []
    method = 'random'
    vec = None

    if displaced_poscar is not None:
        c1 = parse_positions(displaced_poscar)['coords']
        if len(c1) != n:
            raise ValueError(f'位移构型原子数 {len(c1)} 与初猜 {n} 不一致,无法作差生成 MODECAR')
        vec = [[c1[i][j] - c0[i][j] for j in range(3)] for i in range(n)]
        norm = math.sqrt(sum(v * v for row in vec for v in row))
        if norm < 1e-10:
            warnings.append('位移构型与初猜几乎相同,差向量近零;已回退随机微扰生成初始模式。')
            vec = None
        else:
            method = 'difference'

    if vec is None:
        rng = random.Random(seed)
        vec = [[rng.uniform(-1.0, 1.0) * amplitude for _ in range(3)] for _ in range(n)]

    norm = math.sqrt(sum(v * v for row in vec for v in row))
    if norm < 1e-30:
        raise ValueError('MODECAR 模式向量为零,无法归一化(随机微扰也退化?请检查体系)')
    unit = [[v / norm for v in row] for row in vec]
    lines = [f'  {r[0]: .12f} {r[1]: .12f} {r[2]: .12f}' for r in unit]
    return '\n'.join(lines) + '\n', method, warnings


def build_dimer_job(src_dir, out_root, *, displaced_poscar=None, seed: int = 0,
                    amplitude: float = DEFAULT_PERTURB) -> dict:
    """从鞍点初猜目录派生 VTST Dimer 作业(**需 VTST 编译的 VASP**)。

    读 src 的 CONTCAR(缺则 POSCAR)+ INCAR + KPOINTS + POTCAR,生成:POSCAR(初猜,原样)
    + 派生 INCAR(ICHAIN=2/IBRION=3/POTIM=0/IOPT=2,NSW 保证 >0)+ MODECAR(初始 dimer 轴)
    + KPOINTS/POTCAR 原样;写 job.yaml(task_type='dimer')。

    Args:
        src_dir: 鞍点初猜目录。
        out_root: 输出目录。
        displaced_poscar: 位移构型 POSCAR 文本(可省;给了则 MODECAR=位移−初猜,否则随机微扰)。
        seed/amplitude: 随机微扰的种子与幅度(仅无 displaced 时用)。

    Returns:
        ``{'out_dir','changes','warnings','modecar_method'}``。源缺结构/INCAR → ValueError。
    """
    src_dir = str(src_dir)
    source_name, poscar_text = _structure_source(src_dir)
    if poscar_text is None:
        raise ValueError(f'源目录缺 CONTCAR/POSCAR(或均为空),无法派生 Dimer 作业:{src_dir}')
    base_incar = _read_text(os.path.join(src_dir, 'INCAR'))
    if base_incar is None:
        raise ValueError(f'源目录缺 INCAR,无法派生 Dimer 作业:{src_dir}')

    parsed = parse_incar(base_incar)
    set_keys: "OrderedDict" = OrderedDict([
        ('ICHAIN', 2), ('IBRION', 3), ('POTIM', 0), ('IOPT', 2),
    ])
    nsw = parsed.get('NSW')
    try:
        nsw_i = int(nsw) if nsw is not None else None
    except (TypeError, ValueError):
        nsw_i = None
    if nsw_i is None or nsw_i <= 0:
        set_keys['NSW'] = DEFAULT_DIMER_NSW

    banner = ('# === vcstudio Dimer 过渡态(VTST;派生自鞍点初猜 INCAR) ===\n'
              '# 需集群 VASP 用 VTST 补丁编译:标准 VASP 不认 IOPT/ICHAIN,IBRION=3+POTIM=0\n'
              '# 会原地不动、既不 dimer 也不弛豫(不报错但拿不到鞍点)。')
    new_incar, changes = derive_incar(base_incar, set_keys=set_keys, reasons=_REASONS,
                                      banner=banner)
    modecar, method, warnings = build_modecar(
        poscar_text, displaced_poscar, seed=seed, amplitude=amplitude)
    warnings.insert(0, 'Dimer 需集群 VASP 用 VTST(vtstcode)补丁重新编译;标准 VASP 会忽略 '
                       'IOPT/ICHAIN 并原地不动(不报错、无鞍点),提交前务必确认 VASP 版本。')
    warnings.append('Dimer 收敛后须另跑频率计算,用 thermo.classify_imaginary(context="ts")'
                    '验证恰一个大虚频(verify_saddle 可一步接好),再确认为一阶鞍点。')

    os.makedirs(out_root, exist_ok=True)
    with open(os.path.join(out_root, 'POSCAR'), 'w', encoding='utf-8') as f:
        f.write(poscar_text)
    with open(os.path.join(out_root, 'INCAR'), 'w', encoding='utf-8') as f:
        f.write(new_incar)
    with open(os.path.join(out_root, 'MODECAR'), 'w', encoding='utf-8') as f:
        f.write(modecar)
    if not _copy_if(src_dir, out_root, 'KPOINTS'):
        warnings.append('源目录缺 KPOINTS,未复制;提交前须补齐。')
    if not _copy_if(src_dir, out_root, 'POTCAR'):
        warnings.append('源目录缺 POTCAR,未复制;提交前须补齐同一套赝势。')

    syms, _counts = parse_poscar_species(poscar_text)
    system = poscar_text.splitlines()[0].strip() if poscar_text.strip() else Path(out_root).name
    parent = str(Path(src_dir).resolve())
    from vcstudio.generate.method_recipe import builder_recipe
    inputs = {
        'engine': 'vasp', 'parent_job': parent, 'derived_from': source_name,
        'modecar_method': method, 'incar_changes': changes,
        'elements': list(syms),
        'method_recipe': builder_recipe(
            builder='vcstudio.generate.dimer_builder/v1', task_type='dimer',
            calc_type='slab', validate=True,
            completions={'incar_changes': changes}, kpoints_source='parent-copy',
            extra={'modecar_method': method, 'seed': int(seed),
                   'amplitude': float(amplitude)}),
    }
    m = manifest_mod.new_manifest(
        job_id=f'{Path(out_root).name}-dimer', system=system, task_type='dimer',
        calc_type='slab', inputs=inputs, warnings=warnings)
    m['parent_job'] = parent
    from vcstudio.shared.scientific_inputs import record_input_closure
    record_input_closure(out_root, m)
    manifest_mod.save_manifest(out_root, m)
    return {'out_dir': str(out_root), 'changes': changes, 'warnings': warnings,
            'modecar_method': method}


def verify_saddle(outcar_text_or_dir, *, noise_threshold: float = None) -> dict:
    """Dimer 收敛后的鞍点验证:解析频率 OUTCAR → classify_imaginary(context='ts')。

    接受 OUTCAR **文本**或**作业目录**(目录则读其 OUTCAR)。复用 thermo 的解析链:
    parse_outcar_frequencies + classify_imaginary。恰一个大虚频 → verdict='valid_ts';
    0 个或 ≥2 个 → 'invalid_ts'(见 thermo.classify_imaginary)。无频率行 → note 提示。

    Returns:
        thermo.classify_imaginary 的结果 dict(附 'n_freq_found');OUTCAR 不可读/无频率 →
        ``{'verdict': None, 'note': ...}``(绝不编造鞍点判定)。
    """
    text = outcar_text_or_dir
    if isinstance(text, str) and '\n' not in text and os.path.isdir(text):
        text = _read_text(os.path.join(text, 'OUTCAR'))
    elif isinstance(text, str) and '\n' not in text and os.path.isfile(text):
        text = _read_text(text)
    if not text:
        return {'verdict': None, 'note': 'OUTCAR 不可读或为空,无法验证鞍点(需频率计算 OUTCAR)。'}
    from vcstudio.project import thermo          # 延迟 import:避免 generate 载入期依赖 project 层
    _real, _imag, imag_cm = thermo.parse_outcar_frequencies(text)
    if not _real and not imag_cm:
        return {'verdict': None,
                'note': '未在 OUTCAR 找到频率行:Dimer 收敛后须**另跑频率计算**(IBRION=5/6),'
                        '再用本函数验证一阶鞍点。'}
    kw = {} if noise_threshold is None else {'noise_threshold': noise_threshold}
    res = thermo.classify_imaginary(imag_cm, context='ts', **kw)
    res['n_freq_found'] = len(_real) + len(imag_cm)
    return res
