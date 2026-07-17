"""SAC 基底模板库(F1)——石墨烯超胞 + 六类 M-Nx 配位位点的确定性构造。

面向张洪毅论文 12 体系的建模入口:在石墨烯超胞中心挖单/双空位、近邻 C 替换为
N/P/S/B、金属置于空位中心并沿 z 抬升作**初猜(非终值)**(文献惯例,须弛豫),
产出可直接送弛豫的 POSCAR + 下游可用的 site_indices(金属/配位原子索引)。

几何全为规则蜂窝格上的确定性构造(不引入 pymatgen/ASE——那是 Phase C 的事):
- 蜂窝格:六方晶格 a1=a(1,0),a2=a(1/2,√3/2),两套子晶格 A=(0,0)、B=(1/3,1/3);
  最近邻 C-C = a/√3(a=2.468 → 1.4249 Å)。
- 邻接分析:周期最近镜像(面内 ±1 像)判近邻,rmax=1.7 Å 只取三个最近邻。
- 空位/替换/抬升按模板规则确定性施加,金属抬升默认 0.4 Å(范围 0.3–0.5)。

本模块同时提供全新的 POSCAR **写出**工具(write_poscar / 分数-笛卡尔互转),供
molecules.py / sites.py / references.py 复用(poscar.py 只有解析,不动它)。
纯 python+numpy;中文注释,英文标识符。
"""
from __future__ import annotations

import math

import numpy as np

GRAPHENE_A = 2.468          # Å 石墨烯面内晶格常数(C-C = a/√3 ≈ 1.4249 Å)
DEFAULT_VACUUM = 20.0       # Å 真空层(z 方向,z 居中)
DEFAULT_METAL_LIFT = 0.4    # Å 金属相对石墨烯平面的 z 抬升初猜(范围 0.3–0.5,非终值)
_NN_RMAX = 1.7              # Å 最近邻判据上限(> C-C 1.425,< 次近邻 2.468)

# 模板 → (空位类型, 配位数, 首配位替换元素 或 None, 是否二壳掺 B)
# 'divac' 双空位(4 配位);'monovac' 单空位(3 配位)。
_TEMPLATES = {
    'MN4':   ('divac',   4, None, False),
    'MN3':   ('monovac', 3, None, False),
    'MP1N3': ('divac',   4, 'P',  False),
    'MS1N3': ('divac',   4, 'S',  False),
    'MB1N3': ('divac',   4, 'B',  False),
    'MN4+B': ('divac',   4, None, True),
}


# ── 纯 numpy 向量/矩阵工具(**刻意不碰 np.linalg**) ───────────────────────────
# np.linalg 是 numpy 的惰性子模块(numpy.__getattr__ 首访即 import numpy.linalg)。
# 测试全套跑时 test_kpoints 会把 'numpy' 从 sys.modules pop 掉(延迟依赖测试),此后
# 首次 np.linalg 访问会重载 numpy、污染 sys.modules,击穿 native_charts 的 setdefault
# 修复引发 RecursionError。故本模块的模、逆一律用非惰性算子(np.dot/np.sqrt)+ 手写
# 3×3 伴随矩阵求逆,绝不触发惰性子模块加载。
def _norm(v):
    """向量 2-范数(不用 np.linalg)。"""
    v = np.asarray(v, dtype=float)
    return float(np.sqrt(np.dot(v, v)))


def _inv3(m):
    """3×3 矩阵求逆(伴随矩阵/行列式;不用 np.linalg)。奇异 → ValueError。"""
    m = np.asarray(m, dtype=float)
    (a, b, c), (d, e, f), (g, h, i) = m
    ca, cb, cc = e * i - f * h, f * g - d * i, d * h - e * g
    det = a * ca + b * cb + c * cc
    if abs(det) < 1e-12:
        raise ValueError('晶格矢量退化(行列式≈0),无法求逆')
    return np.array([[ca, c * h - b * i, b * f - c * e],
                     [cb, a * i - c * g, c * d - a * f],
                     [cc, b * g - a * h, a * e - b * d]], dtype=float) / det


# ── POSCAR 写出工具(全新;poscar.py 仅解析,本处补写出能力) ────────────────────
def _species_order(elements, element_order=None):
    """元素去重保持首次出现顺序;element_order 指定的优先靠前,其余按首现补后。"""
    seen: list[str] = []
    for el in elements:
        if el not in seen:
            seen.append(el)
    if element_order:
        head = [e for e in element_order if e in seen]
        tail = [e for e in seen if e not in head]
        return head + tail
    return seen


def _grouping(elements, element_order=None):
    """(elements 构造序) → (species, counts, perm)。

    perm[new_index] = old_index(构造序下标),即按元素分组后的原子重排。
    counts 与 species 对齐,供 POSCAR 计数行。
    """
    species = _species_order(elements, element_order)
    perm: list[int] = []
    for el in species:
        for i, e in enumerate(elements):
            if e == el:
                perm.append(i)
    counts = [sum(1 for e in elements if e == el) for el in species]
    return species, counts, perm


