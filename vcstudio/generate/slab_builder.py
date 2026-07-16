"""slab 结构工具(纯函数,复用 poscar.py / structure_view.py 解析,不重复造 parser)。

面向催化 slab 建模的常见需求 + 续算前几何健全检查:
- fix_bottom_layers:按 z 分层,冻结最底 n 层(Selective dynamics F F F),其余放开 T T T。
- vacuum_thickness / count_layers:真空层厚度与层数(建模自检 / advisor 真空规则)。
- min_interatomic_distance:周期最小镜像最近原子间距(续算前防几何病态)。

"层"的定义:原子按笛卡尔 z 升序,相邻 z 间隙 ≤ LAYER_TOL(默认 0.5 Å)归为同一层
(slab 惯例 c 沿 z)。坐标/晶格一律走 poscar.read_cell_vectors 与 structure_view.parse_positions,
与结构预览同源;负缩放因子沿用其显式拒绝语义。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math

from vcstudio.generate.poscar import read_cell_vectors
from vcstudio.generate.structure_view import parse_positions

LAYER_TOL = 0.5   # Å:相邻 z 间隙 ≤ 此值视为同一原子层


def _z_coords(poscar_text: str) -> list:
    """POSCAR 文本 → 每原子笛卡尔 z 列表(复用 parse_positions,不另造 parser)。"""
    return [c[2] for c in parse_positions(poscar_text)['coords']]


def _layer_index(zs: list, tol: float = LAYER_TOL):
    """笛卡尔 z 列表 → (每原子层号 layer_of, 层数 n_layers)。

    原子按 z 升序,相邻间隙 > tol 处切层;最底层(最小 z)层号 0。layer_of 顺序对齐输入原子。
    """
    n = len(zs)
    layer_of = [0] * n
    order = sorted(range(n), key=lambda i: zs[i])
    cur = 0
    for k in range(1, n):
        if zs[order[k]] - zs[order[k - 1]] > tol:
            cur += 1
        layer_of[order[k]] = cur
    return layer_of, cur + 1


def count_layers(poscar_text: str) -> int:
    """按 z 聚类(容差 LAYER_TOL)的原子层数;无原子 → 0。"""
    zs = _z_coords(poscar_text)
    return _layer_index(zs)[1] if zs else 0


def vacuum_thickness(poscar_text: str) -> float:
    """c 方向真空层厚度(Å)= |c| − (z_max − z_min)。

    |c| 取第三晶格矢量长度,(z_max−z_min) 为原子笛卡尔 z 跨度(slab 惯例 c 沿 z,
    与 structure_view 同一口径)。无原子 → |c|(整胞皆真空)。
    """
    cell = read_cell_vectors(poscar_text)
    c_len = math.sqrt(sum(x * x for x in cell[2]))
    zs = _z_coords(poscar_text)
    return c_len if not zs else c_len - (max(zs) - min(zs))


def fix_bottom_layers(poscar_text: str, n_layers: int) -> str:
    """冻结最底 n 层原子(Selective dynamics F F F),其余放开(T T T),返回新 POSCAR 文本。

    - 按 z 聚类分层(容差 LAYER_TOL);n_layers 指从最底部起要冻结的层数。
    - 已有 Selective dynamics 的只改每原子标志;无则插入 'Selective dynamics' 行并给全部
      坐标行补三标志。Direct/Cartesian 均支持(坐标数值原样保留,只动标志)。
    - 坐标块之后的尾部行(如速度块)原样透传。
    - n_layers ≤ 0 或 ≥ 总层数 → ValueError(中文):没有可冻结的底层,或会冻结整块无原子弛豫。
    """
    zs = _z_coords(poscar_text)
    layer_of, total_layers = _layer_index(zs) if zs else ([], 0)
    if n_layers <= 0:
        raise ValueError(f'冻结层数须为正整数,收到 {n_layers}')
    if n_layers >= total_layers:
        raise ValueError(
            f'冻结层数 {n_layers} ≥ 总层数 {total_layers},将冻结整个 slab;'
            f'请给小于总层数的值(至少留 1 层弛豫)')

    lines = poscar_text.splitlines()
    # 定位坐标块起点(与 parse_positions 同一口径:第 8 行可选 Selective dynamics 行)
    has_sd = len(lines) > 7 and lines[7].strip()[:1].lower() == 's'
    mode_idx = 8 if has_sd else 7
    coord_start = mode_idx + 1
    natoms = len(zs)

    out = list(lines[:7])                 # 注释 / 缩放 / 三矢量 / 元素 / 计数
    out.append('Selective dynamics')      # 规范化写入(替换旧行或新增)
    out.append(lines[mode_idx])           # Direct/Cartesian 模式行原样保留
    for k in range(natoms):
        parts = lines[coord_start + k].split()
        flags = 'F F F' if layer_of[k] < n_layers else 'T T T'
        out.append(f'  {parts[0]} {parts[1]} {parts[2]}  {flags}')
    out.extend(lines[coord_start + natoms:])   # 尾部行(速度块等)透传
    return '\n'.join(out) + '\n'


# ── 续算前几何健全检查:周期最小镜像最近原子间距 ─────────────────────────────────
def _inv3x3(m: list) -> list:
    """3×3 矩阵求逆(纯 Python,避免引入 numpy;晶格必可逆)。奇异 → ValueError。"""
    (a, b, c), (d, e, f), (g, h, i) = m[0], m[1], m[2]
    A = e * i - f * h
    B = f * g - d * i
    C = d * h - e * g
    det = a * A + b * B + c * C
    if abs(det) < 1e-12:
        raise ValueError('晶格矢量退化(行列式≈0),无法求周期最小镜像')
    inv_det = 1.0 / det
    return [
        [A * inv_det, (c * h - b * i) * inv_det, (b * f - c * e) * inv_det],
        [B * inv_det, (a * i - c * g) * inv_det, (c * d - a * f) * inv_det],
        [C * inv_det, (b * g - a * h) * inv_det, (a * e - b * d) * inv_det],
    ]


def min_interatomic_distance(poscar_text: str) -> float:
    """周期最小镜像下的最近原子间距(Å);原子数 < 2 → inf(无对可比)。

    口径:两原子分数坐标差先 wrap 到 [−0.5, 0.5) 取最近镜像,转笛卡尔求模;再对该 wrap 结果的
    ±1 邻域镜像(3×3×3)取最小,保证**非正交胞**也拿到真正最小镜像(纯 wrap 只对正交胞严格,
    斜胞需搜邻域)。O(N²) 全对扫描,催化体系规模足够。续算前据此判 CONTCAR 是否原子重叠。
    """
    p = parse_positions(poscar_text)
    coords, cell = p['coords'], p['cell']
    n = len(coords)
    if n < 2:
        return float('inf')
    inv = _inv3x3(cell)
    # 笛卡尔 → 分数:frac_j = Σ_k cart_k · inv[k][j](cell 行为晶格矢量,cart = frac·cell)
    fracs = [[c[0] * inv[0][j] + c[1] * inv[1][j] + c[2] * inv[2][j] for j in range(3)]
             for c in coords]
    a_vec, b_vec, c_vec = cell[0], cell[1], cell[2]
    best2 = float('inf')
    for i in range(n):
        fi = fracs[i]
        for j in range(i + 1, n):
            fj = fracs[j]
            df = [fi[k] - fj[k] for k in range(3)]
            df = [d - math.floor(d + 0.5) for d in df]     # wrap 到 [−0.5, 0.5)
            for na in (-1, 0, 1):
                fa = df[0] + na
                for nb in (-1, 0, 1):
                    fb = df[1] + nb
                    for nc in (-1, 0, 1):
                        fc = df[2] + nc
                        dx = fa * a_vec[0] + fb * b_vec[0] + fc * c_vec[0]
                        dy = fa * a_vec[1] + fb * b_vec[1] + fc * c_vec[1]
                        dz = fa * a_vec[2] + fb * b_vec[2] + fc * c_vec[2]
                        d2 = dx * dx + dy * dy + dz * dz
                        if d2 < best2:
                            best2 = d2
    return math.sqrt(best2)
