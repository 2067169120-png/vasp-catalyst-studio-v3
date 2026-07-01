"""POSCAR 解析:物种/计数 + 晶格矢量(含缩放因子修正) + 读文件。

- `parse_poscar_species`:位置格式,第6行元素、第7行计数(VASP5);VASP4/畸形降级空。
  (逐字复用自 E: layer2_science/poscar_utils.py 的单一来源实现。)
- `read_cell_vectors`:第2行缩放因子 × 第3-5行矢量。**修正 E: convergence.py 内联解析
  漏乘缩放因子的隐性缺陷。** 负缩放(=目标体积)M1 用 factor=1.0 占位(TODO)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os


def _is_int_token(tok: str) -> bool:
    return tok.lstrip('+-').isdigit()


def parse_poscar_species(content: str) -> tuple[list[str], list[int]]:
    """从 POSCAR 文本解析 ``(elements, counts)``(POSCAR 物种顺序)。

    - VASP5(第6行是元素符号):返回 (元素列表, 计数列表)。
    - VASP4(第6行是数字)或文本过短/畸形:返回 ``([], [])``,调用方据此降级。

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


def read_cell_vectors(content: str) -> list[list[float]]:
    """解析 3×3 晶格矢量(已乘缩放因子)。

    POSCAR 结构:line0=注释,line1=缩放因子,line2-4=三个晶格矢量。
    - 正缩放:每个矢量分量 × scale(标准 VASP 语义)。
    - 负缩放:|scale| 为目标体积(Å³)。M1 用 factor=1.0 占位(TODO:体积反解 factor)。
    行数不足或矢量列数不足 → ValueError。
    """
    lines = content.splitlines()
    if len(lines) < 5:
        raise ValueError('POSCAR 行数不足以解析晶格矢量(需至少 5 行)')
    try:
        scale = float(lines[1].split()[0])
    except (IndexError, ValueError):
        raise ValueError('POSCAR 第2行不是合法缩放因子')

    raw: list[list[float]] = []
    for i in (2, 3, 4):
        parts = lines[i].split()
        if len(parts) < 3:
            raise ValueError(f'POSCAR 第{i + 1}行不是合法晶格矢量(需 3 个分量)')
        raw.append([float(x) for x in parts[:3]])

    factor = scale if scale > 0 else 1.0  # TODO(M1): 负 scale=目标体积,后续按 det 反解
    return [[c * factor for c in v] for v in raw]


def read_poscar(path: str | os.PathLike) -> str:
    """读取 POSCAR 文件文本(UTF-8)。"""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()
