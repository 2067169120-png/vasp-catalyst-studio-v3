"""吸附位点枚举与吸附质摆放(F5)——SAC 语义位点 + 分子锚定/取向/旋转采样。

- enumerate_sac_sites:据 site_indices 产出 SAC 语义位点(金属顶位 / 配位原子顶位 /
  金属-配位桥位 / 空位中心 hollow),位置以**分数坐标**给出。
- place_adsorbate:分子锚定原子(S 端朝金属 / Li 端朝 N 的双端启发式,orientation 控制)
  置于位点上方 height Å,body 朝 +z,绕 z 采样 rotations 生成多构型;**分子-表面最近距离
  检查**(<1.5 Å 拒绝并说明);合并原子写出合法 POSCAR(元素分组计数正确、可往返解析)。
- adsorption_batch:位点 × 分子 × 取向的构型族,命名 {mol}@{site}_r{deg}。

纯 python+numpy;中文注释,英文标识符。
"""
from __future__ import annotations

import math

import numpy as np

from vcstudio.generate.molecules import molecule_geometry
from vcstudio.generate.sac_builder import cart_to_frac, frac_to_cart, write_poscar
from vcstudio.generate.structure_view import parse_positions

MIN_SURFACE_DIST = 1.5     # Å 分子-表面最近原子间距下限(< 此值拒绝该构型)


def _norm(v):
    # 刻意不用 np.linalg(惰性子模块;见 sac_builder 说明,避免重载 numpy 污染 sys.modules)
    v = np.asarray(v, dtype=float)
    return float(np.sqrt(np.dot(v, v)))


# ── 位点枚举 ─────────────────────────────────────────────────────────────────
def enumerate_sac_sites(poscar_text, site_indices):
    """SAC 语义位点 → list[{'name','position'(分数坐标),'kind'}]。

    位点:top_metal(金属顶位)、top_<配位原子>(配位顶位)、
    bridge_<金属>-<配位>(金属-配位桥位)、hollow(配位原子几何中心,近空位中心)。
    """
    p = parse_positions(poscar_text)
    coords = np.array(p['coords'], dtype=float)
    cell = np.array(p['cell'], dtype=float)
    els = p['elements']
    metal = site_indices['metal']
    coord = list(site_indices['coord'])

    sites = []

    def _add(name, cart, kind):
        frac = cart_to_frac(cart, cell)
        sites.append({'name': name, 'position': [float(x) for x in frac], 'kind': kind})

    _add('top_metal', coords[metal], 'top_metal')
    for c in coord:
        _add(f'top_{els[c]}{c}', coords[c], 'top_coord')
    for c in coord:
        mid = (coords[metal] + coords[c]) / 2.0
        _add(f'bridge_{els[metal]}{metal}_{els[c]}{c}', mid, 'bridge')
    _add('hollow', coords[coord].mean(axis=0), 'hollow')
    return sites


# ── 取向 / 旋转辅助 ──────────────────────────────────────────────────────────
def _unit(v):
    v = np.asarray(v, dtype=float)
    n = _norm(v)
    return v / n if n > 1e-9 else v


def _orthonormal_pair(d):
    d = _unit(d)
    ref = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = _unit(np.cross(d, ref))
    return u, np.cross(d, u)


def _rot_axis(axis, theta):
    """绕单位轴 axis 转 theta 的旋转矩阵(Rodrigues)。"""
    axis = _unit(axis)
    ct, st = math.cos(theta), math.sin(theta)
    kx, ky, kz = axis
    k = np.array([[0, -kz, ky], [kz, 0, -kx], [-ky, kx, 0]], dtype=float)
    return np.eye(3) + st * k + (1 - ct) * (k @ k)


def _rot_from_to(a, b):
    """把单位向量 a 旋到 b 的旋转矩阵。"""
    a, b = _unit(a), _unit(b)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if _norm(v) < 1e-8:
        if c > 0:
            return np.eye(3)
        return _rot_axis(_orthonormal_pair(a)[0], math.pi)   # 反平行 → 180°
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]], dtype=float)
    return np.eye(3) + vx + vx @ vx * (1.0 / (1.0 + c))


def _rot_z(xyz, deg):
    th = math.radians(deg)
    c, s = math.cos(th), math.sin(th)
    r = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]], dtype=float)
    return xyz @ r.T


def _choose_anchor(els, orientation):
    """据 orientation 选锚定原子下标(相同元素取最小下标,确定性)。"""
    if isinstance(orientation, dict):
        if 'anchor_index' in orientation:
            return int(orientation['anchor_index'])
        target = orientation.get('anchor_element')
    elif orientation == 's_down':
        target = 'S'
    elif orientation == 'li_down':
        target = 'Li'
    elif orientation == 'auto':
        target = 'S' if 'S' in els else ('Li' if 'Li' in els else None)
    else:
        raise ValueError(f"未知 orientation {orientation!r};可选 'auto'/'s_down'/'li_down'/dict")
    if target is None:
        return 0
    idxs = [i for i, e in enumerate(els) if e == target]
    if not idxs:
        raise ValueError(f'分子不含锚定元素 {target!r},无法按该取向摆放')
    return min(idxs)


