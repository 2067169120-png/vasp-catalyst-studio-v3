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


def _cross(a: list, b: list) -> list:
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _dot(a: list, b: list) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _reciprocal_lengths(cell_vectors: list) -> list:
    """晶格矢量 → 倒格矢长度 |b_i|(约定 b_i = (a_j×a_k)/V,不含 2π)。

    修复:此前直接用实空间边长 1/|a_i| 定网格,对**非正交胞**(六方石墨烯 / hcp 金属
    slab、三斜)会系统性欠/过采样——而这些正是催化最常见的表面。改用真正的倒格矢长度。
    对正交胞 |b_i| ≡ 1/|a_i|,故立方/正交体系结果与旧版逐位一致(无回归)。
    退化胞(体积≈0)回退 1/|a_i| 兜底,避免除零。
    """
    a1, a2, a3 = ([float(c) for c in v[:3]] for v in cell_vectors[:3])
    vol = _dot(a1, _cross(a2, a3))
    if abs(vol) < 1e-12:
        lens = [math.sqrt(_dot(v, v)) for v in (a1, a2, a3)]
        return [1.0 / L if L > 0 else 1.0 for L in lens]
    b1, b2, b3 = (_cross(a2, a3), _cross(a3, a1), _cross(a1, a2))
    return [math.sqrt(_dot(b, b)) / abs(vol) for b in (b1, b2, b3)]


def recommend_kpoints(cell_vectors: list, calc_type: str = 'slab') -> list:
    """据倒格矢尺寸推荐 KPOINTS 网格。

    k_i = ceil(|b_i| / spacing),spacing ≈ 0.03 Å⁻¹(适合过渡金属);
    b_i 为倒格矢(不含 2π),对正交胞 |b_i| = 1/|a_i|(与旧版数值一致)。

    Args:
        cell_vectors: 3 个晶格矢量,每个 [x,y,z]。
        calc_type: 'molecule' → [1,1,1];'slab' → kz=1;'bulk' → 完整 3D。

    Returns:
        [kx, ky, kz](Gamma-centered Monkhorst-Pack)。
    """
    if calc_type == 'molecule':
        return [1, 1, 1]

    recip = _reciprocal_lengths(cell_vectors)

    target_spacing = 0.03  # Å⁻¹
    kpts = [max(1, int(math.ceil(b / target_spacing))) for b in recip]

    # 奇数化(Gamma-centered 惯例):偶数 +1
    kpts = [k if k % 2 == 1 else k + 1 for k in kpts]

    # cap 9 —— 再高需显式收敛测试
    kpts = [min(k, 9) for k in kpts]

    if calc_type == 'slab':
        kpts[2] = 1  # 表面法向仅 1 个 k 点

    logger.info('recommend_kpoints: |b|=%s type=%s -> kpts=%s',
                [round(float(b), 3) for b in recip], calc_type, kpts)
    return kpts


def kpoints_str(kpts: list) -> str:
    """生成 KPOINTS 文件体(Gamma-centered 自动网格)。"""
    return f'Automatic\n0\nGamma\n{kpts[0]} {kpts[1]} {kpts[2]}\n0 0 0\n'
