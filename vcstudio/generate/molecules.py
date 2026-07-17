"""LiPS 分子库与盒装(F4)——锂硫体系 + 通用参考小分子的内置初始几何。

所有坐标为**文献常见初始构型**(Å),供结构弛豫起点,**非终值**;键长键角取化学
合理值(S-S≈2.05,Li-S≈2.4,C-O≈1.43,O=O≈1.21…),保证几何健全(无重叠)。

内置:S8(冠状 D4d)、Li2S、Li2S2、Li2S3、Li2S4、Li2S6、Li2S8(链状多硫化物)、
LiS/LiS2(开壳自由基,附自旋提示)、DOL/DME(电解液)、O2/H2/H2O/CO(通用参考)。

纯 python+numpy;中文注释,英文标识符。POSCAR 写出复用 sac_builder.write_poscar。
"""
from __future__ import annotations

import math

import numpy as np

from vcstudio.generate.sac_builder import write_poscar

# 键长(Å)
_SS = 2.05          # S-S
_LIS = 2.4          # Li-S
_CO_ETHER = 1.43    # C-O(醚)
_CC = 1.52          # C-C
_CH = 1.09          # C-H
_S_ANGLE = 106.0    # S-S-S 链角(度)


def _unit(v):
    # 刻意不用 np.linalg(惰性子模块;见 sac_builder 说明,避免重载 numpy 污染 sys.modules)
    v = np.asarray(v, dtype=float)
    n = float(np.sqrt(np.dot(v, v)))
    return v / n if n > 1e-9 else v


def _orthonormal_pair(d):
    """给单位向量 d,返回两条与之正交的单位向量。"""
    d = _unit(d)
    ref = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = _unit(np.cross(d, ref))
    v = np.cross(d, u)
    return u, v


def _cap_hydrogens(c_pos, neighbor_positions, n_h, bond=_CH):
    """在碳原子 c_pos 上补 n_h 个 H(据已有近邻方向张开,确定性,无重叠)。"""
    c_pos = np.asarray(c_pos, dtype=float)
    dirs = [_unit(np.asarray(p, dtype=float) - c_pos) for p in neighbor_positions]
    away = _unit(-sum(dirs)) if dirs else np.array([0.0, 0.0, 1.0])
    hs = []
    if n_h == 2:
        # 两 H 关于"已有键平面"对称张开(轴取两键法向,退化时取任一正交轴)
        axis = _unit(np.cross(dirs[0], dirs[1])) if len(dirs) >= 2 else _orthonormal_pair(away)[0]
        for s in (+1.0, -1.0):
            hs.append(c_pos + bond * _unit(0.5 * away + 0.866 * s * axis))
    elif n_h == 3:
        u, v = _orthonormal_pair(away)
        cos_a, sin_a = 0.334, 0.9426          # 109.47° 相对 away
        for k in range(3):
            phi = 2.0 * math.pi * k / 3.0
            d = cos_a * away + sin_a * (math.cos(phi) * u + math.sin(phi) * v)
            hs.append(c_pos + bond * _unit(d))
    else:  # n_h == 1
        hs.append(c_pos + bond * away)
    return hs


def _sulfur_chain(n, bond=_SS, angle_deg=_S_ANGLE):
    """平面锯齿硫链 S_n(键长 bond,链角 angle_deg),返回 n 个坐标。"""
    c = math.cos(math.radians(angle_deg))
    dx = bond * math.sqrt((1.0 - c) / 2.0)
    h = bond * math.sqrt((1.0 + c) / 2.0)
    return [np.array([k * dx, (h if k % 2 else 0.0), 0.0]) for k in range(n)]


def _li2sn(n):
    """链状 Li2S_n:S_n 锯齿链两端各封一个 Li(指向链外)。"""
    s = _sulfur_chain(n)
    li0 = s[0] + _LIS * _unit(s[0] - s[1])
    li1 = s[-1] + _LIS * _unit(s[-1] - s[-2])
    return [('Li', li0), ('Li', li1)] + [('S', p) for p in s]


def _lisn(n):
    """开壳 LiS_n:S_n 链一端封一个 Li(自由基,单端)。"""
    s = _sulfur_chain(n) if n >= 2 else [np.array([0.0, 0.0, 0.0])]
    if n >= 2:
        li = s[0] + _LIS * _unit(s[0] - s[1])
    else:
        li = np.array([-_LIS, 0.0, 0.0])
    return [('Li', li)] + [('S', p) for p in s]


