"""旧版结果导入:E:/V2.0.0 老格式组目录 → vcstudio 项目(job.yaml+project.yaml)→ 新报告。

老格式(sac_results/<组>/<体系>/,含 CONTCAR/OSZICAR/OUTCAR):裸底物名=组名,
吸附体系名=<组>_<物种>。**原数据只读**——job.yaml/project.yaml 全部落在导入目标目录,
能量/收敛在导入时从原 OSZICAR/OUTCAR 本地解析写入 manifest(单一真相源从此在新侧)。
逐物种参考(E_ads = E_sys − E_slab − E_mol)经 project['species_refs'] 走
adsorption.delta_e_rows。纯本地文件操作,离线可测。中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from pathlib import Path

from vcstudio.project import freeenergy
from vcstudio.shared import manifest as mm

_SKIP = {'_refs', '_analysis_done', '_palette_preview', 'data', 'figures'}
_CONVERGED_MARK = 'reached required accuracy'


def _converged(member_dir) -> bool:
    """本地 OUTCAR 收敛判据(与远端取证同口径;读不到 → False)。"""
    try:
        with open(os.path.join(str(member_dir), 'OUTCAR'), 'r',
                  encoding='utf-8', errors='replace') as f:
            for line in f:
                if _CONVERGED_MARK in line:
                    return True
    except OSError:
        pass
    return False


def _import_member(src_dir, out_dir, system: str) -> dict:
    """单个老体系目录 → 新作业目录(仅 job.yaml;能量/收敛本地解析)。返回 manifest。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    e0 = freeenergy.read_e0(src_dir)
    ok = _converged(src_dir) and e0 is not None
    m = mm.new_manifest(job_id=f'legacy-{out_dir.name}', system=system,
                        task_type='relax', calc_type='slab',
                        inputs={'legacy_source': str(src_dir)})
    mm.set_state(m, 'DONE' if ok else 'NEEDS_HUMAN',
                 note=('旧版结果导入:OUTCAR 达到要求精度' if ok else
                       '旧版结果导入:未见收敛标志或无 E0,请人工核对'))
    if e0 is not None:
        m['results'] = {'energy_e0_eV': e0}
    mm.save_manifest(out_dir, m)
    return m


def import_group(group_dir, out_root, molecules_dir=None) -> dict:
    """老格式组目录 → 新项目。返回 project dict(project.yaml 已写入 out_root)。

    - 裸底物(目录名 == 组名)→ clean_slab;其余 <组>_<物种> → configs;
    - molecules_dir(mol_<物种>/)→ project['species_refs'](逐物种 E_mol);
    - gas_ref=None(逐物种参考替代单一参考)。
    """
    group_dir = Path(group_dir)
    gname = group_dir.name
    out_root = Path(out_root)
    slab_dir, config_dirs = None, []
    for entry in sorted(os.listdir(group_dir)):
        src = group_dir / entry
        if entry in _SKIP or not src.is_dir() or entry.startswith('_'):
            continue
        dst = out_root / entry
        _import_member(src, dst, system=entry)
        if entry == gname:
            slab_dir = str(dst)
        else:
            config_dirs.append(str(dst))
    if slab_dir is None:
        raise ValueError(f'组 {gname} 缺裸底物目录(应与组同名 {gname})')
    _ORDER = ('S8', 'Li2S8', 'Li2S6', 'Li2S4', 'Li2S2', 'Li2S')   # 放电顺序(图表 X 轴)

    def _rank(d):
        n = os.path.basename(d)
        return next((i for i, sp in enumerate(_ORDER) if n.endswith('_' + sp)), 99)

    config_dirs.sort(key=_rank)
    project = {
        'schema': 1, 'name': gname, 'root': str(out_root.resolve()),
        'created_at': mm._now_iso(), 'legacy_source': str(group_dir.resolve()),
        'members': {'clean_slab': slab_dir, 'gas_ref': None, 'configs': config_dirs},
    }
    if molecules_dir:
        refs = freeenergy.load_molecule_energies(molecules_dir)
        if refs:
            project['species_refs'] = refs
    from vcstudio.project import adsorption
    adsorption.save_project(out_root, project)
    return project
