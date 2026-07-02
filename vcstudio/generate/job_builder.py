"""生成区编排:POSCAR + 用户 INCAR → VASP 输入四件套目录。

只产 VASP 输入文件(INCAR/POTCAR/KPOINTS/POSCAR),不写提交脚本(提交移交 M2 集群区)。
INCAR 处理遵循决策1:**原文透传 + 追加补全**——用户 INCAR 一字不改,校验补全项追加于文末。
错误(ValueError/PotcarError)向上冒泡,不吞。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
import shutil
from collections import OrderedDict

from vcstudio.generate.poscar import read_poscar, parse_poscar_species, read_cell_vectors
from vcstudio.generate.potcar import build_potcar, max_enmax
from vcstudio.generate.kpoints import recommend_kpoints, kpoints_str
from vcstudio.generate.incar_builder import (
    parse_incar, validate_and_complete_incar, incar_dict_to_str,
)

_APPEND_BANNER = '# --- vcstudio 自动补全 (validate_and_complete_incar) ---'


def _coerce_incar(incar):
    """把 incar 输入规整为 (incar_dict[大写key], original_text|None)。

    - dict → (大写键 dict, None)(无原文可保,走序列化)。
    - str 且为存在文件 → 读文件文本;否则视为原始 INCAR 文本。均 parse_incar 得 dict。
    """
    if isinstance(incar, dict):
        return OrderedDict((str(k).upper(), v) for k, v in incar.items()), None
    if isinstance(incar, str):
        text = read_poscar(incar) if os.path.isfile(incar) else incar
        return parse_incar(text), text
    raise TypeError('incar 须为 dict / 文件路径 / INCAR 文本')


def _render_incar(original_text, incar_dict, completions, system_name):
    """决策1:有原文 → 原文透传 + 追加补全;无原文(dict 输入)→ 合并序列化。"""
    if original_text is not None:
        out = original_text if original_text.endswith('\n') else original_text + '\n'
        if completions:
            out += '\n' + _APPEND_BANNER + '\n' + incar_dict_to_str(completions)
        return out
    merged = OrderedDict(incar_dict)
    merged.update(completions)
    return incar_dict_to_str(merged, system_name)


def build_job_dir(poscar_path, incar, out_dir, *,
                  calc_type: str = 'slab', kpoints=None, validate: bool = True,
                  lib_root: str | None = None, system_name: str = '') -> dict:
    """生成 VASP 输入四件套到 out_dir。

    Args:
        poscar_path: POSCAR 文件路径。
        incar: dict / INCAR 文件路径 / 原始 INCAR 文本。
        out_dir: 输出目录(exist_ok,幂等覆盖)。
        calc_type: 'molecule'|'slab'|'bulk',决定 KPOINTS 策略。
        kpoints: 显式 [kx,ky,kz];缺省则按 cell 自动推荐。
        validate: 是否校验补全用户 INCAR(缺 ENCUT/MAGMOM 等)。
        lib_root: POTCAR 库根(缺省从 config 读)。

    Returns:
        {'ok','out_dir','warnings','kpoints','elements','completions','calc_type'}。
        VASP4/畸形 POSCAR → ValueError;ENMAX>ENCUT/库缺失 → PotcarError(冒泡)。
    """
    content = read_poscar(poscar_path)
    elements, counts = parse_poscar_species(content)
    if not elements:
        raise ValueError(
            'POSCAR 缺元素符号行(VASP4 或畸形),无法生成 POTCAR/MAGMOM;'
            '请补第6行元素符号(VASP5 格式)。')

    incar_dict, original_text = _coerce_incar(incar)

    completions, warnings = OrderedDict(), []
    if validate:
        completions, warnings = validate_and_complete_incar(
            incar_dict, elements, counts, lib_root)

    # ENCUT(仅供 POTCAR 的 ENMAX≤ENCUT 检查;从不写入 INCAR)。用成员判断避免把
    # 用户 ENCUT=0 等假值吞掉;三种来源互斥:用户显式 > 校验补全 > (无则)VASP 默认 max ENMAX。
    if 'ENCUT' in incar_dict:
        raw_encut = incar_dict['ENCUT']
        try:
            if isinstance(raw_encut, bool):   # .TRUE./.FALSE. 被 parse 成 bool,非合法 ENCUT
                raise ValueError
            encut = int(float(raw_encut))
        except (TypeError, ValueError):
            raise ValueError(
                f'INCAR 的 ENCUT 非数字: {raw_encut!r};请提供数值(如 ENCUT = 500)。')
    elif 'ENCUT' in completions:
        encut = int(completions['ENCUT'])
    else:
        # validate=False 且用户未写 ENCUT:VASP 默认取 max ENMAX,用它检查即等价放行
        encut = int(math.ceil(max_enmax(elements, lib_root)))

    potcar_text = build_potcar(elements, encut=encut, lib_root=lib_root)

    kpts = list(kpoints) if kpoints is not None else \
        recommend_kpoints(read_cell_vectors(content), calc_type)

    incar_out = _render_incar(original_text, incar_dict, completions, system_name)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'INCAR'), 'w', encoding='utf-8') as f:
        f.write(incar_out)
    with open(os.path.join(out_dir, 'POTCAR'), 'w', encoding='utf-8') as f:
        f.write(potcar_text)
    with open(os.path.join(out_dir, 'KPOINTS'), 'w', encoding='utf-8') as f:
        f.write(kpoints_str(kpts))
    shutil.copyfile(poscar_path, os.path.join(out_dir, 'POSCAR'))

    return {'ok': True, 'out_dir': str(out_dir), 'warnings': warnings,
            'kpoints': kpts, 'elements': elements,
            # 附加回传(向后兼容的新增键):manifest/预览用
            'completions': dict(completions), 'calc_type': calc_type}
