"""晶胞优化(变胞弛豫,ISIF=3)作业生成端——从固定胞弛豫升级到晶格+离子同弛豫。

对标「结构优化」之外的一类:体相/二维材料求平衡晶格常数、应变研究前置,须放开晶胞
(ISIF=3 = 应力张量驱动晶格形状+体积+离子同时弛豫)。从完成(固定胞)弛豫的目录派生:
CONTCAR→POSCAR + 由源 INCAR 派生变胞 INCAR + KPOINTS/POTCAR 原样。

方法学要点(写进 changes/warnings,绝不静默):
- **Pulay 应力**:平面波基组随晶胞体积变化不完备,变胞弛豫会引入虚假应力(Pulay stress)。
  标准做法是把 ENCUT 提到固定胞的 ~1.3×(降低基组不完备度),并在同一 ENCUT 下比较能量;
  或变胞后在更高 ENCUT 定容再优化。本模块默认**只提醒**(warning);``bump_encut=True`` 才
  按 ``encut_scale``(默认 1.3)实际抬高 ENCUT。
- ISIF=3 需要 IBRION=1/2(离子弛豫);NSW 必须 >0。源若为静态(NSW=0)则补一个正的 NSW。

派生复用 conv_scan.derive_incar(单点实现,changes 留痕)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
from collections import OrderedDict
from pathlib import Path

from vcstudio.generate.conv_scan import _read_text, _structure_source, derive_incar
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.shared import manifest as manifest_mod

DEFAULT_ENCUT_SCALE = 1.3          # Pulay 应力:变胞 ENCUT 建议 ×1.3
DEFAULT_CELLOPT_NSW = 100          # 源无有效 NSW 时补的离子步上限

_REASONS = {
    'ISIF': '变胞弛豫:应力驱动晶格形状+体积+离子同弛豫',
    'IBRION': '离子弛豫算法(共轭梯度),ISIF=3 需 IBRION=1/2',
    'NSW': '离子步上限(晶胞优化须 NSW>0)',
    'ENCUT': 'Pulay 应力:变胞抬高 ENCUT 降基组不完备度',
}


def _copy_if(src_dir, out_dir, name):
    src = os.path.join(src_dir, name)
    if os.path.isfile(src):
        import shutil
        shutil.copyfile(src, os.path.join(out_dir, name))
        return True
    return False


def build_cellopt_job(src_dir, out_dir, *, bump_encut: bool = False,
                      encut_scale: float = DEFAULT_ENCUT_SCALE) -> dict:
    """从(固定胞)弛豫目录派生晶胞优化(ISIF=3)作业。

    Args:
        src_dir: 源弛豫目录(读 CONTCAR/POSCAR + INCAR + KPOINTS + POTCAR)。
        out_dir: 输出目录(exist_ok)。
        bump_encut: True → 把 ENCUT 抬到 ``ceil(源 ENCUT × encut_scale)``(缓解 Pulay 应力);
            False(默认)→ 不改 ENCUT,仅在 warnings 给出 1.3× 建议。
        encut_scale: bump_encut=True 时的 ENCUT 放大倍数(默认 1.3)。

    Returns:
        ``{'out_dir','changes','warnings'}``。源缺 CONTCAR/POSCAR 或 INCAR → ValueError。
    """
    src_dir = str(src_dir)
    source_name, poscar_text = _structure_source(src_dir)
    if poscar_text is None:
        raise ValueError(f'源目录缺 CONTCAR/POSCAR(或均为空),无法派生晶胞优化:{src_dir}')
    base_incar = _read_text(os.path.join(src_dir, 'INCAR'))
    if base_incar is None:
        raise ValueError(f'源目录缺 INCAR,无法派生晶胞优化:{src_dir}')

    parsed = parse_incar(base_incar)
    warnings: list[str] = []
    set_keys: "OrderedDict" = OrderedDict()
    set_keys['ISIF'] = 3

    # IBRION:非 1/2 的(如静态 -1 或频率 5/6)改成 2
    ibr = parsed.get('IBRION')
    try:
        ibr_i = int(ibr) if ibr is not None else None
    except (TypeError, ValueError):
        ibr_i = None
    if ibr_i not in (1, 2):
        set_keys['IBRION'] = 2

    # NSW:缺失或 ≤0 → 补正的离子步上限
    nsw = parsed.get('NSW')
    try:
        nsw_i = int(nsw) if nsw is not None else None
    except (TypeError, ValueError):
        nsw_i = None
    if nsw_i is None or nsw_i <= 0:
        set_keys['NSW'] = DEFAULT_CELLOPT_NSW

    # ENCUT:默认只提醒;bump_encut 才实际抬高
    old_encut = parsed.get('ENCUT')
    try:
        old_encut_f = float(old_encut) if old_encut is not None else None
    except (TypeError, ValueError):
        old_encut_f = None
    if bump_encut and old_encut_f is not None:
        new_encut = int(math.ceil(old_encut_f * float(encut_scale) / 10.0) * 10)
        set_keys['ENCUT'] = new_encut
        warnings.append(f'已按 ×{encut_scale:g} 把 ENCUT {old_encut_f:g} → {new_encut} eV'
                        '(缓解变胞 Pulay 应力);同一体系各变胞比较能量须用同一 ENCUT。')
    else:
        tgt = f'{int(math.ceil((old_encut_f or 0) * float(encut_scale)))} eV' \
            if old_encut_f is not None else f'源 ENCUT × {encut_scale:g}'
        warnings.append(f'变胞弛豫存在 Pulay 应力(平面波基组随体积变化不完备):建议把 ENCUT '
                        f'提到约 {tgt}(≈源 ×{encut_scale:g}),或变胞后在更高 ENCUT 定容再优化;'
                        '传 bump_encut=True 可自动抬高。')
    warnings.append('晶胞优化收敛后,务必用弛豫末态 CONTCAR 在**目标 ENCUT** 下重跑一次'
                    '(消除 Pulay 应力残留),再取能量/晶格常数入库。')

    banner = '# === vcstudio 晶胞优化(变胞弛豫 ISIF=3;派生自固定胞弛豫 INCAR) ===\n' \
             '# 注意 Pulay 应力:变胞能量比较须同一 ENCUT,建议 ENCUT≈源×1.3。'
    new_incar, changes = derive_incar(base_incar, set_keys=set_keys, reasons=_REASONS,
                                      banner=banner)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'POSCAR'), 'w', encoding='utf-8') as f:
        f.write(poscar_text)
    with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
        f.write(new_incar)
    if not _copy_if(src_dir, out_dir, 'KPOINTS'):
        warnings.append('源目录缺 KPOINTS,未复制;请补齐(变胞下建议略密以稳应力张量)。')
    if not _copy_if(src_dir, out_dir, 'POTCAR'):
        warnings.append('源目录缺 POTCAR,未复制;提交前须补齐同一套赝势。')

    syms, _counts = parse_poscar_species(poscar_text)
    system = poscar_text.splitlines()[0].strip() if poscar_text.strip() else Path(out_dir).name
    parent = str(Path(src_dir).resolve())
    m = manifest_mod.new_manifest(
        job_id=f'{Path(out_dir).name}-cellopt', system=system, task_type='cellopt',
        calc_type='bulk',
        inputs={'parent_job': parent, 'derived_from': source_name,
                'isif': 3, 'bump_encut': bool(bump_encut),
                'incar_changes': changes, 'elements': list(syms)},
        warnings=warnings)
    m['parent_job'] = parent
    manifest_mod.save_manifest(out_dir, m)
    return {'out_dir': str(out_dir), 'changes': changes, 'warnings': warnings}