def _s8_crown():
    """冠状 S8(D4d):8 原子交替 ±z 起伏,近邻 S-S≈2.05。"""
    radius, pucker = 2.337, 0.5           # 解出使近邻 = 2.049 Å
    atoms = []
    for k in range(8):
        th = math.radians(45.0 * k)
        z = pucker if k % 2 == 0 else -pucker
        atoms.append(('S', np.array([radius * math.cos(th), radius * math.sin(th), z])))
    return atoms


def _li2s():
    """Li-S-Li 弯曲分子(角 ~109.5°,Li-S 2.4)。"""
    ang = math.radians(109.5)
    s = np.array([0.0, 0.0, 0.0])
    li1 = np.array([_LIS, 0.0, 0.0])
    li2 = np.array([_LIS * math.cos(ang), _LIS * math.sin(ang), 0.0])
    return [('Li', li1), ('S', s), ('Li', li2)]


def _dol():
    """1,3-二氧戊环 C3H6O2:五元环(O1-C2-O3-C4-C5)近平面 + 各 CH2 补 2H。"""
    r = 1.25
    order = ['O1', 'C2', 'O3', 'C4', 'C5']            # 环连接序
    ang0 = {'O1': 90, 'C2': 162, 'O3': 234, 'C4': 306, 'C5': 18}
    ring = {k: np.array([r * math.cos(math.radians(ang0[k])),
                         r * math.sin(math.radians(ang0[k])), 0.0]) for k in order}
    atoms = [('O', ring['O1']), ('C', ring['C2']), ('O', ring['O3']),
             ('C', ring['C4']), ('C', ring['C5'])]
    # CH2:C2(邻 O1,O3)、C4(邻 O3,C5)、C5(邻 C4,O1)
    for c, na, nb in (('C2', 'O1', 'O3'), ('C4', 'O3', 'C5'), ('C5', 'C4', 'O1')):
        for hp in _cap_hydrogens(ring[c], [ring[na], ring[nb]], 2):
            atoms.append(('H', hp))
    return atoms


def _dme():
    """乙二醇二甲醚 C4H10O2:C-O-C-C-O-C 锯齿骨架 + 甲基/亚甲基补 H。"""
    bond, ang = 1.47, 112.0
    c = math.cos(math.radians(ang))
    dx = bond * math.sqrt((1.0 - c) / 2.0)
    h = bond * math.sqrt((1.0 + c) / 2.0)
    back = [np.array([k * dx, (h if k % 2 else 0.0), 0.0]) for k in range(6)]
    labels = ['C', 'O', 'C', 'C', 'O', 'C']           # C1-O1-C2-C3-O2-C4
    atoms = [(labels[k], back[k]) for k in range(6)]
    # 端甲基 C1(邻 O1,3H)、C4(邻 O2,3H);亚甲基 C2(邻 O1,C3)、C3(邻 C2,O2)
    for hp in _cap_hydrogens(back[0], [back[1]], 3):
        atoms.append(('H', hp))
    for hp in _cap_hydrogens(back[2], [back[1], back[3]], 2):
        atoms.append(('H', hp))
    for hp in _cap_hydrogens(back[3], [back[2], back[4]], 2):
        atoms.append(('H', hp))
    for hp in _cap_hydrogens(back[5], [back[4]], 3):
        atoms.append(('H', hp))
    return atoms


def _diatomic(el_a, el_b, bond):
    return [(el_a, np.array([0.0, 0.0, 0.0])), (el_b, np.array([bond, 0.0, 0.0]))]


def _h2o():
    ang = math.radians(104.5)
    return [('O', np.array([0.0, 0.0, 0.0])),
            ('H', np.array([0.96, 0.0, 0.0])),
            ('H', np.array([0.96 * math.cos(ang), 0.96 * math.sin(ang), 0.0]))]


# 名称 → 构造函数(惰性求值,避免导入期算全部)
_BUILDERS = {
    'S8': _s8_crown,
    'Li2S': _li2s,
    'Li2S2': lambda: _li2sn(2),
    'Li2S3': lambda: _li2sn(3),
    'Li2S4': lambda: _li2sn(4),
    'Li2S6': lambda: _li2sn(6),
    'Li2S8': lambda: _li2sn(8),
    'LiS': lambda: _lisn(1),
    'LiS2': lambda: _lisn(2),
    'DOL': _dol,
    'DME': _dme,
    'O2': lambda: _diatomic('O', 'O', 1.21),
    'H2': lambda: _diatomic('H', 'H', 0.741),
    'H2O': _h2o,
    'CO': lambda: _diatomic('C', 'O', 1.128),
}

