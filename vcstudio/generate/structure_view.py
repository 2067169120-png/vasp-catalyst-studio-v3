"""结构 3D 预览(C2)——POSCAR/CONTCAR → 笛卡尔坐标 + 分子-衬底间隙分析 + XYZ。

纯函数,零 IO,离线可测。复用 poscar.py 既有件(物种/晶格/完整缩放语义)。
间隙分析口径:

- **分离**:全原子按 z 排序,最大相邻间隙 ≥ ``Z_SPLIT``(1.8 Å)且两侧非空 →
  上方=分子、下方=衬底(与历史实践"骨架/吸附物按 z 分离"一致)。
- **分离成功**:报垂直间隙 + 3D 最近原子对(衬底扩 ±1 **面内**周期像,防分子
  贴胞边漏判);``min_dist`` < GAP_CRASH(1.5)→ crash,< GAP_WARN(2.0)→ warn。
- **分离失败**(纯分子/纯衬底/已撞车粘连):共价半径兜底——任意原子对距离
  < 0.6×(r_i+r_j) 视为原子重叠/过近(S8 撞车 1.24 Å < 0.6×2.10=1.26 命中;
  C-H 键 1.09 Å 不误报)。历史事故标定:目标间隙 ~3.0 Å,撞车实例 1.24/1.396 Å。
"""
from __future__ import annotations

import math

from vcstudio.generate.poscar import (
    parse_poscar_species,
    read_cell_vectors,
    read_scale_factors,
)

# 阈值(Å):历史 S8 撞车事故标定,见模块 docstring。
Z_SPLIT = 1.8       # z 相邻间隙 ≥ 此值才认定分子/衬底可分离
GAP_WARN = 2.0      # 分子-衬底最近对 < 此值 → 偏近(初始结构应 ~3.0)
GAP_CRASH = 1.5     # < 此值 → 撞车红警
CLASH_FACTOR = 0.6  # 共价半径兜底:dist < 0.6×(r_i+r_j) → 重叠

# 共价半径表(Cordero 2008,Å;缺省 1.2)。覆盖本项目体系(C-N 骨架/Li-S/过渡金属)。
COVALENT_RADII = {
    'H': 0.31, 'Li': 1.28, 'B': 0.84, 'C': 0.76, 'N': 0.71, 'O': 0.66,
    'F': 0.57, 'Na': 1.66, 'Mg': 1.41, 'Al': 1.21, 'Si': 1.11, 'P': 1.07,
    'S': 1.05, 'Cl': 1.02, 'K': 2.03, 'Ca': 1.76, 'Sc': 1.70, 'Ti': 1.60,
    'V': 1.53, 'Cr': 1.39, 'Mn': 1.39, 'Fe': 1.32, 'Co': 1.26, 'Ni': 1.24,
    'Cu': 1.32, 'Zn': 1.22, 'Ga': 1.22, 'Ge': 1.20, 'Se': 1.20, 'Br': 1.20,
    'Zr': 1.75, 'Nb': 1.64, 'Mo': 1.54, 'Ru': 1.46, 'Rh': 1.42, 'Pd': 1.39,
    'Ag': 1.45, 'Cd': 1.44, 'Sn': 1.39, 'Sb': 1.39, 'Te': 1.38, 'I': 1.39,
    'Hf': 1.75, 'Ta': 1.70, 'W': 1.62, 'Re': 1.51, 'Os': 1.44, 'Ir': 1.41,
    'Pt': 1.36, 'Au': 1.36, 'Hg': 1.32, 'Pb': 1.46, 'Bi': 1.48,
}
_R_DEFAULT = 1.2

# 重叠全对扫描的规模上限(O(9n²/2) 纯 Python;600 原子实测 ~1s,超过则跳过并 notes 说明)
_FALLBACK_MAX_ATOMS = 600