def write_poscar(comment, cell, elements, coords, *, mode='Cartesian',
                 element_order=None, scale=1.0):
    """写出合法 VASP5 POSCAR 文本(元素分组、计数正确、可被 poscar 解析器往返读回)。

    - elements / coords 为**构造序**并行列表;内部按元素分组(_grouping)重排后写出。
    - mode='Cartesian' → coords 为笛卡尔 Å;'Direct' → coords 为分数坐标。
    - element_order 可指定物种书写优先序(如金属置首),未列元素按首现补后。
    """
    species, counts, perm = _grouping(list(elements), element_order)
    cell = np.asarray(cell, dtype=float)
    coords = np.asarray(coords, dtype=float)
    lines = [str(comment), f'{scale:.10g}']
    for vec in cell:
        lines.append(f'  {vec[0]:.10f} {vec[1]:.10f} {vec[2]:.10f}')
    lines.append('  ' + ' '.join(species))
    lines.append('  ' + ' '.join(str(c) for c in counts))
    lines.append(mode)
    for oi in perm:
        x, y, z = coords[oi]
        lines.append(f'  {x:.10f} {y:.10f} {z:.10f}')
    return '\n'.join(lines) + '\n'


def cart_to_frac(cart, cell):
    """笛卡尔坐标 → 分数坐标(cell 行为晶格矢量,cart = frac·cell)。支持 (3,) 或 (N,3)。"""
    return np.asarray(cart, dtype=float) @ _inv3(cell)


def frac_to_cart(frac, cell):
    """分数坐标 → 笛卡尔坐标。支持 (3,) 或 (N,3)。"""
    return np.asarray(frac, dtype=float) @ np.asarray(cell, dtype=float)


# ── 石墨烯超胞 ────────────────────────────────────────────────────────────────
def _graphene_lattice(nx, ny, a, vacuum):
    """构造 nx×ny 石墨烯超胞 → (elements, coords[N,3] 笛卡尔, cell[3,3])。z 居中。"""
    a1 = np.array([a, 0.0, 0.0])
    a2 = np.array([a * 0.5, a * math.sqrt(3.0) / 2.0, 0.0])
    basis_b = (a1 + a2) / 3.0          # B 子晶格 = 分数 (1/3, 1/3)
    z = vacuum / 2.0                   # z 居中
    coords = []
    for i in range(nx):
        for j in range(ny):
            base = i * a1 + j * a2
            coords.append([base[0], base[1], z])                       # A
            bb = base + basis_b
            coords.append([bb[0], bb[1], z])                           # B
    coords = np.array(coords, dtype=float)
    elements = ['C'] * len(coords)
    cell = np.array([nx * a1, ny * a2, [0.0, 0.0, vacuum]], dtype=float)
    return elements, coords, cell


def graphene_supercell(nx=4, ny=4, *, a=GRAPHENE_A, vacuum=DEFAULT_VACUUM):
    """石墨烯 nx×ny 超胞 POSCAR 文本(蜂窝格,z 居中,原子序稳定,笛卡尔写出)。

    原子数 = 2·nx·ny;最近邻 C-C = a/√3。
    """
    if nx < 1 or ny < 1:
        raise ValueError(f'超胞尺寸须为正整数,收到 nx={nx}, ny={ny}')
    elements, coords, cell = _graphene_lattice(nx, ny, a, vacuum)
    return write_poscar(
        f'graphene {nx}x{ny} supercell (a={a} A, vacuum={vacuum} A)',
        cell, elements, coords, mode='Cartesian')


# ── 邻接分析(周期面内最近镜像) ───────────────────────────────────────────────
def _min_image_pos(coords, cell, j, shift):
    return coords[j] + shift[0] * cell[0] + shift[1] * cell[1]


def _neighbors(coords, cell, rmax=_NN_RMAX):
    """每原子的近邻表:[(j, dist, (ia,ib)), ...](面内 ±1 像最近镜像,按距离升序)。"""
    n = len(coords)
    out: list[list] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            best = None
            for ia in (-1, 0, 1):
                for ib in (-1, 0, 1):
                    pos = coords[j] + ia * cell[0] + ib * cell[1]
                    d = _norm(pos - coords[i])
                    if d <= rmax and (best is None or d < best[0]):
                        best = (d, (ia, ib))
            if best is not None:
                out[i].append((j, best[0], best[1]))
        out[i].sort(key=lambda t: (t[1], t[0]))
    return out


