"""显式溶剂化复合物组装(F,论文 Li2S3+2DOL+1DME 电解液口径)。

定位(2026-07):多硫化物在醚类电解液中的真实构型需**显式溶剂化**——核(多硫化锂)
周围裹几个溶剂分子(DOL/DME),送 AIMD/弛豫看溶剂化结构与结合能。本模块从分子库
(generate.molecules)取核与溶剂几何,把溶剂分子按**确定性随机**(种子固定)取向撒在
核周围的球面上,逐个做**原子级最小间距校验 + 重试**(避免叠原子),再装进立方盒,
写出可直接送算的 POSCAR。

确定性:同 seed → 同结构(np.random.RandomState 固定种子,顺序消费)。几何健全:
组装后做**片段间**最小间距校验(> 1.5 Å;分子内成键距离如 C-H 0.94/1.09 Å 不计入)。
纯 python+numpy(刻意不碰 np.linalg,见 sac_builder 说明);中文注释,英文标识符。
"""
from __future__ import annotations

import numpy as np

from vcstudio.generate import molecules
from vcstudio.generate.sac_builder import write_poscar

# 电解液预设:溶剂 (名, 个数) 列表
SOLVENT_PRESETS = {
    'lis_electrolyte': [('DOL', 2), ('DME', 1)],   # 论文口径:1,3-二氧戊环 ×2 + 乙二醇二甲醚 ×1
    'dol_only': [('DOL', 3)],
    'dme_only': [('DME', 2)],
}

_HEALTH_FLOOR = 1.5        # Å 片段间最小间距健全下限
_MARGIN = 1.0             # Å 距盒壁留白


def _norm_rows(v):
    """逐行 2-范数(不用 np.linalg)。v 形 (N,3) → (N,);(3,) → 标量。"""
    v = np.asarray(v, dtype=float)
    return np.sqrt(np.sum(v * v, axis=-1))


def _max_radius(coords, center) -> float:
    """点集相对 center 的最大半径(定球面撒布起始半径与盒装尺度)。"""
    coords = np.asarray(coords, dtype=float)
    if len(coords) == 0:
        return 0.0
    return float(_norm_rows(coords - np.asarray(center, dtype=float)).max())


def _rand_unit(rng):
    """RNG → 单位球面均匀方向(高斯采样归一;退化重采)。不用 np.linalg。"""
    for _ in range(16):
        v = rng.normal(size=3)
        n = float(np.sqrt(np.dot(v, v)))
        if n > 1e-6:
            return v / n
    return np.array([1.0, 0.0, 0.0])


def _rand_rotation(rng):
    """RNG → 随机 3×3 旋转矩阵(Z-Y-X 欧拉角;显式矩阵,不用 np.linalg)。"""
    a, b, c = rng.uniform(0.0, 2.0 * np.pi, size=3)
    ca, sa = np.cos(a), np.sin(a)
    cb, sb = np.cos(b), np.sin(b)
    cc, sc = np.cos(c), np.sin(c)
    rz = np.array([[ca, -sa, 0.0], [sa, ca, 0.0], [0.0, 0.0, 1.0]])
    ry = np.array([[cb, 0.0, sb], [0.0, 1.0, 0.0], [-sb, 0.0, cb]])
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cc, -sc], [0.0, sc, cc]])
    return rz @ ry @ rx


def _min_cross(a, b) -> float:
    """两点集间最小原子-原子距离(向量化)。a:(m,3),b:(n,3)。"""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) == 0 or len(b) == 0:
        return float('inf')
    diff = a[:, None, :] - b[None, :, :]
    return float(np.sqrt(np.sum(diff * diff, axis=2)).min())


def _min_interfragment(coords, fragments) -> float:
    """片段间最小间距(分子内距离不计;健全性判据)。"""
    dmin = float('inf')
    coords = np.asarray(coords, dtype=float)
    for i in range(len(fragments)):
        for j in range(i + 1, len(fragments)):
            d = _min_cross(coords[fragments[i]], coords[fragments[j]])
            if d < dmin:
                dmin = d
    return dmin


def _place_one(rng, geom0, center, core_radius, s_radius, min_sep, box, existing,
               *, max_tries: int = 800):
    """把一个溶剂分子(几何 geom0,COM 已置原点)撒在核周围,满足最小间距 + 在盒内。

    每次尝试:随机取向 + 随机方向的球面位置(半径在 [base_r, r_cap] 内随机),校验候选
    原子与 existing 全体的最小间距 ≥ min_sep 且全在盒内;满足即返回,否则重试。
    尝试耗尽 → None(由调用方报"盒太小/溶剂太多,无法无叠置放")。
    """
    base_r = core_radius + max(float(min_sep), 0.6 * s_radius)
    r_cap = box / 2.0 - _MARGIN - s_radius
    if r_cap < base_r:
        r_cap = base_r
    lo, hi = _MARGIN, box - _MARGIN
    for _ in range(int(max_tries)):
        rot = _rand_rotation(rng)
        d = _rand_unit(rng)
        r = base_r + (rng.uniform(0.0, r_cap - base_r) if r_cap > base_r else 0.0)
        cand = (geom0 @ rot.T) + center + r * d
        if cand.min() < lo or cand.max() > hi:            # 越出盒(留白内)→ 重试
            continue
        if _min_cross(cand, existing) >= min_sep:
            return cand
    return None