def parse_positions(content: str) -> dict:
    """POSCAR 文本 → ``{'elements','coords','cell'}``(坐标为笛卡尔 Å,逐原子元素)。

    Direct/其他 → 分数坐标×晶格;首字母 c/C/k/K → 笛卡尔逐分量应用第2行
    缩放因子(VASP 语义，含负值目标体积和三个各向尺度)。
    Selective dynamics 行可选;坐标行只取前 3 列。VASP4(无元素行)/行数不足 →
    ValueError(无元素无法着色出 XYZ)。
    """
    if not content or not content.strip():
        raise ValueError('POSCAR 内容为空')
    syms, counts = parse_poscar_species(content)
    if not syms or not counts or len(syms) != len(counts):
        raise ValueError('无法解析 POSCAR 物种/计数(VASP4 无元素行的文件不支持预览)')
    cell = read_cell_vectors(content)
    lines = content.splitlines()
    scale_x, scale_y, scale_z = read_scale_factors(content)

    # 第 8 行(index 7)起:Selective dynamics(可选)→ 坐标模式行 → 坐标块
    idx = 7
    if idx >= len(lines):
        raise ValueError('POSCAR 行数不足(缺坐标模式行)')
    if lines[idx].strip()[:1].lower() == 's':
        idx += 1
        if idx >= len(lines):
            raise ValueError('POSCAR 行数不足(Selective dynamics 后缺坐标模式行)')
    cartesian = lines[idx].strip()[:1].lower() in ('c', 'k')
    idx += 1

    natoms = sum(counts)
    coords: list[list[float]] = []
    for k in range(natoms):
        if idx + k >= len(lines):
            raise ValueError(f'POSCAR 坐标行数不足(需 {natoms} 行,只有 {k} 行)')
        parts = lines[idx + k].split()
        if len(parts) < 3:
            raise ValueError(f'POSCAR 第 {idx + k + 1} 行不是合法坐标(需 3 个分量)')
        try:
            a, b, c = float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            raise ValueError(f'POSCAR 第 {idx + k + 1} 行坐标无法解析为数值')
        if cartesian:
            coords.append([a * scale_x, b * scale_y, c * scale_z])
        else:
            coords.append([
                a * cell[0][0] + b * cell[1][0] + c * cell[2][0],
                a * cell[0][1] + b * cell[1][1] + c * cell[2][1],
                a * cell[0][2] + b * cell[1][2] + c * cell[2][2],
            ])

    elements: list[str] = []
    for s, n in zip(syms, counts):
        elements.extend([s] * n)
    return {'elements': elements, 'coords': coords, 'cell': cell}


def _dist(p, q) -> float:
    return math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2)


def _rc(elem: str) -> float:
    return COVALENT_RADII.get(elem, _R_DEFAULT)


def _unwrap_z(coords: list[list[float]], cell: list[list[float]]):
    """跨 z 边界回卷检测与展开 → ``(coords2, wrapped:bool)``。

    CONTCAR 常态:slab 底层原子弛豫越过 z=0 被 VASP 回卷到分数 z≈0.99x,
    直接按 z 排序会误把它当"最高的原子"。处理:z 先 mod 归一到 [0,Lz)
    (Lz=cell[2][2],slab 惯例 c 沿 z),在**圆周**上找最大相邻间隙作为切口,
    切口以下的块整体 +Lz,使结构沿 z 连续。展开后周期 c 像不可能比胞内更近
    (像间距 ≥ 切口间隙),后续分析无需再扩 c 像。Lz 无效或 n<2 时原样返回。
    """
    n = len(coords)
    lz = cell[2][2] if len(cell) > 2 and len(cell[2]) > 2 else 0.0
    if n < 2 or lz <= 0:
        return coords, False
    zs = [((c[2] % lz) + lz) % lz for c in coords]
    order = sorted(range(n), key=lambda i: zs[i])
    # 圆周相邻间隙:k=0..n-2 为排序相邻;k=n-1 为回卷间隙(最高 → 最低+Lz)
    best_k = n - 1
    best_gap = zs[order[0]] + lz - zs[order[-1]]
    for k in range(n - 1):
        g = zs[order[k + 1]] - zs[order[k]]
        if g > best_gap:
            best_gap, best_k = g, k
    if best_k == n - 1:
        # 最大间隙正好横跨边界 → 结构本就连续,只做 mod 归一(通常无变化)
        coords2 = [[c[0], c[1], zs[i]] for i, c in enumerate(coords)]
        return coords2, False
    low = set(order[:best_k + 1])
    coords2 = [[c[0], c[1], zs[i] + (lz if i in low else 0.0)]
               for i, c in enumerate(coords)]
    return coords2, True


def _clash_scan(elements: list[str], coords: list[list[float]],
                cell: list[list[float]]):
    """共价半径重叠扫描(±1 面内像)→ ``(dist, i, j)`` 或 None(取最严重比值对)。"""
    n = len(coords)
    a_vec, b_vec = cell[0], cell[1]
    worst = None   # (d, i, j, ratio)
    for i in range(n):
        for j in range(i + 1, n):
            thresh = CLASH_FACTOR * (_rc(elements[i]) + _rc(elements[j]))
            if thresh <= 0:
                continue
            for ia in (-1, 0, 1):
                for ib in (-1, 0, 1):
                    q = [coords[j][0] + ia * a_vec[0] + ib * b_vec[0],
                         coords[j][1] + ia * a_vec[1] + ib * b_vec[1],
                         coords[j][2] + ia * a_vec[2] + ib * b_vec[2]]
                    d = _dist(coords[i], q)
                    ratio = d / thresh
                    if d < thresh and (worst is None or ratio < worst[3]):
                        worst = (d, i, j, ratio)
    return worst[:3] if worst else None