def _orient_body_up(xyz, anchor):
    """把分子摆成 anchor 朝下、body(质心)朝 +z,并使最低原子落在 z=0。"""
    xyz = xyz - xyz[anchor]                     # anchor 移到原点
    centroid = xyz.mean(axis=0)
    if _norm(centroid) > 1e-6:
        xyz = xyz @ _rot_from_to(centroid, np.array([0.0, 0.0, 1.0])).T
    xyz = xyz.copy()
    xyz[:, 2] -= xyz[:, 2].min()                # 最低原子贴 z=0(anchor 为接触点)
    return xyz


def _min_mol_slab_dist(mol, slab, cell):
    """分子-表面最近原子间距(面内 ±1 周期像)。"""
    a_vec, b_vec = cell[0], cell[1]
    best = math.inf
    for m in mol:
        for s in slab:
            for ia in (-1, 0, 1):
                for ib in (-1, 0, 1):
                    q = s + ia * a_vec + ib * b_vec
                    d = _norm(m - q)
                    if d < best:
                        best = d
    return best


def _try_place(slab_poscar, molecule_name, site, height, orientation, deg):
    """摆放单个构型 → (poscar_text 或 None, min_dist)。太近则返回 (None, dist)。"""
    p = parse_positions(slab_poscar)
    slab_coords = np.array(p['coords'], dtype=float)
    cell = np.array(p['cell'], dtype=float)
    slab_els = list(p['elements'])

    atoms = molecule_geometry(molecule_name)
    mol_els = [e for e, _ in atoms]
    mol_xyz = np.array([xyz for _, xyz in atoms], dtype=float)

    anchor = _choose_anchor(mol_els, orientation)
    mol_xyz = _orient_body_up(mol_xyz, anchor)
    mol_xyz = _rot_z(mol_xyz, deg)

    site_cart = frac_to_cart(np.array(site['position'], dtype=float), cell)
    placed = mol_xyz + np.array([site_cart[0], site_cart[1], site_cart[2] + height])

    dmin = _min_mol_slab_dist(placed, slab_coords, cell)
    if dmin < MIN_SURFACE_DIST:
        return None, dmin

    all_els = slab_els + mol_els
    all_xyz = np.vstack([slab_coords, placed])
    text = write_poscar(
        f'{molecule_name}@{site["name"]}_r{int(deg)}', cell, all_els, all_xyz,
        mode='Cartesian', element_order=list(dict.fromkeys(slab_els)))
    return text, dmin


def place_adsorbate(slab_poscar, molecule_name, site, *, height=2.2,
                    orientation='auto', rotations=(0,)):
    """吸附质摆放 → list[poscar_text](每个 accepted 旋转一份)。

    分子锚定原子朝表面、body 朝上放到位点上方 height Å,绕 z 旋转采样 rotations。
    每个构型做分子-表面最近距离检查(< MIN_SURFACE_DIST 拒绝并跳过);若全部被拒,
    抛 ValueError 说明最近距离(拒绝并说明)。
    """
    texts, rejected = [], []
    for deg in rotations:
        text, dmin = _try_place(slab_poscar, molecule_name, site, height, orientation, deg)
        (texts if text is not None else rejected).append(text if text is not None else (deg, dmin))
    if texts:
        return texts
    if rejected:
        deg, dmin = min(rejected, key=lambda t: t[1])
        raise ValueError(
            f'分子 {molecule_name} 在位点 {site["name"]} 所有取向/旋转的分子-表面最近距离均 '
            f'< {MIN_SURFACE_DIST} Å(最近 {dmin:.2f} Å,r{int(deg)}°),已全部拒绝;'
            f'请增大 height、换位点或换取向。')
    raise ValueError('rotations 为空,未生成任何构型')


def adsorption_batch(slab_poscar, site_indices, molecules, *, height=2.2,
                     orientation='auto', rotations=(0,)):
    """位点 × 分子 × 旋转的构型族 → list[{'name','poscar','site','molecule','min_dist'(,'note')}]。

    命名 {mol}@{site}_r{deg}。被拒构型 poscar=None 并附 note 说明(便于下游知悉缺失原因)。
    """
    sites = enumerate_sac_sites(slab_poscar, site_indices)
    out = []
    for site in sites:
        for mol in molecules:
            for deg in rotations:
                text, dmin = _try_place(slab_poscar, mol, site, height, orientation, deg)
                entry = {'name': f'{mol}@{site["name"]}_r{int(deg)}',
                         'site': site['name'], 'molecule': mol,
                         'poscar': text, 'min_dist': round(dmin, 4)}
                if text is None:
                    entry['note'] = (f'分子-表面最近 {dmin:.2f} Å < {MIN_SURFACE_DIST},'
                                     f'已拒绝(未生成 POSCAR)')
                out.append(entry)
    return out
