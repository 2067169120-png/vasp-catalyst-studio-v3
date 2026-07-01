"""KPOINTS 推荐 + 文件体生成。

`recommend_kpoints` 逐字复用自 E: layer2_science/convergence.py(源自 ASE_VASP_automation
kpoint_lists):按倒空间尺寸取 Gamma-centered 网格,间距 ~0.03 Å⁻¹。
`kpoints_str` 抽出 E: 内联的 KPOINTS 文件体(convergence.py 中原为内联 f-string)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


def recommend_kpoints(cell_vectors: list, calc_type: str = 'slab') -> list:
    """据倒空间尺寸推荐 KPOINTS 网格。

    k_i = ceil(1 / (spacing · |a_i|)),spacing ≈ 0.03 Å⁻¹(适合过渡金属)。

    Args:
        cell_vectors: 3 个晶格矢量,每个 [x,y,z]。
        calc_type: 'molecule' → [1,1,1];'slab' → kz=1;'bulk' → 完整 3D。

    Returns:
        [kx, ky, kz](Gamma-centered Monkhorst-Pack)。
    """
    if calc_type == 'molecule':
        return [1, 1, 1]

    lengths = [math.sqrt(sum(c * c for c in v[:3])) for v in cell_vectors[:3]]

    target_spacing = 0.03  # Å⁻¹
    kpts = [max(1, int(math.ceil(1.0 / (target_spacing * l)))) for l in lengths]

    # 奇数化(Gamma-centered 惯例):偶数 +1
    kpts = [k if k % 2 == 1 else k + 1 for k in kpts]

    # cap 9 —— 再高需显式收敛测试
    kpts = [min(k, 9) for k in kpts]

    if calc_type == 'slab':
        kpts[2] = 1  # 表面法向仅 1 个 k 点

    logger.info('recommend_kpoints: lengths=%s type=%s -> kpts=%s',
                [round(float(l), 2) for l in lengths], calc_type, kpts)
    return kpts


def kpoints_str(kpts: list) -> str:
    """生成 KPOINTS 文件体(Gamma-centered 自动网格)。"""
    return f'Automatic\n0\nGamma\n{kpts[0]} {kpts[1]} {kpts[2]}\n0 0 0\n'