def analyze_gap(elements: list[str], coords: list[list[float]],
                cell: list[list[float]]) -> dict:
    """分子-衬底间隙分析 → 见模块 docstring。绝不抛(输入已由 parse 校验)。

    顺序:①z 回卷展开 ②z 最大间隙分离(成功报垂直间隙/最近对,warn/ok)
    ③共价重叠扫描**无条件**执行(分子内部/骨架内部融合也必须拦),命中压成 crash。
    """
    notes: list[str] = []
    out = {'separated': False, 'vertical_gap': None, 'min_dist': None,
           'pair': None, 'mol_formula': None, 'n_mol': 0, 'n_slab': 0,
           'level': None, 'clash': None, 'notes': notes}
    n = len(coords)
    if n < 2:
        notes.append('原子数不足 2,无间隙可分析')
        return out

    # ── ① 跨 z 边界回卷展开(CONTCAR 常态) ──
    coords, wrapped = _unwrap_z(coords, cell)
    if wrapped:
        notes.append('检测到原子跨 z 边界回卷,已按周期展开后分析')

    # ── ② z 最大相邻间隙 → 分离 ──
    order = sorted(range(n), key=lambda i: coords[i][2])
    gap_at, gap_max = -1, 0.0
    for k in range(n - 1):
        dz = coords[order[k + 1]][2] - coords[order[k]][2]
        if dz > gap_max:
            gap_max, gap_at = dz, k

    if gap_max >= Z_SPLIT and 0 <= gap_at < n - 1:
        slab_idx = order[:gap_at + 1]
        mol_idx = order[gap_at + 1:]
        out['separated'] = True
        out['n_slab'], out['n_mol'] = len(slab_idx), len(mol_idx)
        out['vertical_gap'] = round(gap_max, 4)
        # 分子化学式(元素字母序计数)
        cnt: dict[str, int] = {}
        for i in mol_idx:
            cnt[elements[i]] = cnt.get(elements[i], 0) + 1
        out['mol_formula'] = ''.join(
            f'{e}{c}' for e, c in sorted(cnt.items()))
        # 3D 最近对:衬底扩 ±1 面内周期像(a/b 方向)
        a_vec, b_vec = cell[0], cell[1]
        best, best_pair = None, None
        for i in mol_idx:
            for j in slab_idx:
                for ia in (-1, 0, 1):
                    for ib in (-1, 0, 1):
                        q = [coords[j][0] + ia * a_vec[0] + ib * b_vec[0],
                             coords[j][1] + ia * a_vec[1] + ib * b_vec[1],
                             coords[j][2] + ia * a_vec[2] + ib * b_vec[2]]
                        d = _dist(coords[i], q)
                        if best is None or d < best:
                            best, best_pair = d, (i, j)
        out['min_dist'] = round(best, 4)
        out['pair'] = {'i': best_pair[0], 'j': best_pair[1],
                       'elem_i': elements[best_pair[0]],
                       'elem_j': elements[best_pair[1]]}
        # 注:分离成立时 min_dist ≥ dz ≥ Z_SPLIT(1.8)> GAP_CRASH,故此处只有 warn/ok;
        # 撞车级问题由 ③ 的重叠扫描统一兜住。
        if best < GAP_WARN:
            out['level'] = 'warn'
            notes.append(f'分子-衬底最近距离 {best:.2f} Å 偏近(初始结构建议 ~3.0 Å;'
                         f'弛豫后成键属正常)')
        else:
            out['level'] = 'ok'
    else:
        notes.append('未能按 z 分离分子/衬底(可能为纯分子、纯衬底或已粘连)')

    # ── ③ 共价重叠扫描:无条件(分子内部/骨架内部融合也要拦) ──
    if n > _FALLBACK_MAX_ATOMS:
        notes.append(f'原子数 {n} 超过 {_FALLBACK_MAX_ATOMS},跳过原子重叠扫描')
        return out
    hit = _clash_scan(elements, coords, cell)
    if hit is not None:
        d, i, j = hit
        out['clash'] = {'dist': round(d, 4), 'i': i, 'j': j,
                        'elem_i': elements[i], 'elem_j': elements[j]}
        out['level'] = 'crash'
        notes.append(f'{elements[i]}-{elements[j]} 距离 {d:.2f} Å 低于共价判据'
                     f'——疑似原子重叠/过近,提交前必须修正')
        if not out['separated']:
            # 分离失败时以重叠对充当"最近对"报告(既有语义)
            out['min_dist'] = round(d, 4)
            out['pair'] = {'i': i, 'j': j,
                           'elem_i': elements[i], 'elem_j': elements[j]}
    return out


def structure_view(content: str) -> dict:
    """POSCAR/CONTCAR 文本 → ``{'xyz','natoms','formula','gap','notes'}``。

    解析失败 raise ValueError(api 层兜成结构化 error)。
    """
    p = parse_positions(content)
    elements, coords = p['elements'], p['coords']
    syms, counts = parse_poscar_species(content)
    formula = ' '.join(f'{s}{c}' for s, c in zip(syms, counts))
    xyz_lines = [str(len(elements)), 'vcstudio structure preview']
    for e, (x, y, z) in zip(elements, coords):
        xyz_lines.append(f'{e} {x:.6f} {y:.6f} {z:.6f}')
    gap = analyze_gap(elements, coords, p['cell'])
    notes: list[str] = []
    if len(elements) > 5000:
        notes.append(f'原子数 {len(elements)} 较大,渲染降为仅棍状')
    return {'xyz': '\n'.join(xyz_lines), 'natoms': len(elements),
            'formula': formula, 'gap': gap, 'notes': notes}
