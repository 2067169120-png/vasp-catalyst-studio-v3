"""VASPsol 真空/隐式溶剂同几何静态配对作业。

溶剂化能只能由同一几何、同一电子学设置的 ``E_solvent - E_vacuum`` 得到；
本模块一次生成两份互相绑定的标准 manifest，避免用户分别建作业后混用口径。
"""
from __future__ import annotations

import os
import shutil
import uuid

from vcstudio.generate import estatic
from vcstudio.generate.incar_builder import VASPSOL_ADVISORY
from vcstudio.shared import manifest as manifest_mod


def build_pair(source_dir: str, out_root: str | None = None, *, eb_k: float = 78.4) -> dict:
    """从已完成作业派生真空/溶剂两份冻结几何静态作业。"""
    source = os.path.abspath(str(source_dir))
    parent_manifest = manifest_mod.load_manifest(source)
    if parent_manifest is None:
        raise ValueError('源目录缺 job.yaml；请先导入已完成结果或选择台账中的作业')
    if parent_manifest.get('state') != 'DONE':
        raise ValueError(
            f"源作业状态为 {parent_manifest.get('state')!r}，必须确认 DONE 后才能派生 VASPsol 配对")
    dielectric = float(eb_k)
    if not (1.0 <= dielectric <= 1000.0):
        raise ValueError('EB_K 必须在 1–1000 之间')

    base = os.path.basename(os.path.normpath(source))
    root = os.path.abspath(str(out_root or os.path.dirname(source)))
    vacuum_dir = os.path.join(root, f'{base}_vaspsol_vacuum')
    solvent_dir = os.path.join(root, f'{base}_vaspsol_ebk_{dielectric:g}'.replace('.', 'p'))
    existing = [path for path in (vacuum_dir, solvent_dir) if os.path.exists(path)]
    if existing:
        raise FileExistsError('VASPsol 目标目录已存在，拒绝覆盖：' + '、'.join(existing))

    pair_id = str(uuid.uuid4())
    try:
        common = dict(
            purpose='esp', drop_incar=('LSOL', 'EB_K'),
            kpts_multiplier=1.0,
        )
        vacuum = estatic.build_static_job(
            source, vacuum_dir, extra_incar={'LSOL': False},
            extra_meta={'vaspsol_role': 'vacuum', 'vaspsol_pair_id': pair_id},
            **common)
        solvent = estatic.build_static_job(
            source, solvent_dir,
            extra_incar={'LSOL': True, 'EB_K': dielectric},
            extra_meta={'vaspsol_role': 'solvent', 'vaspsol_pair_id': pair_id,
                        'eb_k': dielectric},
            **common)

        for role, path, peer in (
                ('vacuum', vacuum_dir, solvent_dir),
                ('solvent', solvent_dir, vacuum_dir)):
            manifest = manifest_mod.load_manifest(path)
            if manifest is None:                         # pragma: no cover - 防御性
                raise RuntimeError(f'派生后缺 job.yaml：{path}')
            manifest['task_type'] = 'vaspsol'
            manifest['vaspsol'] = {
                'pair_id': pair_id, 'role': role, 'peer_dir': peer,
                'eb_k': dielectric, 'source_job': source,
            }
            manifest.setdefault('inputs', {})['vaspsol'] = dict(manifest['vaspsol'])
            manifest.setdefault('warnings', []).append(VASPSOL_ADVISORY)
            manifest_mod.save_manifest(path, manifest)
    except Exception:
        # 这两个目录在入口已证明不存在，只回滚本次新建目标，绝不碰用户源目录。
        for path in (solvent_dir, vacuum_dir):
            if os.path.isdir(path):
                shutil.rmtree(path)
        raise

    warnings = list(dict.fromkeys(
        list(vacuum.get('warnings') or []) + list(solvent.get('warnings') or []) +
        [VASPSOL_ADVISORY]))
    return {
        'pair_id': pair_id,
        'job_dirs': [vacuum_dir, solvent_dir],
        'vacuum_dir': vacuum_dir,
        'solvent_dir': solvent_dir,
        'eb_k': dielectric,
        'changes': [
            '真空对照：LSOL=.FALSE.',
            f'隐式溶剂：LSOL=.TRUE., EB_K={dielectric:g}',
            '两份作业共用同一 POSCAR/KPOINTS/POTCAR 与除 LSOL/EB_K 外的 INCAR 设置',
        ],
        'warnings': warnings,
    }
