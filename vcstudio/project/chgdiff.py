"""差分电荷工作流(F21):CHGCAR 网格 I/O + 吸附态拆分 + 三单点派生 + Δρ 网格代数。

Δρ = ρ(AB) − ρ(A) − ρ(B):同一晶胞/同一冻结几何下,复合体系(AB)、仅表面(A)、
仅吸附质(B)三次独立静态各出一个 CHGCAR,逐点相减 → 差分电荷密度(VESTA 可读)。
外加沿法向的面平均 Δρ(z) 曲线作定量补充。

CHGCAR 网格代数纯 python 实现(read_chgcar/write_chgcar/same_grid/same_lattice
同时供 bader.sum_aeccar 复用);一致性硬校验(晶格/网格/原子数),不一致拒算并给
中文说明,绝不静默错位相减。

口径说明:CHGCAR 存的是 ρ×V_cell(VASP 惯例),三体系同 V,故 (ρ_AB−ρ_A−ρ_B)×V
仍为 Δρ×V,直接对存储值做代数即可;面平均再除以 V 还原为电荷密度 e/Å³。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
from collections import OrderedDict

from vcstudio.generate.poscar import parse_poscar_species


def _read_maybe(src) -> str:
    """路径或文本 → 文本。含换行的 str 视为内容,否则视为路径(缺文件抛 OSError)。"""
    if isinstance(src, os.PathLike):
        path = os.fspath(src)
    elif isinstance(src, str) and '\n' not in src:
        path = src
    else:
        return str(src)
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def _is_all_int(toks) -> bool:
    try:
        [int(t) for t in toks]
        return bool(toks)
    except ValueError:
        return False


def _det3(m) -> float:
    """3×3 行列式(晶胞体积用)。"""
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


# ── CHGCAR 读/写(纯 python;供 compute_chgdiff 与 bader.sum_aeccar 复用)────────

def read_chgcar(path_or_text) -> dict:
    """CHGCAR/AECCAR(路径或文本)→ 头信息 + 网格值。

    返回 ``{'comment','scale','lattice'(3×3 原始),'species','counts','natoms',
    'ngx','ngy','ngz','grid'(长度 NGX*NGY*NGZ),'header'(POSCAR 头行,原样供重写)}``。
    只读主电荷网格,PAW 增广(augmentation)尾段读满 NGX*NGY*NGZ 即停、不解析。
    行数不足/网格维度非整数/数据不足或含非数值 → 中文 ValueError。
    """
    text = _read_maybe(path_or_text)
    lines = text.splitlines()
    if len(lines) < 8:
        raise ValueError('CHGCAR 行数不足,无法解析(需 POSCAR 头 + 网格维度 + 数据)')
    comment = lines[0]
    try:
        scale = float(lines[1].split()[0])
    except (ValueError, IndexError):
        raise ValueError('CHGCAR 第2行缩放因子非数字')
    lattice = []
    for i in (2, 3, 4):
        parts = lines[i].split()
        if len(parts) < 3:
            raise ValueError(f'CHGCAR 第{i + 1}行晶格矢量不足 3 分量')
        lattice.append([float(x) for x in parts[:3]])
    toks5 = lines[5].split()
    if _is_all_int(toks5):                       # VASP4:第6行即计数(无元素符号行)
        species, counts, idx = [], [int(t) for t in toks5], 6
    else:
        species = toks5
        try:
            counts = [int(t) for t in lines[6].split()]
        except ValueError:
            raise ValueError('CHGCAR 第7行原子计数非整数')
        idx = 7
    natoms = sum(counts)
    if idx < len(lines) and lines[idx].strip()[:1] in ('s', 'S'):
        idx += 1                                 # Selective dynamics 行
    idx += 1                                     # 坐标模式行(Direct/Cartesian)
    idx += natoms                                # 跳过坐标块
    header = lines[:idx]
    while idx < len(lines) and lines[idx].strip() == '':
        idx += 1                                 # 坐标块后的空行
    if idx >= len(lines):
        raise ValueError('CHGCAR 缺网格维度行(坐标块后应有 NGX NGY NGZ)')
    gparts = lines[idx].split()
    if len(gparts) < 3:
        raise ValueError(f'CHGCAR 网格维度行格式异常:{lines[idx]!r}')
    try:
        ngx, ngy, ngz = int(gparts[0]), int(gparts[1]), int(gparts[2])
    except ValueError:
        raise ValueError(f'CHGCAR 网格维度非整数:{lines[idx]!r}')
    idx += 1
    ntot = ngx * ngy * ngz
    grid: list[float] = []
    while idx < len(lines) and len(grid) < ntot:
        for p in lines[idx].split():
            if len(grid) >= ntot:
                break
            try:
                grid.append(float(p))
            except ValueError:
                raise ValueError(f'CHGCAR 网格数据含非数值:{p!r}')
        idx += 1
    if len(grid) < ntot:
        raise ValueError(f'CHGCAR 网格数据不足:需 {ntot} 个,读到 {len(grid)}(文件截断?)')
    return {'comment': comment, 'scale': scale, 'lattice': lattice,
            'species': species, 'counts': counts, 'natoms': natoms,
            'ngx': ngx, 'ngy': ngy, 'ngz': ngz, 'grid': grid, 'header': header}


def write_chgcar(ref: dict, grid, out_path) -> str:
    """按 ref 的 POSCAR 头 + 给定 grid 写出 CHGCAR(VESTA 可读:每行 5 值,%18.11E)。"""
    ngx, ngy, ngz = ref['ngx'], ref['ngy'], ref['ngz']
    ntot = ngx * ngy * ngz
    if len(grid) != ntot:
        raise ValueError(f'写 CHGCAR:网格值 {len(grid)} 个 != NGX*NGY*NGZ={ntot}')
    parts = ['\n'.join(ref['header']), '', f' {ngx} {ngy} {ngz}']
    buf = []
    for v in grid:
        buf.append(f'{v:18.11E}')
        if len(buf) == 5:
            parts.append(''.join(buf))
            buf = []
    if buf:
        parts.append(''.join(buf))
    out_dir = os.path.dirname(os.path.abspath(str(out_path)))
    os.makedirs(out_dir or '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts) + '\n')
    return str(out_path)


def same_grid(a: dict, b: dict) -> bool:
    return (a['ngx'], a['ngy'], a['ngz']) == (b['ngx'], b['ngy'], b['ngz'])


def same_lattice(a: dict, b: dict, tol: float = 1e-4) -> bool:
    la = [[a['scale'] * x for x in v] for v in a['lattice']]
    lb = [[b['scale'] * x for x in v] for v in b['lattice']]
    return all(abs(la[i][j] - lb[i][j]) <= tol for i in range(3) for j in range(3))


# ── 吸附态 POSCAR 拆三件(AB / A 仅表面 / B 仅吸附质,同晶胞同坐标)────────────

def split_adsorption_poscar(poscar_text: str, adsorbate_indices) -> dict:
    """吸附态 POSCAR → ``{'ab','a','b'}`` 三份冻结几何 POSCAR 文本。

    - ab:全原子(= 输入,按物种重排规范化);a:去掉吸附质(仅表面);b:仅吸附质。
    - adsorbate_indices:吸附质原子序号(**1 起**,对齐 POSCAR 原子顺序)。
    - 三份同晶胞、坐标逐字保留(含 Selective dynamics 的 T/F 冻结标记),各自按物种
      重新归组并写对应元素行/计数行(供各自 POTCAR 拼接)。
    VASP4(无元素行)/序号越界/吸附质为空或含全部原子 → 中文 ValueError。
    """
    lines = poscar_text.splitlines()
    if len(lines) < 8:
        raise ValueError('POSCAR 行数不足,无法拆分吸附体系')
    comment, scale_line, latt = lines[0], lines[1], lines[2:5]
    toks5 = lines[5].split()
    if _is_all_int(toks5):
        raise ValueError('POSCAR 缺元素符号行(VASP4 格式);差分电荷拆分需 VASP5 元素行')
    species = toks5
    try:
        counts = [int(t) for t in lines[6].split()]
    except ValueError:
        raise ValueError('POSCAR 第7行原子计数非整数')
    if len(species) != len(counts):
        raise ValueError('POSCAR 元素符号与计数不等长')
    natoms = sum(counts)
    idx, sel_dyn = 7, False
    if lines[idx].strip()[:1] in ('s', 'S'):
        sel_dyn = True
        idx += 1
    mode_line = lines[idx]
    idx += 1
    coord_lines = lines[idx:idx + natoms]
    if len(coord_lines) < natoms:
        raise ValueError(f'POSCAR 坐标行不足:需 {natoms} 行,仅 {len(coord_lines)} 行(截断?)')
    per_atom_symbol = [s for s, c in zip(species, counts) for _ in range(c)]

    ads = sorted({int(i) for i in adsorbate_indices})
    bad = [i for i in ads if i < 1 or i > natoms]
    if bad:
        raise ValueError(f'吸附质序号越界 {bad}(有效 1..{natoms};序号 1 起,对齐 POSCAR)')
    ads_set = set(ads)
    a_atoms = [i for i in range(natoms) if (i + 1) not in ads_set]
    b_atoms = [i for i in range(natoms) if (i + 1) in ads_set]
    if not b_atoms:
        raise ValueError('adsorbate_indices 为空,无法拆出吸附质 B')
    if not a_atoms:
        raise ValueError('吸附质包含全部原子,无表面 A 可拆(请只标吸附质原子)')

    def build(sub):
        groups: "OrderedDict[str, list]" = OrderedDict()
        for i in sub:
            groups.setdefault(per_atom_symbol[i], []).append(coord_lines[i])
        out = [comment, scale_line, latt[0], latt[1], latt[2],
               ' '.join(groups.keys()),
               ' '.join(str(len(v)) for v in groups.values())]
        if sel_dyn:
            out.append('Selective dynamics')
        out.append(mode_line)
        for cl in groups.values():
            out.extend(cl)
        return '\n'.join(out) + '\n'

    return {'ab': build(list(range(natoms))), 'a': build(a_atoms), 'b': build(b_atoms)}


def _split_potcar_blocks(text: str) -> list:
    """POTCAR 文本按 'End of Dataset' 切成逐物种块(保留结尾行)。"""
    blocks, cur = [], []
    for line in text.splitlines(keepends=True):
        cur.append(line)
        if 'End of Dataset' in line:
            blocks.append(''.join(cur))
            cur = []
    if any(s.strip() for s in cur):
        blocks.append(''.join(cur))
    return blocks


def slice_potcar(potcar_text: str, parent_species, wanted_species):
    """按物种从母 POTCAR 切出子体系 POTCAR(块数与母物种数不符/缺物种 → None 降级)。"""
    blocks = _split_potcar_blocks(potcar_text)
    if len(blocks) != len(parent_species):
        return None
    by_symbol: dict = {}
    for sym, blk in zip(parent_species, blocks):
        by_symbol.setdefault(sym, blk)          # 同元素 POTCAR 相同,取首块
    out = []
    for sym in wanted_species:
        if sym not in by_symbol:
            return None
        out.append(by_symbol[sym])
    return ''.join(out)


def build_chgdiff_jobs(relax_dir, out_root, adsorbate_indices) -> dict:
    """从弛豫目录派生差分电荷三静态作业(_AB/_A/_B,purpose='chgdiff',复用 estatic)。

    - 读母 CONTCAR(优先)/POSCAR 作 AB 几何 → split_adsorption_poscar 拆三件;
    - 各作业复用母 INCAR(电子学)与母 KPOINTS(加密),仅 POSCAR/POTCAR 换成子体系;
      A/B 移除母体 MAGMOM(原子集不同,磁性体系须自行重设);
    - 母 POTCAR 按物种切片给各子体系;job.yaml 记 chgdiff_role 与兄弟目录。
    返回 ``{'out_root','dirs','results'}``。
    """
    from vcstudio.generate.estatic import build_static_job     # 延迟导入,避免循环
    ab_text = None
    for nm in ('CONTCAR', 'POSCAR'):
        p = os.path.join(relax_dir, nm)
        if os.path.isfile(p):
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                ab_text = f.read()
            break
    if ab_text is None:
        raise ValueError(f'弛豫目录缺 CONTCAR/POSCAR:{relax_dir};无法派生差分电荷作业')
    parts = split_adsorption_poscar(ab_text, adsorbate_indices)

    potcar_text = None
    pp = os.path.join(relax_dir, 'POTCAR')
    if os.path.isfile(pp):
        with open(pp, 'r', encoding='utf-8', errors='replace') as f:
            potcar_text = f.read()
    parent_species, _ = parse_poscar_species(ab_text)

    roles = [('_AB', 'ab'), ('_A', 'a'), ('_B', 'b')]
    dirs = {tag: os.path.join(out_root, tag) for tag, _ in roles}
    results: "OrderedDict[str, dict]" = OrderedDict()
    for tag, key in roles:
        sub_text = parts[key]
        sub_species, _ = parse_poscar_species(sub_text)
        if tag == '_AB':
            sub_potcar = potcar_text                 # 物种与母体相同,直接用原 POTCAR
        elif potcar_text is not None and parent_species:
            sub_potcar = slice_potcar(potcar_text, parent_species, sub_species)
        else:
            sub_potcar = None
        siblings = {t2: dirs[t2] for t2, _ in roles if t2 != tag}
        drop = ('MAGMOM', 'NUPDOWN') if tag != '_AB' else None    # 子体系原子集不同
        results[tag] = build_static_job(
            relax_dir, dirs[tag], purpose='chgdiff',
            poscar_text=sub_text, potcar_text=sub_potcar, copy_parent_potcar=False,
            drop_incar=drop,
            extra_meta={'chgdiff_role': tag.strip('_'), 'siblings': siblings})
    return {'out_root': str(out_root), 'dirs': dirs, 'results': results}


# ── Δρ 网格代数 + 面平均 ─────────────────────────────────────────────────────

def compute_chgdiff(ab_chgcar, a_chgcar, b_chgcar, out_path) -> dict:
    """Δρ = ρ(AB) − ρ(A) − ρ(B) 网格代数 → 写 CHGDIFF.vasp(VESTA 可读)。

    一致性硬校验:三体系晶格一致、网格(NGXF/NGYF/NGZF)一致、原子数守恒
    (N_AB = N_A + N_B);任一不满足拒算并给中文说明。
    返回 ``{'out','max','min','n_grid'}``(max/min 为 Δ(ρ×V) 的极值)。
    """
    ab, a, b = read_chgcar(ab_chgcar), read_chgcar(a_chgcar), read_chgcar(b_chgcar)
    for other, name in ((a, 'A'), (b, 'B')):
        if not same_grid(ab, other):
            raise ValueError(
                f'AB 与 {name} 网格不一致:{(ab["ngx"], ab["ngy"], ab["ngz"])} vs '
                f'{(other["ngx"], other["ngy"], other["ngz"])};差分电荷要求三体系同网格'
                '(同 NGXF/NGYF/NGZF),拒绝计算')
        if not same_lattice(ab, other):
            raise ValueError(f'AB 与 {name} 晶格不一致;差分电荷要求同一晶胞,拒绝计算')
    if ab['natoms'] != a['natoms'] + b['natoms']:
        raise ValueError(
            f'原子数不守恒:N(AB)={ab["natoms"]} != N(A)+N(B)='
            f'{a["natoms"]}+{b["natoms"]}={a["natoms"] + b["natoms"]};'
            'AB 应为表面 A 与吸附质 B 之并(同顺序),拒绝计算')
    diff = [ab['grid'][i] - a['grid'][i] - b['grid'][i] for i in range(len(ab['grid']))]
    write_chgcar(ab, diff, out_path)
    return {'out': str(out_path), 'max': max(diff), 'min': min(diff), 'n_grid': len(diff)}


def plane_averaged(chgcar_path_or_diff, axis: str = 'z') -> dict:
    """面平均电荷(密度)沿某轴:``{'z':[...],'rho':[...],'axis'}``。

    对每个 axis 网格切片求面内均值再除以晶胞体积 → 电荷密度 ρ̄(z)(e/Å³);
    z 为沿该晶格矢量方向的坐标(Å,z[k]=k/NG_axis·|a_axis|)。axis ∈ {'x','y','z'}。
    线性索引约定 i = ix + NGX·(iy + NGY·iz)(x 最快,VASP CHGCAR 顺序)。
    """
    c = read_chgcar(chgcar_path_or_diff)
    ngx, ngy, ngz, grid = c['ngx'], c['ngy'], c['ngz'], c['grid']
    axis = str(axis).lower()
    if axis == 'x':
        na, key = ngx, (lambda i: i % ngx)
    elif axis == 'y':
        na, key = ngy, (lambda i: (i // ngx) % ngy)
    elif axis == 'z':
        na, key = ngz, (lambda i: i // (ngx * ngy))
    else:
        raise ValueError(f"axis 只能是 x/y/z,收到 {axis!r}")
    sums = [0.0] * na
    cnts = [0] * na
    for i, v in enumerate(grid):
        k = key(i)
        sums[k] += v
        cnts[k] += 1
    vol = abs(_det3([[c['scale'] * x for x in vec] for vec in c['lattice']]))
    axis_vec = [c['scale'] * x for x in c['lattice'][{'x': 0, 'y': 1, 'z': 2}[axis]]]
    length = math.sqrt(sum(x * x for x in axis_vec))
    zs = [(k / na) * length for k in range(na)]
    rho = [(sums[k] / cnts[k] / vol) if (cnts[k] and vol > 0) else 0.0
           for k in range(na)]
    return {'z': zs, 'rho': rho, 'axis': axis}
