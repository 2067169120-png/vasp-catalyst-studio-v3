"""POSCAR 解析:物种/计数 + 晶格矢量(含完整缩放语义) + 读文件。

- `parse_poscar_species`:位置格式,第6行元素、第7行计数(VASP5);VASP4/畸形降级空。
  (逐字复用自 E: layer2_science/poscar_utils.py 的单一来源实现。)
- `read_scale_factors`:支持 VASP 第2行的一或三个数；单个负数按目标体积反解统一尺度。
- `read_cell_vectors`:把上述尺度正确应用到第3-5行矢量。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os


def _is_int_token(tok: str) -> bool:
    return tok.lstrip('+-').isdigit()


def parse_poscar_species(content: str) -> tuple[list[str], list[int]]:
    """从 POSCAR 文本解析 ``(elements, counts)``(POSCAR 物种顺序)。

    - VASP5(第6行是元素符号):返回 (元素列表, 计数列表)。
    - VASP4(第6行是数字)或文本过短:返回 ``([], [])``,调用方完全降级。
    - 元素行有效但计数行无法解析:返回 (元素列表, [])。**有意保留物种**——仍可
      拼 POTCAR / 推荐 KPOINTS,仅 counts 降级(不写 MAGMOM),比丢弃元素更有用。

    位置解析,不删除行(删内部空行会移位)。可容忍尾部空行。元素顺序即
    POTCAR 拼接顺序与 MAGMOM 顺序。
    """
    if not content:
        return [], []
    lines = content.splitlines()
    if len(lines) < 7:
        return [], []

    sym_tokens = lines[5].split()
    if not sym_tokens:
        return [], []
    if all(_is_int_token(t) for t in sym_tokens):
        # VASP4:第6行是数字(无元素符号行)→ 物种不可知,完全降级返回空。
        return [], []

    cnt_tokens = lines[6].split()
    try:
        counts = [int(t) for t in cnt_tokens]
    except ValueError:
        counts = []
    return sym_tokens, counts


def _raw_cell_and_scale_factors(
        content: str) -> tuple[list[list[float]], tuple[float, float, float]]:
    """解析未缩放晶格与 VASP 实际 Cartesian 分量尺度。

    VASP 第2行允许两种格式：

    - 一个非零数：正数是统一尺度；负数的绝对值是目标晶胞体积，统一尺度由
      ``(|scale| / |det(raw_cell)|) ** (1/3)`` 反解；
    - 三个正数：分别缩放 x/y/z Cartesian 分量，晶格与 Cartesian 原子坐标
      使用同一组三分量尺度。

    返回 ``(raw_cell, (sx, sy, sz))``。非法数量、非有限值、零尺度或退化晶格
    一律显式报错，避免生成表面可读但几何错误的结构。
    """
    lines = content.splitlines()
    if len(lines) < 5:
        raise ValueError('POSCAR 行数不足以解析晶格矢量(需至少 5 行)')

    tokens = lines[1].split()
    if len(tokens) not in (1, 3):
        raise ValueError('POSCAR 第2行缩放因子须包含 1 个或 3 个数值')
    try:
        scales = [float(token) for token in tokens]
    except ValueError as exc:
        raise ValueError('POSCAR 第2行不是合法缩放因子') from exc
    if not all(math.isfinite(value) for value in scales):
        raise ValueError('POSCAR 缩放因子须为有限数值')

    raw: list[list[float]] = []
    for i in (2, 3, 4):
        parts = lines[i].split()
        if len(parts) < 3:
            raise ValueError(f'POSCAR 第{i + 1}行不是合法晶格矢量(需 3 个分量)')
        try:
            vector = [float(x) for x in parts[:3]]
        except ValueError as exc:
            raise ValueError(f'POSCAR 第{i + 1}行晶格矢量含非数值分量') from exc
        if not all(math.isfinite(value) for value in vector):
            raise ValueError(f'POSCAR 第{i + 1}行晶格矢量须为有限数值')
        raw.append(vector)

    if len(scales) == 3:
        if any(value <= 0 for value in scales):
            raise ValueError('POSCAR 的三个分量缩放因子必须全部为正数')
        return raw, (scales[0], scales[1], scales[2])

    scale = scales[0]
    if scale == 0:
        raise ValueError('POSCAR 缩放因子为 0(无物理意义)')
    if scale > 0:
        return raw, (scale, scale, scale)

    # 单个负数按 VASP 语义表示目标晶胞体积(Å³)，不是负的长度尺度。
    (a, b, c), (d, e, f), (g, h, i) = raw
    determinant = (
        a * (e * i - f * h)
        - b * (d * i - f * g)
        + c * (d * h - e * g)
    )
    raw_volume = abs(determinant)
    if raw_volume <= 1e-15:
        raise ValueError('POSCAR 晶格退化，无法按目标体积反解负缩放因子')
    factor = (abs(scale) / raw_volume) ** (1.0 / 3.0)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError('POSCAR 目标体积无法得到合法缩放因子')
    return raw, (factor, factor, factor)


def read_scale_factors(content: str) -> tuple[float, float, float]:
    """返回应用于晶格和 Cartesian 原子坐标的 ``(sx, sy, sz)``。"""
    _raw, factors = _raw_cell_and_scale_factors(content)
    return factors


def read_cell_vectors(content: str) -> list[list[float]]:
    """解析 3×3 晶格矢量(已应用 VASP 第2行缩放语义)。

    POSCAR 结构:line0=注释,line1=缩放因子,line2-4=三个晶格矢量。
    - 单个正数:所有分量乘统一尺度。
    - 单个负数:绝对值为目标体积(Å³)，按原始行列式反解统一尺度。
    - 三个正数:分别缩放 x/y/z Cartesian 分量。
    行数不足或矢量列数不足 → ValueError。
    """
    raw, factors = _raw_cell_and_scale_factors(content)
    return [[component * factors[j] for j, component in enumerate(vector)]
            for vector in raw]


def read_poscar(path: str | os.PathLike) -> str:
    """读取 POSCAR 文件文本(UTF-8)。"""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()