# ── SAC 构造 ─────────────────────────────────────────────────────────────────
def build_sac(template='MN4', metal='Fe', *, nx=4, ny=4, vacuum=DEFAULT_VACUUM,
              a=GRAPHENE_A, lift=DEFAULT_METAL_LIFT):
    """构造单原子催化剂位点 → {'poscar','description','site_indices'}。

    site_indices = {'metal': int, 'coord': [int,...], 'metal_element': str
                    (, 'dopant': int 仅 MN4+B)},索引为最终 POSCAR(分组后)下标。

    六类模板(张洪毅论文):
    - MN4  双空位 + 4N 配位;   MN3 单空位 + 3N 配位。
    - MP1N3/MS1N3/MB1N3 双空位 4 配位,其中 1 个 N 换成 P/S/B。
    - MN4+B 双空位 4N 配位,近邻(二壳)掺 1 个 B。
    金属置空位中心并沿 z 抬升 lift Å 作初猜(非终值)。
    """
    if template not in _TEMPLATES:
        raise ValueError(f'未知 SAC 模板 {template!r};可选:{", ".join(_TEMPLATES)}')
    vac_kind, ncoord, first_sub, dope_b = _TEMPLATES[template]

    elements, coords, cell = _graphene_lattice(nx, ny, a, vacuum)
    nbrs = _neighbors(coords, cell)
    plane_z = vacuum / 2.0
    center_xy = coords[:, :2].mean(axis=0)
    c1 = int(np.argmin(np.sqrt(((coords[:, :2] - center_xy) ** 2).sum(axis=1))))

    removed: set[int] = set()
    coord_idx: list[int] = []
    if vac_kind == 'monovac':
        removed.add(c1)
        coord_idx = [j for (j, _d, _sh) in nbrs[c1]][:ncoord]
        metal_xy = coords[c1][:2]
    else:  # divac:选一个近邻 c2 使 c1-c2 中点最靠近中心(双空位居中)
        best = None
        for (j, _d, sh) in nbrs[c1]:
            pos_j = _min_image_pos(coords, cell, j, sh)
            mid = (coords[c1] + pos_j) / 2.0
            dd = _norm(mid[:2] - center_xy)
            if best is None or dd < best[0]:
                best = (dd, j, pos_j)
        _, c2, c2_pos = best
        removed.update((c1, c2))
        # 4 配位 = c1 近邻(除 c2)∪ c2 近邻(除 c1),去重、剔除已删
        cand: list[int] = []
        for src in (c1, c2):
            for (j, _d, _sh) in nbrs[src]:
                if j not in removed and j not in cand:
                    cand.append(j)
        coord_idx = cand[:ncoord]
        metal_xy = ((coords[c1] + c2_pos) / 2.0)[:2]

    # 配位元素:默认 N;首配位可换 P/S/B(MP1N3/MS1N3/MB1N3)。coord 按索引排序保稳定。
    coord_sorted = sorted(coord_idx)
    subst: dict[int, str] = {}
    for k, idx in enumerate(coord_sorted):
        subst[idx] = first_sub if (k == 0 and first_sub is not None) else 'N'

    dopant_idx = None
    if dope_b:  # 二壳:配位 N 的近邻 C(非配位、未删),取最靠近中心者 → B
        coord_set = set(coord_sorted)
        best_c = None
        for nidx in coord_sorted:
            for (j, _d, _sh) in nbrs[nidx]:
                if j in removed or j in coord_set:
                    continue
                dd = _norm(coords[j][:2] - center_xy)
                if best_c is None or dd < best_c[1]:
                    best_c = (j, dd)
        if best_c is not None:
            dopant_idx = best_c[0]
            subst[dopant_idx] = 'B'

    # 组装:存活原子(原序,施加替换)+ 末尾金属原子
    build_elems: list[str] = []
    build_coords: list = []
    old_to_build: dict[int, int] = {}
    for i in range(len(coords)):
        if i in removed:
            continue
        old_to_build[i] = len(build_elems)
        build_elems.append(subst.get(i, 'C'))
        build_coords.append(coords[i])
    metal_build = len(build_elems)
    build_elems.append(metal)
    build_coords.append(np.array([metal_xy[0], metal_xy[1], plane_z + lift]))
    build_coords = np.array(build_coords, dtype=float)

    element_order = [metal, 'N', 'P', 'S', 'B', 'C']
    _species, _counts, perm = _grouping(build_elems, element_order)
    new_of_build = {perm[k]: k for k in range(len(perm))}

    site_indices = {
        'metal': new_of_build[metal_build],
        'coord': sorted(new_of_build[old_to_build[i]] for i in coord_sorted),
        'metal_element': metal,
    }
    if dopant_idx is not None:
        site_indices['dopant'] = new_of_build[old_to_build[dopant_idx]]

    text = write_poscar(
        f'{metal}@{template} SAC on graphene {nx}x{ny}',
        cell, build_elems, build_coords, mode='Cartesian',
        element_order=element_order)
    kind_cn = '双空位' if vac_kind == 'divac' else '单空位'
    desc = (f'{metal}@{template}:{kind_cn} {ncoord} 配位;金属置空位中心并沿 z '
            f'抬升 {lift} Å 作初猜(**非终值**,文献惯例,须结构弛豫)。')
    return {'poscar': text, 'description': desc, 'site_indices': site_indices}


def sac_matrix(metals, templates, **kw):
    """金属 × 模板矩阵批量生成(引擎侧"周期表换金属")。

    返回 list[{'name': 'Fe@MN4', 'metal','template','poscar','description','site_indices'}],
    命名规范 M@T。顺序:外层金属、内层模板。
    """
    out = []
    for metal in metals:
        for template in templates:
            res = dict(build_sac(template, metal, **kw))
            res['name'] = f'{metal}@{template}'
            res['metal'] = metal
            res['template'] = template
            out.append(res)
    return out
