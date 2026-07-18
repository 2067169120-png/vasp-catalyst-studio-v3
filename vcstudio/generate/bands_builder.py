"""能带结构作业生成端:高对称 k 路径库(Setyawan-Curtarolo)+ 非自洽能带作业派生。

能带是**两步**计算:先自洽静态(产出收敛 CHGCAR),再沿布里渊区高对称路径做**非自洽**
(ICHARG=11 读固定 CHGCAR)得 E(k)。本模块负责第二步:给定平衡结构 + 已有自洽 CHGCAR,
按晶格类型选标准 k 路径,写 line-mode KPOINTS + 派生 INCAR(ICHARG=11 + LORBIT=11)。

高对称路径库按 Setyawan & Curtarolo, Comput. Mater. Sci. 49, 299 (2010) 惯例(**原胞**约定,
分数坐标于倒空间);覆盖 cubic(简单立方)/fcc/bcc/hexagonal/tetragonal/orthorhombic。'|' 表示
路径断开(不连线的另起一段)。

晶格自动判别(coarse):从晶格矢量长度/夹角粗判——立方(a=b=c,90°)、fcc(60°)、bcc
(109.47°)、六方(a=b,γ=120°)、四方(a=b≠c,90°)、正交(a≠b≠c,90°)。判不出(或含糊,
如惯用胞 fcc 的 90° 会被判成简单立方)→ **显式 raise 要求给 lattice**,绝不猜错路径。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
from collections import OrderedDict
from pathlib import Path

from vcstudio.generate.conv_scan import _read_text, _structure_source, derive_incar
from vcstudio.generate.poscar import parse_poscar_species, read_cell_vectors
from vcstudio.shared import manifest as manifest_mod

_G = 'Γ'   # Gamma 标签(输出注释用;KPOINTS 注释可含 Unicode)

# 各晶格:高对称点分数坐标(倒空间原胞)+ 路径(段列表,每段连续折线;段间断开)。
HIGH_SYMMETRY = {
    'cubic': {
        'points': {'GAMMA': (0.0, 0.0, 0.0), 'X': (0.0, 0.5, 0.0),
                   'M': (0.5, 0.5, 0.0), 'R': (0.5, 0.5, 0.5)},
        'path': [['GAMMA', 'X', 'M', 'GAMMA', 'R', 'X'], ['M', 'R']],
        'note': 'Setyawan-Curtarolo CUB(简单立方原胞):Γ-X-M-Γ-R-X | M-R',
    },
    'fcc': {
        'points': {'GAMMA': (0.0, 0.0, 0.0), 'X': (0.5, 0.0, 0.5),
                   'W': (0.5, 0.25, 0.75), 'K': (0.375, 0.375, 0.75),
                   'L': (0.5, 0.5, 0.5), 'U': (0.625, 0.25, 0.625)},
        'path': [['GAMMA', 'X', 'W', 'K', 'GAMMA', 'L', 'U', 'W', 'L', 'K'], ['U', 'X']],
        'note': 'Setyawan-Curtarolo FCC 原胞:Γ-X-W-K-Γ-L-U-W-L-K | U-X',
    },
    'bcc': {
        'points': {'GAMMA': (0.0, 0.0, 0.0), 'H': (0.5, -0.5, 0.5),
                   'N': (0.0, 0.0, 0.5), 'P': (0.25, 0.25, 0.25)},
        'path': [['GAMMA', 'H', 'N', 'GAMMA', 'P', 'H'], ['P', 'N']],
        'note': 'Setyawan-Curtarolo BCC 原胞:Γ-H-N-Γ-P-H | P-N',
    },
    'hexagonal': {
        'points': {'GAMMA': (0.0, 0.0, 0.0), 'M': (0.5, 0.0, 0.0),
                   'K': (1.0 / 3.0, 1.0 / 3.0, 0.0), 'A': (0.0, 0.0, 0.5),
                   'L': (0.5, 0.0, 0.5), 'H': (1.0 / 3.0, 1.0 / 3.0, 0.5)},
        'path': [['GAMMA', 'M', 'K', 'GAMMA', 'A', 'L', 'H', 'A'], ['L', 'M'], ['K', 'H']],
        'note': 'Setyawan-Curtarolo HEX:Γ-M-K-Γ-A-L-H-A | L-M | K-H',
    },
    'tetragonal': {
        'points': {'GAMMA': (0.0, 0.0, 0.0), 'X': (0.0, 0.5, 0.0),
                   'M': (0.5, 0.5, 0.0), 'Z': (0.0, 0.0, 0.5),
                   'R': (0.0, 0.5, 0.5), 'A': (0.5, 0.5, 0.5)},
        'path': [['GAMMA', 'X', 'M', 'GAMMA', 'Z', 'R', 'A', 'Z'], ['X', 'R'], ['M', 'A']],
        'note': 'Setyawan-Curtarolo TET(唯一轴 c):Γ-X-M-Γ-Z-R-A-Z | X-R | M-A',
    },
    'orthorhombic': {
        'points': {'GAMMA': (0.0, 0.0, 0.0), 'X': (0.5, 0.0, 0.0),
                   'S': (0.5, 0.5, 0.0), 'Y': (0.0, 0.5, 0.0),
                   'Z': (0.0, 0.0, 0.5), 'U': (0.5, 0.0, 0.5),
                   'R': (0.5, 0.5, 0.5), 'T': (0.0, 0.5, 0.5)},
        'path': [['GAMMA', 'X', 'S', 'Y', 'GAMMA', 'Z', 'U', 'R', 'T', 'Z'],
                 ['Y', 'T'], ['U', 'X'], ['S', 'R']],
        'note': 'Setyawan-Curtarolo ORC:Γ-X-S-Y-Γ-Z-U-R-T-Z | Y-T | U-X | S-R',
    },
}
LATTICES = tuple(HIGH_SYMMETRY.keys())


def _norm(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _angle_deg(a, b):
    na, nb = _norm(a), _norm(b)
    if na == 0 or nb == 0:
        return 0.0
    c = (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (na * nb)
    c = max(-1.0, min(1.0, c))
    return math.degrees(math.acos(c))


def detect_lattice(cell, *, len_tol: float = 0.02, ang_tol: float = 2.0):
    """从晶格矢量粗判布拉菲类型 → HIGH_SYMMETRY 的键;判不出 → None。

    cell:3×3(晶格矢量为行)。len_tol 为相对长度容差,ang_tol 为角度容差(度)。
    仅粗判(见模块 docstring 的局限):唯一轴须为 c(四方/六方),惯用胞 fcc/bcc 会被误判。
    """
    a, b, c = cell[0], cell[1], cell[2]
    la, lb, lc = _norm(a), _norm(b), _norm(c)
    alpha, beta, gamma = _angle_deg(b, c), _angle_deg(a, c), _angle_deg(a, b)

    def crel(x, y):
        return abs(x - y) <= len_tol * max(x, y, 1e-12)

    def cang(x, y):
        return abs(x - y) <= ang_tol

    ab, bc, ac = crel(la, lb), crel(lb, lc), crel(la, lc)
    all90 = cang(alpha, 90) and cang(beta, 90) and cang(gamma, 90)

    if ab and bc and ac:                       # a≈b≈c
        if all90:
            return 'cubic'
        if cang(alpha, 60) and cang(beta, 60) and cang(gamma, 60):
            return 'fcc'
        if cang(alpha, 109.4712) and cang(beta, 109.4712) and cang(gamma, 109.4712):
            return 'bcc'
    if ab and cang(gamma, 120) and cang(alpha, 90) and cang(beta, 90):
        return 'hexagonal'                     # a≈b,γ=120°,唯一轴 c
    if all90:
        if ab and not ac:
            return 'tetragonal'                # a≈b≠c,唯一轴 c
        if not ab and not bc and not ac:
            return 'orthorhombic'
    return None


def kpath_segments(lattice: str) -> list:
    """晶格类型 → line-mode 端点对列表 [((f0, name0), (f1, name1)), ...](相邻高对称点成对)。"""
    if lattice not in HIGH_SYMMETRY:
        raise ValueError(f'未知晶格类型 {lattice!r};可选:{LATTICES}')
    spec = HIGH_SYMMETRY[lattice]
    pts = spec['points']
    pairs = []
    for seg in spec['path']:
        for i in range(len(seg) - 1):
            n0, n1 = seg[i], seg[i + 1]
            pairs.append(((pts[n0], n0), (pts[n1], n1)))
    return pairs


def _label(name: str) -> str:
    return _G if name == 'GAMMA' else name


def kpoints_line_mode(lattice: str, npoints: int = 40) -> str:
    """生成 line-mode KPOINTS 文本(能带路径;倒空间分数坐标,每段 npoints 点)。"""
    pairs = kpath_segments(lattice)
    lines = [f'k-points along high symmetry lines ({lattice}, Setyawan-Curtarolo)',
             f'{int(npoints)}', 'Line-mode', 'reciprocal']
    for (f0, n0), (f1, n1) in pairs:
        lines.append(f'  {f0[0]:.6f} {f0[1]:.6f} {f0[2]:.6f}  ! {_label(n0)}')
        lines.append(f'  {f1[0]:.6f} {f1[1]:.6f} {f1[2]:.6f}  ! {_label(n1)}')
        lines.append('')
    return '\n'.join(lines).rstrip('\n') + '\n'


_REASONS = {
    'ICHARG': '非自洽能带:读固定自洽 CHGCAR(须先有自洽静态产出的 CHGCAR)',
    'LORBIT': '投影能带(轨道/原子权重),供 fatband/贡献分析',
    'NSW': '能带不做离子步',
    'IBRION': '静态(不弛豫)',
    'ISMEAR': 'line-mode 非规则 k 网格,四面体不可用,改高斯展宽',
    'SIGMA': '高斯展宽宽度',
    'ISIF': '能带无需应力/变胞', 'EDIFFG': '能带无离子弛豫判据',
}


def _copy_if(src_dir, out_dir, name):
    src = os.path.join(src_dir, name)
    if os.path.isfile(src):
        import shutil
        shutil.copyfile(src, os.path.join(out_dir, name))
        return True
    return False


def build_bands_job(src_dir, out_root, *, lattice=None, npoints: int = 40) -> dict:
    """从平衡结构 + 已有自洽 CHGCAR 派生**非自洽能带**作业(两步法第二步)。

    读 src 的 CONTCAR(缺则 POSCAR)+ INCAR + POTCAR(+ 若有 CHGCAR 则复制);写:POSCAR
    + 派生 INCAR(ICHARG=11 + LORBIT=11 + ISMEAR=0,静态)+ line-mode KPOINTS(按晶格路径)
    + POTCAR 原样;job.yaml(task_type='bands',记晶格/路径/npoints)。

    Args:
        lattice: None → 从晶格矢量自动判别(判不出则 raise 要求显式);或直接给
            'cubic'/'fcc'/'bcc'/'hexagonal'/'tetragonal'/'orthorhombic'。
        npoints: 每段 k 点数(默认 40)。

    Returns:
        ``{'out_dir','lattice','changes','warnings'}``。源缺结构/INCAR、或晶格判不出 → ValueError。
    """
    src_dir = str(src_dir)
    source_name, poscar_text = _structure_source(src_dir)
    if poscar_text is None:
        raise ValueError(f'源目录缺 CONTCAR/POSCAR(或均为空),无法派生能带作业:{src_dir}')
    base_incar = _read_text(os.path.join(src_dir, 'INCAR'))
    if base_incar is None:
        raise ValueError(f'源目录缺 INCAR,无法派生能带作业:{src_dir}')

    if lattice is None:
        cell = read_cell_vectors(poscar_text)
        lattice = detect_lattice(cell)
        if lattice is None:
            raise ValueError(
                '无法从晶格矢量粗判布拉菲类型(可能是惯用胞/低对称/含糊角度);'
                f'请显式给 lattice(可选:{LATTICES})。')
    elif lattice not in HIGH_SYMMETRY:
        raise ValueError(f'未知晶格类型 {lattice!r};可选:{LATTICES}')

    warnings: list[str] = []
    set_keys = OrderedDict([('ICHARG', 11), ('LORBIT', 11), ('NSW', 0),
                            ('IBRION', -1), ('ISMEAR', 0), ('SIGMA', 0.05)])
    banner = ('# === vcstudio 能带作业(非自洽 ICHARG=11;两步法第二步) ===\n'
              f'# k 路径:{HIGH_SYMMETRY[lattice]["note"]}\n'
              '# 前置:须先自洽静态产出收敛 CHGCAR 并置于本目录(ICHARG=11 读它)。')
    new_incar, changes = derive_incar(base_incar, set_keys=set_keys,
                                      strip_keys=('ISIF', 'EDIFFG'), reasons=_REASONS,
                                      banner=banner)

    os.makedirs(out_root, exist_ok=True)
    with open(os.path.join(out_root, 'POSCAR'), 'w', encoding='utf-8') as f:
        f.write(poscar_text)
    with open(os.path.join(out_root, 'INCAR'), 'w', encoding='utf-8') as f:
        f.write(new_incar)
    with open(os.path.join(out_root, 'KPOINTS'), 'w', encoding='utf-8') as f:
        f.write(kpoints_line_mode(lattice, npoints))
    if not _copy_if(src_dir, out_root, 'POTCAR'):
        warnings.append('源目录缺 POTCAR,未复制;提交前须补齐同一套赝势。')
    if _copy_if(src_dir, out_root, 'CHGCAR'):
        warnings.append('已从源目录复制 CHGCAR(ICHARG=11 将读它);请确认它来自**已收敛**的自洽静态。')
    else:
        warnings.append('源目录无 CHGCAR:能带用 ICHARG=11 读固定 CHGCAR,须先跑自洽静态'
                        '(LCHARG=.TRUE.)产出 CHGCAR 后放入本目录,否则作业会因缺 CHGCAR 失败。')
    warnings.append(f'k 路径按 {lattice} 标准(Setyawan-Curtarolo);晶格判别为 coarse,'
                    '惯用胞/低对称体系请核对或显式指定 lattice。')

    syms, _counts = parse_poscar_species(poscar_text)
    system = poscar_text.splitlines()[0].strip() if poscar_text.strip() else Path(out_root).name
    parent = str(Path(src_dir).resolve())
    m = manifest_mod.new_manifest(
        job_id=f'{Path(out_root).name}-bands', system=system, task_type='bands',
        calc_type='bulk',
        inputs={'parent_job': parent, 'derived_from': source_name, 'lattice': lattice,
                'npoints': int(npoints), 'kpath_note': HIGH_SYMMETRY[lattice]['note'],
                'incar_changes': changes, 'elements': list(syms)},
        warnings=warnings)
    m['parent_job'] = parent
    manifest_mod.save_manifest(out_root, m)
    return {'out_dir': str(out_root), 'lattice': lattice, 'changes': changes,
            'warnings': warnings}