def build_solvated_complex(core: str = 'Li2S3',
                           solvents=(('DOL', 2), ('DME', 1)), *,
                           box: float = 18.0, min_sep: float = 2.5,
                           seed: int = 42) -> dict:
    """组装显式溶剂化复合物:核 + 若干溶剂分子 → 立方盒 POSCAR。

    Args:
        core:     核分子名(分子库 key,如 'Li2S3';多硫化锂/S8 等)。
        solvents: [(溶剂名, 个数), ...],如 [('DOL', 2), ('DME', 1)];也可传 SOLVENT_PRESETS
                  的 key(字符串)。
        box:      立方盒边长(Å;核居盒心,溶剂撒于周围)。
        min_sep:  组装时强制的**原子-原子**最小间距(Å;核/已放溶剂与新溶剂之间)。
        seed:     随机种子(固定 → 结构确定);控制取向与球面位置采样。

    Returns:
        ``{'poscar': 文本, 'n_atoms': int, 'note': 中文说明}``。

    Raises:
        ValueError: 未知分子名 / 盒太小或溶剂过多无法无叠置放 / 组装后片段间距 ≤ 1.5 Å。
    """
    if isinstance(solvents, str):
        if solvents not in SOLVENT_PRESETS:
            raise ValueError(f'未知溶剂预设 {solvents!r},可选:{", ".join(SOLVENT_PRESETS)}')
        solvents = SOLVENT_PRESETS[solvents]
    solvents = [(str(nm), int(cnt)) for nm, cnt in solvents]
    if box <= 0:
        raise ValueError(f'盒边长须为正,收到 box={box}')

    rng = np.random.RandomState(int(seed))
    center = np.array([box / 2.0] * 3)

    # 核:质心置盒心
    core_atoms = molecules.molecule_geometry(core)     # 未知名 → ValueError
    core_coords = np.array([xyz for _, xyz in core_atoms], dtype=float)
    core_coords = core_coords - core_coords.mean(axis=0) + center
    els = [el for el, _ in core_atoms]
    coords = [c for c in core_coords]
    fragments = [list(range(len(core_coords)))]
    core_radius = _max_radius(core_coords, center)

    placed_counts = []
    for name, count in solvents:
        for _ in range(count):
            geom = molecules.molecule_geometry(name)
            g_els = [el for el, _ in geom]
            g = np.array([xyz for _, xyz in geom], dtype=float)
            g = g - g.mean(axis=0)                      # COM → 原点
            s_radius = _max_radius(g, np.zeros(3))
            existing = np.array(coords, dtype=float)
            placed = _place_one(rng, g, center, core_radius, s_radius,
                                 float(min_sep), float(box), existing)
            if placed is None:
                raise ValueError(
                    f'无法在 {box:g} Å 盒内为溶剂 {name} 找到满足最小间距 {min_sep:g} Å 的'
                    f'无叠置位(盒太小或溶剂过多);请增大 box 或减少溶剂数')
            start = len(coords)
            for k in range(len(placed)):
                coords.append(placed[k])
                els.append(g_els[k])
            fragments.append(list(range(start, len(coords))))
        placed_counts.append(f'{count}×{name}')

    coords = np.array(coords, dtype=float)
    dmin = _min_interfragment(coords, fragments)
    if dmin <= _HEALTH_FLOOR:
        raise ValueError(
            f'几何不健全:片段间最小间距 {dmin:.2f} Å ≤ {_HEALTH_FLOOR} Å(疑似叠原子)')

    cell = np.diag([float(box)] * 3)
    comment = f'{core} solvated by {" + ".join(placed_counts)} in {box:g} A box (seed={seed})'
    poscar = write_poscar(comment, cell, els, coords, mode='Cartesian',
                          element_order=['Li', 'S', 'C', 'O', 'H'])
    note = (f'显式溶剂化复合物:核 {core} + {" + ".join(placed_counts)};'
            f'盒 {box:g} Å,组装最小间距 {min_sep:g} Å,片段间实测最小间距 {dmin:.2f} Å;'
            f'种子 {seed}(确定性)。初始构型供弛豫/AIMD 起点,非终值。')
    return {'poscar': poscar, 'n_atoms': len(els), 'note': note}