# 元信息:化学式 / 电荷 / 自旋提示 / 来源说明
_DOUBLET = 'ISPIN=2, NUPDOWN=1(开壳单电子自由基,须自旋极化)'
_TRIPLET = 'ISPIN=2, NUPDOWN=2(三重态基态,须自旋极化)'
_INFO = {
    'S8':   {'formula': 'S8',      'spin_hint': None,     'note': '冠状 S8(D4d),S-S≈2.05 Å'},
    'Li2S': {'formula': 'Li2S',    'spin_hint': None,     'note': 'Li-S-Li 弯曲,Li-S≈2.4 Å'},
    'Li2S2': {'formula': 'Li2S2',  'spin_hint': None,     'note': '链状 Li2S2,S-S≈2.05/Li-S≈2.4 Å'},
    'Li2S3': {'formula': 'Li2S3',  'spin_hint': None,     'note': '链状多硫化锂 Li2S3'},
    'Li2S4': {'formula': 'Li2S4',  'spin_hint': None,     'note': '链状多硫化锂 Li2S4'},
    'Li2S6': {'formula': 'Li2S6',  'spin_hint': None,     'note': '链状多硫化锂 Li2S6'},
    'Li2S8': {'formula': 'Li2S8',  'spin_hint': None,     'note': '链状多硫化锂 Li2S8(区别于冠状 S8)'},
    'LiS':  {'formula': 'LiS',     'spin_hint': _DOUBLET, 'note': 'LiS 开壳自由基(奇电子数)'},
    'LiS2': {'formula': 'LiS2',    'spin_hint': _DOUBLET, 'note': 'LiS2 开壳自由基(奇电子数)'},
    'DOL':  {'formula': 'C3H6O2',  'spin_hint': None,     'note': '1,3-二氧戊环(电解液溶剂)'},
    'DME':  {'formula': 'C4H10O2', 'spin_hint': None,     'note': '乙二醇二甲醚(电解液溶剂)'},
    'O2':   {'formula': 'O2',      'spin_hint': _TRIPLET, 'note': 'O2 三重态基态,O=O≈1.21 Å'},
    'H2':   {'formula': 'H2',      'spin_hint': None,     'note': 'H2,H-H≈0.74 Å'},
    'H2O':  {'formula': 'H2O',     'spin_hint': None,     'note': 'H2O,O-H≈0.96 Å/角 104.5°'},
    'CO':   {'formula': 'CO',      'spin_hint': None,     'note': 'CO,C-O≈1.13 Å'},
}

_SOURCE_NOTE = '文献常见初始构型,供结构弛豫起点,非终值。'


def list_molecules():
    """返回内置分子名列表(稳定顺序:锂硫链由大到小 + 通用小分子)。"""
    return ['S8', 'Li2S8', 'Li2S6', 'Li2S4', 'Li2S3', 'Li2S2', 'Li2S',
            'LiS2', 'LiS', 'DOL', 'DME', 'O2', 'H2', 'H2O', 'CO']


def molecule_geometry(name):
    """分子名 → [(element, ndarray[3]), ...](Å,构造序)。未知名 → ValueError。"""
    if name not in _BUILDERS:
        raise ValueError(f'未知分子 {name!r};可选:{", ".join(list_molecules())}')
    return _BUILDERS[name]()


def molecule_info(name):
    """分子名 → {'formula','charge','spin_hint','source_note'}。"""
    if name not in _INFO:
        raise ValueError(f'未知分子 {name!r};可选:{", ".join(list_molecules())}')
    info = _INFO[name]
    return {'formula': info['formula'], 'charge': 0,
            'spin_hint': info['spin_hint'],
            'source_note': info['note'] + ';' + _SOURCE_NOTE}


def molecule_in_box(name, *, box=15.0, center=True):
    """分子装入立方盒(边长 box Å)→ POSCAR 文本。center=True 时质心置盒中心。"""
    atoms = molecule_geometry(name)
    els = [el for el, _ in atoms]
    coords = np.array([xyz for _, xyz in atoms], dtype=float)
    if center:
        coords = coords - coords.mean(axis=0) + np.array([box / 2.0] * 3)
    cell = np.diag([float(box)] * 3)
    return write_poscar(f'{name} in {box} A cubic box', cell, els, coords,
                        mode='Cartesian')
